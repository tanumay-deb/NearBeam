"""Tests for Auto-Organize Received (#5): config, routing, and recursive /api/received."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

import core.server as srv_mod
from core.config import ConfigManager
from core.server import NearBeamServer


class TestAutoOrganizeConfigAndRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nb_org_")
        self.cfg = ConfigManager(os.path.join(self.tmp, "cfg.json"))
        self.cfg.set_save_directory(os.path.join(self.tmp, "recv"))
        self.cfg.set_auto_save(True)
        self.orig = srv_mod.config
        srv_mod.config = self.cfg
        self.server = NearBeamServer(port=5971, enable_mesh=False)
        self.client = self.server.app.test_client()

    def tearDown(self):
        self.server.shutdown()
        srv_mod.config = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_defaults_off_persists_and_sanitizes(self):
        self.assertEqual(self.cfg.auto_organize_mode, "off")
        self.cfg.set_auto_organize_mode("type")
        reloaded = ConfigManager(self.cfg.config_path)
        self.assertEqual(reloaded.auto_organize_mode, "type")
        with open(self.cfg.config_path, "w", encoding="utf-8") as f:
            json.dump({"auto_organize_mode": "invalid_mode"}, f)
        sanitized = ConfigManager(self.cfg.config_path)
        self.assertEqual(sanitized.auto_organize_mode, "off")

    def test_mode_type_organizes_loose_files_and_preserves_folder_uploads_and_guest_drops(self):
        self.cfg.set_auto_organize_mode("type")
        # Loose image via multipart /api/upload (iOS Shortcut compatible)
        r_img = self.client.post(
            "/api/upload",
            data={"file": (io.BytesIO(b"jpg"), "sunset.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r_img.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, "Photos", "sunset.jpg")))

        # Loose video via chunk upload
        r_vid = self.client.post(
            "/api/upload/chunk",
            data=b"mp4data",
            headers={
                "X-Session-Id": "org-vid-1",
                "X-Filename": "clip.mp4",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "7",
            },
        )
        self.assertEqual(r_vid.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, "Videos", "clip.mp4")))

        # Explicit folder upload MUST NOT split across Photos/Documents
        r_dir = self.client.post(
            "/api/upload/chunk",
            data=b"pdfdata",
            headers={
                "X-Session-Id": "org-dir-1",
                "X-Filename": "plan.pdf",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "7",
                "X-Relative-Dir": "Vacation/Docs",
            },
        )
        self.assertEqual(r_dir.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, "Vacation", "Docs", "plan.pdf")))

        # Guest Drop stays under Guest Drops/
        token = self.client.post("/api/drop/create").get_json()["token"]
        r_guest = self.client.post(
            f"/api/drop/{token}/upload/chunk",
            data=b"guestimg",
            headers={
                "X-Session-Id": "org-guest-1",
                "X-Filename": "snap.png",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "8",
            },
        )
        self.assertEqual(r_guest.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, "Guest Drops", "snap.png")))

    def test_mode_date_and_device_routing(self):
        today = datetime.date.today().isoformat()
        self.cfg.set_auto_organize_mode("date")
        r_date = self.client.post(
            "/api/upload",
            data={"file": (io.BytesIO(b"hello"), "notes.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r_date.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, today, "notes.txt")))

        # Folder upload in date mode keeps folder tree intact under the date bucket
        r_date_dir = self.client.post(
            "/api/upload/chunk",
            data=b"abc",
            headers={
                "X-Session-Id": "org-date-dir-1",
                "X-Filename": "a.txt",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "3",
                "X-Relative-Dir": "Trip/Sub",
            },
        )
        self.assertEqual(r_date_dir.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, today, "Trip", "Sub", "a.txt")))

        self.cfg.set_auto_organize_mode("device")
        r_dev = self.client.post(
            "/api/upload/chunk",
            data=b"abc",
            headers={
                "X-Session-Id": "org-dev-1",
                "X-Filename": "shot.jpg",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "3",
                "X-Device-Name": "My Laptop",
            },
        )
        self.assertEqual(r_dev.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, "My Laptop", "shot.jpg")))

    def test_recursive_received_listing_download_and_traversal_blocking(self):
        self.cfg.set_auto_organize_mode("type")
        self.client.post(
            "/api/upload",
            data={"file": (io.BytesIO(b"photo-bytes"), "pic.jpg")},
            content_type="multipart/form-data",
        )
        # Drop a hidden partial upload inside Photos/ — must stay hidden and un-downloadable
        with open(os.path.join(self.cfg.save_directory, "Photos", ".partial.tmp"), "wb") as f:
            f.write(b"secret-partial")

        listed = self.client.get("/api/received").get_json()["files"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["filename"], "pic.jpg")
        self.assertEqual(listed[0]["rel_path"], "Photos/pic.jpg")
        self.assertEqual(listed[0]["folder"], "Photos")

        dl = self.client.get("/api/received/download/Photos/pic.jpg")
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.data, b"photo-bytes")
        dl.close()

        pv = self.client.get("/api/received/preview/Photos/pic.jpg")
        self.assertEqual(pv.status_code, 200)
        self.assertEqual(pv.data, b"photo-bytes")
        pv.close()

        # Traversal and hidden dotfile MUST be refused
        self.assertIn(self.client.get("/api/received/download/Photos/../../cfg.json").status_code, (403, 404))
        self.assertIn(self.client.get("/api/received/download/..%2Fcfg.json").status_code, (403, 404))
        self.assertEqual(self.client.get("/api/received/download/Photos/.partial.tmp").status_code, 404)
        self.assertEqual(self.client.get("/api/received/preview/Photos/.partial.tmp").status_code, 404)


class TestAutoOrganizeSettingsGui(unittest.TestCase):
    def test_settings_dropdown_defaults_off_and_updates_config(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        import gui

        app = QApplication.instance() or QApplication([])
        tmp = tempfile.mkdtemp(prefix="nb_org_gui_")
        cfg = ConfigManager(os.path.join(tmp, "cfg.json"))
        cfg.set_save_directory(os.path.join(tmp, "recv"))
        orig = (gui.config, srv_mod.config, gui.NearBeamServer)
        gui.config = srv_mod.config = cfg
        gui.NearBeamServer = lambda port: srv_mod.NearBeamServer(port=port, enable_mesh=False)
        win = None
        try:
            win = gui.MainWindow()
            app.processEvents()
            cmb = win.settings_page.cmb_organize
            self.assertEqual(cmb.currentData(), "off")

            idx_type = cmb.findData("type")
            self.assertGreaterEqual(idx_type, 0)
            cmb.setCurrentIndex(idx_type)
            app.processEvents()
            self.assertEqual(cfg.auto_organize_mode, "type")

            idx_date = cmb.findData("date")
            self.assertGreaterEqual(idx_date, 0)
            cmb.setCurrentIndex(idx_date)
            app.processEvents()
            self.assertEqual(cfg.auto_organize_mode, "date")

            idx_dev = cmb.findData("device")
            self.assertGreaterEqual(idx_dev, 0)
            cmb.setCurrentIndex(idx_dev)
            app.processEvents()
            self.assertEqual(cfg.auto_organize_mode, "device")
        finally:
            if win is not None:
                win.tray_icon.hide()
                win._clean_exit()
                win.deleteLater()
                app.processEvents()
            gui.config, srv_mod.config, gui.NearBeamServer = orig
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
