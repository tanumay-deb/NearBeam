"""
Offscreen smoke test for the desktop Control Panel: builds MainWindow against a
throwaway config, visits every page, and feeds fake incoming-upload events.
Set NEARBEAM_SNAPSHOT_DIR to also save a PNG of each page for visual review.
"""

import json
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import core.server as srv_mod
import gui
from core.config import ConfigManager


class TestControlPanelSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.tmp = tempfile.mkdtemp(prefix="landrop_gui_")
        cls.cfg = ConfigManager(os.path.join(cls.tmp, "cfg.json"))
        cls.cfg.set_save_directory(os.path.join(cls.tmp, "recv"))
        cls.orig = (gui.config, srv_mod.config, gui.NearBeamServer)
        gui.config = srv_mod.config = cls.cfg
        gui.NearBeamServer = lambda port: srv_mod.LandropServer(port=port, enable_mesh=False)
        cls.win = gui.MainWindow()
        cls.win.resize(1000, 700)
        cls.win.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.win.tray_icon.hide()
        cls.win._clean_exit()
        gui.config, srv_mod.config, gui.NearBeamServer = cls.orig
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _settle(self, ms=400):
        from PySide6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def test_every_page_renders(self):
        snap_dir = os.environ.get("NEARBEAM_SNAPSHOT_DIR")
        names = ["home", "transfers", "shared", "clipboard", "settings"]
        for i, name in enumerate(names):
            self.win._go(i)
            self._settle()
            self.assertEqual(self.win.stack.currentIndex(), i)
            self.assertTrue(self.win.nav_buttons[i].isChecked())
            if snap_dir:
                os.makedirs(snap_dir, exist_ok=True)
                self.win.grab().save(os.path.join(snap_dir, f"{i}_{name}.png"))
        self.assertGreaterEqual(self.win.home.devices_grid.count(), 1)

    def test_incoming_upload_events_become_rows_then_history(self):
        q = self.win.server_events
        q.put(json.dumps({"type": "upload_progress", "session_id": "abc", "filename": "movie.mp4",
                          "percent": 40, "received_bytes": 40_000_000, "total_bytes": 100_000_000,
                          "from_addr": "10.0.0.9"}))
        self.win._drain_server_events()
        self.assertIn("in:abc", self.win.active_transfers)
        self.assertEqual(self.win.active_transfers["in:abc"]["row"].pct.text(), "40%")
        self.assertEqual(self.win.nav_buttons[1].badge.text(), "1")
        self.win.cfg_record = self.cfg.add_transfer_history({"filename": "movie.mp4", "size": 100_000_000, "peer": "10.0.0.9"})
        q.put(json.dumps({"type": "file_received", "session_id": "abc", "file": {"filename": "movie.mp4"}}))
        self.win._drain_server_events()
        self._settle(1800)
        self.assertNotIn("in:abc", self.win.active_transfers)
        self.assertGreaterEqual(self.win.transfers_page.history_box.count(), 1)
        if os.environ.get("NEARBEAM_SNAPSHOT_DIR"):
            self.win._go(1)
            self._settle()
            self.win.grab().save(os.path.join(os.environ["NEARBEAM_SNAPSHOT_DIR"], "1b_transfers_history.png"))

    def test_phone_download_events_show_a_row_with_speed_and_eta(self):
        q = self.win.server_events
        q.put(json.dumps({"type": "download_progress", "download_id": "dl:x:a.mp4", "filename": "a.mp4",
                          "sent": 10_000_000, "total": 100_000_000, "to_addr": "10.0.0.7"}))
        self.win._drain_server_events()
        self._settle(300)
        q.put(json.dumps({"type": "download_progress", "download_id": "dl:x:a.mp4", "filename": "a.mp4",
                          "sent": 20_000_000, "total": 100_000_000, "to_addr": "10.0.0.7"}))
        self.win._drain_server_events()
        row = self.win.active_transfers["dl:x:a.mp4"]["row"]
        self.assertIn("/s", row.stats.text())
        self.assertIn("left", row.stats.text())
        if os.environ.get("NEARBEAM_SNAPSHOT_DIR"):
            self.win._go(1)
            self._settle()
            self.win.grab().save(os.path.join(os.environ["NEARBEAM_SNAPSHOT_DIR"], "1c_download_row.png"))
        q.put(json.dumps({"type": "download_finished", "download_id": "dl:x:a.mp4", "completed": True}))
        self.win._drain_server_events()
        self._settle(1800)
        self.assertNotIn("dl:x:a.mp4", self.win.active_transfers)

    def test_clipboard_switches_stay_in_step(self):
        start = self.cfg.clipboard_sync_enabled
        self.win.clipboard_page.sync_row.switch.click()
        self.assertEqual(self.win.chk_clipboard_sync.isChecked(), not start)
        self.assertEqual(self.cfg.clipboard_sync_enabled, not start)
        self.win.chk_clipboard_sync.click()
        self.assertEqual(self.win.clipboard_page.sync_row.switch.isChecked(), start)

    def test_clipboard_page_shows_text(self):
        self.win._show_clip("text", "hello from phone", "From another device")
        self.assertEqual(self.win.clipboard_page.text_view.toPlainText(), "hello from phone")
        self.assertIn("From another device", self.win.clipboard_page.meta.text())


if __name__ == "__main__":
    unittest.main()
