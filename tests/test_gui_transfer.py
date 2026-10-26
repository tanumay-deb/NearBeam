"""
Tests for ui.transfer.OutgoingTransfer against a live in-process server:
byte-identical delivery, pause/resume, cancel cleanup, monotonic progress.
"""

import os
import shutil
import tempfile
import threading
import time
import unittest

from werkzeug.serving import make_server

import core.server as srv_mod
from core.config import ConfigManager
from core.server import LandropServer, NearBeamRequestHandler
from ui.transfer import OutgoingTransfer

PORT = 5897
MB = 1024 * 1024


class TestOutgoingTransfer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="landrop_tx_")
        cls.save_dir = os.path.join(cls.base, "recv")
        cls.cfg = ConfigManager(os.path.join(cls.base, "cfg.json"))
        cls.cfg.set_save_directory(cls.save_dir)
        cls.cfg.set_auto_save(True)
        cls.orig_config = srv_mod.config
        srv_mod.config = cls.cfg
        cls.server = LandropServer(port=PORT, enable_mesh=False)
        cls.httpd = make_server("127.0.0.1", PORT, cls.server.app, threaded=True, request_handler=NearBeamRequestHandler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{PORT}/api/upload"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.server.shutdown()
        srv_mod.config = cls.orig_config
        shutil.rmtree(cls.base, ignore_errors=True)

    def _make_file(self, name, size):
        path = os.path.join(self.base, name)
        with open(path, "wb") as f:
            f.write(os.urandom(size))
        return path

    def _received(self, name):
        with open(os.path.join(self.save_dir, name), "rb") as f:
            return f.read()

    def _run(self, tx):
        done = {}
        tx.on_done = lambda ok, msg: done.update(ok=ok, msg=msg)
        return done

    def test_sends_byte_identical_with_monotonic_progress(self):
        src = self._make_file("big.bin", 40 * MB + 123)
        progress = []
        tx = OutgoingTransfer(self.url, "peer", src, on_progress=lambda s, t, bps: progress.append(s))
        done = self._run(tx)
        tx.start()
        tx.join(60)
        self.assertTrue(done.get("ok"), done)
        with open(src, "rb") as f:
            self.assertEqual(self._received("big.bin"), f.read())
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(progress[-1], os.path.getsize(src))

    def test_pause_then_resume_completes_identically(self):
        src = self._make_file("paused.bin", 8 * MB)
        first_chunk = threading.Event()
        tx = OutgoingTransfer(self.url, "peer", src, chunk_size=1 * MB,
                              on_progress=lambda s, t, bps: first_chunk.set())
        done = self._run(tx)
        tx.pause()  # paused before the first chunk goes out
        tx.start()
        time.sleep(0.3)
        self.assertEqual(tx.sent, 0)
        tx.resume()
        self.assertTrue(first_chunk.wait(10))
        tx.pause()
        time.sleep(0.3)
        stalled_at = tx.sent
        time.sleep(0.3)
        self.assertEqual(tx.sent, stalled_at)
        self.assertLess(stalled_at, tx.total)
        tx.resume()
        tx.join(30)
        self.assertTrue(done.get("ok"), done)
        with open(src, "rb") as f:
            self.assertEqual(self._received("paused.bin"), f.read())

    def test_cancel_removes_partial_temp_file(self):
        src = self._make_file("cancel.bin", 4 * MB)
        first_chunk = threading.Event()
        tx = OutgoingTransfer(self.url, "peer", src, chunk_size=1 * MB,
                              on_progress=lambda s, t, bps: (tx.pause(), first_chunk.set()))
        done = self._run(tx)
        tx.start()
        self.assertTrue(first_chunk.wait(10))
        tmp = os.path.join(self.save_dir, f".{tx.session_id}.tmp")
        self.assertTrue(os.path.exists(tmp))
        tx.cancel()
        tx.join(10)
        self.assertEqual(done.get("ok"), False)
        self.assertFalse(os.path.exists(tmp))
        self.assertFalse(os.path.exists(os.path.join(self.save_dir, "cancel.bin")))

    def test_shared_file_download_reports_progress_and_history(self):
        import json
        import queue
        import urllib.request
        src = self._make_file("shared.bin", 3 * MB)
        entry = self.cfg.add_safe_folder(src)
        q = queue.Queue()
        self.server.event_subscribers.append(q)
        try:
            url = f"http://127.0.0.1:{PORT}/api/safelist/download?folder_id={entry['id']}&subpath="
            with urllib.request.urlopen(url, timeout=30) as resp:
                body = resp.read()
            with open(src, "rb") as f:
                self.assertEqual(body, f.read())
            events = []
            deadline = time.time() + 5
            while time.time() < deadline and not any(e["type"] == "download_finished" for e in events):
                try:
                    events.append(json.loads(q.get(timeout=0.5)))
                except queue.Empty:
                    pass
        finally:
            self.server.event_subscribers.remove(q)
        progress = [e for e in events if e["type"] == "download_progress"]
        finished = [e for e in events if e["type"] == "download_finished"]
        self.assertTrue(progress)
        self.assertEqual(progress[0]["total"], 3 * MB)
        self.assertTrue(finished and finished[-1]["completed"])
        self.assertEqual(self.cfg.get_transfer_history()[0]["filename"], "shared.bin")
        self.assertEqual(self.cfg.get_transfer_history()[0]["direction"], "sent")

    def test_unreachable_target_reports_failure(self):
        src = self._make_file("nowhere.bin", 10)
        tx = OutgoingTransfer("http://127.0.0.1:1/api/upload", "ghost", src)
        done = self._run(tx)
        tx.run()
        self.assertEqual(done.get("ok"), False)
        self.assertTrue(done.get("msg"))


if __name__ == "__main__":
    unittest.main()
