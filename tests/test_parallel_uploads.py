"""
Tests for Parallel Multi-Stream Uploads (#7):
server out-of-order/concurrent chunk writes, resume status, cancel safety,
0-byte and midnight stability, desktop OutgoingTransfer, and web JS contracts.
"""

import concurrent.futures
import datetime
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

from werkzeug.serving import make_server

import core.server as srv_mod
from core.config import ConfigManager
from core.server import NearBeamRequestHandler, NearBeamServer


class TestServerParallelChunks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nb_par_srv_")
        self.cfg = ConfigManager(os.path.join(self.tmp, "cfg.json"))
        self.cfg.set_save_directory(os.path.join(self.tmp, "recv"))
        self.cfg.set_auto_save(True)
        self.orig = srv_mod.config
        srv_mod.config = self.cfg
        self.server = NearBeamServer(port=5972, enable_mesh=False)
        self.client = self.server.app.test_client()

    def tearDown(self):
        self.server.shutdown()
        srv_mod.config = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post_chunk(self, sid, name, data, idx, total, offset, size, extra=None):
        headers = {
            "X-Parallel": "1",
            "X-Session-Id": sid,
            "X-Filename": name,
            "X-Chunk-Index": str(idx),
            "X-Total-Chunks": str(total),
            "X-Chunk-Offset": str(offset),
            "X-File-Size": str(size),
        }
        if extra:
            headers.update(extra)
        return self.client.post("/api/upload/chunk", data=data, headers=headers)

    def test_out_of_order_chunks_assemble_byte_identically(self):
        parts = [b"AAAA" * 1000, b"BBBB" * 1000, b"CCCC" * 1000, b"DDDD" * 250]
        full = b"".join(parts)
        offsets = [0, 4000, 8000, 12000]
        # Send chunks completely out of order: 3, 1, 0, 2
        for idx in (3, 1, 0):
            r = self._post_chunk("ooo-1", "movie.mp4", parts[idx], idx, 4, offsets[idx], len(full))
            self.assertEqual(r.status_code, 200, r.data)
            self.assertFalse(r.get_json()["completed"])

        # Status reports received chunk indices accurately
        st = self.client.get("/api/upload/status/ooo-1").get_json()
        self.assertEqual(st["received_chunks"], [0, 1, 3])
        self.assertFalse(st["completed"])

        # Final missing chunk (index 2) completes and finalizes
        r_last = self._post_chunk("ooo-1", "movie.mp4", parts[2], 2, 4, offsets[2], len(full))
        self.assertEqual(r_last.status_code, 200, r_last.data)
        self.assertTrue(r_last.get_json()["completed"])

        saved = os.path.join(self.cfg.save_directory, "movie.mp4")
        with open(saved, "rb") as f:
            self.assertEqual(f.read(), full)
        self.assertEqual([n for n in os.listdir(self.cfg.save_directory) if n.endswith(".tmp")], [])

        # Status after completion reports completed=True
        st_done = self.client.get("/api/upload/status/ooo-1").get_json()
        self.assertTrue(st_done["completed"])

    def test_duplicate_chunk_retry_is_idempotent(self):
        p0, p1 = b"first-half", b"secondhalf"
        full = p0 + p1
        r0 = self._post_chunk("dup-1", "dup.bin", p0, 0, 2, 0, len(full))
        self.assertFalse(r0.get_json()["completed"])
        # Duplicate chunk 0 retry MUST NOT satisfy total_chunks == 2
        r0_again = self._post_chunk("dup-1", "dup.bin", p0, 0, 2, 0, len(full))
        self.assertEqual(r0_again.status_code, 200)
        self.assertFalse(r0_again.get_json()["completed"])

        r1 = self._post_chunk("dup-1", "dup.bin", p1, 1, 2, len(p0), len(full))
        self.assertTrue(r1.get_json()["completed"])
        # Retrying after completion returns same finalized record without duplicate file
        r1_again = self._post_chunk("dup-1", "dup.bin", p1, 1, 2, len(p0), len(full))
        self.assertEqual(r1_again.status_code, 200)
        self.assertTrue(r1_again.get_json()["completed"])
        self.assertEqual(os.listdir(self.cfg.save_directory), ["dup.bin"])

    def test_zero_byte_parallel_upload_completes_cleanly(self):
        r = self._post_chunk("zero-1", "empty.txt", b"", 0, 1, 0, 0)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.get_json()["completed"])
        saved = os.path.join(self.cfg.save_directory, "empty.txt")
        self.assertTrue(os.path.isfile(saved))
        self.assertEqual(os.path.getsize(saved), 0)

    def test_header_mismatch_and_overflow_rejected_with_400(self):
        r0 = self._post_chunk("mis-1", "file.bin", b"12345", 0, 2, 0, 10)
        self.assertEqual(r0.status_code, 200)
        # Wrong file size on chunk 1
        bad_size = self._post_chunk("mis-1", "file.bin", b"67890", 1, 2, 5, 999)
        self.assertEqual(bad_size.status_code, 400)
        # Overflow beyond declared file_size
        bad_overflow = self._post_chunk("mis-1", "file.bin", b"67890EXTRA", 1, 2, 5, 10)
        self.assertEqual(bad_overflow.status_code, 400)

    def test_cancel_cleans_parallel_session_and_rejects_late_chunks(self):
        r0 = self._post_chunk("can-1", "abort.bin", b"12345", 0, 2, 0, 10)
        self.assertEqual(r0.status_code, 200)
        rc = self.client.post("/api/upload/cancel", json={"session_id": "can-1"})
        self.assertEqual(rc.status_code, 200)
        self.assertTrue(rc.get_json()["removed_tmp"])

        r1 = self._post_chunk("can-1", "abort.bin", b"67890", 1, 2, 5, 10)
        self.assertEqual(r1.status_code, 499)
        self.assertFalse(os.path.exists(os.path.join(self.cfg.save_directory, "abort.bin")))

    def test_cross_midnight_date_mode_keeps_starting_folder(self):
        self.cfg.set_auto_organize_mode("date")

        class Day1Date(datetime.date):
            @classmethod
            def today(cls):
                return cls(2026, 11, 24)

        class Day2Date(datetime.date):
            @classmethod
            def today(cls):
                return cls(2026, 11, 25)

        with mock.patch("core.server.datetime.date", Day1Date):
            r0 = self._post_chunk("mid-1", "night.bin", b"12345", 0, 2, 0, 10)
            self.assertEqual(r0.status_code, 200)

        with mock.patch("core.server.datetime.date", Day2Date):
            r1 = self._post_chunk("mid-1", "night.bin", b"67890", 1, 2, 5, 10)
            self.assertEqual(r1.status_code, 200, r1.data)
            self.assertTrue(r1.get_json()["completed"])

        saved = os.path.join(self.cfg.save_directory, "2026-11-24", "night.bin")
        self.assertTrue(os.path.isfile(saved))
        with open(saved, "rb") as f:
            self.assertEqual(f.read(), b"1234567890")

    def test_concurrent_multi_thread_live_http_parallel_upload(self):
        port = 5973
        httpd = make_server("127.0.0.1", port, self.server.app, threaded=True, request_handler=NearBeamRequestHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            payload = os.urandom(4 * 1024 * 1024 + 321)
            chunk_size = 512 * 1024
            chunks = [
                (i, i * chunk_size, payload[i * chunk_size : (i + 1) * chunk_size])
                for i in range((len(payload) + chunk_size - 1) // chunk_size)
            ]
            total = len(chunks)

            def send_one(entry):
                idx, offset, data = entry
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/upload/chunk",
                    data=data,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Parallel": "1",
                        "X-Session-Id": "live-par-1",
                        "X-Filename": "concurrent.bin",
                        "X-Chunk-Index": str(idx),
                        "X-Total-Chunks": str(total),
                        "X-Chunk-Offset": str(offset),
                        "X-File-Size": str(len(payload)),
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.status, resp.read()

            # Deliberately reverse chunk order across 4 concurrent threads
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(send_one, reversed(chunks)))

            self.assertTrue(all(status == 200 for status, _ in results))
            saved = os.path.join(self.cfg.save_directory, "concurrent.bin")
            self.assertTrue(os.path.isfile(saved))
            with open(saved, "rb") as f:
                self.assertEqual(f.read(), payload)
            self.assertEqual([n for n in os.listdir(self.cfg.save_directory) if n.endswith(".tmp")], [])
        finally:
            httpd.shutdown()

    def test_parallel_rejects_incomplete_or_gap_chunks_without_finalizing_zeros(self):
        # Declare 10 KB file across 2 chunks, send empty/short bodies with a gap
        r0 = self._post_chunk("gap-1", "holes.bin", b"", 0, 2, 0, 10240)
        self.assertEqual(r0.status_code, 400)
        # Send 2 non-empty chunks that do NOT cover the full [0, 10240) range (gap in middle)
        r1 = self._post_chunk("gap-2", "holes2.bin", b"A" * 100, 0, 2, 0, 10240)
        self.assertEqual(r1.status_code, 200)
        r2 = self._post_chunk("gap-2", "holes2.bin", b"B" * 100, 1, 2, 5000, 10240)
        self.assertEqual(r2.status_code, 400)
        self.assertFalse(os.path.exists(os.path.join(self.cfg.save_directory, "holes2.bin")))

    def test_parallel_and_sequential_mix_on_same_session_rejected_with_409(self):
        # 1) Sequential chunk 0 then parallel chunk 1 -> 409
        r_seq = self.client.post(
            "/api/upload/chunk",
            data=b"AAAA",
            headers={
                "X-Session-Id": "mix-seq-1",
                "X-Filename": "mix1.bin",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "2",
                "X-Chunk-Offset": "0",
                "X-File-Size": "8",
            },
        )
        self.assertEqual(r_seq.status_code, 200)
        r_par = self._post_chunk("mix-seq-1", "mix1.bin", b"BBBB", 1, 2, 4, 8)
        self.assertEqual(r_par.status_code, 409)

        # 2) Parallel chunk 0 then sequential chunk 1 -> 409
        r_par2 = self._post_chunk("mix-par-1", "mix2.bin", b"AAAA", 0, 2, 0, 8)
        self.assertEqual(r_par2.status_code, 200)
        r_seq2 = self.client.post(
            "/api/upload/chunk",
            data=b"BBBB",
            headers={
                "X-Session-Id": "mix-par-1",
                "X-Filename": "mix2.bin",
                "X-Chunk-Index": "1",
                "X-Total-Chunks": "2",
                "X-Chunk-Offset": "4",
                "X-File-Size": "8",
            },
        )
        self.assertEqual(r_seq2.status_code, 409)

    def test_session_lock_not_evicted_while_parallel_session_active(self):
        r0 = self._post_chunk("active-lock-1", "keep.bin", b"AAAA", 0, 2, 0, 8)
        self.assertEqual(r0.status_code, 200)
        orig_lock = self.server._get_session_lock("active-lock-1")
        for i in range(600):
            self.server._get_session_lock(f"noise-sess-{i}")
        self.assertIs(self.server._get_session_lock("active-lock-1"), orig_lock)

    def test_finalize_upload_cleans_placeholder_if_temp_missing(self):
        with self.assertRaises(OSError):
            srv_mod.finalize_upload(
                os.path.join(self.cfg.save_directory, ".nonexistent.tmp"),
                self.cfg.save_directory,
                "orphan.bin",
            )
        self.assertFalse(os.path.exists(os.path.join(self.cfg.save_directory, "orphan.bin")))

    def test_http11_keep_alive_default_and_pipelining(self):
        import socket as sock_mod

        httpd = make_server(
            "127.0.0.1", 0, self.server.app, threaded=True, request_handler=NearBeamRequestHandler
        )
        port = httpd.server_port
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with sock_mod.create_connection(("127.0.0.1", port), timeout=3.0) as s:
                # Two pipelined HTTP/1.1 requests sent together WITHOUT explicit Connection: keep-alive
                pipelined = (
                    f"GET /api/info HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n"
                    f"GET /api/info HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
                s.sendall(pipelined)
                raw = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
            self.assertEqual(raw.count(b"HTTP/1.1 200 OK"), 2, raw[:400])
            self.assertIn(b"Connection: keep-alive", raw)
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestOutgoingTransferParallel(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="nearbeam_outpar_")
        self.save_dir = os.path.join(self.base, "recv")
        self.cfg = ConfigManager(os.path.join(self.base, "cfg.json"))
        self.cfg.set_save_directory(self.save_dir)
        self.cfg.set_auto_save(True)
        self.cfg.set_auto_organize_mode("off")
        self.orig_config = srv_mod.config
        srv_mod.config = self.cfg
        self.server = NearBeamServer(port=0, enable_mesh=False)
        self.observed_headers = []
        self.observed_clients = []

        orig_app = self.server.app

        def recording_wsgi(environ, start_response):
            if environ.get("PATH_INFO") == "/api/upload/chunk":
                self.observed_headers.append({
                    "parallel": environ.get("HTTP_X_PARALLEL"),
                    "device": environ.get("HTTP_X_DEVICE_NAME"),
                })
                self.observed_clients.append(
                    (environ.get("REMOTE_ADDR"), environ.get("REMOTE_PORT"))
                )
            return orig_app(environ, start_response)

        self.httpd = make_server(
            "127.0.0.1", 0, recording_wsgi, threaded=True, request_handler=NearBeamRequestHandler
        )
        self.port = self.httpd.server_port
        self._srv_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._srv_thread.start()
        self.url = f"http://127.0.0.1:{self.port}/api/upload"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server.shutdown()
        srv_mod.config = self.orig_config
        shutil.rmtree(self.base, ignore_errors=True)

    def _make_file(self, name: str, size: int) -> str:
        path = os.path.join(self.base, name)
        with open(path, "wb") as f:
            f.write(os.urandom(size))
        return path

    def test_large_file_uses_parallel_multi_stream_and_delivers_byte_identical(self):
        from ui.transfer import OutgoingTransfer

        mb = 1024 * 1024
        src = self._make_file("parallel_20mb.bin", 20 * mb + 321)
        progress = []
        done = {}
        tx = OutgoingTransfer(
            self.url,
            "peer",
            src,
            on_progress=lambda s, t, bps: progress.append(s),
            on_done=lambda ok, msg: done.update(ok=ok, msg=msg),
            chunk_size=2 * mb,
            max_streams=4,
            parallel_threshold=4 * mb,
        )
        tx.start()
        tx.join(60)
        self.assertTrue(done.get("ok"), done)
        saved = os.path.join(self.save_dir, "parallel_20mb.bin")
        with open(src, "rb") as f1, open(saved, "rb") as f2:
            self.assertEqual(f2.read(), f1.read())
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(progress[-1], os.path.getsize(src))
        self.assertTrue(self.observed_headers)
        self.assertTrue(all(h["parallel"] == "1" for h in self.observed_headers))

    def test_keep_alive_reuses_tcp_connection(self):
        from ui.transfer import OutgoingTransfer

        mb = 1024 * 1024
        src = self._make_file("keepalive.bin", 4 * mb)
        done = {}
        tx = OutgoingTransfer(
            self.url,
            "peer",
            src,
            on_done=lambda ok, msg: done.update(ok=ok, msg=msg),
            chunk_size=1 * mb,
            max_streams=1,
            parallel_threshold=16 * mb,
        )
        tx.run()
        self.assertTrue(done.get("ok"), done)
        self.assertEqual(len(self.observed_clients), 4)
        # All 4 sequential chunks reused a single HTTP/1.1 TCP socket (same client port)
        self.assertEqual(len(set(self.observed_clients)), 1, self.observed_clients)

    def test_parallel_pause_resume_and_mid_chunk_cancel(self):
        from ui.transfer import OutgoingTransfer

        mb = 1024 * 1024
        # 1) Pause & resume in parallel mode
        src = self._make_file("par_pause.bin", 12 * mb)
        first_chunk = threading.Event()
        done = {}

        class PacedTransfer(OutgoingTransfer):
            def _pieces(self, data):
                for piece in super()._pieces(data):
                    time.sleep(0.01)
                    yield piece

        tx = PacedTransfer(
            self.url,
            "peer",
            src,
            on_progress=lambda s, t, bps: first_chunk.set(),
            on_done=lambda ok, msg: done.update(ok=ok, msg=msg),
            chunk_size=1 * mb,
            max_streams=3,
            parallel_threshold=4 * mb,
        )
        tx.start()
        self.assertTrue(first_chunk.wait(10))
        tx.pause()
        time.sleep(0.4)
        stalled_at = tx.sent
        time.sleep(0.3)
        self.assertEqual(tx.sent, stalled_at)
        self.assertLess(stalled_at, tx.total)
        tx.resume()
        tx.join(30)
        self.assertTrue(done.get("ok"), done)
        with open(src, "rb") as f1, open(os.path.join(self.save_dir, "par_pause.bin"), "rb") as f2:
            self.assertEqual(f2.read(), f1.read())

        # 2) Cancel mid-parallel upload
        src2 = self._make_file("par_cancel.bin", 12 * mb)
        done2 = {}
        tx2 = PacedTransfer(
            self.url,
            "peer",
            src2,
            on_done=lambda ok, msg: done2.update(ok=ok, msg=msg),
            chunk_size=1 * mb,
            max_streams=4,
            parallel_threshold=4 * mb,
        )
        tx2.start()
        time.sleep(0.15)
        tx2.cancel()
        tx2.join(10)
        self.assertEqual(done2.get("ok"), False)
        self.assertEqual(done2.get("msg"), "Cancelled")
        tmp = os.path.join(self.save_dir, f".{tx2.session_id}.tmp")
        deadline = time.time() + 3
        while os.path.exists(tmp) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(os.path.exists(tmp))
        self.assertFalse(os.path.exists(os.path.join(self.save_dir, "par_cancel.bin")))

    def test_device_name_header_integrates_with_auto_organize_device_mode(self):
        from ui.transfer import OutgoingTransfer

        self.cfg.set_auto_organize_mode("device")
        src = self._make_file("clip.mp4", 64 * 1024)
        done = {}
        tx = OutgoingTransfer(
            self.url,
            "peer",
            src,
            on_done=lambda ok, msg: done.update(ok=ok, msg=msg),
            device_name="Studio-PC",
        )
        tx.run()
        self.assertTrue(done.get("ok"), done)
        saved = os.path.join(self.save_dir, "Studio-PC", "clip.mp4")
        self.assertTrue(os.path.isfile(saved))
        with open(src, "rb") as f1, open(saved, "rb") as f2:
            self.assertEqual(f2.read(), f1.read())

    def test_outgoing_transfer_parallel_error_while_paused_does_not_hang(self):
        from ui.transfer import OutgoingTransfer

        mb = 1024 * 1024
        src = self._make_file("err_pause.bin", 8 * mb)
        done = {}
        err_triggered = threading.Event()

        class FailingWorkerTransfer(OutgoingTransfer):
            def _post_chunk(self, conn_holder, idx, total_chunks, offset, data, parallel):
                if idx == 0:
                    # Pause sibling workers then fail on worker 0
                    self.pause()
                    time.sleep(0.05)
                    err_triggered.set()
                    raise RuntimeError("simulated worker boom")
                return super()._post_chunk(conn_holder, idx, total_chunks, offset, data, parallel)

        tx = FailingWorkerTransfer(
            self.url,
            "peer",
            src,
            on_done=lambda ok, msg: done.update(ok=ok, msg=msg),
            chunk_size=1 * mb,
            max_streams=3,
            parallel_threshold=2 * mb,
        )
        tx.start()
        tx.join(5.0)
        self.assertTrue(err_triggered.is_set())
        self.assertFalse(tx._thread.is_alive(), "Parallel worker deadlocked on _resume.wait() after sibling error")
        self.assertEqual(done.get("ok"), False)
        self.assertIn("simulated worker boom", done.get("msg", ""))


class TestWebParallelAndReceivedContracts(unittest.TestCase):
    def test_app_js_parallel_transfer_manager_and_received_folder_contracts(self):
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        js_path = root / "static" / "js" / "app.js"
        src = js_path.read_text(encoding="utf-8")

        for token in (
            "LARGE_FILE_THRESHOLD",
            "X-Parallel",
            "inFlightXhrs",
            "completedChunks",
            "/api/upload/status/",
            "file.rel_path",
            "file.folder",
            "completed.clear()",
            "if (!t.paused)",
        ):
            self.assertIn(token, src, f"Missing expected JS contract token: {token}")

        # Verify syntax with Node if available
        res = subprocess.run(["node", "--check", str(js_path)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
