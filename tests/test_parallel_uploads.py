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


if __name__ == "__main__":
    unittest.main()
