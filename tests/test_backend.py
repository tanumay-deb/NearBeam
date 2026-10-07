"""
Automated unit and integration test suite for LANDrop.
Tests:
- Network discovery
- Config and Safe List persistence
- Path traversal security sandboxing
- Auto-save file uploads and name collision handling
- Safe folder directory browsing, file downloading, and ZIP generation
- Shared clipboard sync
"""

import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core.config import ConfigManager
from core.network import get_connection_urls, get_hostname, get_local_ip, is_port_available
from core.server import LandropServer


class TestLandrop(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="landrop_test_")
        self.config_file = os.path.join(self.test_dir, "test_config.json")
        self.save_dir = os.path.join(self.test_dir, "received")
        self.safe_share_dir = os.path.join(self.test_dir, "shared_docs")
        os.makedirs(self.safe_share_dir, exist_ok=True)
        os.makedirs(self.save_dir, exist_ok=True)

        # Create sample files in safe share dir
        self.sample_txt = os.path.join(self.safe_share_dir, "hello.txt")
        with open(self.sample_txt, "w") as f:
            f.write("Hello LANDrop test!")

        # Create subfolder in safe share dir
        self.sub_dir = os.path.join(self.safe_share_dir, "subfolder")
        os.makedirs(self.sub_dir, exist_ok=True)
        with open(os.path.join(self.sub_dir, "nested.txt"), "w") as f:
            f.write("Nested file content")

        # Initialize test config manager
        self.cfg = ConfigManager(self.config_file)
        self.cfg.set_save_directory(self.save_dir)
        self.cfg.set_auto_save(True)
        self.safe_entry = self.cfg.add_safe_folder(self.safe_share_dir, "Test Shared Docs")

        # Patch server to use our test config
        import core.server as srv_mod
        self.orig_config = srv_mod.config
        srv_mod.config = self.cfg

        self.server = LandropServer(port=5999)
        self.app = self.server.app
        self.client = self.app.test_client()

    def tearDown(self):
        if hasattr(self, "server") and self.server:
            self.server.shutdown()
        import core.server as srv_mod
        srv_mod.config = self.orig_config
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # 1. Network tests
    def test_network_discovery(self):
        hostname = get_hostname()
        self.assertTrue(len(hostname) > 0)
        local_ip = get_local_ip()
        self.assertTrue(len(local_ip) > 0)
        urls = get_connection_urls(5000)
        self.assertIn("mdns_url", urls)
        self.assertIn(".local:5000", urls["mdns_url"])
        self.assertIn("primary_url", urls)

    # 2. Config & Security Sandboxing tests
    def test_safe_path_sandboxing(self):
        # Valid subpath
        real_path = self.cfg.resolve_safe_path(self.safe_entry["id"], "hello.txt")
        self.assertEqual(os.path.realpath(self.sample_txt), real_path)

        # Valid nested subpath
        nested_real = self.cfg.resolve_safe_path(self.safe_entry["id"], "subfolder/nested.txt")
        self.assertTrue(os.path.exists(nested_real))

        # Path traversal attack attempts MUST fail
        with self.assertRaises(PermissionError):
            self.cfg.resolve_safe_path(self.safe_entry["id"], "../../../test_config.json")

        with self.assertRaises(PermissionError):
            self.cfg.resolve_safe_path(self.safe_entry["id"], "..\\..\\Windows\\System32")

    # 3. Auto-Save unique filename tests
    def test_auto_save_unique_filepath(self):
        p1, f1 = self.cfg.get_unique_filepath("photo.jpg")
        self.assertEqual(f1, "photo.jpg")
        # Create file
        with open(p1, "w") as f:
            f.write("test")

        # Second time should return photo (1).jpg
        p2, f2 = self.cfg.get_unique_filepath("photo.jpg")
        self.assertEqual(f2, "photo (1).jpg")

    # 4. REST API: Info & Web App
    def test_api_info_and_index(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"NearBeam", res.data)

        res = self.client.get("/api/info")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["auto_save"])
        self.assertEqual(data["safe_folders_count"], 1)

    # 5. REST API: Safe List browse & download
    def test_safelist_browse_and_download(self):
        res = self.client.get("/api/safelist")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(len(data["folders"]), 1)
        folder_id = data["folders"][0]["id"]

        # Browse root
        res = self.client.get(f"/api/safelist/browse?folder_id={folder_id}&subpath=")
        self.assertEqual(res.status_code, 200)
        browse_data = res.get_json()
        names = [item["name"] for item in browse_data["items"]]
        self.assertIn("hello.txt", names)
        self.assertIn("subfolder", names)

        # Download file
        res = self.client.get(f"/api/safelist/download?folder_id={folder_id}&subpath=hello.txt")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, b"Hello LANDrop test!")
        res.close()

        # Download folder as ZIP
        res = self.client.get(f"/api/safelist/download-zip?folder_id={folder_id}&subpath=")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "application/zip")
        self.assertTrue(len(res.data) > 0)
        res.close()

        # Test asynchronous ZIP packaging job with progress tracking
        prep_res = self.client.post(
            "/api/safelist/prepare-zip",
            json={"folder_id": folder_id, "subpath": ""},
        )
        self.assertEqual(prep_res.status_code, 200)
        prep_data = prep_res.get_json()
        self.assertTrue(prep_data["success"])
        job_id = prep_data["job_id"]
        self.assertGreater(prep_data["total_files"], 0)

        # Poll progress until completed (with timeout)
        import time
        completed = False
        for _ in range(50):
            prog_res = self.client.get(f"/api/safelist/zip-progress?job_id={job_id}")
            self.assertEqual(prog_res.status_code, 200)
            prog_data = prog_res.get_json()
            if prog_data["status"] == "completed":
                self.assertEqual(prog_data["percent"], 100.0)
                completed = True
                break
            time.sleep(0.05)
        self.assertTrue(completed, "ZIP packaging job did not complete in time")

        # Download the prepared ZIP file
        dl_res = self.client.get(f"/api/safelist/download-zip-file?job_id={job_id}")
        self.assertEqual(dl_res.status_code, 200)
        self.assertEqual(dl_res.mimetype, "application/zip")
        self.assertGreater(len(dl_res.data), 0)
        dl_res.close()

        # Test cancelling a ZIP job
        prep_res2 = self.client.post(
            "/api/safelist/prepare-zip",
            json={"folder_id": folder_id, "subpath": ""},
        )
        self.assertEqual(prep_res2.status_code, 200)
        job_id2 = prep_res2.get_json()["job_id"]
        cancel_res = self.client.post(
            "/api/safelist/cancel-zip",
            json={"job_id": job_id2},
        )
        self.assertEqual(cancel_res.status_code, 200)
        self.assertTrue(cancel_res.get_json()["success"])

        # Path traversal attack via API must return 403
        res = self.client.get(f"/api/safelist/browse?folder_id={folder_id}&subpath=../../")
        self.assertEqual(res.status_code, 403)

    # 6. REST API: Auto-Save Upload
    def test_autosave_upload(self):
        test_content = b"Binary video or photo content for testing"
        data = {
            "files": (io.BytesIO(test_content), "vacation_pic.png"),
        }
        res = self.client.post(
            "/api/upload",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 200)
        resp = res.get_json()
        self.assertTrue(resp["success"])
        self.assertEqual(resp["saved_count"], 1)

        # Verify file is physically saved in save_dir
        saved_path = os.path.join(self.save_dir, "vacation_pic.png")
        self.assertTrue(os.path.exists(saved_path))
        with open(saved_path, "rb") as f:
            self.assertEqual(f.read(), test_content)

        # Verify /api/received lists it
        res = self.client.get("/api/received")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        filenames = [f["filename"] for f in data["files"]]
        self.assertIn("vacation_pic.png", filenames)

    # 7. REST API: Clipboard sharing
    def test_clipboard_sharing(self):
        post_res = self.client.post(
            "/api/clipboard",
            json={"text": "Wi-Fi transfer secret code: 987654"},
        )
        self.assertEqual(post_res.status_code, 200)

        get_res = self.client.get("/api/clipboard")
        self.assertEqual(get_res.status_code, 200)
        self.assertEqual(get_res.get_json()["text"], "Wi-Fi transfer secret code: 987654")

    # 8. REST API: Mesh Cluster & Peer Discovery
    def test_mesh_nodes_api(self):
        res = self.client.get("/api/mesh/nodes")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("role", data)
        self.assertIn("nodes", data)
        self.assertTrue(len(data["nodes"]) >= 1)
        self.assertTrue(any(n["is_self"] for n in data["nodes"]))

    # 9. REST API: Secondary Node Registration & Heartbeat
    def test_mesh_node_registration_and_heartbeat(self):
        reg_res = self.client.post(
            "/api/mesh/register",
            json={
                "node_id": "test_laptop_123",
                "name": "Deb_Laptop",
                "ip": "192.168.1.88",
                "port": 5000,
            },
        )
        self.assertEqual(reg_res.status_code, 200)
        reg_data = reg_res.get_json()
        self.assertTrue(reg_data["success"])

        # Check nodes list now includes the registered laptop
        nodes_res = self.client.get("/api/mesh/nodes")
        self.assertEqual(nodes_res.status_code, 200)
        nodes = nodes_res.get_json()["nodes"]
        node_ids = [n["id"] for n in nodes]
        self.assertIn("test_laptop_123", node_ids)

        # Heartbeat ping
        hb_res = self.client.post(
            "/api/mesh/heartbeat",
            json={"node_id": "test_laptop_123"},
        )
        self.assertEqual(hb_res.status_code, 200)
        self.assertTrue(hb_res.get_json()["updated"])

    # 10. REST API: Cross-node clipboard propagation
    def test_mesh_clipboard_sync(self):
        sync_res = self.client.post(
            "/api/mesh/clipboard-sync",
            json={
                "text": "Cross-device synced text via mesh",
                "source_node_id": "test_laptop_123",
            },
        )
        self.assertEqual(sync_res.status_code, 200)
        self.assertTrue(sync_res.get_json()["success"])

        # Verify clipboard on host updated
        clip_res = self.client.get("/api/clipboard")
        self.assertEqual(clip_res.status_code, 200)
        self.assertEqual(clip_res.get_json()["text"], "Cross-device synced text via mesh")


if __name__ == "__main__":
    unittest.main()

