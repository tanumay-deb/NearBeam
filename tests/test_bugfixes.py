"""
Regression tests for upload hardening and stability fixes:
- Path traversal via filename, relative dir and session id (host + guest drop)
- Malformed chunk headers return 400, not 500
- Resent chunks (X-Chunk-Offset) overwrite instead of corrupting the file
- Headless startup survives non-UTF-8 consoles
- Concurrent transfer-history saves keep config.json valid
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

import core.server as srv_mod
from core.config import ConfigManager
from core.server import LandropServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestUploadHardening(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="landrop_fix_")
        self.save_dir = os.path.join(self.test_dir, "received")
        self.cfg = ConfigManager(os.path.join(self.test_dir, "cfg.json"))
        self.cfg.set_save_directory(self.save_dir)
        self.cfg.set_auto_save(True)
        self.orig_config = srv_mod.config
        srv_mod.config = self.cfg
        self.server = LandropServer(port=5998, enable_mesh=False)
        self.client = self.server.app.test_client()

    def tearDown(self):
        self.server.shutdown()
        srv_mod.config = self.orig_config
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _chunk(self, data, sid="sess1", name="f.bin", idx=0, total=1, rel=None, offset=None, url="/api/upload/chunk", **extra):
        headers = {
            "X-Session-Id": sid,
            "X-Filename": quote(name),
            "X-Chunk-Index": str(idx),
            "X-Total-Chunks": str(total),
        }
        if rel is not None:
            headers["X-Relative-Dir"] = quote(rel)
        if offset is not None:
            headers["X-Chunk-Offset"] = str(offset)
        headers.update(extra)
        return self.client.post(url, data=data, headers=headers, content_type="application/octet-stream")

    def _files_outside_save_dir(self):
        return [n for n in os.listdir(self.test_dir) if n not in ("received", "cfg.json", "cfg.json.tmp")]

    def test_filename_traversal_stays_in_save_dir(self):
        for rel in (None, "sub"):
            resp = self._chunk(b"x", sid=f"t{rel}", name="..\\..\\escaped.txt", rel=rel)
            self.assertEqual(resp.status_code, 200)
            saved = resp.get_json()["file"]["saved_path"]
            self.assertTrue(os.path.realpath(saved).startswith(os.path.realpath(self.save_dir)))
            self.assertEqual(os.path.basename(saved), "escaped.txt")
        self.assertEqual(self._files_outside_save_dir(), [])

    def test_relative_dir_escape_rejected(self):
        for rel in (os.path.join(self.test_dir, "absdir"), "../outside", "sub/../../outside"):
            resp = self._chunk(b"x", sid="rel1", rel=rel)
            self.assertEqual(resp.status_code, 400, rel)
        self.assertEqual(self._files_outside_save_dir(), [])
        # Legit nested folder still works
        resp = self._chunk(b"x", sid="rel2", name="a.txt", rel="photos/2026")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(os.path.exists(os.path.join(self.save_dir, "photos", "2026", "a.txt")))

    def test_session_id_traversal_rejected(self):
        for sid in ("..\\..\\pwn", "../pwn", "a/b", "x" * 200):
            resp = self._chunk(b"x", sid=sid, total=2)
            self.assertEqual(resp.status_code, 400, sid)
        self.assertEqual(self._files_outside_save_dir(), [])

    def test_guest_drop_uses_same_guards(self):
        token = self.client.post("/api/drop/create", json={}).get_json()["token"]
        url = f"/api/drop/{token}/upload/chunk"
        self.assertEqual(self._chunk(b"x", sid="../pwn", url=url).status_code, 400)
        self.assertEqual(self._chunk(b"x", sid="g1", rel="../../out", url=url).status_code, 400)
        resp = self._chunk(b"x", sid="g2", name="..\\..\\g.txt", url=url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(os.path.dirname(resp.get_json()["file"]["saved_path"]), os.path.join(self.save_dir, "Guest Drops"))
        self.assertEqual(self._files_outside_save_dir(), [])

    def test_malformed_chunk_headers_return_400(self):
        bad = [
            {"X-Chunk-Index": "abc"},
            {"X-Total-Chunks": "1.5"},
            {"X-Chunk-Offset": "nope"},
            {"X-Chunk-Index": "-1"},
            {"X-Chunk-Index": "3", "X-Total-Chunks": "2"},
            {"X-Total-Chunks": "0"},
            {"X-Chunk-Offset": "-5"},
        ]
        for hdrs in bad:
            resp = self._chunk(b"x", sid="bad1", **hdrs)
            self.assertEqual(resp.status_code, 400, hdrs)

    def test_resent_chunk_with_offset_does_not_corrupt(self):
        sid = "retry1"
        self._chunk(b"A" * 10, sid=sid, name="r.bin", idx=0, total=3, offset=0)
        self._chunk(b"B" * 10, sid=sid, name="r.bin", idx=1, total=3, offset=10)
        self._chunk(b"B" * 10, sid=sid, name="r.bin", idx=1, total=3, offset=10)  # resend after a blip
        resp = self._chunk(b"C" * 10, sid=sid, name="r.bin", idx=2, total=3, offset=20)
        info = resp.get_json()["file"]
        self.assertEqual(info["size"], 30)
        with open(info["saved_path"], "rb") as f:
            self.assertEqual(f.read(), b"A" * 10 + b"B" * 10 + b"C" * 10)

    def test_partially_written_chunk_is_overwritten_on_retry(self):
        sid = "retry2"
        self._chunk(b"A" * 10, sid=sid, name="p.bin", idx=0, total=2, offset=0)
        # Simulate a dropped connection that left half of chunk 1 on disk
        with open(os.path.join(self.save_dir, f".{sid}.tmp"), "ab") as f:
            f.write(b"B" * 4)
        info = self._chunk(b"B" * 10, sid=sid, name="p.bin", idx=1, total=2, offset=10).get_json()["file"]
        self.assertEqual(info["size"], 20)

    def test_offset_beyond_received_data_is_409(self):
        self._chunk(b"A" * 10, sid="gap1", idx=0, total=3, offset=0)
        resp = self._chunk(b"C" * 10, sid="gap1", idx=2, total=3, offset=20)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["received_bytes"], 10)

    def test_legacy_clients_without_offset_still_append(self):
        self._chunk(b"A" * 5, sid="leg1", name="l.bin", idx=0, total=2)
        info = self._chunk(b"B" * 5, sid="leg1", name="l.bin", idx=1, total=2).get_json()["file"]
        self.assertEqual(info["size"], 10)


class TestConfigConcurrency(unittest.TestCase):
    def test_parallel_history_writes_keep_config_valid(self):
        test_dir = tempfile.mkdtemp(prefix="landrop_cfg_")
        try:
            path = os.path.join(test_dir, "cfg.json")
            cfg = ConfigManager(path)
            cfg.set_save_directory(os.path.join(test_dir, "r"))
            errors = []

            def worker(n):
                try:
                    for i in range(40):
                        cfg.add_transfer_history({"filename": f"w{n}_{i}.bin"})
                except Exception as e:  # pragma: no cover - surfaced via assertion
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data["transfer_history"]), 100)
            self.assertFalse(os.path.exists(path + ".tmp"))
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)


class TestHeadlessConsole(unittest.TestCase):
    def test_headless_banner_survives_cp1252_console(self):
        script = (
            "import sys, main\n"
            "main.ensure_windows_start_menu_shortcut = lambda: None\n"
            "main.config.data['shell_integration_enabled'] = False\n"
            "main.run_headless = lambda port: print('\\U0001f680 NearBeam ready')\n"
            "sys.argv = ['main.py', '--headless']\n"
            "main.main()\n"
        )
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        proc = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
        self.assertIn(b"NearBeam ready", proc.stdout)


if __name__ == "__main__":
    unittest.main()
