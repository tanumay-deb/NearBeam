"""
Settings must live in one per-user file that installers and updates never touch,
and be carried over once from where older versions kept them.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core.config as config_mod
from core.config import ConfigManager


class TestConfigLocation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="landrop_cfgloc_"))
        self.appdata = self.tmp / "AppData"
        self.legacy = self.tmp / "install_dir" / "config.json"
        self.legacy.parent.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"APPDATA": str(self.appdata)})
        legacy = mock.patch.object(config_mod, "legacy_config_paths", return_value=[self.legacy])
        env.start()
        legacy.start()
        self.addCleanup(env.stop)
        self.addCleanup(legacy.stop)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_default_location_is_per_user_app_data(self):
        cfg = ConfigManager()
        self.assertEqual(cfg.config_path, self.appdata / "NearBeam" / "config.json")

    def test_settings_are_carried_over_once_from_old_location(self):
        self._write(self.legacy, {"safe_folders": [{"id": "a1", "name": "Video", "path": str(self.tmp)}], "transfer_history": [{"filename": "x.bin"}]})
        cfg = ConfigManager()
        self.assertEqual([f["name"] for f in cfg.get_safe_folders()], ["Video"])
        self.assertEqual(len(cfg.get_transfer_history()), 1)
        self.assertTrue(cfg.config_path.exists())
        self.assertTrue(self.legacy.exists(), "old file is kept as a backup")

    def test_existing_new_settings_win_over_old_file(self):
        self._write(self.appdata / "NearBeam" / "config.json", {"safe_folders": [{"id": "n1", "name": "New", "path": str(self.tmp)}]})
        self._write(self.legacy, {"safe_folders": [{"id": "o1", "name": "Old", "path": str(self.tmp)}]})
        cfg = ConfigManager()
        self.assertEqual([f["name"] for f in cfg.get_safe_folders()], ["New"])

    def test_first_save_creates_the_folder(self):
        cfg = ConfigManager()
        cfg.add_transfer_history({"filename": "y.bin"})
        saved = json.loads((self.appdata / "NearBeam" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["transfer_history"][0]["filename"], "y.bin")


if __name__ == "__main__":
    unittest.main()
