"""Offscreen PySide6 GUI tests for bulk Shared Folders and Home Send Batches."""

import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication, QTableWidget

from core.config import ConfigManager


class TestGuiFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nearbeam_gui_test_")
        self.cfg = ConfigManager(os.path.join(self.tmp, "cfg.json"))
        self.cfg.data["port"] = 5979
        self.cfg.set_save_directory(os.path.join(self.tmp, "save"))

        import core.config as cfg_mod
        import core.server as srv_mod
        import gui as gui_mod
        self._orig = (cfg_mod.config, srv_mod.config, gui_mod.config)
        cfg_mod.config = srv_mod.config = gui_mod.config = self.cfg

        self.win = gui_mod.MainWindow()

    def tearDown(self):
        if getattr(self.win, "server_thread", None):
            self.win.server_thread.shutdown()
        if getattr(self.win, "server_instance", None):
            self.win.server_instance.shutdown()
        if getattr(self.win, "nearby_timer", None):
            self.win.nearby_timer.stop()
        if getattr(self.win, "events_timer", None):
            self.win.events_timer.stop()
        self.win.deleteLater()
        import core.config as cfg_mod
        import core.server as srv_mod
        import gui as gui_mod
        cfg_mod.config, srv_mod.config, gui_mod.config = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shared_folders_bulk_add_select_and_remove(self):
        self.assertEqual(self.win.safe_table.selectionMode(), QTableWidget.ExtendedSelection)
        self.assertTrue(hasattr(self.win.shared_page, "btn_add_files"))
        self.assertTrue(hasattr(self.win.shared_page, "btn_select_all"))
        self.assertTrue(hasattr(self.win.shared_page, "btn_clear_all"))

        f1 = os.path.join(self.tmp, "one.txt")
        f2 = os.path.join(self.tmp, "two.txt")
        d1 = os.path.join(self.tmp, "dir1")
        os.makedirs(d1, exist_ok=True)
        for p in (f1, f2):
            with open(p, "w") as fh:
                fh.write("data")

        self.win._add_safe_paths([f1, f2, d1])
        self.assertEqual(self.win.safe_table.rowCount(), 3)

        # Select rows 0 and 2 and remove in one bulk operation
        sel = self.win.safe_table.selectionModel()
        sel.clearSelection()
        sel.select(
            self.win.safe_table.model().index(0, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
        sel.select(
            self.win.safe_table.model().index(2, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
        self.win._remove_selected_safe_rows(confirm=False)
        self.assertEqual(self.win.safe_table.rowCount(), 1)

        # Clear all
        self.win._clear_safe_folders(confirm=False)
        self.assertEqual(self.win.safe_table.rowCount(), 0)

    def test_send_to_phone_creates_batch_not_shared_folder(self):
        photos = []
        for i in range(5):
            p = os.path.join(self.tmp, f"img_{i}.jpg")
            with open(p, "wb") as fh:
                fh.write(b"fakejpg")
            photos.append(p)

        self.win._handle_shared_paths(photos, source="Drop zone", send_to_peers=False)

        # Permanent Shared folders MUST stay empty
        self.assertEqual(len(self.cfg.get_safe_folders()), 0)
        self.assertEqual(self.win.safe_table.rowCount(), 0)

        # Active Send Batch created & visible on HomePage
        batches = self.win.server_instance.batch_store.list_batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["file_count"], 5)
        self.assertFalse(self.win.home.batches_section.isHidden())

        # Empty folder drop creates no ghost batch
        empty_dir = os.path.join(self.tmp, "empty")
        os.makedirs(empty_dir, exist_ok=True)
        self.win._handle_shared_paths([empty_dir], source="Drop zone", send_to_peers=False)
        self.assertEqual(len(self.win.server_instance.batch_store.list_batches()), 1)

        # Clear all batches hides Home section
        self.win._clear_all_batches()
        self.assertEqual(len(self.win.server_instance.batch_store.list_batches()), 0)
        self.assertTrue(self.win.home.batches_section.isHidden())


if __name__ == "__main__":
    unittest.main()
