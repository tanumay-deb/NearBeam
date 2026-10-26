"""
/api/safelist/thumb: small cached JPEGs for browsing big shared folders over slow Wi-Fi.
"""

import io
import os
import shutil
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
