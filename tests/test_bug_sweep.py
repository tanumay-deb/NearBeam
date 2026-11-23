"""
Comprehensive regression tests for the full-codebase bug sweep (Passes 1-4).
Covers backend concurrency/validation, ZIP timestamps, mesh/Zeroconf lifecycle,
PySide6 GUI stability, OutgoingTransfer races, and web frontend/Guest Drop contracts.
"""

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import core.server as srv_mod
from core.batches import BatchStore
from core.config import ConfigManager
from core.mesh import MeshManager, MeshNode
from core.network import ZeroconfBroadcaster
from core.server import NearBeamServer, is_lan_host, safe_filename
from core.version import parse_version_tuple
from ui.transfer import OutgoingTransfer

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestVersionAndConfigSweep(unittest.TestCase):
    def test_parse_version_tuple_handles_prerelease_tags(self):
        self.assertEqual(parse_version_tuple("v1.6.3-rc1"), (1, 6, 3))
        self.assertEqual(parse_version_tuple("V2.0.1-beta2"), (2, 0, 1))
        self.assertEqual(parse_version_tuple("1.10.4"), (1, 10, 4))
        self.assertEqual(parse_version_tuple(""), (0, 0, 0))

    def test_config_null_json_sanitized_and_saved_path_preserved(self):
        tmp = tempfile.mkdtemp(prefix="nb_cfg_sweep_")
        try:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "safe_folders": None,
                        "transfer_history": None,
                        "port": "not-an-int",
                        "auto_save": None,
                        "save_directory": None,
                    },
                    f,
                )
            cfg = ConfigManager(cfg_path)
            self.assertEqual(cfg.get_safe_folders(), [])
            self.assertEqual(cfg.get_transfer_history(), [])
            self.assertEqual(cfg.data["port"], 5000)
            self.assertIsInstance(cfg.auto_save_enabled, bool)
            self.assertTrue(bool(cfg.save_directory))

            # get_safe_folders returns a detached shallow copy
            d = os.path.join(tmp, "shared")
            os.makedirs(d)
            entry = cfg.add_safe_folder(d)
            snap = cfg.get_safe_folders()
            snap.clear()
            self.assertEqual(len(cfg.get_safe_folders()), 1)

            # resolve_safe_path tolerates None subpath
            self.assertEqual(cfg.resolve_safe_path(entry["id"], None), os.path.realpath(d))

            # add_transfer_history preserves saved_path for Guest Drops / subfolder reveal
            guest_file = os.path.join(tmp, "Guest Drops", "doc.pdf")
            cfg.add_transfer_history({"filename": "doc.pdf", "saved_path": guest_file})
            self.assertEqual(cfg.get_transfer_history()[0]["saved_path"], guest_file)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestServerBugSweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nb_srv_sweep_")
        self.share = os.path.join(self.tmp, "share")
        os.makedirs(self.share)
        self.cfg = ConfigManager(os.path.join(self.tmp, "cfg.json"))
        self.cfg.set_save_directory(os.path.join(self.tmp, "recv"))
        self.cfg.set_auto_save(True)
        self.orig = srv_mod.config
        srv_mod.config = self.cfg
        self.server = NearBeamServer(port=5981, enable_mesh=False)
        self.client = self.server.app.test_client()

    def tearDown(self):
        self.server.mesh = None
        self.server.shutdown()
        srv_mod.config = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_is_lan_host_validates_bracketed_ipv6_literals(self):
        self.assertTrue(is_lan_host("[::1]:5000"))
        self.assertTrue(is_lan_host("[fe80::1]:5000"))
        self.assertTrue(is_lan_host("[::1]"))
        self.assertFalse(is_lan_host("[evil.example.com]:5000"))
        self.assertFalse(is_lan_host("[::1]:abc"))
        self.assertFalse(is_lan_host("[::1"))
        self.assertFalse(is_lan_host(""))

    def test_safe_filename_handles_conin_and_trailing_space_device_names(self):
        self.assertTrue(safe_filename("CONIN$.txt").startswith("_"))
        self.assertTrue(safe_filename("CONOUT$.log").startswith("_"))
        self.assertTrue(safe_filename("CON .txt").startswith("_"))
        self.assertEqual(safe_filename("normal.txt"), "normal.txt")

    def test_zip_download_handles_pre_1980_timestamps(self):
        old_file = os.path.join(self.share, "ancient.txt")
        with open(old_file, "wb") as f:
            f.write(b"retro payload")
        # 1975-01-01 UTC (< 1980 ZIP epoch)
        os.utime(old_file, (157766400, 157766400))
        fid = self.cfg.add_safe_folder(self.share)["id"]

        # Sync ZIP endpoint
        r = self.client.get(f"/api/safelist/download-zip?folder_id={fid}&subpath=")
        self.assertEqual(r.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            self.assertEqual(zf.namelist(), ["ancient.txt"])
            self.assertEqual(zf.read("ancient.txt"), b"retro payload")
        r.close()

        # Async prepare-zip endpoint
        prep = self.client.post("/api/safelist/prepare-zip", json={"folder_id": fid, "subpath": ""})
        self.assertEqual(prep.status_code, 200)
        jid = prep.get_json()["job_id"]
        status = None
        for _ in range(50):
            prog = self.client.get(f"/api/safelist/zip-progress?job_id={jid}").get_json()
            status = prog["status"]
            if status in ("completed", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(status, "completed")

    def test_non_dict_json_returns_400_instead_of_500(self):
        endpoints = [
            "/api/clipboard",
            "/api/clipboard/image",
            "/api/upload/cancel",
            "/api/shell-send",
            "/api/safelist/prepare-zip",
            "/api/safelist/cancel-zip",
            "/api/mesh/register",
            "/api/mesh/heartbeat",
            "/api/mesh/clipboard-sync",
            "/api/nearby/heartbeat",
        ]
        for ep in endpoints:
            for payload in ([1, 2, 3], "plain-string", 42):
                r = self.client.post(ep, json=payload)
                self.assertEqual(r.status_code, 400, (ep, payload, r.data))

    def test_prepare_zip_validation_and_browse_404(self):
        fid = self.cfg.add_safe_folder(self.share)["id"]
        self.assertEqual(self.client.get(f"/api/safelist/browse?folder_id={fid}&subpath=does_not_exist").status_code, 404)
        self.assertEqual(self.client.post("/api/safelist/prepare-zip", json={"folder_id": "missing", "subpath": ""}).status_code, 404)
        self.assertEqual(self.client.post("/api/safelist/prepare-zip", json={"folder_id": fid, "items": []}).status_code, 400)
        self.assertEqual(self.client.post("/api/safelist/prepare-zip", json={"folder_id": fid, "items": "not-a-list"}).status_code, 400)

        f1 = os.path.join(self.share, "a.txt")
        with open(f1, "w") as f:
            f.write("hi")
        batch = self.server.create_send_batch([f1])
        self.assertEqual(
            self.client.post(f"/api/batches/{batch['id']}/prepare-zip", json={"item_ids": []}).status_code,
            400,
        )

    def test_relative_dir_with_colon_prefix_not_misread_as_windows_drive(self):
        r = self.client.post(
            "/api/upload/chunk",
            data=b"abc",
            headers={
                "X-Session-Id": "colon-dir-1",
                "X-Filename": "note.txt",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "3",
                "X-Relative-Dir": "D: notes/sub",
            },
        )
        self.assertEqual(r.status_code, 200, r.data)
        saved = os.path.join(self.cfg.save_directory, "D_ notes", "sub", "note.txt")
        self.assertTrue(os.path.isfile(saved))

        # Bare Windows drive root and parent traversal must still be rejected
        for bad_rel in ("C:/Windows", "../escape", "/abs/path"):
            bad = self.client.post(
                "/api/upload/chunk",
                data=b"x",
                headers={
                    "X-Session-Id": f"bad-{abs(hash(bad_rel))}",
                    "X-Filename": "a.txt",
                    "X-Chunk-Index": "0",
                    "X-Total-Chunks": "1",
                    "X-Relative-Dir": bad_rel,
                },
            )
            self.assertEqual(bad.status_code, 400, bad_rel)

    def test_mesh_clipboard_image_sync_does_not_overwrite_with_stale_text(self):
        broadcasts = []

        class DummyMesh:
            role = "leader"

            def broadcast_clipboard_mesh(self, text="", source_node_id=None, image_base64=None, image_mime="image/png"):
                broadcasts.append((text, bool(image_base64), image_mime))

        self.server.mesh = DummyMesh()
        self.server.clipboard_content = "stale old text"
        self.server.set_clipboard_image(b"\x89PNG\r\n\x1a\nimg", mime_type="image/png", notify_local_os=False, propagate_mesh=True)
        self.assertEqual(broadcasts, [("", True, "image/png")])

        # Incoming mesh sync carrying both image and text must NOT invoke text OS callback over the image
        text_cbs, img_cbs = [], []
        self.server.add_clipboard_callback(text_cbs.append)
        self.server.add_clipboard_image_callback(lambda b, m: img_cbs.append((b, m)))
        b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nnewimg").decode("ascii")
        r = self.client.post(
            "/api/mesh/clipboard-sync",
            json={"text": "fresh text", "image_base64": b64, "image_mime": "image/png", "source_node_id": "peer1"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(img_cbs), 1)
        self.assertEqual(text_cbs, [])

    def test_cancel_mid_upload_prevents_finalization_and_cleans_tmp(self):
        r0 = self.client.post(
            "/api/upload/chunk",
            data=b"12345",
            headers={
                "X-Session-Id": "cancel-mid-1",
                "X-Filename": "aborted.bin",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "2",
                "X-Chunk-Offset": "0",
                "X-File-Size": "10",
            },
        )
        self.assertEqual(r0.status_code, 200)
        rc = self.client.post("/api/upload/cancel", json={"session_id": "cancel-mid-1"})
        self.assertEqual(rc.status_code, 200)
        self.assertTrue(rc.get_json()["removed_tmp"])

        # A late chunk 1 arriving after cancel must not recreate or finalize the file
        r1 = self.client.post(
            "/api/upload/chunk",
            data=b"67890",
            headers={
                "X-Session-Id": "cancel-mid-1",
                "X-Filename": "aborted.bin",
                "X-Chunk-Index": "1",
                "X-Total-Chunks": "2",
                "X-Chunk-Offset": "5",
                "X-File-Size": "10",
            },
        )
        self.assertEqual(r1.status_code, 499)
        self.assertFalse(os.path.exists(os.path.join(self.cfg.save_directory, "aborted.bin")))
        self.assertFalse(os.path.exists(os.path.join(self.cfg.save_directory, ".cancel-mid-1.tmp")))

    def test_qr_endpoint_supports_guest_drop_token_and_rejects_invalid(self):
        drop = self.client.post("/api/drop/create").get_json()
        token = drop["token"]
        qr_ok = self.client.get(f"/api/qr?drop={token}")
        self.assertEqual(qr_ok.status_code, 200)
        self.assertTrue(qr_ok.data.startswith(b"\x89PNG"))
        qr_bad = self.client.get("/api/qr?drop=nonexistent-token")
        self.assertEqual(qr_bad.status_code, 404)

    def test_shell_send_routes_dirs_to_safelist_and_files_to_batch(self):
        sub_dir = os.path.join(self.tmp, "SharedAlbum")
        os.makedirs(sub_dir)
        solo_file = os.path.join(self.tmp, "shot.jpg")
        with open(solo_file, "wb") as f:
            f.write(b"jpgdata")

        r = self.client.post("/api/shell-send", json={"paths": [sub_dir, solo_file]})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual([f["name"] for f in self.cfg.get_safe_folders()], ["SharedAlbum"])
        self.assertIn("batch", data)
        self.assertEqual(data["batch"]["file_count"], 1)

    def test_server_init_with_mesh_enabled_initializes_stopping_first(self):
        with mock.patch.object(MeshManager, "_discover_and_elect", lambda self: self.on_role_change("leader")):
            srv = NearBeamServer(port=5982, enable_mesh=True)
            try:
                self.assertTrue(hasattr(srv, "_stopping"))
                self.assertFalse(srv._stopping.is_set())
            finally:
                srv.shutdown()


class TestMeshNetworkAndBatchesSweep(unittest.TestCase):
    def test_zeroconf_broadcaster_closes_on_register_failure(self):
        import importlib.util
        import types

        # tests/conftest.py stubs core.network.ZeroconfBroadcaster.start globally;
        # load a private copy of core/network.py to test the real implementation.
        spec = importlib.util.spec_from_file_location("_nb_net_real", str(REPO_ROOT / "core" / "network.py"))
        real_net = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real_net)

        closed = []

        class FailingZC:
            def register_service(self, info):
                raise RuntimeError("boom")

            def close(self):
                closed.append(True)

        fake_mod = types.ModuleType("zeroconf")
        fake_mod.Zeroconf = FailingZC
        fake_mod.ServiceInfo = lambda **kw: kw
        b = real_net.ZeroconfBroadcaster(port=5983)
        with mock.patch.dict("sys.modules", {"zeroconf": fake_mod}):
            b.start("192.168.1.50")
        self.assertEqual(closed, [True])
        self.assertIsNone(b.zc)

    def test_mesh_stop_prevents_zombie_promotion(self):
        roles = []
        m = MeshManager(port=5984, device_name="NODE", on_role_change=roles.append)
        m.role = "node"
        m.stop()
        m.promote_to_leader()
        self.assertEqual(m.role, "node")
        self.assertEqual(roles, [])

    def test_mesh_clipboard_skips_hash_cache_when_no_peers(self):
        m = MeshManager(port=5985, device_name="SOLO")
        m.role = "leader"
        m.broadcast_clipboard_mesh("hello world")
        self.assertIsNone(m._last_synced_clipboard_hash)

        # Once a peer joins, the same text is accepted and hashed
        m.register_node(MeshNode("peer1", "Peer", "127.0.0.1", 5999))
        with mock.patch("threading.Thread.start") as mock_start:
            m.broadcast_clipboard_mesh("hello world")
            self.assertEqual(mock_start.call_count, 1)
            self.assertIsNotNone(m._last_synced_clipboard_hash)

    def test_batches_duplicate_merge_preserves_original_label(self):
        tmp = tempfile.mkdtemp(prefix="nb_batch_sweep_")
        try:
            p = os.path.join(tmp, "report.pdf")
            with open(p, "wb") as f:
                f.write(b"pdf")
            store = BatchStore()
            b1 = store.create_or_merge([p], source="Explorer", merge_window=5.0)
            b2 = store.create_or_merge([p], source="Explorer", merge_window=5.0)
            self.assertEqual(b1["id"], b2["id"])
            self.assertEqual(b2["label"], "report.pdf")
            self.assertEqual(b2["file_count"], 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestGuiAndTransferSweep(unittest.TestCase):
    def test_outgoing_transfer_cancel_before_run_skips_open_and_remote_abort(self):
        tmp = tempfile.mkdtemp(prefix="nb_tx_sweep_")
        try:
            fp = os.path.join(tmp, "sample.bin")
            with open(fp, "wb") as f:
                f.write(b"0123456789")
            done = []
            tx = OutgoingTransfer("http://127.0.0.1:1/api/upload", "peer", fp, on_done=lambda ok, msg: done.append((ok, msg)))
            with mock.patch.object(tx, "_abort_remote") as mock_abort:
                tx.cancel()
                tx.run()
            self.assertEqual(done, [(False, "Cancelled")])
            mock_abort.assert_not_called()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gui_autostart_recursion_shell_persist_clipboard_echo_and_fade_in(self):
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint
        from PySide6.QtGui import QColor, QImage
        from PySide6.QtWidgets import QApplication, QWidget

        import gui
        from ui.widgets import fade_in

        app = QApplication.instance() or QApplication([])
        tmp = tempfile.mkdtemp(prefix="nb_gui_sweep_")
        cfg = ConfigManager(os.path.join(tmp, "cfg.json"))
        cfg.set_save_directory(os.path.join(tmp, "recv"))
        orig = (gui.config, srv_mod.config, gui.NearBeamServer)
        gui.config = srv_mod.config = cfg
        gui.NearBeamServer = lambda port: NearBeamServer(port=port, enable_mesh=False)
        win = gui.MainWindow()
        try:
            # 1. _toggle_autostart failure must not recurse infinitely
            with mock.patch.object(gui, "set_autostart", return_value=False), \
                 mock.patch.object(gui.QMessageBox, "warning"):
                win.chk_autostart.blockSignals(True)
                win.chk_autostart.setChecked(True)
                win.chk_autostart.blockSignals(False)
                win._toggle_autostart(True)
                self.assertFalse(win.chk_autostart.isChecked())

            # 2. _toggle_shell_integration failure must NOT overwrite persisted config
            cfg.data["shell_integration_enabled"] = False
            with mock.patch.object(gui, "set_shell_integration", return_value=False), \
                 mock.patch.object(gui.QMessageBox, "warning"):
                win._toggle_shell_integration(True)
                self.assertFalse(cfg.data["shell_integration_enabled"])

            # 3. Incoming JPEG image must not echo back even when _ignore_clipboard_until == 0
            win._set_clipboard_switches(True)
            img = QImage(10, 10, QImage.Format_RGB32)
            img.fill(QColor("teal"))
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.WriteOnly)
            img.save(buf, "JPG")
            buf.close()
            win.server_instance.clipboard_image_bytes = None
            win._on_remote_clipboard_image_received(bytes(ba), "image/jpeg")
            win._ignore_clipboard_until = 0.0  # simulate timer expired before Windows fires dataChanged
            win._on_local_clipboard_changed()
            self.assertIsNone(win.server_instance.clipboard_image_bytes)

            # 4. Rapid fade_in triggers must not drift widget y-position
            parent = QWidget()
            parent.resize(200, 200)
            child = QWidget(parent)
            child.move(0, 0)
            for _ in range(5):
                fade_in(child, duration=40, rise=10)
            deadline = time.time() + 1.0
            while time.time() < deadline and getattr(child, "_nb_slide_anim", None) is not None:
                app.processEvents()
                time.sleep(0.02)
            self.assertEqual(child.pos(), QPoint(0, 0))
            parent.deleteLater()
        finally:
            win._clean_exit()
            win.deleteLater()
            app.processEvents()
            gui.config, srv_mod.config, gui.NearBeamServer = orig
            shutil.rmtree(tmp, ignore_errors=True)


class TestWebFrontendContractsSweep(unittest.TestCase):
    def test_guest_drop_template_and_js_contracts(self):
        tmp = tempfile.mkdtemp(prefix="nb_web_sweep_")
        orig = srv_mod.config
        cfg = ConfigManager(os.path.join(tmp, "cfg.json"))
        cfg.set_save_directory(os.path.join(tmp, "recv"))
        cfg.set_auto_save(True)
        srv_mod.config = cfg
        srv = NearBeamServer(port=5986, enable_mesh=False)
        try:
            client = srv.app.test_client()
            token = client.post("/api/drop/create").get_json()["token"]
            html = client.get(f"/drop/{token}").get_data(as_text=True)
            self.assertIn("window.NEARBEAM_GUEST_DROP = true;", html)
            self.assertIn("window.NEARBEAM_DROP_TOKEN", html)
            self.assertNotIn("var(--bg)", html)
            self.assertNotIn("var(--surface)", html)
            self.assertNotIn("var(--border)", html)
            self.assertIn("global-drop-box", html)
            self.assertIn("transfer-manager-header", html)
        finally:
            srv.shutdown()
            srv_mod.config = orig
            shutil.rmtree(tmp, ignore_errors=True)

        js = (REPO_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn('t.uploadUrl.replace(/\\/upload$/, "/upload/chunk")', js)
        self.assertIn("getCancelBaseUrl(uploadUrl)", js)
        self.assertIn("const captured = [];", js)
        self.assertIn("function closePreviewModal()", js)
        self.assertIn("/api/qr?drop=", js)
        self.assertIn("let nearbyDevices = [];", js)

        # Node syntax check
        res = subprocess.run(["node", "--check", str(REPO_ROOT / "static" / "js" / "app.js")], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_css_tokens_modal_zindex_and_index_selection_bar_placement(self):
        css = (REPO_ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".clipboard-image-card.hidden", css)
        self.assertNotIn("var(--surface-card)", css)
        self.assertNotIn("var(--surface-card-alt)", css)
        self.assertNotIn("var(--bg-deep)", css)
        self.assertIn(".nearby-device-card.active-target", css)
        self.assertIn(".nearby-device-card.drag-target", css)
        self.assertIn("bottom: calc(76px + env(safe-area-inset-bottom, 0px));", css)

        index_html = (REPO_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        # #selectionActionBar must sit after </main> so backdrop-filter on .section-card doesn't trap fixed positioning
        self.assertGreater(index_html.index('id="selectionActionBar"'), index_html.index("</main>"))

        finder_html = (REPO_ROOT / "templates" / "finder.html").read_text(encoding="utf-8")
        self.assertIn("window.location.port", finder_html)
        self.assertNotIn("const PORT = 5000;", finder_html)


if __name__ == "__main__":
    unittest.main()
