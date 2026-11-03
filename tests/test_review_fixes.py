"""
Regression tests for the full-app review: one test per confirmed finding.
"""

import io
import json
import os
import queue
import shutil
import tempfile
import threading
import time
import unittest
import zipfile

import core.server as srv_mod
from core.config import ConfigManager
from core.mesh import MeshManager
from core.server import LandropServer, finalize_upload, remove_stale_uploads, remove_when_released

REMOTE = {"REMOTE_ADDR": "192.168.1.50"}
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="landrop_review_")
        self.share = os.path.join(self.tmp, "share")
        os.makedirs(self.share)
        self.cfg = ConfigManager(os.path.join(self.tmp, "cfg.json"))
        self.cfg.set_save_directory(os.path.join(self.tmp, "recv"))
        self.cfg.set_auto_save(True)
        self.orig = srv_mod.config
        srv_mod.config = self.cfg
        self.server = LandropServer(port=5992, enable_mesh=False)
        self.client = self.server.app.test_client()

    def tearDown(self):
        self.server.mesh = None
        self.server.shutdown()
        srv_mod.config = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestServerFixes(ServerCase):
    def test_shell_send_refuses_network_devices(self):
        secret = os.path.join(self.tmp, "private")
        os.makedirs(secret)
        r = self.client.post("/api/shell-send", json={"paths": [secret]}, environ_base=REMOTE)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.cfg.get_safe_folders(), [])
        r = self.client.post("/api/shell-send", json={"paths": [secret]}, environ_base=LOCAL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([f["name"] for f in self.cfg.get_safe_folders()], ["private"])

    def test_guest_link_points_at_this_pc_when_not_leader(self):
        class Node:
            role = "node"
        self.server.mesh = Node()
        url = self.client.post("/api/drop/create").get_json()["url"]
        self.assertNotIn("nearbeam.local", url)
        self.server.mesh = None
        self.assertIn("nearbeam.local", self.client.post("/api/drop/create").get_json()["url"])

    def test_zip_skips_links_that_leave_the_shared_folder(self):
        outside_dir = os.path.join(self.tmp, "outside")
        os.makedirs(outside_dir)
        with open(os.path.join(outside_dir, "secret.txt"), "w") as f:
            f.write("secret")
        with open(os.path.join(self.share, "inside.txt"), "w") as f:
            f.write("ok")
        try:
            os.symlink(os.path.join(outside_dir, "secret.txt"), os.path.join(self.share, "link.txt"))
        except (OSError, NotImplementedError):
            if os.name != "nt":
                raise
            # File symlinks need Developer Mode on Windows; a directory junction needs nothing.
            import subprocess
            subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(self.share, "junction"), outside_dir],
                           check=True, capture_output=True)
        fid = self.cfg.add_safe_folder(self.share)["id"]
        r = self.client.get(f"/api/safelist/download-zip?folder_id={fid}&subpath=")
        names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
        self.assertEqual(names, ["inside.txt"])

    def test_same_name_uploads_finishing_together_keep_every_file(self):
        dest = os.path.join(self.tmp, "dest")
        temps = []
        for i in range(30):
            p = os.path.join(self.tmp, f"t{i}.tmp")
            with open(p, "wb") as f:
                f.write(str(i).encode())
            temps.append(p)
        barrier = threading.Barrier(30)
        results = []

        def go(p):
            barrier.wait()
            results.append(finalize_upload(p, dest, "IMG_0001.jpg"))

        threads = [threading.Thread(target=go, args=(p,)) for p in temps]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(set(results)), 30)
        contents = sorted(int(open(p, "rb").read()) for p in results)
        self.assertEqual(contents, list(range(30)))

    def test_legacy_multipart_upload_leaves_no_temp_files(self):
        r = self.client.post("/api/upload", data={"file": (io.BytesIO(b"hello"), "note.txt")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        recv = self.cfg.save_directory
        self.assertEqual(sorted(os.listdir(recv)), ["note.txt"])

    def test_status_and_cancel_reject_unsafe_session_ids(self):
        recv = self.cfg.save_directory
        with open(os.path.join(recv, ".x"), "w") as f:
            f.write("keep")
        r = self.client.post("/api/upload/cancel", json={"session_id": "x:y"})
        self.assertFalse(r.get_json()["removed_tmp"])
        self.assertEqual(self.client.get("/api/upload/status/x:y").get_json(), {"received_bytes": 0})

    def test_partial_uploads_are_not_downloadable(self):
        recv = self.cfg.save_directory
        with open(os.path.join(recv, ".abc123.tmp"), "wb") as f:
            f.write(b"partial")
        self.assertEqual(self.client.get("/api/received/download/.abc123.tmp").status_code, 404)
        self.assertEqual(self.client.get("/api/received/preview/.abc123.tmp").status_code, 404)

    def test_concurrent_downloads_get_separate_progress_ids(self):
        big = os.path.join(self.share, "big.bin")
        with open(big, "wb") as f:
            f.write(os.urandom(2 * 1024 * 1024))
        fid = self.cfg.add_safe_folder(self.share)["id"]
        import queue
        q = queue.Queue()
        self.server.event_subscribers.append(q)
        for _ in range(2):
            self.client.get(f"/api/safelist/download?folder_id={fid}&subpath=big.bin").close()
        ids = set()
        while not q.empty():
            ev = json.loads(q.get_nowait())
            if ev["type"] == "download_finished":
                ids.add(ev["download_id"])
        self.assertEqual(len(ids), 2)


class TestTempCleanup(unittest.TestCase):
    def test_temp_zip_removed_only_after_release(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "job.zip")
        f = open(path, "wb")
        f.write(b"zip")
        done = threading.Event()
        threading.Thread(
            target=remove_when_released, args=(path,),
            kwargs={"first_delay": 0, "retry": 0.05, "on_removed": done.set}, daemon=True,
        ).start()
        time.sleep(0.3)
        if os.name == "nt":
            self.assertTrue(os.path.exists(path), "still streaming: must not be gone yet")
        f.close()
        self.assertTrue(done.wait(3))
        self.assertFalse(os.path.exists(path))
        shutil.rmtree(tmp, ignore_errors=True)


class TestConfigLocking(unittest.TestCase):
    def test_folder_changes_and_history_writes_dont_lose_each_other(self):
        tmp = tempfile.mkdtemp()
        cfg = ConfigManager(os.path.join(tmp, "c.json"))
        folders = []
        for i in range(20):
            d = os.path.join(tmp, f"f{i}")
            os.makedirs(d)
            folders.append(d)

        def add_folders():
            for d in folders:
                cfg.add_safe_folder(d)

        def add_history():
            for i in range(60):
                cfg.add_transfer_history({"filename": f"h{i}"})

        ts = [threading.Thread(target=add_folders), threading.Thread(target=add_history)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        saved = json.load(open(os.path.join(tmp, "c.json"), encoding="utf-8"))
        self.assertEqual(len(saved["safe_folders"]), 20)
        self.assertEqual(len(saved["transfer_history"]), 60)
        shutil.rmtree(tmp, ignore_errors=True)


def chunk(client, name, data, sid, index=0, total=1, offset=0, size=None, rel=None):
    headers = {"X-Session-Id": sid, "X-Filename": name, "X-Chunk-Index": str(index), "X-Total-Chunks": str(total),
               "X-Chunk-Offset": str(offset), "X-File-Size": str(len(data) if size is None else size)}
    if rel:
        headers["X-Relative-Dir"] = rel
    return client.post("/api/upload/chunk", data=data, headers=headers)


class TestSecondReview(ServerCase):
    def test_websites_cannot_read_nearbeam_through_the_browser(self):
        for origin in ("https://evil.example", "http://[2606:4700::1111]:5000"):
            for path in ("/api/clipboard", "/api/safelist", "/api/history", "/api/received", "/api/info"):
                r = self.client.get(path, headers={"Origin": origin})
                self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"), (origin, path))

    def test_other_nearbeam_pages_can_still_probe_and_send(self):
        lan = "http://192.168.1.20:5000"
        self.assertEqual(self.client.get("/api/info", headers={"Origin": lan}).headers.get("Access-Control-Allow-Origin"), lan)
        pre = self.client.options("/api/upload/chunk", headers={
            "Origin": lan, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-session-id,x-filename"})
        self.assertEqual(pre.headers.get("Access-Control-Allow-Origin"), lan)

    def test_dns_rebinding_host_is_refused(self):
        self.assertEqual(self.client.get("/api/clipboard", headers={"Host": "rebind.attacker.example:5000"}).status_code, 421)
        for host in ("192.168.1.7:5000", "nearbeam.local:5000", "desktop-pc:5000", "[::1]:5000", "localhost"):
            self.assertEqual(self.client.get("/api/clipboard", headers={"Host": host}).status_code, 200, host)

    def test_names_windows_cannot_store_are_cleaned(self):
        recv = self.cfg.save_directory
        for i, (raw, saved) in enumerate({"IMG 10:00:00.jpg": "IMG 10_00_00.jpg", "photo:1.jpg": "photo_1.jpg", "what?.txt": "what_.txt"}.items()):
            r = chunk(self.client, raw, b"hello", f"name{i}")
            self.assertEqual(r.status_code, 200, raw)
            self.assertEqual(r.get_json()["file"]["filename"], saved)
            with open(os.path.join(recv, saved), "rb") as f:
                self.assertEqual(f.read(), b"hello")
        r = chunk(self.client, "a.txt", b"x", "folder1", rel="Trip: day 1/x?")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(recv, "Trip_ day 1", "x_", "a.txt")))
        self.assertEqual([n for n in os.listdir(recv) if n.endswith(".tmp")], [])

    def test_resent_last_chunk_after_save_neither_fails_nor_duplicates(self):
        first = chunk(self.client, "one.bin", b"abc", "single").get_json()
        again = chunk(self.client, "one.bin", b"abc", "single")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()["file"]["filename"], first["file"]["filename"])
        chunk(self.client, "two.bin", b"12345", "multi", index=0, total=2, offset=0, size=10)
        chunk(self.client, "two.bin", b"67890", "multi", index=1, total=2, offset=5, size=10)
        retry = chunk(self.client, "two.bin", b"67890", "multi", index=1, total=2, offset=5, size=10)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.get_json()["completed"])
        self.assertEqual(sorted(os.listdir(self.cfg.save_directory)), ["one.bin", "two.bin"])

    def test_zip_job_still_packing_is_not_pruned(self):
        old = time.time() - 4000
        self.server.zip_jobs.update({"packing": {"status": "processing", "created_at": old}, "done": {"status": "completed", "created_at": old}})
        self.server._cleanup_old_zip_jobs()
        self.assertEqual(list(self.server.zip_jobs), ["packing"])

    def test_network_change_readvertises_and_tells_the_window(self):
        calls = []

        class Mesh:
            role = "leader"

            def set_ip(self, ip):
                calls.append(("mesh", ip))

        self.server.mesh = Mesh()
        self.server.broadcaster.stop = lambda: calls.append(("stop",))
        self.server.broadcaster.start = lambda ip=None: calls.append(("start", ip))
        q = queue.Queue()
        self.server.event_subscribers.append(q)
        orig = srv_mod.get_local_ip
        srv_mod.get_local_ip = lambda: "10.1.2.3"
        try:
            self.server.check_network()
            self.server.check_network()  # unchanged: nothing more happens
        finally:
            srv_mod.get_local_ip = orig
        self.assertEqual(calls, [("mesh", "10.1.2.3"), ("stop",), ("start", "10.1.2.3")])
        self.assertIn({"type": "network_changed", "ip": "10.1.2.3"}, [json.loads(q.get_nowait()) for _ in range(q.qsize())])


class TestStaleUploads(unittest.TestCase):
    def test_day_old_partial_uploads_are_removed_but_received_files_kept(self):
        tmp = tempfile.mkdtemp()
        two_days_ago = time.time() - 2 * 86400
        for name, mtime in ((".abc-123.tmp", two_days_ago), (".fresh.tmp", None), ("notes.tmp", two_days_ago), ("photo.jpg", two_days_ago)):
            path = os.path.join(tmp, name)
            open(path, "w").close()
            if mtime:
                os.utime(path, (mtime, mtime))
        remove_stale_uploads(tmp)
        self.assertEqual(sorted(os.listdir(tmp)), [".fresh.tmp", "notes.tmp", "photo.jpg"])
        shutil.rmtree(tmp, ignore_errors=True)


class TestMeshAddressChange(unittest.TestCase):
    def test_node_reregisters_from_its_new_address(self):
        m = MeshManager(port=5000, device_name="N")
        m.role, m.leader_info = "node", {"node_id": "L", "ip": "10.0.0.1", "port": 5000}
        registered = threading.Event()
        m._register_with_leader = registered.set
        m.set_ip("10.0.0.9")
        self.assertTrue(registered.wait(2))
        self.assertEqual(m._leader_announcement()["ip"], "10.0.0.9")


class TestLeaderBeacon(unittest.TestCase):
    def test_leaders_probe_so_old_version_leaders_answer(self):
        m = MeshManager(port=5000, device_name="NEW")
        sent = []
        m._send_broadcast = lambda msg: sent.append(msg["type"])
        for _ in range(10):
            m._leader_beacon()
        self.assertEqual(sent.count("LEADER_ANNOUNCE"), 10)
        self.assertEqual(sent.count("WHO_IS_LEADER"), 2)


if __name__ == "__main__":
    unittest.main()
