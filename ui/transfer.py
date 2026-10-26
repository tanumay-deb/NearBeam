"""
Outgoing desktop-to-peer file transfer with pause, resume and cancel.
Holds no Qt objects; the GUI wires the callbacks to signals.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from typing import Callable, Optional

CHUNK_SIZE = 16 * 1024 * 1024  # 16 MB streaming chunks


class OutgoingTransfer:
    def __init__(
        self,
        upload_url: str,
        target_name: str,
        file_path: str,
        on_progress: Optional[Callable[[int, int, float], None]] = None,
        on_done: Optional[Callable[[bool, str], None]] = None,
        chunk_size: int = CHUNK_SIZE,
    ):
        base = upload_url.rstrip("/")
        self.chunk_url = base if base.endswith("/chunk") else base + "/chunk"
        self.cancel_url = self.chunk_url[: -len("/chunk")] + "/cancel"
        self.target_name = target_name
        self.file_path = file_path
        self.filename = os.path.basename(file_path)
        self.total = os.path.getsize(file_path)
        self.chunk_size = chunk_size
        self.session_id = f"desk_{uuid.uuid4().hex[:12]}"
        self.id = self.session_id
        self.sent = 0
        self.on_progress = on_progress or (lambda sent, total, bps: None)
        self.on_done = on_done or (lambda ok, msg: None)
        self._resume = threading.Event()
        self._resume.set()
        self._cancelled = False
        self._thread: Optional[threading.Thread] = None

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def pause(self):
        self._resume.clear()

    def resume(self):
        self._resume.set()

    def cancel(self):
        self._cancelled = True
        self._resume.set()  # wake a paused loop so it can exit

    def join(self, timeout: Optional[float] = None):
        if self._thread:
            self._thread.join(timeout)

    def run(self):
        total_chunks = max(1, (self.total + self.chunk_size - 1) // self.chunk_size)
        t0 = time.monotonic()
        sent_at_t0 = 0
        try:
            with open(self.file_path, "rb", buffering=self.chunk_size) as f:
                for idx in range(total_chunks):
                    # Pause/cancel take effect between chunks.
                    was_paused = self.paused
                    self._resume.wait()
                    if self._cancelled:
                        self._abort_remote()
                        self.on_done(False, "Cancelled")
                        return
                    if was_paused:
                        # Don't count paused time in the speed.
                        t0, sent_at_t0 = time.monotonic(), self.sent
                    offset = idx * self.chunk_size
                    f.seek(offset)
                    data = f.read(self.chunk_size)
                    req = urllib.request.Request(
                        self.chunk_url,
                        data=data,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "X-Session-Id": self.session_id,
                            "X-Filename": urllib.parse.quote(self.filename),
                            "X-Chunk-Index": str(idx),
                            "X-Total-Chunks": str(total_chunks),
                            "X-Chunk-Offset": str(offset),
                            "X-File-Size": str(self.total),
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=60.0) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP status {resp.status}")
                    self.sent = offset + len(data)
                    elapsed = max(time.monotonic() - t0, 1e-6)
                    self.on_progress(self.sent, self.total, (self.sent - sent_at_t0) / elapsed)
            self.on_done(True, "")
        except Exception as e:
            self.on_done(False, str(e))

    def _abort_remote(self):
        """Ask the receiver to delete its partial temp file."""
        try:
            req = urllib.request.Request(
                self.cancel_url,
                data=json.dumps({"session_id": self.session_id}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10.0).close()
        except Exception:
            pass
