"""
One NearBeam per user: a second launch hands its request to the running window and exits.
"""

import os
import subprocess
import sys
import time
import unittest
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication

import gui


class TestSingleInstance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, until, seconds=3.0):
        end = time.time() + seconds
        while time.time() < end and not until():
            self.app.processEvents()
            time.sleep(0.02)

    def test_no_running_instance_means_start_normally(self):
        self.assertFalse(gui.hand_off_to_running_instance(f"NearBeam-test-{uuid.uuid4().hex}", {"show": True}))

    def _second_launch(self, key, message):
        """A real second process, like clicking the Start-menu icon while NearBeam runs."""
        code = (
            "import json, sys; import gui; "
            f"print(gui.hand_off_to_running_instance({key!r}, {message!r}))"
        )
        return subprocess.Popen(
            [sys.executable, "-c", code], cwd=REPO_ROOT,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_second_launch_reaches_first_and_carries_paths(self):
        key = f"NearBeam-test-{uuid.uuid4().hex}"
        received = []
        server = gui.listen_for_other_instances(key, received.append)
        try:
            proc = self._second_launch(key, {"show": True, "paths": ["C:\\x\\photo.jpg"]})
            self._pump(lambda: received or proc.poll() is not None, seconds=30)
            self._pump(lambda: received, seconds=2)
            out, err = proc.communicate(timeout=30)
            self.assertEqual(out.strip().splitlines()[-1], "True", err)
            self.assertEqual(received, [{"show": True, "paths": ["C:\\x\\photo.jpg"]}])

            # A later autostart (--minimized) launch is absorbed too, without asking to show
            proc = self._second_launch(key, {"show": False, "paths": []})
            self._pump(lambda: len(received) == 2, seconds=30)
            proc.communicate(timeout=30)
            self.assertEqual(received[1], {"show": False, "paths": []})
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
