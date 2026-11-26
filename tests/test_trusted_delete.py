"""
TDD test suite for Trusted Device File Deletion:
- core/recycle.py Windows Recycle Bin (SHFileOperationW, zero pip deps, never hard-deletes)
- core/config.py SHA-256 hashed trusted device tokens + revoke persistence
- core/server.py one-time 6-digit pairing (TTL, lockout, single-use, HttpOnly SameSite=Lax cookie)
- POST /api/safelist/delete (files-only inside Safe List, bulk delete, single-file auto-prune,
  traversal/symlink/directory/ADS/CSRF/untrusted blocking)
- Desktop SettingsPage Trusted Devices card + Web UI pairing/delete controls
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import core.server as srv_mod
from core.config import ConfigManager
from core.server import NearBeamServer


class TestRecycleBinModule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nb_recycle_")
        self.sample = os.path.join(self.tmp, "photo.jpg")
        with open(self.sample, "wb") as f:
            f.write(b"jpeg-bytes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_send_to_recycle_bin_uses_allowundo_and_double_nul(self):
        from core import recycle

        captured = {}

        def fake_sh_op(path_str: str, flags: int):
            captured["path"] = path_str
            captured["flags"] = flags
            # Simulate Windows moving file to Recycle Bin by removing it in temp test dir
            os.remove(path_str)
            return (0, False)

        with mock.patch.object(recycle.sys, "platform", "win32"), mock.patch.object(
            recycle, "_sh_file_operation", side_effect=fake_sh_op
        ):
            recycle.send_to_recycle_bin(self.sample)

        self.assertEqual(captured["path"], os.path.abspath(self.sample))
        self.assertTrue(captured["flags"] & recycle.FOF_ALLOWUNDO)
        self.assertTrue(captured["flags"] & recycle.FOF_NOCONFIRMATION)
        self.assertTrue(captured["flags"] & recycle.FOF_NOERRORUI)
        self.assertTrue(captured["flags"] & recycle.FOF_SILENT)
        self.assertFalse(os.path.exists(self.sample))

    def test_send_to_recycle_bin_never_hard_deletes_on_failure_or_abort(self):
        from core import recycle

        with mock.patch.object(recycle.sys, "platform", "win32"), mock.patch.object(
            recycle, "_sh_file_operation", return_value=(1223, True)
        ):
            with self.assertRaises(recycle.RecycleError):
                recycle.send_to_recycle_bin(self.sample)
        self.assertTrue(os.path.exists(self.sample))

        with mock.patch.object(recycle.sys, "platform", "win32"), mock.patch.object(
            recycle, "_sh_file_operation", return_value=(0, False)
        ):
            # File still exists on disk even though rc == 0 -> must error
            with self.assertRaises(recycle.RecycleError):
                recycle.send_to_recycle_bin(self.sample)
        self.assertTrue(os.path.exists(self.sample))

    def test_send_to_recycle_bin_refuses_dir_unc_and_missing(self):
        from core import recycle

        with self.assertRaises(PermissionError):
            recycle.send_to_recycle_bin(self.tmp)
        with self.assertRaises(recycle.RecycleError):
            recycle.send_to_recycle_bin(r"\\fileserver\share\doc.txt")
        with self.assertRaises(FileNotFoundError):
            recycle.send_to_recycle_bin(os.path.join(self.tmp, "does_not_exist.txt"))


class TestTrustedDevicesConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nb_trust_cfg_")
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.cfg = ConfigManager(self.cfg_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_token_stored_as_sha256_hash_and_survives_reload(self):
        raw_token = "super_secret_random_token_1234567890abcdef"
        dev = self.cfg.add_trusted_device("Tanumay's iPhone", raw_token, ip="192.168.1.42")
        self.assertNotIn("token_hash", dev)
        self.assertEqual(dev["name"], "Tanumay's iPhone")
        self.assertEqual(dev["ip"], "192.168.1.42")

        # Raw token must NEVER appear on disk
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            raw_disk = f.read()
        self.assertNotIn(raw_token, raw_disk)
        disk_json = json.loads(raw_disk)
        self.assertEqual(len(disk_json["trusted_devices"]), 1)
        self.assertEqual(len(disk_json["trusted_devices"][0]["token_hash"]), 64)

        # Public listing never leaks token_hash
        listed = self.cfg.get_trusted_devices()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token_hash", listed[0])

        # Reloaded config verifies valid token and rejects wrong token
        reloaded = ConfigManager(self.cfg_path)
        self.assertIsNotNone(reloaded.verify_trusted_token(raw_token))
        self.assertIsNone(reloaded.verify_trusted_token("wrong_token_1234567890abcdef"))

        # Revoke works and persists
        self.assertTrue(reloaded.revoke_trusted_device(dev["id"]))
        self.assertIsNone(reloaded.verify_trusted_token(raw_token))
        self.assertEqual(ConfigManager(self.cfg_path).get_trusted_devices(), [])


class TestTrustedPairingAndSafeDeleteAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nb_trust_api_")
        self.recv_dir = os.path.join(self.tmp, "recv")
        self.share_dir = os.path.join(self.tmp, "shared")
        self.outside_file = os.path.join(self.tmp, "outside_secret.txt")
        self.single_shared = os.path.join(self.tmp, "standalone.pdf")
        os.makedirs(self.recv_dir, exist_ok=True)
        os.makedirs(os.path.join(self.share_dir, "nested"), exist_ok=True)

        for p, content in (
            (os.path.join(self.share_dir, "a.txt"), b"alpha"),
            (os.path.join(self.share_dir, "b.txt"), b"beta"),
            (os.path.join(self.share_dir, "nested", "c.txt"), b"gamma"),
            (self.outside_file, b"top-secret"),
            (self.single_shared, b"%PDF-1.4"),
            (os.path.join(self.recv_dir, "received.txt"), b"from-phone"),
        ):
            with open(p, "wb") as f:
                f.write(content)

        self.cfg = ConfigManager(os.path.join(self.tmp, "cfg.json"))
        self.cfg.set_save_directory(self.recv_dir)
        self.folder_id = self.cfg.add_safe_folder(self.share_dir)["id"]
        self.single_id = self.cfg.add_safe_folder(self.single_shared)["id"]

        self.orig_cfg = srv_mod.config
        srv_mod.config = self.cfg
        self.server = NearBeamServer(port=0, enable_mesh=False)
        self.client = self.server.app.test_client()
        self.recycled_paths = []

    def tearDown(self):
        self.server.shutdown()
        srv_mod.config = self.orig_cfg
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_recycle(self, path: str):
        self.recycled_paths.append(path)
        os.remove(path)

    def _pair_client(self, client=None, name="Test Phone"):
        client = client or self.client
        info = self.server.start_device_pairing()
        code = info["code"]
        res = client.post("/api/trust/pair", json={"code": f"{code[:3]} {code[3:]}", "device_name": name})
        self.assertEqual(res.status_code, 200, res.data)
        return res

    def test_pairing_lifecycle_lockout_expiry_and_cookie_attributes(self):
        # Initially untrusted, no active pairing
        st = self.client.get("/api/trust/status")
        self.assertEqual(st.status_code, 200)
        self.assertEqual(st.get_json()["trusted"], False)
        self.assertEqual(st.get_json()["pairing_active"], False)

        # Pair without active code -> 400
        r_no = self.client.post("/api/trust/pair", json={"code": "123456"})
        self.assertEqual(r_no.status_code, 400)

        # Start pairing; status never leaks the code itself
        pair = self.server.start_device_pairing()
        self.assertEqual(len(pair["code"]), 6)
        self.assertTrue(pair["code"].isdigit())
        st2 = self.client.get("/api/trust/status").get_json()
        self.assertTrue(st2["pairing_active"])
        self.assertNotIn("code", st2)

        # 5 wrong attempts lock out and invalidate active pairing code
        wrong_code = "000000" if pair["code"] != "000000" else "999999"
        for i in range(4):
            r_bad = self.client.post("/api/trust/pair", json={"code": wrong_code})
            self.assertEqual(r_bad.status_code, 403)
        r_lock = self.client.post("/api/trust/pair", json={"code": wrong_code})
        self.assertEqual(r_lock.status_code, 429)
        self.assertIsNone(self.server.get_active_pairing())

        # Expired code rejected
        pair2 = self.server.start_device_pairing()
        self.server._active_pair_code["expires_at"] = time.monotonic() - 1.0
        r_exp = self.client.post("/api/trust/pair", json={"code": pair2["code"]})
        self.assertEqual(r_exp.status_code, 400)

        # Successful pairing sets HttpOnly SameSite=Lax cookie and consumes code (single-use)
        pair3 = self.server.start_device_pairing()
        r_ok = self.client.post("/api/trust/pair", json={"code": pair3["code"], "device_name": "Pixel 9"})
        self.assertEqual(r_ok.status_code, 200)
        cookie_hdr = r_ok.headers.get("Set-Cookie", "")
        self.assertIn("nearbeam_trust=", cookie_hdr)
        self.assertIn("HttpOnly", cookie_hdr)
        self.assertIn("SameSite=Lax", cookie_hdr)

        # Reusing the same 6-digit code fails
        other_client = self.server.app.test_client()
        r_reuse = other_client.post("/api/trust/pair", json={"code": pair3["code"]})
        self.assertEqual(r_reuse.status_code, 400)

        # Status now trusted
        st3 = self.client.get("/api/trust/status").get_json()
        self.assertTrue(st3["trusted"])
        self.assertEqual(st3["device"]["name"], "Pixel 9")

        # Unpair clears cookie and revokes server-side
        r_unpair = self.client.post("/api/trust/unpair")
        self.assertEqual(r_unpair.status_code, 200)
        self.assertFalse(self.client.get("/api/trust/status").get_json()["trusted"])
        self.assertEqual(self.cfg.get_trusted_devices(), [])

    def test_safelist_delete_requires_trust_and_recycles_safely(self):
        target_a = os.path.join(self.share_dir, "a.txt")
        target_b = os.path.join(self.share_dir, "b.txt")
        target_c = os.path.join(self.share_dir, "nested", "c.txt")

        with mock.patch("core.server.send_to_recycle_bin", side_effect=self._fake_recycle):
            # Untrusted client -> 403, file intact
            r_unauth = self.client.post("/api/safelist/delete", json={"folder_id": self.folder_id, "subpath": "a.txt"})
            self.assertEqual(r_unauth.status_code, 403)
            self.assertTrue(os.path.exists(target_a))

            # Pair client
            self._pair_client()

            # Cross-origin non-LAN POST rejected by CSRF guard even when paired
            r_csrf = self.client.post(
                "/api/safelist/delete",
                json={"folder_id": self.folder_id, "subpath": "a.txt"},
                headers={"Origin": "https://evil.example"},
            )
            self.assertEqual(r_csrf.status_code, 403)
            self.assertNotIn("Access-Control-Allow-Origin", r_csrf.headers)
            self.assertTrue(os.path.exists(target_a))

            # Single file delete succeeds via Recycle Bin
            r_del = self.client.post("/api/safelist/delete", json={"folder_id": self.folder_id, "subpath": "a.txt"})
            self.assertEqual(r_del.status_code, 200, r_del.data)
            self.assertFalse(os.path.exists(target_a))
            self.assertIn(os.path.realpath(target_a), self.recycled_paths)

            # Bulk delete multiple files including nested file
            r_bulk = self.client.post(
                "/api/safelist/delete",
                json={"folder_id": self.folder_id, "items": ["b.txt", "nested/c.txt"]},
            )
            self.assertEqual(r_bulk.status_code, 200, r_bulk.data)
            bulk_data = r_bulk.get_json()
            self.assertTrue(bulk_data["success"])
            self.assertEqual(len(bulk_data["deleted"]), 2)
            self.assertFalse(os.path.exists(target_b))
            self.assertFalse(os.path.exists(target_c))

            # Single shared file entry deletes file AND auto-prunes Safe List entry
            r_single = self.client.post("/api/safelist/delete", json={"folder_id": self.single_id, "subpath": ""})
            self.assertEqual(r_single.status_code, 200, r_single.data)
            self.assertTrue(r_single.get_json()["pruned_folder"])
            self.assertFalse(os.path.exists(self.single_shared))
            self.assertIsNone(self.cfg.get_safe_folder_by_id(self.single_id))

            # PC revoking device immediately blocks further deletes
            dev_id = self.cfg.get_trusted_devices()[0]["id"]
            self.assertTrue(self.server.revoke_trusted_device(dev_id))
            extra = os.path.join(self.share_dir, "keep.txt")
            with open(extra, "wb") as f:
                f.write(b"keep")
            r_revoked = self.client.post("/api/safelist/delete", json={"folder_id": self.folder_id, "subpath": "keep.txt"})
            self.assertEqual(r_revoked.status_code, 403)
            self.assertTrue(os.path.exists(extra))

    def test_safelist_delete_blocks_folders_traversal_ads_and_recycle_errors(self):
        from core.recycle import RecycleError

        self._pair_client()
        keep = os.path.join(self.share_dir, "a.txt")

        with mock.patch("core.server.send_to_recycle_bin", side_effect=self._fake_recycle):
            # Never delete root shared folder
            r_root = self.client.post("/api/safelist/delete", json={"folder_id": self.folder_id, "subpath": ""})
            self.assertIn(r_root.status_code, (400, 403))
            self.assertTrue(os.path.isdir(self.share_dir))

            # Never delete subfolder
            r_dir = self.client.post("/api/safelist/delete", json={"folder_id": self.folder_id, "subpath": "nested"})
            self.assertEqual(r_dir.status_code, 403)
            self.assertTrue(os.path.isdir(os.path.join(self.share_dir, "nested")))

            # Never allow traversal or Windows ADS colon syntax
            for bad in ("../outside_secret.txt", "nested/../../outside_secret.txt", "a.txt:hidden"):
                r_bad = self.client.post("/api/safelist/delete", json={"folder_id": self.folder_id, "subpath": bad})
                self.assertEqual(r_bad.status_code, 403, (bad, r_bad.data))
            self.assertTrue(os.path.exists(self.outside_file))

        # RecycleError returns 500 and preserves file
        with mock.patch("core.server.send_to_recycle_bin", side_effect=RecycleError("Recycle Bin full")):
            r_err = self.client.post("/api/safelist/delete", json={"folder_id": self.folder_id, "subpath": "a.txt"})
            self.assertEqual(r_err.status_code, 500)
            self.assertTrue(os.path.exists(keep))

    def test_web_ui_markup_and_guest_drop_exclusion(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        for dom_id in ("btnTrustNav", "trustModal", "deleteConfirmModal", "btnModalDelete", "btnDeleteSelected"):
            self.assertIn(f'id="{dom_id}"', html)

        # Guest Drop template must NOT expose trust or delete controls
        drop_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates", "drop.html")
        with open(drop_path, "r", encoding="utf-8") as f:
            drop_html = f.read()
        self.assertNotIn("deleteConfirmModal", drop_html)
        self.assertNotIn("/api/safelist/delete", drop_html)

        app_js = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "js", "app.js")
        with open(app_js, "r", encoding="utf-8") as f:
            js = f.read()
        self.assertIn("/api/trust/status", js)
        self.assertIn("/api/trust/pair", js)
        self.assertIn("/api/safelist/delete", js)


class TestDesktopSettingsTrustedDevicesGui(unittest.TestCase):
    def test_settings_page_trusted_devices_widgets(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from ui.pages import SettingsPage

        app = QApplication.instance() or QApplication([])
        page = SettingsPage(
            save_dir=tempfile.gettempdir(),
            autosave=True,
            clipboard=True,
            shell_menu=False,
            autostart=False,
            organize_mode="off",
        )
        self.assertTrue(hasattr(page, "btn_pair_device"))
        self.assertTrue(hasattr(page, "btn_revoke_all_trusted"))
        self.assertTrue(hasattr(page, "lbl_pair_code"))

        # Pairing banner shows formatted 6-digit code
        page.set_pairing_state("482910", 115)
        self.assertFalse(page.pair_box.isHidden())
        self.assertIn("482", page.lbl_pair_code.text())
        self.assertIn("910", page.lbl_pair_code.text())

        # Cancel hides banner
        page.set_pairing_state(None, 0)
        self.assertTrue(page.pair_box.isHidden())

        # Populating paired devices shows Revoke All and wires per-device revoke
        revoked = []
        page.set_trusted_devices(
            [{"id": "dev1", "name": "iPhone 16", "ip": "192.168.1.50", "paired_at": "2026-11-26 16:00"}],
            on_revoke=revoked.append,
        )
        self.assertFalse(page.btn_revoke_all_trusted.isHidden())
        page.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
