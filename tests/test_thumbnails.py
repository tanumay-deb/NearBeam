"""
/api/safelist/thumb: small cached JPEGs for browsing big shared folders over slow Wi-Fi.
"""

import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image

import core.server as srv_mod
from core.config import ConfigManager
from core.server import LandropServer


class TestThumbnails(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="landrop_thumb_")
        self.share = os.path.join(self.tmp, "share")
        os.makedirs(self.share)
        Image.new("RGB", (2400, 1600), (40, 120, 200)).save(os.path.join(self.share, "big.jpg"), "JPEG", quality=95)
        Image.new("RGBA", (300, 900), (0, 200, 0, 128)).save(os.path.join(self.share, "tall.png"))
        with open(os.path.join(self.share, "notes.txt"), "w") as f:
            f.write("hello")
        with open(os.path.join(self.share, "broken.jpg"), "wb") as f:
            f.write(b"not really a jpeg")
        self.cfg = ConfigManager(os.path.join(self.tmp, "cfg.json"))
        self.cfg.set_save_directory(os.path.join(self.tmp, "recv"))
        self.folder_id = self.cfg.add_safe_folder(self.share, "Share")["id"]
        self.orig_config = srv_mod.config
        srv_mod.config = self.cfg
        self.server = LandropServer(port=5996, enable_mesh=False)
        self.client = self.server.app.test_client()

    def tearDown(self):
        self.server.shutdown()
        srv_mod.config = self.orig_config
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _thumb(self, subpath):
        return self.client.get("/api/safelist/thumb", query_string={"folder_id": self.folder_id, "subpath": subpath})

    def test_photo_becomes_small_cacheable_jpeg(self):
        resp = self._thumb("big.jpg")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "image/jpeg")
        self.assertIn("max-age", resp.headers.get("Cache-Control", ""))
        img = Image.open(io.BytesIO(resp.data))
        self.assertLessEqual(max(img.size), 320)
        self.assertAlmostEqual(img.size[0] / img.size[1], 2400 / 1600, places=2)  # aspect ratio kept
        self.assertLess(len(resp.data), 60 * 1024)

    def test_transparent_png_converts(self):
        resp = self._thumb("tall.png")
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(max(Image.open(io.BytesIO(resp.data)).size), 320)

    def test_non_images_and_unreadable_images_are_404(self):
        self.assertEqual(self._thumb("notes.txt").status_code, 404)
        self.assertEqual(self._thumb("broken.jpg").status_code, 404)
        self.assertEqual(self._thumb("missing.jpg").status_code, 404)

    def test_path_traversal_is_refused(self):
        self.assertIn(self._thumb("../cfg.json").status_code, (403, 404))
        self.assertIn(self._thumb("..\\cfg.json").status_code, (403, 404))

    def test_video_and_pdf_expose_has_thumb_and_use_thumbnail_engine(self):
        mp4 = os.path.join(self.share, "demo.mp4")
        pdf = os.path.join(self.share, "doc.pdf")
        with open(mp4, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4\n%%EOF\n")

        browse = self.client.get("/api/safelist/browse", query_string={"folder_id": self.folder_id}).get_json()
        by_name = {it["name"]: it for it in browse["items"]}
        self.assertTrue(by_name["big.jpg"].get("has_thumb"))
        self.assertFalse(by_name["notes.txt"].get("has_thumb"))
        expected_rich = sys.platform == "win32"
        self.assertEqual(by_name["demo.mp4"].get("has_thumb"), expected_rich)
        self.assertEqual(by_name["doc.pdf"].get("has_thumb"), expected_rich)

        buf = io.BytesIO()
        Image.new("RGB", (160, 90), (10, 120, 200)).save(buf, "JPEG")
        fake_jpeg = buf.getvalue()

        if sys.platform == "win32":
            with mock.patch.object(srv_mod, "make_thumbnail", return_value=fake_jpeg):
                resp = self._thumb("demo.mp4")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.mimetype, "image/jpeg")
                self.assertEqual(resp.data, fake_jpeg)

                batch = self.server.batch_store.create_or_merge([mp4], source="Test")
                self.assertTrue(batch["preview_thumbs"])
                item_id = batch["preview_thumbs"][0]
                b_resp = self.client.get(f"/api/batches/{batch['id']}/thumb/{item_id}")
                self.assertEqual(b_resp.status_code, 200)
                self.assertEqual(b_resp.mimetype, "image/jpeg")
                self.assertEqual(b_resp.data, fake_jpeg)

    def test_empty_thumbnail_returns_404(self):
        with mock.patch.object(srv_mod, "make_thumbnail", return_value=b""):
            resp = self._thumb("big.jpg")
            self.assertEqual(resp.status_code, 404)


class TestNativeThumbnailsModule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nearbeam_thumbs_mod_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_is_thumbnailable_extensions(self):
        from core.thumbnails import is_thumbnailable

        for name in ("photo.jpg", "IMG.JPEG", "pic.png", "anim.gif", "hero.webp", "scan.bmp"):
            self.assertTrue(is_thumbnailable(name), name)

        for name in ("notes.txt", "archive.zip", "data.bin", "Makefile", ""):
            self.assertFalse(is_thumbnailable(name), name)

        for name in ("clip.mp4", "report.pdf", "IMG_0001.HEIC", "deck.pptx", "raw.dng"):
            expected = sys.platform == "win32"
            self.assertEqual(is_thumbnailable(name), expected, name)

    def test_pillow_fast_path_and_negative_cache(self):
        from core.thumbnails import make_thumbnail

        make_thumbnail.cache_clear()
        good = os.path.join(self.tmp, "good.jpg")
        Image.new("RGB", (1200, 800), (20, 140, 220)).save(good, "JPEG", quality=92)
        st = os.stat(good)
        data = make_thumbnail(good, st.st_mtime_ns, st.st_size)
        self.assertTrue(data)
        img = Image.open(io.BytesIO(data))
        self.assertLessEqual(max(img.size), 320)

        bad = os.path.join(self.tmp, "corrupt.jpg")
        with open(bad, "wb") as f:
            f.write(b"not-a-valid-image-payload")
        st_bad = os.stat(bad)
        self.assertEqual(make_thumbnail(bad, st_bad.st_mtime_ns, st_bad.st_size), b"")

    def test_bgra_to_jpeg_handles_alpha_and_resizing(self):
        from core.thumbnails import _bgra_to_jpeg

        # 640x480 semi-transparent red in BGRA order (B=0, G=0, R=255, A=128)
        raw_bgra = bytes([0, 0, 255, 128]) * (640 * 480)
        jpeg = _bgra_to_jpeg(raw_bgra, 640, 480)
        self.assertTrue(jpeg)
        img = Image.open(io.BytesIO(jpeg))
        self.assertEqual(img.format, "JPEG")
        self.assertLessEqual(max(img.size), 320)
        self.assertEqual(img.mode, "RGB")

    def test_windows_shell_timeout_returns_empty_without_hanging(self):
        from core.thumbnails import _WindowsShellThumbnailPool

        def slow_extractor(_path):
            time.sleep(2.0)
            return b"late"

        pool = _WindowsShellThumbnailPool(workers=1, extractor=slow_extractor, init_com=False)
        try:
            t0 = time.monotonic()
            out = pool.generate("dummy.mp4", timeout=0.1)
            elapsed = time.monotonic() - t0
            self.assertIsNone(out)
            self.assertLess(elapsed, 0.8)
        finally:
            pool.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows Shell COM thumbnails require Windows")
    def test_real_windows_shell_extractor_handles_real_image_and_unsupported_file(self):
        from core.thumbnails import _WindowsShellThumbnailPool

        jpg = os.path.join(self.tmp, "shell_test.jpg")
        Image.new("RGB", (600, 400), (220, 90, 40)).save(jpg, "JPEG", quality=90)
        txt = os.path.join(self.tmp, "plain.txt")
        with open(txt, "w", encoding="utf-8") as f:
            f.write("just plain text without embedded thumbnail")

        pool = _WindowsShellThumbnailPool(workers=1)
        try:
            jpg_bytes = pool.generate(jpg, timeout=5.0)
            self.assertTrue(jpg_bytes, "Windows Shell failed to thumbnail a real JPEG")
            img = Image.open(io.BytesIO(jpg_bytes))
            self.assertEqual(img.format, "JPEG")
            self.assertLessEqual(max(img.size), 320)

            txt_bytes = pool.generate(txt, timeout=5.0)
            self.assertEqual(txt_bytes, b"", "SIIGBF_THUMBNAILONLY must not return generic file icons")
        finally:
            pool.shutdown()

    def test_transient_timeout_not_permanently_cached(self):
        import core.thumbnails as thumbs_mod

        thumbs_mod.make_thumbnail.cache_clear()
        mp4 = os.path.join(self.tmp, "retry.mp4")
        with open(mp4, "wb") as f:
            f.write(b"fake-mp4")
        st = os.stat(mp4)
        fake_pool = mock.MagicMock()
        fake_pool.generate.side_effect = [None, b"JPEGDATA"]
        with mock.patch.object(thumbs_mod.sys, "platform", "win32"), mock.patch.object(
            thumbs_mod, "_get_shell_pool", return_value=fake_pool
        ):
            first = thumbs_mod.make_thumbnail(mp4, st.st_mtime_ns, st.st_size)
            second = thumbs_mod.make_thumbnail(mp4, st.st_mtime_ns, st.st_size)
            third = thumbs_mod.make_thumbnail(mp4, st.st_mtime_ns, st.st_size)
        self.assertEqual(first, b"")
        self.assertEqual(second, b"JPEGDATA")
        self.assertEqual(third, b"JPEGDATA")
        self.assertEqual(fake_pool.generate.call_count, 2)

    def test_extended_media_categories_for_m4v_heif_raw(self):
        from core.batches import _default_category

        self.assertEqual(srv_mod.get_file_type_category("movie.m4v"), "video")
        self.assertEqual(_default_category("movie.m4v"), "video")
        for img_name in ("a.heif", "b.avif", "c.tif", "d.tiff", "e.cr2", "f.nef", "g.arw", "h.dng"):
            self.assertEqual(srv_mod.get_file_type_category(img_name), "image", img_name)
            self.assertEqual(_default_category(img_name), "image", img_name)


if __name__ == "__main__":
    unittest.main()
