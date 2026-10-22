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

        self.server = LandropServer(port=5999, enable_mesh=False)
        from core.mesh import MeshManager
        self.server.mesh = MeshManager(
            port=5999,
            on_role_change=self.server._on_mesh_role_change,
            on_nodes_change=self.server._on_mesh_nodes_change,
            on_remote_clipboard=self.server._on_mesh_clipboard_received,
        )
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

    # 11. REST API: Nearby Devices Dashboard & Client Heartbeat (NB-02)
    def test_nearby_devices_and_heartbeat(self):
        hb_res = self.client.post(
            "/api/nearby/heartbeat",
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"},
        )
        self.assertEqual(hb_res.status_code, 200)
        hb_data = hb_res.get_json()
        self.assertTrue(hb_data["success"])
        self.assertGreaterEqual(len(hb_data["devices"]), 1)

        nearby_res = self.client.get("/api/nearby")
        self.assertEqual(nearby_res.status_code, 200)
        devices = nearby_res.get_json()["devices"]
        self.assertTrue(any(d.get("kind") == "host" for d in devices))

    # 12. REST API: Transfer History & Upload Cancel Cleanup (NB-03)
    def test_transfer_history_and_cancel(self):
        # Perform a chunked upload and verify it is recorded in persistent history
        chunk_res = self.client.post(
            "/api/upload/chunk",
            data=b"chunked_payload_bytes",
            headers={
                "X-Session-Id": "sess-hist-1",
                "X-Filename": "report.pdf",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
            },
        )
        self.assertEqual(chunk_res.status_code, 200)
        self.assertTrue(chunk_res.get_json()["completed"])

        hist_res = self.client.get("/api/history")
        self.assertEqual(hist_res.status_code, 200)
        history = hist_res.get_json()["history"]
        self.assertTrue(any(h["filename"] == "report.pdf" for h in history))

        # Start a multi-chunk upload then cancel it, verifying .tmp cleanup
        self.client.post(
            "/api/upload/chunk",
            data=b"partial_bytes",
            headers={
                "X-Session-Id": "sess-cancel-99",
                "X-Filename": "partial_video.mp4",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "3",
            },
        )
        tmp_path = os.path.join(self.save_dir, ".sess-cancel-99.tmp")
        self.assertTrue(os.path.exists(tmp_path))

        status_res = self.client.get("/api/upload/status/sess-cancel-99").get_json()
        self.assertEqual(status_res["received_bytes"], len(b"partial_bytes"))

        cancel_res = self.client.post(
            "/api/upload/cancel",
            json={"session_id": "sess-cancel-99"},
        )
        self.assertEqual(cancel_res.status_code, 200)
        self.assertTrue(cancel_res.get_json()["removed_tmp"])
        self.assertFalse(os.path.exists(tmp_path))

        # Clear history
        clr_res = self.client.post("/api/history/clear")
        self.assertEqual(clr_res.status_code, 200)
        self.assertEqual(len(self.client.get("/api/history").get_json()["history"]), 0)

    # 13. REST API: Windows Shell Send & Single-File Safe List (NB-04 & NB-05)
    def test_shell_send_file_and_folder(self):
        single_file = os.path.join(self.test_dir, "direct_share.txt")
        with open(single_file, "w") as f:
            f.write("Shared from Windows Explorer right-click!")

        shell_res = self.client.post(
            "/api/shell-send",
            json={"paths": [single_file]},
        )
        self.assertEqual(shell_res.status_code, 200)
        shell_data = shell_res.get_json()
        self.assertTrue(shell_data["success"])
        self.assertEqual(shell_data["added_count"], 1)

        # Verify single file is browsable and downloadable via Safe List
        sl_res = self.client.get("/api/safelist").get_json()
        file_entry = next((x for x in sl_res["folders"] if x["name"] == "direct_share.txt"), None)
        self.assertIsNotNone(file_entry)
        self.assertTrue(file_entry["is_file"])

        browse_res = self.client.get(f"/api/safelist/browse?folder_id={file_entry['id']}").get_json()
        self.assertEqual(len(browse_res["items"]), 1)
        self.assertEqual(browse_res["items"][0]["name"], "direct_share.txt")

        dl_res = self.client.get(f"/api/safelist/download?folder_id={file_entry['id']}&subpath=direct_share.txt")
        self.assertEqual(dl_res.status_code, 200)
        self.assertEqual(dl_res.data, b"Shared from Windows Explorer right-click!")
        dl_res.close()

    # 14. REST API: Rich Clipboard Sync (Screenshots & Copied Images NB-07)
    def test_rich_clipboard_image_sync(self):
        fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR_FAKE_SCREENSHOT_BYTES"
        up_res = self.client.post(
            "/api/clipboard/image",
            data={"image": (io.BytesIO(fake_png), "screenshot.png", "image/png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(up_res.status_code, 200)
        up_data = up_res.get_json()
        self.assertTrue(up_data["success"])
        self.assertTrue(up_data["has_image"])
        self.assertEqual(up_data["image_size"], len(fake_png))

        # Verify GET /api/clipboard reports the image metadata
        clip_meta = self.client.get("/api/clipboard").get_json()
        self.assertTrue(clip_meta["has_image"])
        self.assertIsNotNone(clip_meta["image_url"])

        # Verify GET /api/clipboard/image returns exact bytes
        img_res = self.client.get("/api/clipboard/image")
        self.assertEqual(img_res.status_code, 200)
        self.assertEqual(img_res.data, fake_png)
        img_res.close()

        # Verify clear endpoint removes the image
        clr_res = self.client.post("/api/clipboard/image/clear")
        self.assertEqual(clr_res.status_code, 200)
        self.assertFalse(clr_res.get_json()["has_image"])
        self.assertEqual(self.client.get("/api/clipboard/image").status_code, 404)

        # Verify mesh clipboard-sync propagates base64 images
        import base64
        peer_png = b"\x89PNG\r\n\x1a\n_PEER_HOST_SNIP"
        mesh_res = self.client.post(
            "/api/mesh/clipboard-sync",
            json={
                "text": "",
                "image_base64": base64.b64encode(peer_png).decode("ascii"),
                "image_mime": "image/png",
                "source_node_id": "peer_laptop_1",
            },
        )
        self.assertEqual(mesh_res.status_code, 200)
        img_res2 = self.client.get("/api/clipboard/image")
        self.assertEqual(img_res2.status_code, 200)
        self.assertEqual(img_res2.data, peer_png)
        img_res2.close()

    # 15. REST API: Guest Drop Zone End-to-End (Token, UI, Chunked Upload, Expiry)
    def test_guest_drop_zone_end_to_end(self):
        create_res = self.client.post("/api/drop/create")
        self.assertEqual(create_res.status_code, 200)
        drop_data = create_res.get_json()
        self.assertTrue(drop_data["success"])
        token = drop_data["token"]

        # Valid drop page renders
        page_res = self.client.get(f"/drop/{token}")
        self.assertEqual(page_res.status_code, 200)
        self.assertIn(b"Guest Drop Zone", page_res.data)
        self.assertIn(b"globalDropOverlay", page_res.data)

        # Invalid token returns 404
        self.assertEqual(self.client.get("/drop/invalid-token-xyz").status_code, 404)

        # Multi-chunk upload into Guest Drops with relative_dir
        for idx, part in enumerate([b"guest_part_1_", b"guest_part_2"]):
            up_res = self.client.post(
                f"/api/drop/{token}/upload/chunk",
                data=part,
                content_type="application/octet-stream",
                headers={
                    "X-Session-Id": "guest-sess-42",
                    "X-Filename": "guest_notes.txt",
                    "X-Chunk-Index": str(idx),
                    "X-Total-Chunks": "2",
                    "X-Relative-Dir": "ProjectA/Docs",
                },
            )
            self.assertEqual(up_res.status_code, 200)
            if idx == 1:
                self.assertTrue(up_res.get_json()["completed"])

        expected_guest_file = os.path.join(self.save_dir, "Guest Drops", "ProjectA", "Docs", "guest_notes.txt")
        self.assertTrue(os.path.exists(expected_guest_file))
        with open(expected_guest_file, "rb") as f:
            self.assertEqual(f.read(), b"guest_part_1_guest_part_2")

    # 16. REST API: HTTP 206 Range Resume & Large File (>512 KB) Async ZIP Packaging
    def test_http_206_range_resume_and_large_zip(self):
        large_file = os.path.join(self.safe_share_dir, "large_dataset.txt")
        large_payload = (b"NEARBEAM_HIGH_SPEED_LINE_0123456789\n" * 25000)  # ~900 KB (> 512 KB threshold)
        with open(large_file, "wb") as f:
            f.write(large_payload)

        folder_id = self.safe_entry["id"]

        # HTTP 206 Partial Content Range request
        range_res = self.client.get(
            f"/api/safelist/download?folder_id={folder_id}&subpath=large_dataset.txt",
            headers={"Range": "bytes=100-199"},
        )
        self.assertEqual(range_res.status_code, 206)
        self.assertEqual(len(range_res.data), 100)
        self.assertEqual(range_res.data, large_payload[100:200])
        range_res.close()

        # Async ZIP packaging with file > 512 KB
        prep_res = self.client.post(
            "/api/safelist/prepare-zip",
            json={"folder_id": folder_id, "subpath": ""},
        )
        self.assertEqual(prep_res.status_code, 200)
        job_id = prep_res.get_json()["job_id"]

        import time
        import zipfile
        for _ in range(60):
            prog = self.client.get(f"/api/safelist/zip-progress?job_id={job_id}").get_json()
            if prog["status"] == "completed":
                break
            time.sleep(0.05)

        dl_res = self.client.get(f"/api/safelist/download-zip-file?job_id={job_id}")
        self.assertEqual(dl_res.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(dl_res.data), "r") as zf:
            self.assertIn("large_dataset.txt", zf.namelist())
            self.assertEqual(zf.read("large_dataset.txt"), large_payload)
        dl_res.close()

    # 17. REST API: QR Code, Finder Page, and SSE Event Stream
    def test_qr_finder_and_sse_events(self):
        qr_res = self.client.get("/api/qr")
        self.assertEqual(qr_res.status_code, 200)
        self.assertEqual(qr_res.mimetype, "image/png")
        self.assertTrue(qr_res.data.startswith(b"\x89PNG"))
        qr_res.close()

        finder_res = self.client.get("/finder")
        self.assertEqual(finder_res.status_code, 200)
        self.assertIn(b"NearBeam", finder_res.data)

        # Verify SSE broadcast delivery
        import json
        import queue
        q = queue.Queue()
        self.server.event_subscribers.append(q)
        try:
            self.server.broadcast_event({"type": "unit_test_ping", "ok": True})
            msg = json.loads(q.get(timeout=2.0))
            self.assertEqual(msg["type"], "unit_test_ping")
        finally:
            if q in self.server.event_subscribers:
                self.server.event_subscribers.remove(q)

    # 18. Frontend DOM Integrity & Windows Shell Integration Command Builder
    def test_frontend_dom_and_shell_integration(self):
        index_html = self.client.get("/").data.decode("utf-8")
        required_ids = [
            "nearbyDashboardCard",
            "nearbyDevicesList",
            "nearbyDevicesCountBadge",
            "globalDropOverlay",
            "clipboardImageCard",
            "clipboardImagePreview",
            "btnDownloadClipboardImage",
            "btnCopyClipboardImage",
            "btnClearClipboardImage",
            "transferManagerCard",
            "activeTransfersList",
            "failedTransfersCard",
            "btnRetryAllFailed",
            "btnClearHistory",
        ]
        for dom_id in required_ids:
            self.assertIn(f'id="{dom_id}"', index_html, f"Missing DOM element #{dom_id} in index.html")

        from core.shell_integration import _get_launch_command_and_icon
        cmd, icon = _get_launch_command_and_icon()
        self.assertIn("--send", cmd)
        self.assertIn('"%1"', cmd)
        self.assertTrue(len(icon) > 0)


if __name__ == "__main__":
    unittest.main()

