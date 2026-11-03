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

    def test_stalled_incoming_upload_does_not_stay_active_forever(self):
        self.win.server_events.put(json.dumps({"type": "upload_progress", "session_id": "stuck", "filename": "v.mp4",
                                               "received_bytes": 50_000_000, "total_bytes": 90_000_000}))
        self.win._drain_server_events()
        entry = self.win.active_transfers["in:stuck"]
        entry["last"] = (entry["last"][0] - 121, entry["last"][1])  # nothing heard for 2 minutes
        self.win._sweep_stalled_rows()
        self._settle(3300)
        self.assertNotIn("in:stuck", self.win.active_transfers)

    def test_secondary_pc_does_not_advertise_nearbeam_local(self):
        self.win._on_mesh_updated("node", [])
        self.assertNotIn("nearbeam.local", self.win.addr_labels["name"].text())
        self.win._on_mesh_updated("leader", [])
        self.assertIn("nearbeam.local", self.win.addr_labels["name"].text())

    def test_folder_shared_from_explorer_shows_in_shared_folders(self):
        # Explorer "Send with NearBeam" reaches a running window through /api/shell-send.
        folder = os.path.join(self.tmp, "from_explorer")
        os.makedirs(folder, exist_ok=True)
        r = self.win.server_instance.app.test_client().post("/api/shell-send", json={"paths": [folder]})
        self.assertEqual(r.status_code, 200)
        self.win._drain_server_events()
        table = self.win.safe_table
        self.assertIn(os.path.realpath(folder), [table.item(i, 1).text() for i in range(table.rowCount())])

    def test_sidebar_follows_a_network_change(self):
        orig = gui.get_connection_urls
        gui.get_connection_urls = lambda port: {**orig(port), "primary_url": f"http://10.9.8.7:{port}"}
        try:
            self.win.server_events.put(json.dumps({"type": "network_changed", "ip": "10.9.8.7"}))
            self.win._drain_server_events()
        finally:
            gui.get_connection_urls = orig
        self.assertEqual(self.win.addr_labels["ip"].text(), f"10.9.8.7:{self.win.port}")

    def test_password_manager_copies_stay_on_this_pc(self):
        from PySide6.QtCore import QByteArray, QMimeData
        self.win._set_clipboard_switches(True)
        self.win._ignore_clipboard_until = 0
        secret = QMimeData()
        secret.setText("hunter2-secret")
        secret.setData('application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing"', QByteArray(b"\x01"))
        QApplication.clipboard().setMimeData(secret)
        self.app.processEvents()
        self.assertNotEqual(self.win.server_instance.clipboard_content, "hunter2-secret")
        QApplication.clipboard().setText("ordinary text")
        self.app.processEvents()
        self.assertEqual(self.win.server_instance.clipboard_content, "ordinary text")

    def test_cloud_clipboard_opt_out_counts_as_private(self):
        from PySide6.QtCore import QByteArray, QMimeData
        fmt = 'application/x-qt-windows-mime;value="CanUploadToCloudClipboard"'
        mime = QMimeData()
        mime.setText("x")
        mime.setData(fmt, QByteArray(b"\0\0\0\0"))
        self.assertTrue(gui.is_private_clipboard(mime))
        mime.setData(fmt, QByteArray(b"\x01\0\0\0"))
        self.assertFalse(gui.is_private_clipboard(mime))

    def test_incoming_image_is_not_sent_back_as_this_pcs(self):
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QColor, QImage
        self.win._set_clipboard_switches(True)
        img = QImage(8, 8, QImage.Format_RGB32)
        img.fill(QColor("orange"))
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "JPG")
        buf.close()
        self.win.server_instance.clipboard_image_bytes = None
        self.win._on_remote_clipboard_image_received(bytes(data), "image/jpeg")
        self.win._on_local_clipboard_changed()  # Windows reports our own write a moment later
        self.assertIsNone(self.win.server_instance.clipboard_image_bytes)

    def test_failed_update_check_is_not_reported_as_up_to_date(self):
        self.win.manual_update_check = False
        self.win._on_update_checked({"checked": False, "error": "offline", "update_available": False})
        self.assertNotIn("Up to date", self.win.btn_update.text())
        self.win._on_update_checked({"checked": True, "update_available": False})
        self.assertIn("Up to date", self.win.btn_update.text())

    def _firewall_launch(self, bat):
        """The command _configure_firewall launches for port 5003, with allow_firewall.bat at `bat`."""
        launched = []
        orig = (gui.get_resource_path, gui.subprocess.Popen, gui.QMessageBox.information, self.win.port)
        gui.get_resource_path = lambda rel: gui.Path(bat)
        gui.subprocess.Popen = lambda args, **kw: launched.append(args)
        gui.QMessageBox.information = lambda *a, **k: None
        self.win.port = 5003
        try:
            self.win._configure_firewall()
        finally:
            gui.get_resource_path, gui.subprocess.Popen, gui.QMessageBox.information, self.win.port = orig
        return launched[0]

    def test_firewall_script_gets_the_port_in_use(self):
        # A stand-in script in a folder with a space and an apostrophe records what it was given.
        import subprocess
        folder = os.path.join(self.tmp, "Program Files", "O'Neil")
        os.makedirs(folder, exist_ok=True)
        bat = os.path.join(folder, "allow_firewall.bat")
        with open(bat, "w") as f:
            f.write('@echo %~1> "%~dp0got_port.txt"\n')
        # Run what NearBeam would launch, minus the elevation prompt.
        args = [a.replace("-Verb RunAs", "-Wait -WindowStyle Hidden") for a in self._firewall_launch(bat)]
        subprocess.run(args, timeout=60)
        with open(os.path.join(folder, "got_port.txt")) as f:
            self.assertEqual(f.read().strip(), "5003")

    def test_firewall_rules_without_the_script_reach_powershell_intact(self):
        # A pip install has no allow_firewall.bat beside gui.py, so the rules go straight to PowerShell.
        import subprocess
        launched = self._firewall_launch(os.path.join(self.tmp, "missing", "allow_firewall.bat"))
        self.assertIn("-LocalPort 5003", launched[-1])
        # Run it minus the elevation prompt, with -WhatIf so no rule is really added.
        out, err = os.path.join(self.tmp, "fw_out.txt"), os.path.join(self.tmp, "fw_err.txt")
        redirect = f"-Wait -NoNewWindow -RedirectStandardOutput '{out}' -RedirectStandardError '{err}'"
        args = [a.replace("-Verb RunAs", redirect).replace("-Action Allow", "-Action Allow -WhatIf") for a in launched]
        subprocess.run(args, timeout=60)
        with open(out) as f_out, open(err) as f_err:
            self.assertEqual(f_err.read().strip(), "")
            self.assertEqual(f_out.read().count("What if"), 4)

    def test_remote_device_names_are_plain_text(self):
        from PySide6.QtCore import Qt
        from ui.widgets import DeviceCard
        card = DeviceCard({"kind": "client", "name": "<b>evil</b>", "device_type": "phone"})
        name = [l for l in card.findChildren(type(self.win.addr_labels["name"])) if l.text() == "<b>evil</b>"][0]
        self.assertEqual(name.textFormat(), Qt.PlainText)

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
