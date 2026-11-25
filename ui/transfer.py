"""
Outgoing desktop-to-peer file transfer with HTTP/1.1 keep-alive,
order-independent parallel multi-stream uploads for large files,
and pause, resume and cancel.
Holds no Qt objects; the GUI wires the callbacks to signals.
"""

from collections import deque
import http.client
import json
import os
import selectors
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid
from typing import Callable, Optional

CHUNK_SIZE = 4 * 1024 * 1024  # pause waits for a chunk boundary on small/sequential transfers
PARALLEL_CHUNK_SIZE = 8 * 1024 * 1024
PARALLEL_THRESHOLD = 16 * 1024 * 1024
DEFAULT_MAX_STREAMS = 4
PIECE_SIZE = 256 * 1024  # cancel is checked between pieces, so it works mid-chunk

_RETRYABLE_CONN_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.CannotSendRequest,
    http.client.BadStatusLine,
    http.client.ResponseNotReady,
    http.client.IncompleteRead,
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)


class _Cancelled(Exception):
    pass


class OutgoingTransfer:
    def __init__(
        self,
        upload_url: str,
        target_name: str,
        file_path: str,
        on_progress: Optional[Callable[[int, int, float], None]] = None,
        on_done: Optional[Callable[[bool, str], None]] = None,
        chunk_size: Optional[int] = None,
        max_streams: int = DEFAULT_MAX_STREAMS,
        parallel_threshold: int = PARALLEL_THRESHOLD,
        device_name: Optional[str] = None,
    ):
        base = upload_url.rstrip("/")
        self.chunk_url = base if base.endswith("/chunk") else base + "/chunk"
        self.cancel_url = self.chunk_url[: -len("/chunk")] + "/cancel"
        parsed = urllib.parse.urlsplit(self.chunk_url)
        self._scheme = (parsed.scheme or "http").lower()
        self._host = urllib.parse.unquote(parsed.hostname or "127.0.0.1")
        self._port = parsed.port or (443 if self._scheme == "https" else 80)
        self._chunk_path = parsed.path or "/api/upload/chunk"
        if parsed.query:
            self._chunk_path = f"{self._chunk_path}?{parsed.query}"

        self.target_name = target_name
        self.file_path = file_path
        self.filename = os.path.basename(file_path)
        self.total = os.path.getsize(file_path)
        self.parallel_threshold = max(0, int(parallel_threshold))
        self.max_streams = max(1, int(max_streams))
        if chunk_size is None:
            self.chunk_size = PARALLEL_CHUNK_SIZE if self.total >= self.parallel_threshold else CHUNK_SIZE
        else:
            self.chunk_size = max(1, int(chunk_size))
        self.device_name = device_name if device_name is not None else socket.gethostname()

        self.session_id = f"desk_{uuid.uuid4().hex[:12]}"
        self.id = self.session_id
        self.sent = 0
        self.on_progress = on_progress or (lambda sent, total, bps: None)
        self.on_done = on_done or (lambda ok, msg: None)
        self._resume = threading.Event()
        self._resume.set()
        self._cancelled = False
        self._started = False
        self._remote_started = False
        self._done_lock = threading.Lock()
        self._done_emitted = False
        self._done_event = threading.Event()
        self._progress_lock = threading.Lock()
        self._pause_lock = threading.Lock()
        self._paused_since: Optional[float] = None
        self._paused_total = 0.0
        self._in_flight = 0
        self._conns_lock = threading.Lock()
        self._active_conns: set[http.client.HTTPConnection] = set()
        self._chunk_bytes: dict[int, int] = {}
        self._thread: Optional[threading.Thread] = None

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def pause(self):
        with self._pause_lock:
            self._resume.clear()
            # Only start accumulating paused time once no chunk is actively transmitting.
            if self._in_flight == 0 and self._paused_since is None:
                self._paused_since = time.monotonic()

    def resume(self):
        with self._pause_lock:
            if self._paused_since is not None:
                self._paused_total += max(0.0, time.monotonic() - self._paused_since)
                self._paused_since = None
            self._resume.set()

    def cancel(self):
        self._cancelled = True
        self.resume()  # wake paused workers so they exit immediately
        # Immediately interrupt any blocked socket send/getresponse instead of waiting 60s.
        with self._conns_lock:
            conns = list(self._active_conns)
        for conn in conns:
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

    def _emit_done(self, ok: bool, msg: str):
        with self._done_lock:
            if self._done_emitted:
                return
            self._done_emitted = True
        self.on_done(ok, msg)

    def join(self, timeout: Optional[float] = None):
        if self._thread:
            self._thread.join(timeout)
        elif self._started:
            self._done_event.wait(timeout)

    def _active_elapsed(self, t0: float) -> float:
        with self._pause_lock:
            paused = self._paused_total
            if self._paused_since is not None:
                paused += max(0.0, time.monotonic() - self._paused_since)
        return max((time.monotonic() - t0) - paused, 1e-6)

    def _open_connection(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=60.0)
        conn.connect()
        if conn.sock is not None:
            try:
                conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
            except OSError:
                pass
        with self._conns_lock:
            self._active_conns.add(conn)
        if self._cancelled:
            with self._conns_lock:
                self._active_conns.discard(conn)
            try:
                conn.close()
            except Exception:
                pass
            raise _Cancelled()
        return conn

    def _close_connection(self, conn_holder: list):
        conn = conn_holder[0]
        conn_holder[0] = None
        if conn is not None:
            with self._conns_lock:
                self._active_conns.discard(conn)
            try:
                conn.close()
            except Exception:
                pass

    def _post_chunk(
        self,
        conn_holder: list,
        idx: int,
        total_chunks: int,
        offset: int,
        data: bytes,
        parallel: bool,
    ):
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
            "Connection": "keep-alive",
            "X-Session-Id": self.session_id,
            "X-Filename": urllib.parse.quote(self.filename),
            "X-Chunk-Index": str(idx),
            "X-Total-Chunks": str(total_chunks),
            "X-Chunk-Offset": str(offset),
            "X-File-Size": str(self.total),
        }
        if parallel:
            headers["X-Parallel"] = "1"
        if self.device_name:
            headers["X-Device-Name"] = urllib.parse.quote(self.device_name)

        for attempt in range(2):
            if self._cancelled:
                raise _Cancelled()
            reused = conn_holder[0] is not None
            if not reused:
                conn_holder[0] = self._open_connection()
            conn = conn_holder[0]
            try:
                conn.putrequest("POST", self._chunk_path)
                for k, v in headers.items():
                    conn.putheader(k, v)
                conn.endheaders()
                self._remote_started = True
                for piece in self._pieces(data):
                    conn.send(piece)
                # Poll socket readability in 0.25s slices before getresponse() so cancel()
                # aborts a silent/blackholed peer in <= 0.25s instead of blocking 60s.
                if conn.sock is not None:
                    sel = selectors.DefaultSelector()
                    try:
                        sel.register(conn.sock, selectors.EVENT_READ)
                        resp_deadline = time.monotonic() + 60.0
                        while time.monotonic() < resp_deadline:
                            if self._cancelled:
                                raise _Cancelled()
                            if sel.select(timeout=0.25):
                                break
                        else:
                            raise TimeoutError("Timed out waiting for peer response")
                    finally:
                        sel.close()
                resp = conn.getresponse()
                body = resp.read()
                status = resp.status
                if resp.will_close:
                    self._close_connection(conn_holder)
                if status != 200:
                    if self._cancelled:
                        raise _Cancelled()
                    detail = body.decode("utf-8", "ignore")[:160].strip()
                    raise RuntimeError(f"HTTP status {status}" + (f": {detail}" if detail else ""))
                return
            except _Cancelled:
                self._close_connection(conn_holder)
                raise
            except _RETRYABLE_CONN_ERRORS:
                self._close_connection(conn_holder)
                if self._cancelled:
                    raise _Cancelled()
                if reused and attempt == 0:
                    continue
                raise
            except Exception:
                self._close_connection(conn_holder)
                raise

    def _record_chunk_done(self, idx: int, size: int, t0: float):
        with self._progress_lock:
            if self._cancelled:
                raise _Cancelled()
            self._chunk_bytes[idx] = size
            total_sent = sum(self._chunk_bytes.values())
            if total_sent > self.sent:
                self.sent = total_sent
            current_sent = self.sent
            bps = current_sent / self._active_elapsed(t0)
            self.on_progress(current_sent, self.total, bps)

    def _begin_in_flight(self):
        with self._pause_lock:
            self._in_flight += 1

    def _end_in_flight(self):
        with self._pause_lock:
            self._in_flight = max(0, self._in_flight - 1)
            if self._in_flight == 0 and not self._resume.is_set() and self._paused_since is None:
                self._paused_since = time.monotonic()

    def run(self):
        self._started = True
        try:
            if self._cancelled:
                self._emit_done(False, "Cancelled")
                return
            total_chunks = max(1, (self.total + self.chunk_size - 1) // self.chunk_size)
            use_parallel = (
                self.total >= self.parallel_threshold
                and total_chunks > 1
                and self.max_streams > 1
            )
            t0 = time.monotonic()
            with self._pause_lock:
                self._paused_total = 0.0
                self._in_flight = 0
                self._paused_since = None if self._resume.is_set() else t0

            try:
                if use_parallel:
                    self._run_parallel(total_chunks, t0)
                else:
                    self._run_sequential(total_chunks, t0)

                if self._cancelled:
                    if self._remote_started:
                        self._abort_remote()
                    self._emit_done(False, "Cancelled")
                    return
                self._emit_done(True, "")
            except Exception as e:
                if self._cancelled or isinstance(e, _Cancelled):
                    if self._remote_started:
                        self._abort_remote()
                    self._emit_done(False, "Cancelled")
                else:
                    if self._remote_started:
                        self._abort_remote(timeout=2.0)
                    self._emit_done(False, str(e))
        finally:
            self._done_event.set()

    def _run_sequential(self, total_chunks: int, t0: float):
        conn_holder = [None]
        try:
            with open(self.file_path, "rb", buffering=self.chunk_size) as f:
                for idx in range(total_chunks):
                    while not self._resume.wait(0.2):
                        if self._cancelled:
                            raise _Cancelled()
                    if self._cancelled:
                        raise _Cancelled()
                    offset = idx * self.chunk_size
                    f.seek(offset)
                    data = f.read(self.chunk_size)
                    self._begin_in_flight()
                    try:
                        self._post_chunk(conn_holder, idx, total_chunks, offset, data, parallel=False)
                    finally:
                        self._end_in_flight()
                    if self._cancelled:
                        raise _Cancelled()
                    self._record_chunk_done(idx, len(data), t0)
        finally:
            self._close_connection(conn_holder)

    def _run_parallel(self, total_chunks: int, t0: float):
        pending = deque(range(total_chunks))
        queue_lock = threading.Lock()
        first_error: list[Optional[BaseException]] = [None]
        err_lock = threading.Lock()
        stop_event = threading.Event()

        def worker():
            conn_holder = [None]
            try:
                with open(self.file_path, "rb") as f:
                    while not self._cancelled and not stop_event.is_set():
                        while not self._resume.wait(0.2):
                            if self._cancelled or stop_event.is_set():
                                return
                        if self._cancelled or stop_event.is_set():
                            return
                        with queue_lock:
                            if not pending:
                                return
                            idx = pending.popleft()
                        offset = idx * self.chunk_size
                        f.seek(offset)
                        data = f.read(self.chunk_size)
                        self._begin_in_flight()
                        try:
                            self._post_chunk(conn_holder, idx, total_chunks, offset, data, parallel=True)
                        finally:
                            self._end_in_flight()
                        if self._cancelled or stop_event.is_set():
                            return
                        self._record_chunk_done(idx, len(data), t0)
            except _Cancelled:
                stop_event.set()
                self._resume.set()
            except BaseException as exc:
                with err_lock:
                    if first_error[0] is None:
                        first_error[0] = exc
                stop_event.set()
                self._resume.set()
            finally:
                self._close_connection(conn_holder)

        n_workers = min(self.max_streams, total_chunks)
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(n_workers)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        if self._cancelled:
            raise _Cancelled()
        if first_error[0] is not None:
            raise first_error[0]

    def _pieces(self, data: bytes):
        for i in range(0, len(data), PIECE_SIZE):
            if self._cancelled:
                raise _Cancelled()
            yield data[i:i + PIECE_SIZE]

    def _abort_remote(self, timeout: float = 2.0):
        """Ask the receiver to delete its partial temp file."""
        try:
            req = urllib.request.Request(
                self.cancel_url,
                data=json.dumps({"session_id": self.session_id}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=timeout).close()
        except Exception:
            pass
