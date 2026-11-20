"""Unit tests for core.batches.BatchStore."""

import os
import shutil
import tempfile
import unittest

from core.batches import BatchStore


class TestBatchStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nearbeam_batches_test_")
        self.store = BatchStore(max_batches=3)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_file(self, rel_path: str, data: bytes = b"hello") -> str:
        full = os.path.join(self.tmp, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        return full

    def test_single_and_multi_file_batches_with_duplicate_basenames(self):
        p1 = self._make_file("dir1/IMG_0001.jpg", b"jpg-one")
        p2 = self._make_file("dir2/IMG_0001.jpg", b"jpg-two-longer")

        summary = self.store.create_or_merge([p1, p2], source="Drop zone")
        self.assertIsNotNone(summary)
        self.assertEqual(summary["file_count"], 2)
        self.assertEqual(summary["total_bytes"], len(b"jpg-one") + len(b"jpg-two-longer"))
        self.assertEqual(summary["label"], "2 photos")
        self.assertIsNone(summary["single_item"])
        self.assertEqual(len(summary["preview_thumbs"]), 2)

        _, items = self.store.get_batch_items(summary["id"])
        rel_paths = [it["rel_path"] for it in items]
        self.assertEqual(len(set(rel_paths)), 2)
        self.assertIn("IMG_0001.jpg", rel_paths)
        self.assertIn("IMG_0001 (1).jpg", rel_paths)

        # Single file batch exposes single_item and uses filename as label
        p3 = self._make_file("report.pdf", b"pdf-bytes")
        s_single = self.store.create_or_merge([p3], source="Send button")
        self.assertEqual(s_single["label"], "report.pdf")
        self.assertIsNotNone(s_single["single_item"])
        self.assertEqual(s_single["single_item"]["name"], "report.pdf")

    def test_directory_expansion_and_empty_folder_ignored(self):
        empty_dir = os.path.join(self.tmp, "empty_dir")
        os.makedirs(empty_dir, exist_ok=True)
        self.assertIsNone(self.store.create_or_merge([empty_dir, os.path.join(self.tmp, "missing.bin")]))
        self.assertEqual(len(self.store.list_batches()), 0)

        self._make_file("album/a.png", b"111")
        self._make_file("album/sub/b.txt", b"2222")
        album_dir = os.path.join(self.tmp, "album")
        summary = self.store.create_or_merge([album_dir], source="Drop zone")
        self.assertEqual(summary["file_count"], 2)
        self.assertEqual(summary["label"], "album")
        _, items = self.store.get_batch_items(summary["id"])
        rels = {it["rel_path"].replace("\\", "/") for it in items}
        self.assertEqual(rels, {"album/a.png", "album/sub/b.txt"})

    def test_explorer_rapid_send_merges_within_window(self):
        p1 = self._make_file("f1.txt", b"aaa")
        p2 = self._make_file("f2.txt", b"bbbb")
        p3 = self._make_file("f3.txt", b"ccccc")

        b1 = self.store.create_or_merge([p1], source="Explorer", merge_window=2.0)
        b2 = self.store.create_or_merge([p2, p1], source="Explorer", merge_window=2.0)  # p1 duplicate ignored
        self.assertEqual(b1["id"], b2["id"])
        self.assertEqual(b2["file_count"], 2)
        self.assertEqual(len(self.store.list_batches()), 1)

        # Different source does not merge
        b3 = self.store.create_or_merge([p3], source="Drop zone", merge_window=0.0)
        self.assertNotEqual(b3["id"], b1["id"])
        self.assertEqual(len(self.store.list_batches()), 2)

    def test_max_batches_cap_and_delete_clear(self):
        ids = []
        for i in range(4):
            p = self._make_file(f"file_{i}.txt", b"x")
            ids.append(self.store.create_or_merge([p], source="Drop zone")["id"])
        # max_batches=3 -> oldest (ids[0]) evicted
        listed_ids = [b["id"] for b in self.store.list_batches()]
        self.assertEqual(len(listed_ids), 3)
        self.assertNotIn(ids[0], listed_ids)
        self.assertEqual(listed_ids[0], ids[3])  # newest first

        self.assertTrue(self.store.delete_batch(ids[3]))
        self.assertFalse(self.store.delete_batch("unknown"))
        self.assertEqual(self.store.clear(), 2)
        self.assertEqual(self.store.list_batches(), [])

    def test_collect_zip_files_selective_and_missing_disk_file(self):
        p1 = self._make_file("keep.txt", b"keep-data")
        p2 = self._make_file("vanish.txt", b"gone-soon")
        summary = self.store.create_or_merge([p1, p2], source="Drop zone")
        _, items = self.store.get_batch_items(summary["id"])
        id_by_name = {it["name"]: it["id"] for it in items}

        # Delete second file from disk
        os.remove(p2)
        label, files_list, total = self.store.collect_zip_files(summary["id"])
        self.assertEqual(len(files_list), 1)
        self.assertEqual(files_list[0][1], "keep.txt")
        self.assertEqual(total, len(b"keep-data"))

        # Selective item_ids with only the deleted file yields empty list
        _, empty_files, empty_total = self.store.collect_zip_files(summary["id"], [id_by_name["vanish.txt"]])
        self.assertEqual(empty_files, [])
        self.assertEqual(empty_total, 0)

    def test_symlink_escape_skipped_during_directory_walk(self):
        from unittest.mock import patch
        outside = self._make_file("secret.txt", b"top-secret")
        safe_dir = os.path.join(self.tmp, "shared")
        os.makedirs(safe_dir, exist_ok=True)
        self._make_file("shared/ok.txt", b"public")
        fake_link = self._make_file("shared/escape.txt", b"placeholder")

        orig_realpath = os.path.realpath
        real_fake_link = orig_realpath(fake_link)
        real_outside = orig_realpath(outside)

        def fake_realpath(p):
            resolved = orig_realpath(p)
            if os.path.normcase(resolved) == os.path.normcase(real_fake_link):
                return real_outside
            return resolved

        with patch("core.batches.os.path.realpath", side_effect=fake_realpath):
            summary = self.store.create_or_merge([safe_dir], source="Drop zone")
        _, items = self.store.get_batch_items(summary["id"])
        names = [it["name"] for it in items]
        self.assertIn("ok.txt", names)
        self.assertNotIn("escape.txt", names)


if __name__ == "__main__":
    unittest.main()
