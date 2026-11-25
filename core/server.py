"""
Flask Server for NearBeam.
Provides REST and SSE endpoints for Safe List folder exploration,
Auto-Save file uploads, clipboard synchronization, and media previews.
"""

import datetime
import io
import ipaddress
import json
import mimetypes
import os
import queue
import re
import selectors
import stat
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
)
from flask_cors import CORS
from werkzeug.exceptions import InternalServerError
from werkzeug.serving import WSGIRequestHandler, connection_dropped_errors
from werkzeug.wsgi import FileWrapper
import socket

# Enable HTTP/1.1 Keep-Alive, 4 MB file wrapper blocks, and TCP_NODELAY for high-speed LAN transfers
WSGIRequestHandler.protocol_version = "HTTP/1.1"
WSGIRequestHandler.timeout = 60


def _high_speed_file_wrapper(file_obj, buffer_size: int = 4 * 1024 * 1024):
    return FileWrapper(file_obj, buffer_size=buffer_size)


class _CountingReader:
    """Wraps rfile for WSGI requests so NearBeamRequestHandler knows when the body was 100% consumed."""

    def __init__(self, stream, expected_length: int):
        self._stream = stream
        self.expected_length = expected_length
        self.bytes_read = 0

    @property
    def remaining(self) -> int:
        return max(0, self.expected_length - self.bytes_read)

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            to_read = self.remaining
        else:
            to_read = min(size, self.remaining)
        if to_read <= 0:
            return b""
        data = self._stream.read(to_read)
        self.bytes_read += len(data)
        return data

    def read1(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        to_read = self.remaining if (size is None or size < 0) else min(size, self.remaining)
        fn = getattr(self._stream, "read1", self._stream.read)
        data = fn(to_read)
        self.bytes_read += len(data)
        return data

    def readinto(self, b) -> int:
        if self.remaining <= 0:
            return 0
        view = memoryview(b).cast("B")
        if len(view) > self.remaining:
            view = view[:self.remaining]
        if hasattr(self._stream, "readinto"):
            n = self._stream.readinto(view)
            if n:
                self.bytes_read += n
            return n or 0
        data = self._stream.read(len(view))
        n = len(data)
        view[:n] = data
        self.bytes_read += n
        return n

    def readline(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        limit = self.remaining if (size is None or size < 0) else min(size, self.remaining)
        data = self._stream.readline(limit)
        self.bytes_read += len(data)
        return data

    def readlines(self, hint: int = -1):
        lines = []
        total = 0
        while self.remaining > 0:
            line = self.readline()
            if not line:
                break
            lines.append(line)
            total += len(line)
            if hint is not None and hint > 0 and total >= hint:
                break
        return lines

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def close(self):
        # Never close the shared socket rfile on keep-alive connections
        pass

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


class NearBeamRequestHandler(WSGIRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60
    keep_alive_timeout = 15.0

    def setup(self):
        super().setup()
        self._requests_handled = 0
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        except Exception:
            pass

    def handle_one_request(self):
        if getattr(self, "_requests_handled", 0) > 0:
            # First check if pipelined bytes are already buffered in rfile without tripping
            # SocketIO._timeout_occurred (non-blocking peek raises BlockingIOError cleanly).
            has_buffered = False
            prev_timeout = self.connection.gettimeout()
            try:
                self.connection.setblocking(False)
                try:
                    # On non-blocking SocketIO, peek(1) returns b"" (not BlockingIOError) when empty;
                    # non-empty bytes guarantee pipelined data is already buffered in rfile.
                    has_buffered = bool(self.rfile.peek(1))
                except BlockingIOError:
                    has_buffered = False
            except OSError:
                self.close_connection = True
                return
            finally:
                try:
                    self.connection.settimeout(prev_timeout if prev_timeout is not None else self.timeout)
                except OSError:
                    pass

            if not has_buffered:
                deadline = time.monotonic() + self.keep_alive_timeout
                sel = selectors.DefaultSelector()
                try:
                    sel.register(self.connection, selectors.EVENT_READ)
                    ready = False
                    while time.monotonic() < deadline:
                        shut_down = getattr(self.server, "_BaseServer__is_shut_down", None)
                        if shut_down is not None and shut_down.is_set():
                            self.close_connection = True
                            return
                        if sel.select(timeout=0.25):
                            ready = True
                            break
                    if not ready:
                        self.close_connection = True
                        return
                except OSError:
                    self.close_connection = True
                    return
                finally:
                    sel.close()
        self._requests_handled = getattr(self, "_requests_handled", 0) + 1
        super().handle_one_request()

    def make_environ(self):
        env = super().make_environ()
        env["wsgi.file_wrapper"] = _high_speed_file_wrapper
        return env

    def run_wsgi(self) -> None:
        # BaseHTTPRequestHandler.parse_request() already writes 100 Continue via handle_expect_100()
        # for HTTP/1.1+ requests; only flush here to avoid emitting a duplicate 100 Continue.
        if self.headers.get("Expect", "").lower().strip() == "100-continue":
            self.wfile.flush()

        self.environ = environ = self.make_environ()
        conn_hdr = (self.headers.get("Connection") or "").lower()
        transfer_enc = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        cl_values = self.headers.get_all("Content-Length") or []
        # RFC 9112 §9.3: HTTP/1.1 connections are persistent by default unless Connection: close is sent.
        want_keep_alive = (
            self.request_version >= "HTTP/1.1"
            and "close" not in conn_hdr
            and not transfer_enc
            and len(cl_values) <= 1
        )
        reader: Optional[_CountingReader] = None
        if want_keep_alive:
            raw_cl = cl_values[0] if cl_values else None
            try:
                expected_len = int(raw_cl) if raw_cl is not None else 0
                if expected_len < 0:
                    want_keep_alive = False
                else:
                    reader = _CountingReader(self.rfile, expected_len)
                    environ["wsgi.input"] = reader
            except ValueError:
                want_keep_alive = False

        status_set = None
        headers_set = None
        status_sent = None
        headers_sent = None
        chunk_response = False
        keep_alive = False

        def write(data: bytes) -> None:
            nonlocal status_sent, headers_sent, chunk_response, keep_alive
            assert status_set is not None, "write() before start_response"
            assert headers_set is not None, "write() before start_response"
            if status_sent is None:
                status_sent = status_set
                headers_sent = headers_set
                try:
                    code_str, msg = status_sent.split(None, 1)
                except ValueError:
                    code_str, msg = status_sent, ""
                code = int(code_str)
                self.send_response(code, msg)
                header_keys = set()
                content_type = ""
                for key, value in headers_sent:
                    lk = key.lower()
                    if lk == "connection":
                        continue
                    self.send_header(key, value)
                    header_keys.add(lk)
                    if lk == "content-type":
                        content_type = value.lower()

                framed_no_chunk = (
                    "content-length" in header_keys
                    or environ["REQUEST_METHOD"] == "HEAD"
                    or (100 <= code < 200)
                    or code in {204, 304}
                )
                if not framed_no_chunk and self.protocol_version >= "HTTP/1.1":
                    chunk_response = True
                    self.send_header("Transfer-Encoding", "chunked")

                is_sse = "text/event-stream" in content_type
                can_keep_alive = (
                    want_keep_alive
                    and reader is not None
                    and reader.remaining == 0
                    and code < 400
                    and not is_sse
                    and (framed_no_chunk or chunk_response)
                )
                if can_keep_alive:
                    keep_alive = True
                    self.send_header("Connection", "keep-alive")
                    self.send_header("Keep-Alive", f"timeout={int(self.keep_alive_timeout)}")
                    self.close_connection = False
                else:
                    keep_alive = False
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()

            assert isinstance(data, bytes), "applications must write bytes"
            if data:
                if chunk_response:
                    self.wfile.write(hex(len(data))[2:].encode())
                    self.wfile.write(b"\r\n")
                self.wfile.write(data)
                if chunk_response:
                    self.wfile.write(b"\r\n")
            self.wfile.flush()

        def start_response(status, headers, exc_info=None):
            nonlocal status_set, headers_set
            if exc_info:
                try:
                    if headers_sent:
                        raise exc_info[1].with_traceback(exc_info[2])
                finally:
                    exc_info = None
            elif headers_set:
                raise AssertionError("Headers already set")
            status_set = status
            headers_set = headers
            return write

        def execute(app) -> None:
            application_iter = app(environ, start_response)
            try:
                for data in application_iter:
                    write(data)
                if not headers_sent:
                    write(b"")
                if chunk_response:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            finally:
                if not keep_alive:
                    selector = selectors.DefaultSelector()
                    try:
                        selector.register(self.connection, selectors.EVENT_READ)
                        total_size = 0
                        total_reads = 0
                        while selector.select(timeout=0.01):
                            data = self.rfile.read(10_000_000)
                            total_size += len(data)
                            total_reads += 1
                            if not data or total_size >= 10_000_000_000 or total_reads > 1000:
                                break
                    except OSError:
                        pass
                    finally:
                        selector.close()

                if hasattr(application_iter, "close"):
                    application_iter.close()

        try:
            execute(self.server.app)
        except connection_dropped_errors as e:
            self.close_connection = True
            self.connection_dropped(e, environ)
        except Exception as e:
            self.close_connection = True
            if self.server.passthrough_errors:
                raise
            try:
                if status_sent is None:
                    status_set = None
                    headers_set = None
                execute(InternalServerError())
            except Exception:
                pass
            from werkzeug.debug.tbtools import DebugTraceback
            msg = DebugTraceback(e).render_traceback_text()
            self.server.log("error", f"Error on request:\n{msg}")

PRECOMPRESSED_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz", ".iso",
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"
}

from core.batches import BatchStore
from core.config import config
from core.mesh import MeshManager
from core.network import (
    ZeroconfBroadcaster,
    generate_qr_png_bytes,
    get_connection_urls,
    get_hostname,
    get_local_ip,
)
from core.thumbnails import THUMB_PX, is_thumbnailable, make_thumbnail, shutdown_thumbnail_pool
from core.version import APP_VERSION, check_for_updates

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_PARTIAL_UPLOAD_RE = re.compile(r"\.[A-Za-z0-9_-]{1,128}\.tmp")  # dot first: a received 'notes.tmp' isn't one
_LEGACY_PARTIAL_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\.tmp")

# Pages served by other NearBeam PCs need these cross-origin: the finder probes /api/info and a
# phone sends to another PC through /api/upload/*. Nothing else (clipboard, shared files,
# history) may be read from another origin, so a website can't pull it through the browser.
CORS_RESOURCES = {re.compile(r"/api/info$"): {}, re.compile(r"/api/upload/"): {}}
# Name endings routers and mDNS hand out on a LAN; none can be registered by a website.
LAN_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa", ".fritz.box", ".gateway", ".router")
# No IPv6 addresses: NearBeam listens on IPv4 only (0.0.0.0), so no NearBeam page has one as its origin.
LAN_ORIGINS = [re.compile(
    r"https?://(localhost|127(\.\d+){3}|10(\.\d+){3}|192\.168(\.\d+){2}"
    r"|172\.(1[6-9]|2\d|3[01])(\.\d+){2}|169\.254(\.\d+){2}|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])(\.\d+){2}"
    r"|[a-z0-9-]+|[a-z0-9.-]+(" + "|".join(re.escape(s) for s in LAN_HOST_SUFFIXES) + r"))(:\d+)?$",
    re.IGNORECASE,
)]

_WINDOWS_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_WINDOWS_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _format_mtime(ts: float) -> str:
    """Formats a filesystem timestamp safely without failing on extreme/negative mtimes."""
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def _parse_json_dict(allow_form: bool = False):
    """Returns (dict_payload, error_response). Rejects valid non-object JSON payloads with 400."""
    raw = request.get_json(silent=True)
    if raw is not None:
        if isinstance(raw, dict):
            return raw, None
        return None, (jsonify({"error": "JSON object required"}), 400)
    if allow_form and request.form:
        return request.form, None
    return {}, None


@lru_cache(maxsize=1)
def _own_fqdn() -> str:
    return socket.getfqdn().lower()


def is_lan_host(host: str) -> bool:
    """True for names a LAN device uses to reach this PC: an IP, localhost, a bare or .local name.
    A website's own domain re-pointed at this PC (DNS rebinding) is not one."""
    name = (host or "").strip().lower()
    if not name:
        return False
    if name.startswith("["):
        end = name.find("]")
        if end == -1:
            return False
        rest = name[end + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return False
        addr = name[1:end].split("%", 1)[0]
        try:
            return isinstance(ipaddress.ip_address(addr), ipaddress.IPv6Address)
        except ValueError:
            return False
    name = name.split(":", 1)[0].rstrip(".")
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return "." not in name or name.endswith(LAN_HOST_SUFFIXES) or name == _own_fqdn()


def safe_filename(name: str) -> str:
    """A name Windows can store: no folder parts, reserved characters or device names.
    (':' would otherwise hide the data in an NTFS stream; '?' fails after the whole upload.)"""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _WINDOWS_BAD_CHARS.sub("_", name).strip().rstrip(". ")
    if name.split(".", 1)[0].rstrip().upper() in _WINDOWS_DEVICE_NAMES:
        name = f"_{name}"
    return name or f"unnamed_file_{uuid.uuid4().hex[:8]}"


def remove_stale_uploads(save_dir: str, max_age: float = 24 * 3600):
    """Deletes partial uploads nobody resumed or cancelled within a day (a phone that gave up
    halfway through a 4 GB video would otherwise leave 2 GB hidden in the folder forever)."""
    cutoff = time.time() - max_age
    for folder, pattern in (
        (save_dir, _PARTIAL_UPLOAD_RE),
        (os.path.join(save_dir, "Guest Drops"), _PARTIAL_UPLOAD_RE),
        (os.path.join(tempfile.gettempdir(), "nearbeam_uploads"), _LEGACY_PARTIAL_RE),
    ):
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if pattern.fullmatch(entry.name) and entry.is_file() and entry.stat().st_mtime < cutoff:
                        try:
                            os.remove(entry.path)
                        except OSError:
                            pass
        except OSError:
            continue


def format_size(size_bytes: int) -> str:
    """Formats bytes into human readable string (KB, MB, GB)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def get_file_type_category(filename: str, is_dir: bool = False) -> str:
    """Classifies a file by extension for UI icons and filters."""
    if is_dir:
        return "folder"

    ext = os.path.splitext(filename)[1].lower()
    if ext in {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico",
        ".heic", ".heif", ".avif", ".tif", ".tiff", ".cr2", ".nef", ".arw", ".dng",
    }:
        return "image"
    elif ext in {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v"}:
        return "video"
    elif ext in {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"}:
        return "audio"
    elif ext in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".md", ".csv"}:
        return "document"
    elif ext in {".zip", ".tar", ".gz", ".7z", ".rar", ".bz2"}:
        return "archive"
    elif ext in {".py", ".js", ".html", ".css", ".json", ".ts", ".c", ".cpp", ".java", ".go", ".rs", ".sql"}:
        return "code"
    else:
        return "file"


def remove_when_released(path: str, first_delay: float = 120, retry: float = 30, give_up: float = 6 * 3600, on_removed=None):
    """Deletes a temp file once nothing holds it open. Windows refuses to delete a file that is
    still being streamed, so a slow download keeps it until the download ends."""
    time.sleep(first_delay)
    deadline = time.time() + give_up
    while os.path.exists(path) and time.time() < deadline:
        try:
            os.remove(path)
        except OSError:
            time.sleep(retry)
    if on_removed and not os.path.exists(path):
        on_removed()


def finalize_upload(temp_path: str, dest_dir: str, filename: str) -> str:
    """Moves a finished upload to a free name in dest_dir and returns the final path."""
    os.makedirs(dest_dir, exist_ok=True)
    base, ext = os.path.splitext(filename)
    final_path, counter = os.path.join(dest_dir, filename), 1
    while True:
        try:
            # On Windows, open(..., "xb") raises PermissionError if final_path is an existing directory.
            if os.path.isdir(final_path):
                raise FileExistsError(final_path)
            # Claim the name atomically: two same-name uploads finishing together can't both get it.
            open(final_path, "xb").close()
            break
        except FileExistsError:
            final_path = os.path.join(dest_dir, f"{base} ({counter}){ext}")
            counter += 1
        except PermissionError:
            if os.path.isdir(final_path):
                final_path = os.path.join(dest_dir, f"{base} ({counter}){ext}")
                counter += 1
                continue
            raise
    try:
        try:
            os.replace(temp_path, final_path)
        except OSError:
            import shutil
            shutil.move(temp_path, final_path)
    except Exception:
        # Remove the empty placeholder claimed via open(..., "xb") so no 0-byte orphan remains.
        try:
            if os.path.exists(final_path):
                os.remove(final_path)
        except OSError:
            pass
        raise
    return final_path


def inside_folder(root_real: str, path: str) -> bool:
    """True if path (after resolving links/junctions) stays inside root_real."""
    try:
        return os.path.commonpath([root_real, os.path.realpath(path)]) == root_real
    except ValueError:  # different drive on Windows
        return False


_WIN_LINK_REPARSE_TAGS = (
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
)


def is_link_or_junction(path: str) -> bool:
    """True for symlinks and Windows NTFS junctions (never flags OneDrive cloud folders)."""
    try:
        if os.path.islink(path):
            return True
        if sys.platform == "win32":
            st = os.lstat(path)
            return getattr(st, "st_reparse_tag", 0) in _WIN_LINK_REPARSE_TAGS
    except OSError:
        return True
    return False


def _filter_walk_dirs(root_real: str, walk_root: str, dirnames: list[str], seen_dirs: set[str], skip_hidden: bool = False):
    """Prunes dirnames in-place to block symlink/junction cycles and escapes outside root_real."""
    kept = []
    for d in dirnames:
        if skip_hidden and d.startswith("."):
            continue
        full = os.path.join(walk_root, d)
        if is_link_or_junction(full) or not inside_folder(root_real, full):
            continue
        real_d = os.path.realpath(full)
        if real_d in seen_dirs:
            continue
        seen_dirs.add(real_d)
        kept.append(d)
    dirnames[:] = kept


ORGANIZE_TYPE_FOLDERS = {
    "image": "Photos",
    "video": "Videos",
    "document": "Documents",
    "audio": "Audio",
    "archive": "Archives",
    "code": "Code",
    "file": "Other",
}


def resolve_receive_dir(
    base_save_dir: str,
    filename: str,
    relative_dir: str = "",
    peer_name: str = "",
    mode: str = "off",
    is_guest_drop: bool = False,
) -> str:
    """Computes final destination folder for a received file respecting auto_organize_mode,
    folder uploads (relative_dir), and Guest Drops while keeping paths sandboxed inside base_save_dir."""
    rel_parts = [p for p in (relative_dir or "").replace("\\", "/").split("/") if p not in ("", ".")]
    bucket = ""
    if not is_guest_drop:
        if mode == "type":
            if not rel_parts:
                bucket = ORGANIZE_TYPE_FOLDERS.get(get_file_type_category(filename), "Other")
        elif mode == "date":
            bucket = datetime.date.today().isoformat()
        elif mode == "device":
            raw_peer = (peer_name or "").strip()
            # Reject traversal/dot-only labels and strip leading dots so subfolders stay visible in /api/received.
            stripped_peer = raw_peer.lstrip(". ")
            if not stripped_peer or raw_peer in (".", ".."):
                bucket = "Other Devices"
            else:
                cleaned_peer = safe_filename(stripped_peer).lstrip(". ")
                bucket = cleaned_peer or "Other Devices"

    parts = ([bucket] if bucket else []) + rel_parts
    candidate = os.path.join(base_save_dir, *parts) if parts else base_save_dir
    base_root = os.path.realpath(base_save_dir)
    if not inside_folder(base_root, candidate):
        return base_save_dir
    return candidate


class NearBeamServer:
    def __init__(self, port: int = 5000, enable_mesh: bool = True):
        self.port = port
        if getattr(sys, "frozen", False):
            self.base_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        else:
            self.base_dir = Path(__file__).resolve().parent.parent
        self.template_dir = self.base_dir / "templates"
        self.static_dir = self.base_dir / "static"

        self.app = Flask(
            __name__,
            template_folder=str(self.template_dir),
            static_folder=str(self.static_dir),
        )
        CORS(self.app, resources=CORS_RESOURCES, origins=LAN_ORIGINS)

        # In-memory shared state
        self._stopping = threading.Event()
        self._clipboard_lock = threading.Lock()
        self._events_lock = threading.Lock()
        self._clients_lock = threading.Lock()
        self._upload_state_lock = threading.Lock()
        self._drop_zones_lock = threading.Lock()
        self.clipboard_content = ""
        self.clipboard_updated_at = None
        self.clipboard_image_bytes: Optional[bytes] = None
        self.clipboard_image_mime: str = "image/png"
        self.clipboard_image_id: str = ""
        self.received_files_history = []
        self.connected_clients: Dict[str, Dict[str, Any]] = {}
        self.active_drop_zones: Dict[str, Dict[str, Any]] = {}
        self.event_subscribers: list[queue.Queue] = []
        self.activity_callbacks = []
        self.clipboard_callbacks = []
        self.clipboard_image_callbacks = []
        self.mesh_callbacks = []

        # Mesh / Cluster management
        self.enable_mesh = enable_mesh
        self.mesh: Optional[MeshManager] = None
        if self.enable_mesh:
            self.mesh = MeshManager(
                port=self.port,
                on_role_change=self._on_mesh_role_change,
                on_nodes_change=self._on_mesh_nodes_change,
                on_remote_clipboard=self._on_mesh_clipboard_received,
            )
            self.mesh.start()

        # Zeroconf mDNS broadcaster for permanent nearbeam.local address
        # Only advertise nearbeam.local if Primary Leader (prevents mDNS name collisions)
        self.broadcaster = ZeroconfBroadcaster(name="nearbeam", port=self.port)
        if not self.mesh or self.mesh.role == "leader":
            self.broadcaster.start()
            if self.mesh and self.mesh.role != "leader":
                self.broadcaster.stop()

        # Active background ZIP packaging jobs
        self.zip_jobs: Dict[str, Dict[str, Any]] = {}
        self.zip_jobs_lock = threading.Lock()

        # Ephemeral PC -> Devices Send Batches (separate from permanent Safe List)
        self.batch_store = BatchStore(
            format_size_fn=format_size,
            category_fn=get_file_type_category,
            thumbnailable_fn=is_thumbnailable,
        )

        # Recently finished uploads: a resent last chunk (its response got lost) gets the
        # same answer instead of a 409 or a second copy of the file.
        self.finished_uploads: "OrderedDict[str, dict]" = OrderedDict()
        self._session_locks: Dict[str, threading.Lock] = {}
        self._cancelled_uploads: "OrderedDict[str, float]" = OrderedDict()
        self._parallel_sessions: Dict[str, Dict[str, Any]] = {}

        self.current_ip = get_local_ip()
        threading.Thread(target=self._watch_network, daemon=True).start()
        threading.Thread(target=remove_stale_uploads, args=(config.save_directory,), daemon=True).start()

        self._register_routes()

    def _parse_client_info(self, ip: str, ua: str, custom_name: str = "") -> Dict[str, Any]:
        """Classifies a connected browser client by User-Agent and IP."""
        ua_lower = (ua or "").lower()
        if custom_name:
            name = custom_name.strip()[:40]
            dtype = "phone" if any(k in custom_name.lower() for k in ("iphone", "ipad", "android", "phone")) else "laptop"
            icon = "📱" if dtype == "phone" else "💻"
        elif "iphone" in ua_lower:
            name, icon, dtype = "iPhone", "📱", "phone"
        elif "ipad" in ua_lower:
            name, icon, dtype = "iPad", "📲", "tablet"
        elif "android" in ua_lower:
            name, icon, dtype = "Android Phone", "📱", "phone"
        elif "macintosh" in ua_lower or "mac os" in ua_lower:
            name, icon, dtype = "Mac Browser", "💻", "laptop"
        elif "windows" in ua_lower:
            name, icon, dtype = "Windows Browser", "💻", "laptop"
        elif "linux" in ua_lower:
            name, icon, dtype = "Linux Browser", "💻", "laptop"
        else:
            name, icon, dtype = f"Browser ({ip})", "📱", "phone"

        return {
            "id": f"client_{ip}",
            "kind": "client",
            "name": name,
            "ip": ip,
            "icon": icon,
            "device_type": dtype,
            "status": "Browser connected",
            "connection": "Wi-Fi",
            "online": True,
            "is_self": False,
            "can_beam_direct": False,
            "last_seen": time.time(),
        }

    def record_client_seen(self, ip: str, ua: str = "", custom_name: str = ""):
        """Records an active browser client and broadcasts updates when a new client joins."""
        if not ip:
            return
        now = time.time()
        info = self._parse_client_info(ip, ua, custom_name)
        with self._clients_lock:
            existing = self.connected_clients.get(ip)
            is_new = existing is None or (now - existing.get("last_seen", 0) > 60)
            self.connected_clients[ip] = info
        if is_new:
            self.broadcast_event({
                "type": "nearby_devices_changed",
                "devices": self.get_nearby_devices(),
            })

    def get_nearby_devices(self) -> list[dict]:
        """Returns unified list of nearby Mesh Hosts and active Browser Clients."""
        devices = []
        mesh_ips = set()

        if self.mesh:
            nodes = self.mesh.get_nodes_list()
        else:
            local_ip = get_local_ip()
            hostname = get_hostname()
            nodes = [{
                "id": "self",
                "name": hostname,
                "ip": local_ip,
                "port": self.port,
                "url": f"http://{local_ip}:{self.port}",
                "is_self": True,
                "is_leader": True,
                "online": True,
            }]

        for n in nodes:
            ip = n.get("ip", "")
            mesh_ips.add(ip)
            n_name = n.get("name", "PC")
            is_laptop = "laptop" in n_name.lower()
            icon = "💻" if is_laptop else "🖥️"
            conn_label = "nearbeam.local • LAN" if n.get("is_leader") else "Wi-Fi / High-Speed LAN"
            devices.append({
                "id": n.get("id", "self"),
                "kind": "host",
                "name": n_name,
                "ip": ip,
                "port": n.get("port", self.port),
                "url": n.get("url", f"http://{ip}:{self.port}"),
                "icon": icon,
                "device_type": "laptop" if is_laptop else "desktop",
                "status": "Online",
                "connection": conn_label,
                "online": True,
                "is_self": bool(n.get("is_self")),
                "is_leader": bool(n.get("is_leader")),
                "can_beam_direct": True,
            })

        now = time.time()
        with self._clients_lock:
            stale_ips = []
            for ip, c in list(self.connected_clients.items()):
                if now - c.get("last_seen", 0) > 900:
                    stale_ips.append(ip)
                    continue
                if ip not in mesh_ips:
                    devices.append(dict(c))
            for ip in stale_ips:
                self.connected_clients.pop(ip, None)

        return devices

    def _watch_network(self):
        """Follows the PC's address: Wi-Fi often connects after an autostarted NearBeam, and laptops move networks."""
        while not self._stopping.wait(5.0):
            self.check_network()

    def check_network(self):
        """Re-advertises nearbeam.local and re-announces the mesh when the IP changes."""
        ip = get_local_ip()
        if ip == self.current_ip:
            return
        self.current_ip = ip
        if self.mesh:
            self.mesh.set_ip(ip)
        if not self.mesh or self.mesh.role == "leader":
            self.broadcaster.stop()
            self.broadcaster.start(ip)  # does nothing while offline (127.0.0.1)
            if self.mesh and self.mesh.role != "leader":
                self.broadcaster.stop()
        self.log_activity(f"Network changed: this PC is now at {ip}", "info")
        self.broadcast_event({"type": "network_changed", "ip": ip})

    def shutdown(self):
        """Stops background workers and unregisters mDNS services."""
        self._stopping.set()
        if hasattr(self, "mesh") and self.mesh:
            try:
                self.mesh.stop()
            except Exception:
                pass
        if hasattr(self, "broadcaster") and self.broadcaster:
            try:
                self.broadcaster.stop()
            except Exception:
                pass
        if hasattr(self, "zip_jobs_lock"):
            with self.zip_jobs_lock:
                for job in self.zip_jobs.values():
                    cancel_ev = job.get("cancel_event")
                    if cancel_ev is not None:
                        cancel_ev.set()
                    temp_path = job.get("temp_path")
                    if temp_path and os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass
                self.zip_jobs.clear()
        if hasattr(self, "_upload_state_lock") and hasattr(self, "_parallel_sessions"):
            with self._upload_state_lock:
                sessions = list(self._parallel_sessions.values())
                self._parallel_sessions.clear()
            for sess in sessions:
                tp = sess.get("temp_path")
                if sess.get("active_writers", 0) == 0 and tp and os.path.exists(tp):
                    try:
                        os.remove(tp)
                    except OSError:
                        pass
        try:
            shutdown_thumbnail_pool()
        except Exception:
            pass

    def _cleanup_old_zip_jobs(self):
        """Removes zip jobs older than 30 minutes to prevent temp disk buildup."""
        now = time.time()
        to_delete = []
        with self.zip_jobs_lock:
            for jid, job in self.zip_jobs.items():
                # A big folder can take longer than 30 min to pack; dropping it mid-run would orphan its temp ZIP.
                if job.get("status") != "processing" and now - job.get("created_at", now) > 1800:
                    to_delete.append((jid, job.get("temp_path")))
            for jid, temp_path in to_delete:
                self.zip_jobs.pop(jid, None)
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass

    def _run_zip_worker(self, job_id: str, real_target: str, temp_zip_path: str, files_list: list):
        """Worker thread to compress files into temp_zip_path while tracking progress."""
        try:
            with zipfile.ZipFile(
                temp_zip_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
                compresslevel=1,
                strict_timestamps=False,
            ) as zip_file:
                for file_path, archive_name, f_size in files_list:
                    with self.zip_jobs_lock:
                        job = self.zip_jobs.get(job_id)
                        if not job or job.get("cancel_event", threading.Event()).is_set():
                            break
                        job["current_file"] = os.path.basename(file_path)

                    ext = os.path.splitext(file_path)[1].lower()
                    compress_type = zipfile.ZIP_STORED if ext in PRECOMPRESSED_EXTENSIONS else zipfile.ZIP_DEFLATED

                    try:
                        if f_size <= 1024 * 512:
                            zip_file.write(
                                file_path,
                                archive_name,
                                compress_type=compress_type,
                                compresslevel=1 if compress_type == zipfile.ZIP_DEFLATED else None,
                            )
                            with self.zip_jobs_lock:
                                job = self.zip_jobs.get(job_id)
                                if job:
                                    job["processed_bytes"] += f_size
                                    if job["total_bytes"] > 0:
                                        job["percent"] = min(99.0, round((job["processed_bytes"] / job["total_bytes"]) * 100, 1))
                                    elif job["total_files"] > 0:
                                        job["percent"] = min(99.0, round((job["processed_files"] / job["total_files"]) * 100, 1))
                        else:
                            chunk_size = 4 * 1024 * 1024
                            zinfo = zipfile.ZipInfo.from_file(file_path, archive_name, strict_timestamps=False)
                            zinfo.compress_type = compress_type
                            if compress_type == zipfile.ZIP_DEFLATED:
                                zinfo._compresslevel = 1
                            with open(file_path, "rb", buffering=chunk_size) as src, zip_file.open(zinfo, "w", force_zip64=(f_size > 0xFFFFFFFF)) as dst:
                                while True:
                                    with self.zip_jobs_lock:
                                        job = self.zip_jobs.get(job_id)
                                        if not job or job.get("cancel_event", threading.Event()).is_set():
                                            break
                                    chunk = src.read(chunk_size)
                                    if not chunk:
                                        break
                                    dst.write(chunk)
                                    with self.zip_jobs_lock:
                                        job = self.zip_jobs.get(job_id)
                                        if job:
                                            job["processed_bytes"] += len(chunk)
                                            if job["total_bytes"] > 0:
                                                job["percent"] = min(99.0, round((job["processed_bytes"] / job["total_bytes"]) * 100, 1))
                    except (PermissionError, OSError):
                        with self.zip_jobs_lock:
                            job = self.zip_jobs.get(job_id)
                            if job:
                                job["processed_bytes"] += f_size
                        continue

                    with self.zip_jobs_lock:
                        job = self.zip_jobs.get(job_id)
                        if job:
                            job["processed_files"] += 1
                            if job.get("cancel_event", threading.Event()).is_set():
                                break

            cleanup_temp = False
            with self.zip_jobs_lock:
                job = self.zip_jobs.get(job_id)
                if not job or job.get("cancel_event", threading.Event()).is_set():
                    if job:
                        job["status"] = "cancelled"
                    cleanup_temp = True
                else:
                    job["status"] = "completed"
                    job["percent"] = 100.0
                    job["processed_files"] = len(files_list)
                    job["processed_bytes"] = job["total_bytes"]
                    job["current_file"] = "Complete"
            if cleanup_temp:
                try:
                    if os.path.exists(temp_zip_path):
                        os.remove(temp_zip_path)
                except Exception:
                    pass
        except Exception as e:
            with self.zip_jobs_lock:
                job = self.zip_jobs.get(job_id)
                if job:
                    job["status"] = "error"
                    job["error"] = str(e)
            try:
                if os.path.exists(temp_zip_path):
                    os.remove(temp_zip_path)
            except Exception:
                pass

    def add_mesh_callback(self, callback):
        """Registers a callback for desktop GUI cluster status updates."""
        self.mesh_callbacks.append(callback)

    def _on_mesh_role_change(self, role: str):
        """Triggered when role transitions between LEADER and NODE."""
        if self._stopping.is_set():
            return
        self.log_activity(f"Mesh role established: {role.upper()}", "info")
        if hasattr(self, "broadcaster") and self.broadcaster:
            if role == "leader":
                self.broadcaster.start()
                # Re-check in case mesh stepped down while Zeroconf registration was probing.
                if self.mesh and self.mesh.role != "leader":
                    self.broadcaster.stop()
            elif role == "node":
                self.broadcaster.stop()

        self.broadcast_event({
            "type": "mesh_role_changed",
            "role": role,
            "nodes": self.mesh.get_nodes_list() if self.mesh else [],
        })
        for cb in self.mesh_callbacks:
            try:
                cb(role, self.mesh.get_nodes_list() if self.mesh else [])
            except Exception:
                pass

    def _on_mesh_nodes_change(self, nodes: list):
        """Triggered when cluster nodes join, leave, or update."""
        self.broadcast_event({
            "type": "mesh_nodes_changed",
            "nodes": nodes,
        })
        role = self.mesh.role if self.mesh else "leader"
        for cb in self.mesh_callbacks:
            try:
                cb(role, nodes)
            except Exception:
                pass

    def _on_mesh_clipboard_received(self, text: str):
        """Triggered when clipboard text is received from peer mesh node."""
        if not isinstance(text, str) or not text:
            return
        with self._clipboard_lock:
            if text == self.clipboard_content:
                return
            self.clipboard_content = text
            self.clipboard_updated_at = datetime.datetime.now().strftime("%H:%M:%S")
        state = self.get_clipboard_state()
        self.broadcast_event({
            "type": "clipboard_updated",
            **state,
        })
        for cb in self.clipboard_callbacks:
            try:
                cb(text)
            except Exception:
                pass

    def add_activity_callback(self, callback):
        """Registers a callback for desktop GUI activity logging."""
        self.activity_callbacks.append(callback)

    def add_clipboard_callback(self, callback):
        """Registers a callback for remote clipboard text updates."""
        self.clipboard_callbacks.append(callback)

    def add_clipboard_image_callback(self, callback):
        """Registers a callback for remote clipboard image/screenshot updates."""
        self.clipboard_image_callbacks.append(callback)

    def get_clipboard_state(self) -> dict:
        """Returns current text + image metadata for the shared clipboard."""
        with self._clipboard_lock:
            text = self.clipboard_content
            updated_at = self.clipboard_updated_at
            img_bytes = self.clipboard_image_bytes
            img_id = self.clipboard_image_id
            img_mime = self.clipboard_image_mime
        has_img = bool(img_bytes)
        img_size = len(img_bytes) if img_bytes else 0
        return {
            "text": text,
            "updated_at": updated_at,
            "has_image": has_img,
            "image_id": img_id if has_img else None,
            "image_url": f"/api/clipboard/image?id={img_id}" if has_img else None,
            "image_mime": img_mime if has_img else None,
            "image_size": img_size,
            "image_formatted_size": format_size(img_size) if has_img else None,
        }

    def set_clipboard_image(
        self,
        image_bytes: bytes,
        mime_type: str = "image/png",
        notify_local_os: bool = True,
        propagate_mesh: bool = True,
        source_node_id: Optional[str] = None,
    ) -> dict:
        """Updates the shared clipboard image, broadcasts via SSE, and syncs across mesh/OS."""
        import base64

        clean_mime = mime_type.strip() if isinstance(mime_type, str) and mime_type.strip() else "image/png"
        with self._clipboard_lock:
            self.clipboard_image_bytes = image_bytes
            self.clipboard_image_mime = clean_mime
            self.clipboard_image_id = uuid.uuid4().hex[:12]
            self.clipboard_updated_at = datetime.datetime.now().strftime("%H:%M:%S")

        state = self.get_clipboard_state()
        self.broadcast_event({
            "type": "clipboard_updated",
            **state,
        })

        if notify_local_os:
            for cb in self.clipboard_image_callbacks:
                try:
                    cb(image_bytes, clean_mime)
                except Exception as e:
                    print(f"Error in clipboard image callback: {e}")

        if propagate_mesh and self.mesh and image_bytes:
            b64_str = base64.b64encode(image_bytes).decode("ascii")
            self.mesh.broadcast_clipboard_mesh(
                text="",
                source_node_id=source_node_id,
                image_base64=b64_str,
                image_mime=clean_mime,
            )

        return state

    def log_activity(self, message: str, event_type: str = "info"):
        """Logs an activity event and notifies desktop GUI callbacks."""
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {message}"
        for cb in self.activity_callbacks:
            try:
                cb(formatted, event_type)
            except Exception:
                pass

    def broadcast_event(self, event_data: dict):
        """Broadcasts an SSE event to all connected web clients."""
        payload = json.dumps(event_data)
        with self._events_lock:
            subscribers = list(self.event_subscribers)
        dead = []
        for q in subscribers:
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        if dead:
            with self._events_lock:
                for q in dead:
                    if q in self.event_subscribers:
                        self.event_subscribers.remove(q)

    def create_send_batch(
        self,
        paths: list[str],
        source: str = "Drop zone",
        merge_window: float = 0.0,
    ) -> Optional[dict]:
        """Creates (or merges) an ephemeral PC -> Devices Send Batch without touching Safe List."""
        batch = self.batch_store.create_or_merge(paths, source=source, merge_window=merge_window)
        if not batch:
            return None
        self.log_activity(
            f"Ready for devices ({source}): {batch['label']} ({batch['formatted_size']})",
            event_type="upload",
        )
        self.broadcast_event({
            "type": "batches_updated",
            "batch_id": batch["id"],
            "label": batch["label"],
            "file_count": batch["file_count"],
        })
        return batch

    def _start_zip_job(self, folder_name: str, files_list: list, total_bytes: int) -> dict:
        """Spawns an async background ZIP packaging job and returns its job metadata."""
        self._cleanup_old_zip_jobs()
        job_id = str(uuid.uuid4())
        temp_zip_path = os.path.join(tempfile.gettempdir(), f"nearbeam_zip_{job_id}.zip")
        cancel_event = threading.Event()

        with self.zip_jobs_lock:
            self.zip_jobs[job_id] = {
                "job_id": job_id,
                "folder_name": folder_name,
                "temp_path": temp_zip_path,
                "status": "processing",
                "error": None,
                "total_files": len(files_list),
                "processed_files": 0,
                "total_bytes": total_bytes,
                "processed_bytes": 0,
                "percent": 0.0,
                "current_file": "Initializing...",
                "created_at": time.time(),
                "cancel_event": cancel_event,
            }

        worker_thread = threading.Thread(
            target=self._run_zip_worker,
            args=(job_id, "", temp_zip_path, files_list),
            daemon=True,
        )
        worker_thread.start()

        return {
            "success": True,
            "job_id": job_id,
            "folder_name": folder_name,
            "total_files": len(files_list),
            "total_bytes": total_bytes,
        }

    def _track_download(self, resp, filename: str):
        """Wraps a file response so the desktop app can show phone/browser downloads
        with live progress. Small files and non-file responses pass through untouched."""
        total = resp.content_length
        if request.method == "HEAD" or resp.status_code not in (200, 206) or not total or total < 1024 * 1024:
            return resp
        full_file = resp.status_code == 200
        addr = request.remote_addr or ""
        with self._clients_lock:
            peer = self.connected_clients.get(addr, {}).get("name") or addr or "a device"
        download_id = f"dl:{uuid.uuid4().hex}"  # unique per download: same-named files can run at once
        inner = resp.response
        server = self
        inner_closed = False

        def _close_inner():
            nonlocal inner_closed
            if not inner_closed:
                inner_closed = True
                if hasattr(inner, "close"):
                    inner.close()

        def stream():
            sent = 0
            last_emit = 0.0
            try:
                for chunk in inner:
                    sent += len(chunk)
                    now = time.monotonic()
                    if now - last_emit >= 0.4:
                        last_emit = now
                        server.broadcast_event({
                            "type": "download_progress",
                            "download_id": download_id,
                            "filename": filename,
                            "sent": sent,
                            "total": total,
                            "to_addr": addr,
                        })
                    yield chunk
            finally:
                _close_inner()
                completed = full_file and sent >= total
                server.broadcast_event({
                    "type": "download_finished",
                    "download_id": download_id,
                    "filename": filename,
                    "completed": completed,
                    "partial_range": not full_file,
                    "to_addr": addr,
                })
                if completed:
                    config.add_transfer_history({
                        "filename": filename,
                        "size": total,
                        "formatted_size": format_size(total),
                        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "direction": "sent",
                        "peer": peer,
                        "status": "completed",
                        "category": get_file_type_category(filename),
                    })

        resp.response = stream()
        resp.call_on_close(_close_inner)
        return resp

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._upload_state_lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                if len(self._session_locks) > 512:
                    for key in list(self._session_locks.keys()):
                        if not self._session_locks[key].locked() and key not in self._parallel_sessions:
                            self._session_locks.pop(key, None)
                            if len(self._session_locks) <= 256:
                                break
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _is_upload_cancelled(self, session_id: str) -> bool:
        with self._upload_state_lock:
            return session_id in self._cancelled_uploads

    def _prune_drop_zones_locked(self, now: float):
        expired = [tok for tok, meta in self.active_drop_zones.items() if now > meta.get("expires_at", 0)]
        for tok in expired:
            self.active_drop_zones.pop(tok, None)

    def _resolve_peer_device_label(self) -> str:
        """Returns a friendly sender device name for 'device' auto-organize mode."""
        from urllib.parse import unquote
        hdr = request.headers.get("X-Device-Name", "").strip()
        if hdr:
            return unquote(hdr)[:48]
        addr = request.remote_addr or ""
        with self._clients_lock:
            known = self.connected_clients.get(addr, {}).get("name")
        if known:
            return known
        ua = request.headers.get("User-Agent", "")
        if ua:
            parsed = self._parse_client_info(addr or "device", ua)
            if parsed.get("name"):
                return parsed["name"]
        return addr or "Other Devices"

    def _receive_chunk(self, save_dir: str, peer: str, log_template: str, is_guest_drop: bool = False):
        """Shared chunk receiver for host uploads and guest drops."""
        from urllib.parse import unquote
        session_id = request.headers.get("X-Session-Id") or request.form.get("session_id")
        filename_raw = request.headers.get("X-Filename") or request.form.get("filename")
        if not session_id or not filename_raw:
            return jsonify({"error": "Missing parameters"}), 400
        if not _SESSION_ID_RE.fullmatch(session_id):
            return jsonify({"error": "Invalid session id"}), 400
        try:
            chunk_index = int(request.headers.get("X-Chunk-Index", request.form.get("chunk_index", 0)))
            total_chunks = int(request.headers.get("X-Total-Chunks", request.form.get("total_chunks", 1)))
            offset_raw = request.headers.get("X-Chunk-Offset")
            offset = int(offset_raw) if offset_raw is not None else None
            size_raw = request.headers.get("X-File-Size")
            file_size = int(size_raw) if size_raw is not None else None
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid chunk headers"}), 400
        if (chunk_index < 0 or total_chunks < 1 or chunk_index >= total_chunks
                or (offset is not None and offset < 0) or (file_size is not None and file_size < 0)):
            return jsonify({"error": "Invalid chunk headers"}), 400

        filename = safe_filename(unquote(filename_raw))
        relative_dir = unquote(request.headers.get("X-Relative-Dir", request.form.get("relative_dir", ""))).strip()

        if relative_dir:
            norm_rel = relative_dir.replace("\\", "/")
            if norm_rel.startswith("/"):
                return jsonify({"error": "Invalid relative directory"}), 400
            raw_parts = [p for p in norm_rel.split("/") if p not in ("", ".")]
            if any(p == ".." for p in raw_parts) or (raw_parts and re.fullmatch(r"[A-Za-z]:", raw_parts[0])):
                return jsonify({"error": "Invalid relative directory"}), 400
            # Folder names get the same clean-up as file names ('Trip: day 1' -> 'Trip_ day 1')
            # BEFORE os.path.join so names like 'D: notes' aren't parsed as Windows drive letters,
            # and strip leading dots so uploaded folders aren't hidden from /api/received.
            clean_parts = []
            for p in raw_parts:
                cleaned = safe_filename(p).lstrip(". ")
                if not cleaned:
                    return jsonify({"error": "Invalid relative directory"}), 400
                clean_parts.append(cleaned)
            relative_dir = "/".join(clean_parts)
            probe_dir = os.path.join(save_dir, *clean_parts) if clean_parts else save_dir
            root = os.path.realpath(save_dir)
            if not inside_folder(root, probe_dir):
                return jsonify({"error": "Invalid relative directory"}), 400

        dest_dir = resolve_receive_dir(
            save_dir,
            filename=filename,
            relative_dir=relative_dir,
            peer_name=self._resolve_peer_device_label(),
            mode=config.auto_organize_mode,
            is_guest_drop=is_guest_drop,
        )

        if request.headers.get("X-Parallel") == "1":
            return self._receive_parallel_chunk(
                session_id=session_id,
                save_dir=save_dir,
                dest_dir=dest_dir,
                filename=filename,
                relative_dir=relative_dir,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                offset=offset,
                file_size=file_size,
                peer=peer,
                log_template=log_template,
            )

        session_lock = self._get_session_lock(session_id)
        with session_lock:
            with self._upload_state_lock:
                if session_id in self._cancelled_uploads:
                    return jsonify({"success": False, "cancelled": True}), 499
                if session_id in self._parallel_sessions:
                    return jsonify({"error": "Session is already active in parallel mode"}), 409
                finished = self.finished_uploads.get(session_id)
                if finished:
                    return jsonify({"success": True, "completed": True, "file": finished})

            os.makedirs(save_dir, exist_ok=True)
            temp_path = os.path.join(save_dir, f".{session_id}.tmp")
            legacy_temp = os.path.join(tempfile.gettempdir(), "nearbeam_uploads", f"{session_id}.tmp")
            if chunk_index > 0 and os.path.exists(legacy_temp) and not os.path.exists(temp_path):
                temp_path = legacy_temp

            have = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
            if offset is None:
                # Legacy clients without X-Chunk-Offset: append-only.
                mode = "ab" if chunk_index > 0 else "wb"
                received = have if chunk_index > 0 else 0
            else:
                if offset > have:
                    return jsonify({"error": "Chunk offset beyond received data", "received_bytes": have}), 409
                mode = "r+b" if have else "wb"
                received = offset

            # Older clients don't send X-File-Size; every chunk but the last is full-size.
            total_bytes = file_size if file_size is not None else (request.content_length or 0) * total_chunks
            from_addr = request.remote_addr

            def emit_progress():
                # Live incoming progress for the desktop Transfers page and the host web app.
                if total_bytes < 1024 * 1024:
                    return  # tiny files finish before a progress bar would help
                self.broadcast_event({
                    "type": "upload_progress",
                    "session_id": session_id,
                    "filename": filename,
                    "relative_dir": relative_dir,
                    "chunk_index": chunk_index,
                    "total_chunks": total_chunks,
                    "percent": round(min(received / total_bytes, 1.0) * 100, 1),
                    "received_bytes": received,
                    "total_bytes": total_bytes,
                    "from_addr": from_addr,
                })

            # Small reads so a slow Wi-Fi upload still reports progress every ~0.5 s
            # instead of once per 50 MB chunk; file writes stay buffered.
            read_size = 64 * 1024
            last_emit = time.monotonic()
            cancelled = False
            written = 0
            file_part = request.files.get("file")
            using_multipart = file_part is not None
            expected_cl = None if using_multipart else request.content_length
            with open(temp_path, mode, buffering=8 * 1024 * 1024) as f:
                if offset is not None:
                    # A resent chunk overwrites its own range instead of being appended twice.
                    f.seek(offset)
                    f.truncate()
                stream = file_part.stream if using_multipart else request.stream
                while True:
                    if self._is_upload_cancelled(session_id):
                        cancelled = True
                        break
                    data = stream.read(read_size)
                    if not data:
                        break
                    f.write(data)
                    written += len(data)
                    received += len(data)
                    now = time.monotonic()
                    if now - last_emit >= 0.5:
                        last_emit = now
                        emit_progress()

            if cancelled or self._is_upload_cancelled(session_id):
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass
                return jsonify({"success": False, "cancelled": True}), 499

            if expected_cl is not None and written != expected_cl:
                return jsonify({"error": "Incomplete chunk payload"}), 400

            emit_progress()

            # Re-check cancellation right before returning/finalizing (guards cancel during emit_progress).
            if self._is_upload_cancelled(session_id):
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass
                return jsonify({"success": False, "cancelled": True}), 499

            if chunk_index < total_chunks - 1:
                return jsonify({"success": True, "completed": False, "chunk_index": chunk_index})

            if file_size is not None:
                actual_size = os.path.getsize(temp_path) if os.path.isfile(temp_path) else -1
                if actual_size != file_size:
                    return jsonify({"error": "Uploaded file size does not match X-File-Size"}), 400

            file_info = self._complete_received_upload(
                session_id=session_id,
                temp_path=temp_path,
                dest_dir=dest_dir,
                filename=filename,
                peer=peer,
                log_template=log_template,
            )
            return jsonify({"success": True, "completed": True, "file": file_info})

    def _complete_received_upload(
        self,
        session_id: str,
        temp_path: str,
        dest_dir: str,
        filename: str,
        peer: str,
        log_template: str,
    ) -> dict:
        final_path = finalize_upload(temp_path, dest_dir, filename)
        final_name = os.path.basename(final_path)
        total_bytes = os.path.getsize(final_path)

        file_info = {
            "filename": final_name,
            "original_name": filename,
            "size": total_bytes,
            "formatted_size": format_size(total_bytes),
            "saved_path": final_path,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "category": get_file_type_category(final_name),
        }
        with self._upload_state_lock:
            self.finished_uploads[session_id] = file_info
            self._parallel_sessions.pop(session_id, None)
            if len(self.finished_uploads) > 200:
                self.finished_uploads.popitem(last=False)
            self.received_files_history.insert(0, file_info)
            if len(self.received_files_history) > 100:
                self.received_files_history.pop()
        config.add_transfer_history({
            **file_info,
            "direction": "received",
            "peer": peer,
            "status": "completed",
        })

        self.log_activity(log_template.format(name=final_name, size=format_size(total_bytes)), event_type="upload")
        self.broadcast_event({
            "type": "file_received",
            "file": file_info,
            "session_id": session_id,
        })
        return file_info

    def _prune_stale_parallel_sessions(self, max_age: float = 6 * 3600):
        cutoff = time.time() - max_age
        with self._upload_state_lock:
            candidates = [
                sid for sid, sess in self._parallel_sessions.items()
                if sess.get("active_writers", 0) == 0 and sess.get("last_active", sess.get("created_at", 0)) < cutoff
            ]
        for sid in candidates:
            slock = self._get_session_lock(sid)
            if not slock.acquire(blocking=False):
                continue
            tp = None
            try:
                with self._upload_state_lock:
                    sess = self._parallel_sessions.get(sid)
                    if sess is not None and sess.get("active_writers", 0) == 0 and sess.get("last_active", sess.get("created_at", 0)) < cutoff:
                        self._parallel_sessions.pop(sid, None)
                        tp = sess.get("temp_path")
            finally:
                slock.release()
            if tp and os.path.exists(tp):
                try:
                    os.remove(tp)
                except OSError:
                    pass

    @staticmethod
    def _ranges_cover_file(ranges, file_size: int) -> bool:
        """Verifies committed chunk byte ranges contiguously cover [0, file_size) with no gaps."""
        if file_size == 0:
            return True
        intervals = sorted((s, e) for s, e in ranges if e > s)
        covered = 0
        for start, end in intervals:
            if start > covered:
                return False
            if end > covered:
                covered = end
        return covered == file_size

    def _receive_parallel_chunk(
        self,
        session_id: str,
        save_dir: str,
        dest_dir: str,
        filename: str,
        relative_dir: str,
        chunk_index: int,
        total_chunks: int,
        offset: Optional[int],
        file_size: Optional[int],
        peer: str,
        log_template: str,
    ):
        """Order-independent concurrent chunk writer backed by a pre-allocated .tmp file."""
        if offset is None or file_size is None or offset > file_size:
            return jsonify({"error": "Parallel uploads require valid X-Chunk-Offset and X-File-Size"}), 400

        self._prune_stale_parallel_sessions()
        session_lock = self._get_session_lock(session_id)
        with session_lock:
            with self._upload_state_lock:
                if session_id in self._cancelled_uploads:
                    return jsonify({"success": False, "cancelled": True}), 499
                finished = self.finished_uploads.get(session_id)
                if finished:
                    return jsonify({"success": True, "completed": True, "file": finished})
                sess = self._parallel_sessions.get(session_id)

            if sess is None:
                os.makedirs(save_dir, exist_ok=True)
                temp_path = os.path.join(save_dir, f".{session_id}.tmp")
                if os.path.exists(temp_path):
                    return jsonify({"error": "Session is already active in sequential mode"}), 409
                try:
                    with open(temp_path, "wb") as prealloc:
                        if file_size > 0:
                            prealloc.truncate(file_size)
                except OSError:
                    try:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                    except OSError:
                        pass
                    raise
                sess = {
                    "temp_path": temp_path,
                    "dest_dir": dest_dir,  # locked for session (e.g. across midnight in date mode)
                    "filename": filename,
                    "file_size": file_size,
                    "total_chunks": total_chunks,
                    "received_chunks": set(),
                    "chunk_progress": {},
                    "chunk_ranges": {},
                    "active_writers": 0,
                    "finalized": False,
                    "created_at": time.time(),
                    "last_active": time.time(),
                }
                with self._upload_state_lock:
                    self._parallel_sessions[session_id] = sess
            else:
                if (
                    sess["file_size"] != file_size
                    or sess["total_chunks"] != total_chunks
                    or sess["filename"] != filename
                ):
                    return jsonify({"error": "Parallel session headers mismatch"}), 400
                temp_path = sess["temp_path"]
                sess["last_active"] = time.time()

            sess["active_writers"] += 1

        from_addr = request.remote_addr
        read_size = 64 * 1024
        last_emit = time.monotonic()
        written = 0
        cancelled = False
        overflow = False
        stream_exc = None
        file_part = request.files.get("file")
        using_multipart = file_part is not None
        expected_cl = None if using_multipart else request.content_length

        def _emit_aggregate_progress_locked(cur_written: int):
            if file_size < 1024 * 1024 or sess.get("finalized") or self._is_upload_cancelled(session_id):
                return
            sess["chunk_progress"][chunk_index] = cur_written
            total_received = sum(sess["chunk_progress"].values())
            self.broadcast_event({
                "type": "upload_progress",
                "session_id": session_id,
                "filename": filename,
                "relative_dir": relative_dir,
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "percent": round(min(total_received / file_size, 1.0) * 100, 1) if file_size > 0 else 100.0,
                "received_bytes": min(total_received, file_size),
                "total_bytes": file_size,
                "from_addr": from_addr,
            })

        try:
            with open(temp_path, "r+b", buffering=4 * 1024 * 1024) as f:
                f.seek(offset)
                stream = file_part.stream if using_multipart else request.stream
                while True:
                    if self._is_upload_cancelled(session_id):
                        cancelled = True
                        break
                    data = stream.read(read_size)
                    if not data:
                        break
                    if offset + written + len(data) > file_size:
                        overflow = True
                        break
                    f.write(data)
                    written += len(data)
                    now = time.monotonic()
                    if now - last_emit >= 0.4:
                        last_emit = now
                        with session_lock:
                            _emit_aggregate_progress_locked(written)
        except BaseException as exc:
            stream_exc = exc

        # File handle is guaranteed closed here; hold session_lock continuously through
        # active_writers decrement, byte-coverage verification, and atomic finalization.
        cleanup_tmp = False
        with session_lock:
            sess["active_writers"] = max(0, sess["active_writers"] - 1)
            is_cancelled = cancelled or self._is_upload_cancelled(session_id)
            if is_cancelled:
                if sess["active_writers"] == 0:
                    cleanup_tmp = True
                    with self._upload_state_lock:
                        self._parallel_sessions.pop(session_id, None)
            elif stream_exc is not None:
                if chunk_index not in sess["received_chunks"]:
                    sess["chunk_progress"][chunk_index] = 0
                    sess["chunk_ranges"].pop(chunk_index, None)
                raise stream_exc
            else:
                short_read = expected_cl is not None and written != expected_cl
                empty_nonzero = file_size > 0 and written == 0
                if overflow or short_read or empty_nonzero:
                    if chunk_index not in sess["received_chunks"]:
                        sess["chunk_progress"][chunk_index] = 0
                        sess["chunk_ranges"].pop(chunk_index, None)
                    err_msg = (
                        "Chunk exceeds declared X-File-Size"
                        if overflow
                        else ("Empty chunk not allowed for non-empty file" if empty_nonzero else "Incomplete chunk payload")
                    )
                    return jsonify({"error": err_msg}), 400

                sess["received_chunks"].add(chunk_index)
                sess["chunk_progress"][chunk_index] = written
                sess["chunk_ranges"][chunk_index] = (offset, offset + written)

                if (
                    len(sess["received_chunks"]) == sess["total_chunks"]
                    and sess["active_writers"] == 0
                    and not sess["finalized"]
                ):
                    actual_size = os.path.getsize(temp_path) if os.path.isfile(temp_path) else -1
                    if actual_size != sess["file_size"] or not self._ranges_cover_file(
                        sess["chunk_ranges"].values(), sess["file_size"]
                    ):
                        return jsonify({"error": "Parallel chunks do not cover full file size"}), 400
                    if self._is_upload_cancelled(session_id):
                        cleanup_tmp = True
                        with self._upload_state_lock:
                            self._parallel_sessions.pop(session_id, None)
                    else:
                        sess["finalized"] = True
                        file_info = self._complete_received_upload(
                            session_id=session_id,
                            temp_path=temp_path,
                            dest_dir=sess["dest_dir"],
                            filename=sess["filename"],
                            peer=peer,
                            log_template=log_template,
                        )
                        return jsonify({"success": True, "completed": True, "file": file_info})
                else:
                    sess["last_active"] = time.time()
                    _emit_aggregate_progress_locked(written)
                    if self._is_upload_cancelled(session_id) and sess["active_writers"] == 0:
                        cleanup_tmp = True
                        with self._upload_state_lock:
                            self._parallel_sessions.pop(session_id, None)
                    else:
                        return jsonify({"success": True, "completed": False, "chunk_index": chunk_index})

        if cleanup_tmp:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
        return jsonify({"success": False, "cancelled": True}), 499

    def _register_routes(self):
        app = self.app

        @app.before_request
        def only_lan_hostnames():
            # DNS rebinding: a website's own domain re-pointed at this PC must not reach the API.
            if not is_lan_host(request.host):
                return jsonify({"error": "Unknown host name"}), 421
            # CSRF guard: reject cross-site browser mutations from non-LAN origins (including Origin: null).
            origin = request.headers.get("Origin")
            if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
                from urllib.parse import urlsplit
                origin_host = urlsplit(origin).netloc
                if not origin_host or not is_lan_host(origin_host):
                    return jsonify({"error": "Cross-origin request denied"}), 403

        @app.route("/")
        def index():
            self.record_client_seen(request.remote_addr, request.headers.get("User-Agent", ""))
            self.log_activity(f"Device connected from {request.remote_addr}", "info")
            return render_template("index.html")

        @app.route("/finder")
        def finder():
            self.log_activity(f"Subnet scanner accessed from {request.remote_addr}", "info")
            return render_template("finder.html")

        @app.route("/api/info")
        def api_info():
            self.record_client_seen(request.remote_addr, request.headers.get("User-Agent", ""))
            urls = get_connection_urls(self.port)
            safe_folders = config.get_safe_folders()
            return jsonify({
                "app": "NearBeam",
                "hostname": urls["hostname"],
                "nearbeam_url": urls["nearbeam_url"],
                "landrop_url": urls["nearbeam_url"],  # Legacy alias
                "mdns_url": urls["mdns_url"],
                "primary_ip": urls["primary_ip"],
                "primary_url": urls["primary_url"],
                "all_urls": urls["all_urls"],
                "port": self.port,
                "auto_save": config.auto_save_enabled,
                "save_directory": config.save_directory,
                "safe_folders_count": len(safe_folders),
                "has_clipboard": bool(self.clipboard_content),
                "version": APP_VERSION,
                "mesh_role": self.mesh.role if self.mesh else "standalone",
                "mesh_nodes": self.mesh.get_nodes_list() if self.mesh else [],
                "nearby_devices": self.get_nearby_devices(),
            })

        @app.route("/api/nearby")
        def api_nearby():
            self.record_client_seen(request.remote_addr, request.headers.get("User-Agent", ""))
            return jsonify({
                "role": self.mesh.role if self.mesh else "standalone",
                "devices": self.get_nearby_devices(),
            })

        @app.route("/api/nearby/heartbeat", methods=["POST"])
        def api_nearby_heartbeat():
            data, err = _parse_json_dict()
            if err:
                return err
            device_name = data.get("device_name", "")
            if not isinstance(device_name, str):
                device_name = ""
            self.record_client_seen(
                request.remote_addr,
                request.headers.get("User-Agent", ""),
                custom_name=device_name,
            )
            return jsonify({"success": True, "devices": self.get_nearby_devices()})

        @app.route("/api/version")
        def api_version():
            return jsonify(check_for_updates())

        # --- Safe List Endpoints ---

        @app.route("/api/safelist")
        def api_safelist():
            """Returns list of all exposed Safe List folders and shared files."""
            result = []
            for folder in config.get_safe_folders():
                folder_path = folder["path"]
                is_file = os.path.isfile(folder_path)
                exists = os.path.exists(folder_path)
                item_count = 1 if is_file else 0
                entry = {
                    "id": folder["id"],
                    "name": folder["name"],
                    "path": folder_path,
                    "exists": exists,
                    "is_file": is_file,
                    "item_count": item_count,
                }
                if exists and is_file:
                    try:
                        st = os.stat(folder_path)
                        entry["size"] = st.st_size
                        entry["formatted_size"] = format_size(st.st_size)
                        entry["modified"] = _format_mtime(st.st_mtime)
                        entry["category"] = get_file_type_category(folder["name"])
                        entry["has_thumb"] = is_thumbnailable(folder_path)
                    except OSError:
                        entry["category"] = get_file_type_category(folder["name"])
                        entry["has_thumb"] = is_thumbnailable(folder_path)
                elif exists and not is_file:
                    try:
                        entry["item_count"] = len(os.listdir(folder_path))
                    except Exception:
                        entry["item_count"] = 0

                result.append(entry)
            return jsonify({"folders": result})

        @app.route("/api/safelist/browse")
        def api_safelist_browse():
            """
            Browses files and subdirectories inside a safe folder (or single shared file).
            Query params: folder_id (required), subpath (optional relative path)
            """
            folder_id = request.args.get("folder_id", "")
            subpath = request.args.get("subpath", "").strip()

            if not folder_id:
                return jsonify({"error": "folder_id is required"}), 400

            safe_folder = config.get_safe_folder_by_id(folder_id)
            if not safe_folder:
                return jsonify({"error": "Safe folder not found"}), 404

            try:
                real_target = config.resolve_safe_path(folder_id, subpath)
            except PermissionError as pe:
                return jsonify({"error": str(pe)}), 403
            except (FileNotFoundError, KeyError) as fe:
                return jsonify({"error": str(fe)}), 404

            if os.path.isfile(real_target):
                stat = os.stat(real_target)
                fname = os.path.basename(real_target)
                return jsonify({
                    "folder_id": folder_id,
                    "folder_name": safe_folder["name"],
                    "subpath": "",
                    "breadcrumbs": [{"name": safe_folder["name"], "subpath": ""}],
                    "items": [{
                        "name": fname,
                        "is_dir": False,
                        "size": stat.st_size,
                        "formatted_size": format_size(stat.st_size),
                        "subpath": fname,
                        "modified": _format_mtime(stat.st_mtime),
                        "category": get_file_type_category(fname),
                        "has_thumb": is_thumbnailable(fname),
                    }],
                })

            if not os.path.exists(real_target):
                return jsonify({"error": "Path not found"}), 404

            if not os.path.isdir(real_target):
                return jsonify({"error": "Path is not a directory"}), 400

            # Build breadcrumbs
            norm_subpath = subpath.replace("\\", "/").strip("/")
            parts = [p for p in norm_subpath.split("/") if p]
            breadcrumbs = [{"name": safe_folder["name"], "subpath": ""}]
            accum = []
            for part in parts:
                accum.append(part)
                breadcrumbs.append({"name": part, "subpath": "/".join(accum)})

            # Read directory entries
            dirs = []
            files = []
            try:
                with os.scandir(real_target) as entries:
                    for entry in entries:
                        try:
                            # Skip hidden files
                            if entry.name.startswith("."):
                                continue

                            rel_entry_subpath = f"{norm_subpath}/{entry.name}" if norm_subpath else entry.name
                            stat = entry.stat()
                            mtime = _format_mtime(stat.st_mtime)

                            if entry.is_dir(follow_symlinks=False):
                                dirs.append({
                                    "name": entry.name,
                                    "is_dir": True,
                                    "subpath": rel_entry_subpath,
                                    "modified": mtime,
                                    "category": "folder",
                                })
                            elif entry.is_file(follow_symlinks=False):
                                size = stat.st_size
                                files.append({
                                    "name": entry.name,
                                    "is_dir": False,
                                    "size": size,
                                    "formatted_size": format_size(size),
                                    "subpath": rel_entry_subpath,
                                    "modified": mtime,
                                    "category": get_file_type_category(entry.name),
                                    "has_thumb": is_thumbnailable(entry.name),
                                })
                        except (PermissionError, OSError):
                            continue
            except PermissionError:
                return jsonify({"error": "Permission denied reading folder"}), 403

            # Sort alphabetically (folders first, then files)
            dirs.sort(key=lambda x: x["name"].lower())
            files.sort(key=lambda x: x["name"].lower())

            return jsonify({
                "folder_id": folder_id,
                "folder_name": safe_folder["name"],
                "subpath": norm_subpath,
                "breadcrumbs": breadcrumbs,
                "items": dirs + files,
            })

        @app.route("/api/safelist/download")
        def api_safelist_download():
            """Downloads a file from a Safe List folder with HTTP 206 resume support."""
            folder_id = request.args.get("folder_id", "")
            subpath = request.args.get("subpath", "")

            try:
                real_target = config.resolve_safe_path(folder_id, subpath)
            except PermissionError:
                return jsonify({"error": "Access denied"}), 403
            except Exception as e:
                return jsonify({"error": str(e)}), 404

            if not os.path.isfile(real_target):
                return jsonify({"error": "File not found"}), 404

            filename = os.path.basename(real_target)
            self.log_activity(f"File downloaded: {filename} from {request.remote_addr}")
            return self._track_download(
                send_file(real_target, as_attachment=True, download_name=filename, conditional=True), filename
            )

        @app.route("/api/safelist/preview")
        def api_safelist_preview():
            """Streams media/text file inline for in-browser preview."""
            folder_id = request.args.get("folder_id", "")
            subpath = request.args.get("subpath", "")

            try:
                real_target = config.resolve_safe_path(folder_id, subpath)
            except PermissionError:
                return jsonify({"error": "Access denied"}), 403
            except Exception as e:
                return jsonify({"error": str(e)}), 404

            if not os.path.isfile(real_target):
                return jsonify({"error": "File not found"}), 404

            mime_type, _ = mimetypes.guess_type(real_target)
            return send_file(real_target, mimetype=mime_type or "application/octet-stream", conditional=True)

        @app.route("/api/safelist/thumb")
        def api_safelist_thumb():
            """Small cached JPEG thumbnail for shared photos, videos, PDFs, HEIC/RAW, and Office docs."""
            folder_id = request.args.get("folder_id", "")
            subpath = request.args.get("subpath", "")
            try:
                real_target = config.resolve_safe_path(folder_id, subpath)
            except PermissionError:
                return jsonify({"error": "Access denied"}), 403
            except Exception as e:
                return jsonify({"error": str(e)}), 404
            if not os.path.isfile(real_target) or not is_thumbnailable(real_target):
                return jsonify({"error": "No thumbnail"}), 404
            try:
                st = os.stat(real_target)
                data = make_thumbnail(real_target, st.st_mtime_ns, st.st_size)
            except Exception:
                return jsonify({"error": "No thumbnail"}), 404
            if not data:
                return jsonify({"error": "No thumbnail"}), 404
            resp = send_file(io.BytesIO(data), mimetype="image/jpeg")
            resp.headers["Cache-Control"] = "private, max-age=86400"
            return resp

        @app.route("/api/safelist/prepare-zip", methods=["POST"])
        def api_safelist_prepare_zip():
            """Starts background job to package a Safe List folder, subfolder, or selected items into a ZIP."""
            data, err = _parse_json_dict(allow_form=True)
            if err:
                return err
            folder_id = data.get("folder_id", "")
            subpath = data.get("subpath", "")
            selected_items = data.get("items")

            if not isinstance(folder_id, str) or not isinstance(subpath, str):
                return jsonify({"error": "Invalid folder_id or subpath"}), 400

            try:
                real_target = config.resolve_safe_path(folder_id, subpath)
            except PermissionError as pe:
                return jsonify({"error": str(pe)}), 403
            except (FileNotFoundError, KeyError) as fe:
                return jsonify({"error": str(fe)}), 404

            if not os.path.isdir(real_target):
                return jsonify({"error": "Not a directory"}), 400

            folder_name = os.path.basename(real_target) or "shared_folder"
            files_list = []
            total_bytes = 0

            if selected_items is not None:
                if not isinstance(selected_items, list):
                    return jsonify({"error": "items must be a list of strings"}), 400
                if len(selected_items) == 0:
                    return jsonify({"error": "No items selected"}), 400
                seen_files = set()
                for item_sub in selected_items:
                    if not isinstance(item_sub, str) or not item_sub.strip():
                        continue
                    try:
                        item_real = config.resolve_safe_path(folder_id, item_sub)
                    except PermissionError as pe:
                        return jsonify({"error": str(pe)}), 403
                    except Exception as e:
                        return jsonify({"error": str(e)}), 404
                    if not inside_folder(real_target, item_real):
                        return jsonify({"error": "Access denied: item outside current folder"}), 403

                    if os.path.isfile(item_real):
                        if item_real not in seen_files:
                            seen_files.add(item_real)
                            try:
                                f_size = os.path.getsize(item_real)
                            except OSError:
                                continue
                            archive_name = os.path.relpath(item_real, real_target)
                            files_list.append((item_real, archive_name, f_size))
                            total_bytes += f_size
                    elif os.path.isdir(item_real):
                        seen_dirs = {os.path.realpath(item_real)}
                        for root, dirnames, filenames in os.walk(item_real):
                            _filter_walk_dirs(real_target, root, dirnames, seen_dirs)
                            for filename in filenames:
                                file_path = os.path.join(root, filename)
                                if not inside_folder(real_target, file_path):
                                    continue
                                real_f = os.path.realpath(file_path)
                                if real_f in seen_files or not os.path.isfile(real_f):
                                    continue
                                seen_files.add(real_f)
                                try:
                                    f_size = os.path.getsize(real_f)
                                except OSError:
                                    continue
                                archive_name = os.path.relpath(file_path, real_target)
                                files_list.append((real_f, archive_name, f_size))
                                total_bytes += f_size
                if not files_list:
                    return jsonify({"error": "No readable files in selection"}), 400
                return jsonify(self._start_zip_job(f"{folder_name}-selected", files_list, total_bytes))

            # Full folder scan
            seen_dirs = {os.path.realpath(real_target)}
            for root, dirnames, filenames in os.walk(real_target):
                _filter_walk_dirs(real_target, root, dirnames, seen_dirs)
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    if not inside_folder(real_target, file_path):
                        continue  # link/junction to outside the shared folder: browse/download refuse it too
                    try:
                        f_size = os.path.getsize(file_path)
                    except OSError:
                        f_size = 0
                    archive_name = os.path.relpath(file_path, real_target)
                    files_list.append((file_path, archive_name, f_size))
                    total_bytes += f_size

            return jsonify(self._start_zip_job(folder_name, files_list, total_bytes))

        # --- Ephemeral PC -> Devices Send Batches Endpoints ---

        @app.route("/api/batches")
        def api_batches_list():
            return jsonify({"batches": self.batch_store.list_batches()})

        @app.route("/api/batches/<batch_id>/items")
        def api_batches_items(batch_id):
            res = self.batch_store.get_batch_items(batch_id)
            if not res:
                return jsonify({"error": "Batch not found"}), 404
            summary, items = res
            return jsonify({"batch": summary, "items": items})

        @app.route("/api/batches/<batch_id>/download/<item_id>")
        def api_batches_download_item(batch_id, item_id):
            item = self.batch_store.get_item(batch_id, item_id)
            if not item or not os.path.isfile(item["path"]):
                return jsonify({"error": "File is no longer available on the PC"}), 404
            if request.method != "HEAD":
                self.batch_store.increment_downloads(batch_id)
                self.broadcast_event({"type": "batches_updated", "batch_id": batch_id})
                self.log_activity(f"Batch file downloaded: {item['name']} from {request.remote_addr}")
            filename = item["name"]
            return self._track_download(
                send_file(item["path"], as_attachment=True, download_name=filename, conditional=True),
                filename,
            )

        @app.route("/api/batches/<batch_id>/preview/<item_id>")
        def api_batches_preview_item(batch_id, item_id):
            item = self.batch_store.get_item(batch_id, item_id)
            if not item or not os.path.isfile(item["path"]):
                return jsonify({"error": "File is no longer available on the PC"}), 404
            mime_type, _ = mimetypes.guess_type(item["path"])
            return send_file(item["path"], mimetype=mime_type or "application/octet-stream", conditional=True)

        @app.route("/api/batches/<batch_id>/thumb/<item_id>")
        def api_batches_thumb_item(batch_id, item_id):
            item = self.batch_store.get_item(batch_id, item_id)
            if not item or not os.path.isfile(item["path"]) or not is_thumbnailable(item["path"]):
                return jsonify({"error": "No thumbnail"}), 404
            try:
                st = os.stat(item["path"])
                data = make_thumbnail(item["path"], st.st_mtime_ns, st.st_size)
            except Exception:
                return jsonify({"error": "No thumbnail"}), 404
            if not data:
                return jsonify({"error": "No thumbnail"}), 404
            resp = send_file(io.BytesIO(data), mimetype="image/jpeg")
            resp.headers["Cache-Control"] = "private, max-age=86400"
            return resp

        @app.route("/api/batches/<batch_id>/prepare-zip", methods=["POST"])
        def api_batches_prepare_zip(batch_id):
            data, err = _parse_json_dict(allow_form=True)
            if err:
                return err
            item_ids = data.get("item_ids")
            if item_ids is not None:
                if not isinstance(item_ids, list) or not all(isinstance(x, str) for x in item_ids):
                    return jsonify({"error": "item_ids must be a list of strings"}), 400
                if len(item_ids) == 0:
                    return jsonify({"error": "No items selected"}), 400
            collected = self.batch_store.collect_zip_files(batch_id, item_ids)
            if collected is None:
                return jsonify({"error": "Batch not found"}), 404
            label, files_list, total_bytes = collected
            if not files_list:
                return jsonify({"error": "Files are no longer available on the PC"}), 400
            self.batch_store.increment_downloads(batch_id)
            self.broadcast_event({"type": "batches_updated", "batch_id": batch_id})
            return jsonify(self._start_zip_job(label, files_list, total_bytes))

        @app.route("/api/batches/<batch_id>", methods=["DELETE"])
        def api_batches_delete(batch_id):
            removed = self.batch_store.delete_batch(batch_id)
            if removed:
                self.broadcast_event({"type": "batches_updated", "batch_id": batch_id})
            return jsonify({"success": True, "removed": removed})

        @app.route("/api/batches/clear", methods=["POST"])
        def api_batches_clear():
            cleared = self.batch_store.clear()
            if cleared:
                self.broadcast_event({"type": "batches_updated"})
            return jsonify({"success": True, "cleared": cleared})

        @app.route("/api/safelist/zip-progress")
        def api_safelist_zip_progress():
            """Returns real-time compression progress for an active packaging job."""
            job_id = request.args.get("job_id", "")
            with self.zip_jobs_lock:
                job = self.zip_jobs.get(job_id)
                if not job:
                    return jsonify({"error": "Job not found"}), 404

                return jsonify({
                    "job_id": job["job_id"],
                    "folder_name": job["folder_name"],
                    "status": job["status"],
                    "percent": job["percent"],
                    "processed_files": job["processed_files"],
                    "total_files": job["total_files"],
                    "processed_bytes": job["processed_bytes"],
                    "total_bytes": job["total_bytes"],
                    "current_file": job.get("current_file", ""),
                    "error": job.get("error"),
                })

        @app.route("/api/safelist/cancel-zip", methods=["POST"])
        def api_safelist_cancel_zip():
            """Aborts an active packaging job and cleans up temp files."""
            data, err = _parse_json_dict(allow_form=True)
            if err:
                return err
            job_id = data.get("job_id", "")
            if not isinstance(job_id, str):
                return jsonify({"error": "Invalid job_id"}), 400
            with self.zip_jobs_lock:
                job = self.zip_jobs.get(job_id)
                if job:
                    job["cancel_event"].set()
                    job["status"] = "cancelled"
                    temp_path = job.get("temp_path")
                    if temp_path and os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass
            return jsonify({"success": True})

        @app.route("/api/safelist/download-zip-file")
        def api_safelist_download_zip_file():
            """Transfers the completed ZIP file created by a background job."""
            job_id = request.args.get("job_id", "")
            with self.zip_jobs_lock:
                job = self.zip_jobs.get(job_id)
                if not job:
                    return jsonify({"error": "Job not found"}), 404
                if job["status"] != "completed":
                    return jsonify({"error": f"Job not ready (status: {job['status']})"}), 400
                temp_path = job["temp_path"]
                folder_name = job["folder_name"]

            if not os.path.exists(temp_path):
                return jsonify({"error": "Archive file not found on disk"}), 404

            if request.method != "HEAD":
                self.log_activity(f"Folder downloaded as ZIP: {folder_name} by {request.remote_addr}")

                def forget_job():
                    with self.zip_jobs_lock:
                        self.zip_jobs.pop(job_id, None)

                # The job stays (so a retry can re-download) until the file is actually gone.
                threading.Thread(
                    target=remove_when_released, args=(temp_path,), kwargs={"on_removed": forget_job}, daemon=True
                ).start()

            return self._track_download(
                send_file(
                    temp_path,
                    mimetype="application/zip",
                    as_attachment=True,
                    download_name=f"{folder_name}.zip",
                ),
                f"{folder_name}.zip",
            )

        @app.route("/api/safelist/download-zip")
        def api_safelist_download_zip():
            """Packs a Safe List folder or subfolder into a ZIP and sends it (synchronous / fallback)."""
            folder_id = request.args.get("folder_id", "")
            subpath = request.args.get("subpath", "")

            try:
                real_target = config.resolve_safe_path(folder_id, subpath)
            except PermissionError as e:
                return jsonify({"error": str(e)}), 403
            except (FileNotFoundError, KeyError) as e:
                return jsonify({"error": str(e)}), 404

            if not os.path.isdir(real_target):
                return jsonify({"error": "Not a directory"}), 400

            folder_name = os.path.basename(real_target) or "shared_folder"
            temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            temp_zip.close()

            try:
                with zipfile.ZipFile(
                    temp_zip.name,
                    "w",
                    allowZip64=True,
                    compresslevel=1,
                    strict_timestamps=False,
                ) as zip_file:
                    seen_dirs = {os.path.realpath(real_target)}
                    for root, dirnames, filenames in os.walk(real_target):
                        _filter_walk_dirs(real_target, root, dirnames, seen_dirs)
                        for filename in filenames:
                            file_path = os.path.join(root, filename)
                            if not inside_folder(real_target, file_path):
                                continue
                            archive_name = os.path.relpath(file_path, real_target)
                            ext = os.path.splitext(filename)[1].lower()
                            c_type = zipfile.ZIP_STORED if ext in PRECOMPRESSED_EXTENSIONS else zipfile.ZIP_DEFLATED
                            try:
                                zip_file.write(
                                    file_path,
                                    archive_name,
                                    compress_type=c_type,
                                    compresslevel=1 if c_type == zipfile.ZIP_DEFLATED else None,
                                )
                            except (PermissionError, OSError):
                                continue

                self.log_activity(f"Folder downloaded as ZIP: {folder_name} by {request.remote_addr}")
                response = send_file(
                    temp_zip.name,
                    mimetype="application/zip",
                    as_attachment=True,
                    download_name=f"{folder_name}.zip",
                )

                threading.Thread(target=remove_when_released, args=(temp_zip.name,), daemon=True).start()
                return response
            except Exception as e:
                try:
                    if os.path.exists(temp_zip.name):
                        os.remove(temp_zip.name)
                except Exception:
                    pass
                return jsonify({"error": str(e)}), 500

        # --- Auto-Save Upload Endpoints ---


        # --- Guest Drop Zone Endpoints ---

        @app.route("/api/drop/create", methods=["POST"])
        def api_drop_create():
            if not config.auto_save_enabled:
                return jsonify({"error": "Auto-Save is disabled on host"}), 403
            token = str(uuid.uuid4())
            now = time.time()
            # Token valid for 24 hours
            expires_at = now + (24 * 3600)
            with self._drop_zones_lock:
                self._prune_drop_zones_locked(now)
                self.active_drop_zones[token] = {"expires_at": expires_at}

            urls = get_connection_urls(self.port)
            # nearbeam.local is the mesh leader; a link made on another PC must point here.
            is_leader = not self.mesh or self.mesh.role == "leader"
            drop_url = f"{urls['nearbeam_url'] if is_leader else urls['primary_url']}/drop/{token}"
            return jsonify({"success": True, "token": token, "url": drop_url})

        def _is_valid_drop_token(token: str) -> bool:
            now = time.time()
            with self._drop_zones_lock:
                self._prune_drop_zones_locked(now)
                meta = self.active_drop_zones.get(token)
                return bool(meta and now <= meta.get("expires_at", 0))

        @app.route("/drop/<token>")
        def drop_page(token):
            if not _is_valid_drop_token(token):
                return "Drop link expired or invalid", 404
            return render_template("drop.html", token=token)

        @app.route("/api/drop/<token>/upload/chunk", methods=["POST"])
        def api_drop_upload_chunk(token):
            if not config.auto_save_enabled:
                return jsonify({"error": "Auto-Save is disabled on host"}), 403
            if not _is_valid_drop_token(token):
                return jsonify({"error": "Drop link expired"}), 403
            return self._receive_chunk(
                os.path.join(config.save_directory, "Guest Drops"),
                f"Guest Drop ({request.remote_addr})",
                "Guest dropped '{name}' ({size})",
                is_guest_drop=True,
            )

        @app.route("/api/upload", methods=["POST"])
        def api_upload():
            """
            Handles incoming file uploads.
            Auto-saves directly to the host PC's configured save directory.
            """
            if not config.auto_save_enabled:
                return jsonify({"error": "Auto-Save is disabled on host"}), 403

            if "files" not in request.files and "file" not in request.files:
                return jsonify({"error": "No file uploaded"}), 400

            uploaded_files = request.files.getlist("files") or request.files.getlist("file")
            saved_list = []

            for f in uploaded_files:
                if not f or not f.filename:
                    continue

                original_name = f.filename
                clean_name = safe_filename(original_name)
                save_dir = config.save_directory
                dest_dir = resolve_receive_dir(
                    save_dir,
                    filename=clean_name,
                    relative_dir="",
                    peer_name=self._resolve_peer_device_label(),
                    mode=config.auto_organize_mode,
                    is_guest_drop=False,
                )
                os.makedirs(save_dir, exist_ok=True)
                temp_path = os.path.join(save_dir, f".{uuid.uuid4().hex}.tmp")

                # Stream to a temp file first: an interrupted upload must not leave a
                # truncated file under its real name.
                total_bytes = 0
                buf_size = 8 * 1024 * 1024
                try:
                    with open(temp_path, "wb", buffering=buf_size) as out_file:
                        while True:
                            chunk = f.stream.read(buf_size)
                            if not chunk:
                                break
                            out_file.write(chunk)
                            total_bytes += len(chunk)
                    dest_path = finalize_upload(temp_path, dest_dir, clean_name)
                except Exception:
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except OSError:
                            pass
                    raise
                final_name = os.path.basename(dest_path)

                file_info = {
                    "filename": final_name,
                    "original_name": original_name,
                    "size": total_bytes,
                    "formatted_size": format_size(total_bytes),
                    "saved_path": dest_path,
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "category": get_file_type_category(final_name),
                }
                saved_list.append(file_info)
                with self._upload_state_lock:
                    self.received_files_history.insert(0, file_info)
                    # Keep history reasonable
                    if len(self.received_files_history) > 100:
                        self.received_files_history.pop()
                config.add_transfer_history({
                    **file_info,
                    "direction": "received",
                    "peer": request.remote_addr or "Local Network",
                    "status": "completed",
                })

                # Notify GUI and SSE
                msg = f"Auto-saved '{final_name}' ({format_size(total_bytes)}) from {request.remote_addr}"
                self.log_activity(msg, event_type="upload")
                self.broadcast_event({
                    "type": "file_received",
                    "file": file_info,
                })

            return jsonify({
                "success": True,
                "saved_count": len(saved_list),
                "saved_files": saved_list,
            })


        @app.route("/api/upload/chunk", methods=["POST"])
        def api_upload_chunk():
            if not config.auto_save_enabled:
                return jsonify({"error": "Auto-Save is disabled on host"}), 403
            return self._receive_chunk(
                config.save_directory,
                request.remote_addr or "Local Network",
                f"Auto-saved '{{name}}' ({{size}}) from {request.remote_addr}",
            )

        @app.route("/api/upload/status/<session_id>")
        def api_upload_status(session_id):
            if not _SESSION_ID_RE.fullmatch(session_id):
                return jsonify({"received_bytes": 0})
            with self._upload_state_lock:
                finished = self.finished_uploads.get(session_id)
                psess = self._parallel_sessions.get(session_id)
            if finished:
                return jsonify({
                    "received_bytes": finished.get("size", 0),
                    "received_chunks": [],
                    "completed": True,
                    "file": finished,
                })
            if psess is not None:
                session_lock = self._get_session_lock(session_id)
                with session_lock:
                    with self._upload_state_lock:
                        finished_now = self.finished_uploads.get(session_id)
                    if finished_now:
                        return jsonify({
                            "received_bytes": finished_now.get("size", 0),
                            "received_chunks": [],
                            "completed": True,
                            "file": finished_now,
                        })
                    recv_chunks = sorted(psess["received_chunks"])
                    recv_bytes = sum(psess["chunk_progress"].values())
                return jsonify({
                    "received_bytes": recv_bytes,
                    "received_chunks": recv_chunks,
                    "completed": False,
                })
            session_lock = self._get_session_lock(session_id)
            with session_lock:
                with self._upload_state_lock:
                    finished_now = self.finished_uploads.get(session_id)
                if finished_now:
                    return jsonify({
                        "received_bytes": finished_now.get("size", 0),
                        "received_chunks": [],
                        "completed": True,
                        "file": finished_now,
                    })
                candidates = [
                    os.path.join(config.save_directory, f".{session_id}.tmp"),
                    os.path.join(config.save_directory, "Guest Drops", f".{session_id}.tmp"),
                    os.path.join(tempfile.gettempdir(), "nearbeam_uploads", f"{session_id}.tmp"),
                ]
                for temp_path in candidates:
                    try:
                        size = os.path.getsize(temp_path)
                        return jsonify({"received_bytes": size, "received_chunks": [], "completed": False})
                    except OSError:
                        continue
            return jsonify({"received_bytes": 0, "received_chunks": [], "completed": False})

        @app.route("/api/upload/cancel", methods=["POST"])
        def api_upload_cancel():
            data, err = _parse_json_dict()
            if err:
                return err
            session_id = data.get("session_id", "")
            removed_tmp = False
            if isinstance(session_id, str) and session_id and _SESSION_ID_RE.fullmatch(session_id):
                with self._upload_state_lock:
                    self._cancelled_uploads[session_id] = time.time()
                    if len(self._cancelled_uploads) > 500:
                        self._cancelled_uploads.popitem(last=False)
                    is_parallel = session_id in self._parallel_sessions
                candidates = [
                    os.path.join(config.save_directory, f".{session_id}.tmp"),
                    os.path.join(config.save_directory, "Guest Drops", f".{session_id}.tmp"),
                    os.path.join(tempfile.gettempdir(), "nearbeam_uploads", f"{session_id}.tmp"),
                ]
                session_lock = self._get_session_lock(session_id)
                # Parallel writers never hold session_lock across stream I/O, so a short bounded wait
                # prevents momentary status-poll/progress locks from skipping cleanup.
                acquired = session_lock.acquire(timeout=0.2) if is_parallel else session_lock.acquire(blocking=False)
                try:
                    can_delete = acquired
                    if acquired:
                        with self._upload_state_lock:
                            psess = self._parallel_sessions.get(session_id)
                            if psess is not None:
                                if psess.get("active_writers", 0) == 0:
                                    self._parallel_sessions.pop(session_id, None)
                                    tp = psess.get("temp_path")
                                    if tp and tp not in candidates:
                                        candidates.insert(0, tp)
                                else:
                                    can_delete = False
                    if can_delete:
                        for temp_path in candidates:
                            if os.path.exists(temp_path):
                                try:
                                    os.remove(temp_path)
                                    removed_tmp = True
                                except OSError:
                                    pass
                finally:
                    if acquired:
                        session_lock.release()
                self.broadcast_event({
                    "type": "upload_cancelled",
                    "session_id": session_id,
                })
            return jsonify({"success": True, "removed_tmp": removed_tmp})

        @app.route("/api/history")
        def api_history():
            """Returns persistent transfer history."""
            return jsonify({"history": config.get_transfer_history()})

        @app.route("/api/history/clear", methods=["POST"])
        def api_history_clear():
            """Clears persistent transfer history."""
            config.clear_transfer_history()
            with self._upload_state_lock:
                self.received_files_history.clear()
            return jsonify({"success": True})

        @app.route("/api/shell-send", methods=["POST"])
        def api_shell_send():
            """
            Handles files or folders sent from Windows Explorer right-click ('Send with NearBeam').
            - Directories are added to permanent Shared Folders so Explorer folder-sharing works immediately.
            - Files create (or merge within 2s into) an ephemeral Send Batch without cluttering Safe List.
            """
            # Only the PC itself (Explorer "Send with NearBeam") may add paths: from the
            # network this would let anyone share, then download, any folder on the PC.
            if request.remote_addr not in ("127.0.0.1", "::1"):
                return jsonify({"error": "Only available on this PC"}), 403
            data, err = _parse_json_dict()
            if err:
                return err
            paths = data.get("paths", [])
            if not isinstance(paths, list) or not paths:
                return jsonify({"error": "No paths provided"}), 400

            dir_paths = [p for p in paths if isinstance(p, str) and p.strip() and os.path.isdir(p)]
            file_paths = [p for p in paths if isinstance(p, str) and p.strip() and os.path.isfile(p)]

            added_folders = []
            if dir_paths:
                added_folders = config.add_safe_folders(dir_paths)
                for folder in added_folders:
                    self.log_activity(f"Shared folder from Explorer: {folder['name']}", event_type="info")
                self.broadcast_event({"type": "safelist_updated"})

            batch = None
            if file_paths:
                batch = self.create_send_batch(file_paths, source="Explorer", merge_window=2.0)

            if not dir_paths and not batch:
                return jsonify({"error": "No readable files or folders found in provided paths"}), 400

            resp = {
                "success": True,
                "added_count": len(added_folders) + (batch["file_count"] if batch else 0),
                "added_folders": [f["name"] for f in added_folders],
            }
            if batch:
                resp["batch"] = batch
            return jsonify(resp)

        def _resolve_received_file(filepath: str) -> Optional[str]:
            save_dir = config.save_directory
            norm = (filepath or "").replace("\\", "/").lstrip("/")
            parts = [p for p in norm.split("/") if p not in ("", ".")]
            if not parts or any(p == ".." or p.startswith(".") or ":" in p for p in parts):
                return None
            target = os.path.join(save_dir, *parts)
            root_real = os.path.realpath(save_dir)
            if not inside_folder(root_real, target):
                return None
            real_target = os.path.realpath(target)
            if not os.path.isfile(real_target):
                return None
            return real_target

        @app.route("/api/received")
        def api_received():
            """Lists files in the auto-save directory (including auto-organized subfolders)."""
            save_dir = config.save_directory
            files = []
            if os.path.exists(save_dir) and os.path.isdir(save_dir):
                root_real = os.path.realpath(save_dir)
                seen_dirs = {root_real}
                try:
                    for walk_root, dirnames, filenames in os.walk(save_dir):
                        _filter_walk_dirs(root_real, walk_root, dirnames, seen_dirs, skip_hidden=True)
                        for fname in filenames:
                            if fname.startswith("."):
                                continue
                            fpath = os.path.join(walk_root, fname)
                            if not inside_folder(root_real, fpath):
                                continue
                            try:
                                stat = os.stat(fpath)
                            except OSError:
                                continue
                            rel = os.path.relpath(fpath, save_dir).replace("\\", "/")
                            folder = os.path.dirname(rel).replace("\\", "/")
                            size = stat.st_size
                            files.append({
                                "filename": fname,
                                "rel_path": rel,
                                "folder": folder if folder != "." else "",
                                "size": size,
                                "formatted_size": format_size(size),
                                "modified": _format_mtime(stat.st_mtime),
                                "mtime": stat.st_mtime,
                                "category": get_file_type_category(fname),
                                "has_thumb": is_thumbnailable(fname),
                            })
                except Exception as e:
                    print(f"Error scanning save directory: {e}")

            files.sort(key=lambda x: x.get("mtime", 0), reverse=True)
            return jsonify({"files": files, "save_directory": save_dir})

        @app.route("/api/received/download/<path:filename>")
        def api_received_download(filename: str):
            """Downloads an auto-saved file from the save directory (or an organized subfolder)."""
            target = _resolve_received_file(filename)
            if not target:
                return jsonify({"error": "File not found"}), 404
            safe_name = os.path.basename(target)
            return self._track_download(
                send_file(target, as_attachment=True, download_name=safe_name, conditional=True), safe_name
            )

        @app.route("/api/received/preview/<path:filename>")
        def api_received_preview(filename: str):
            """Streams an auto-saved file inline for preview."""
            target = _resolve_received_file(filename)
            if not target:
                return jsonify({"error": "File not found"}), 404
            mime_type, _ = mimetypes.guess_type(target)
            return send_file(target, mimetype=mime_type or "application/octet-stream", conditional=True)

        # --- Shared Clipboard Endpoints (Text + Rich Images / Screenshots NB-07) ---

        @app.route("/api/clipboard", methods=["GET", "POST"])
        def api_clipboard():
            if request.method == "POST":
                data, err = _parse_json_dict()
                if err:
                    return err
                text = data.get("text")
                image_b64 = data.get("image_base64")
                image_mime = data.get("image_mime", "image/png")
                if not isinstance(image_mime, str) or not image_mime.strip():
                    image_mime = "image/png"

                if image_b64 is not None:
                    if not isinstance(image_b64, str):
                        return jsonify({"error": "Invalid base64 image payload"}), 400
                    if image_b64:
                        import base64
                        if "," in image_b64 and image_b64.startswith("data:"):
                            header, image_b64 = image_b64.split(",", 1)
                            if ";" in header and ":" in header:
                                image_mime = header.split(":", 1)[1].split(";", 1)[0] or "image/png"
                        try:
                            img_bytes = base64.b64decode(image_b64)
                        except Exception:
                            return jsonify({"error": "Invalid base64 image payload"}), 400
                        state = self.set_clipboard_image(
                            img_bytes,
                            mime_type=image_mime,
                            notify_local_os=True,
                            propagate_mesh=True,
                        )
                        self.log_activity(
                            f"Clipboard image ({format_size(len(img_bytes))}) synced from {request.remote_addr}",
                            "upload",
                        )
                        return jsonify({"success": True, **state})

                if text is not None:
                    if not isinstance(text, str):
                        return jsonify({"error": "text must be a string"}), 400
                    with self._clipboard_lock:
                        self.clipboard_content = text
                        self.clipboard_updated_at = datetime.datetime.now().strftime("%H:%M:%S")

                    snippet = (text[:35] + "...") if len(text) > 35 else text
                    self.log_activity(f"Clipboard updated from {request.remote_addr}: '{snippet}'")
                    state = self.get_clipboard_state()
                    self.broadcast_event({
                        "type": "clipboard_updated",
                        **state,
                    })

                    if self.mesh:
                        self.mesh.broadcast_clipboard_mesh(text)

                    for cb in self.clipboard_callbacks:
                        try:
                            cb(text)
                        except Exception as e:
                            print(f"Error in clipboard callback: {e}")

                return jsonify({"success": True, **self.get_clipboard_state()})

            return jsonify(self.get_clipboard_state())

        @app.route("/api/clipboard/image", methods=["GET", "POST"])
        def api_clipboard_image():
            """GET streams the current clipboard image; POST uploads a screenshot/image to the shared clipboard."""
            if request.method == "POST":
                img_bytes = None
                mime_type = "image/png"

                if "image" in request.files or "file" in request.files:
                    f = request.files.get("image") or request.files.get("file")
                    if f:
                        img_bytes = f.read()
                        mime_type = f.mimetype or "image/png"
                elif request.content_type and request.content_type.startswith("image/"):
                    img_bytes = request.get_data()
                    mime_type = request.content_type.split(";")[0].strip()
                else:
                    data, err = _parse_json_dict()
                    if err:
                        return err
                    b64 = data.get("image_base64", "")
                    mime_type = data.get("image_mime", "image/png")
                    if not isinstance(mime_type, str) or not mime_type.strip():
                        mime_type = "image/png"
                    if b64:
                        if not isinstance(b64, str):
                            return jsonify({"error": "Invalid base64 image"}), 400
                        import base64
                        if "," in b64 and b64.startswith("data:"):
                            header, b64 = b64.split(",", 1)
                            if ";" in header and ":" in header:
                                mime_type = header.split(":", 1)[1].split(";", 1)[0] or "image/png"
                        try:
                            img_bytes = base64.b64decode(b64)
                        except Exception:
                            return jsonify({"error": "Invalid base64 image"}), 400

                if not img_bytes:
                    return jsonify({"error": "No image data provided"}), 400

                state = self.set_clipboard_image(
                    img_bytes,
                    mime_type=mime_type,
                    notify_local_os=True,
                    propagate_mesh=True,
                )
                self.log_activity(
                    f"Clipboard image ({format_size(len(img_bytes))}) synced from {request.remote_addr}",
                    "upload",
                )
                return jsonify({"success": True, **state})

            with self._clipboard_lock:
                img_bytes = self.clipboard_image_bytes
                img_mime = self.clipboard_image_mime or "image/png"
                img_id = self.clipboard_image_id or "image"

            if not img_bytes:
                return jsonify({"error": "No image in clipboard"}), 404

            ext = "png"
            if "jpeg" in img_mime or "jpg" in img_mime:
                ext = "jpg"
            elif "webp" in img_mime:
                ext = "webp"
            elif "gif" in img_mime:
                ext = "gif"

            as_download = request.args.get("download") == "1"
            return send_file(
                io.BytesIO(img_bytes),
                mimetype=img_mime,
                as_attachment=as_download,
                download_name=f"nearbeam_clipboard_{img_id}.{ext}",
            )

        @app.route("/api/clipboard/image/clear", methods=["POST"])
        def api_clipboard_image_clear():
            """Clears the current image from the shared clipboard."""
            with self._clipboard_lock:
                self.clipboard_image_bytes = None
                self.clipboard_image_id = ""
                self.clipboard_updated_at = datetime.datetime.now().strftime("%H:%M:%S")
            state = self.get_clipboard_state()
            self.broadcast_event({
                "type": "clipboard_updated",
                **state,
            })
            return jsonify({"success": True, **state})

        # --- Mesh / Cluster Endpoints ---

        @app.route("/api/mesh/nodes")
        def api_mesh_nodes():
            """Returns list of all active cluster nodes."""
            if not self.mesh:
                return jsonify({"role": "standalone", "nodes": []})
            return jsonify({
                "role": self.mesh.role,
                "nodes": self.mesh.get_nodes_list(),
            })

        @app.route("/api/mesh/register", methods=["POST"])
        def api_mesh_register():
            """Secondary nodes register with Primary Leader."""
            data, err = _parse_json_dict()
            if err:
                return err
            node_id = data.get("node_id")
            ip = data.get("ip")
            if not isinstance(node_id, str) or not node_id.strip() or not isinstance(ip, str) or not ip.strip():
                return jsonify({"error": "Invalid node registration data"}), 400

            if self.mesh:
                if self.mesh.role != "leader":
                    return jsonify({
                        "error": "Not leader",
                        "role": self.mesh.role,
                        "leader": self.mesh.leader_info,
                    }), 409
                if not self.mesh.register_node(data):
                    return jsonify({
                        "error": "Not leader",
                        "role": self.mesh.role,
                        "leader": self.mesh.leader_info,
                    }), 409
                self.log_activity(f"Secondary Node joined cluster: {data.get('name')} ({ip})", "info")
                self.broadcast_event({
                    "type": "mesh_nodes_changed",
                    "nodes": self.mesh.get_nodes_list(),
                })
                return jsonify({
                    "success": True,
                    "leader_id": self.mesh.node_id,
                    "leader_name": self.mesh.device_name,
                    "leader_role": self.mesh.role,
                    "leader_started_at": self.mesh.started_at,
                })
            return jsonify({"error": "Mesh not enabled on host"}), 503

        @app.route("/api/mesh/heartbeat", methods=["POST"])
        def api_mesh_heartbeat():
            """Heartbeat ping from active secondary nodes."""
            data, err = _parse_json_dict()
            if err:
                return err
            node_id = data.get("node_id")
            if not isinstance(node_id, str) or not node_id.strip():
                return jsonify({"error": "Missing node_id"}), 400

            if self.mesh:
                if self.mesh.role != "leader":
                    return jsonify({
                        "error": "Not leader",
                        "role": self.mesh.role,
                        "leader": self.mesh.leader_info,
                    }), 409
                updated = self.mesh.update_heartbeat(node_id)
                return jsonify({
                    "status": "ok",
                    "updated": updated,
                    "leader_id": self.mesh.node_id,
                    "leader_name": self.mesh.device_name,
                    "leader_role": self.mesh.role,
                    "leader_started_at": self.mesh.started_at,
                })
            return jsonify({"error": "Mesh not enabled"}), 503

        @app.route("/api/mesh/clipboard-sync", methods=["POST"])
        def api_mesh_clipboard_sync():
            """Receives cross-node clipboard text or image update."""
            data, err = _parse_json_dict()
            if err:
                return err
            text = data.get("text", "")
            if not isinstance(text, str):
                text = ""
            image_b64 = data.get("image_base64")
            if not isinstance(image_b64, str):
                image_b64 = None
            image_mime = data.get("image_mime", "image/png")
            if not isinstance(image_mime, str) or not image_mime.strip():
                image_mime = "image/png"
            source_node_id = data.get("source_node_id")
            if not isinstance(source_node_id, str):
                source_node_id = None

            image_applied = False
            if image_b64:
                import base64
                try:
                    img_bytes = base64.b64decode(image_b64)
                    if img_bytes:
                        self.set_clipboard_image(
                            img_bytes,
                            mime_type=image_mime,
                            notify_local_os=True,
                            propagate_mesh=(self.mesh is not None and self.mesh.role == "leader"),
                            source_node_id=source_node_id,
                        )
                        if self.mesh and hasattr(self.mesh, "note_remote_clipboard"):
                            self.mesh.note_remote_clipboard(image_base64=image_b64)
                        image_applied = True
                        self.log_activity(f"Clipboard image ({format_size(len(img_bytes))}) synced from peer host", "info")
                except Exception:
                    pass

            if text:
                with self._clipboard_lock:
                    changed = text != self.clipboard_content
                    if changed:
                        self.clipboard_content = text
                        self.clipboard_updated_at = datetime.datetime.now().strftime("%H:%M:%S")
                if changed:
                    snippet = (text[:30] + "...") if len(text) > 30 else text
                    self.log_activity(f"Clipboard synced from peer host: '{snippet}'", "info")

                    self.broadcast_event({
                        "type": "clipboard_updated",
                        **self.get_clipboard_state(),
                    })

                    if not image_applied:
                        for cb in self.clipboard_callbacks:
                            try:
                                cb(text)
                            except Exception:
                                pass

                    # If Leader, forward to other secondary nodes first, then record hash.
                    if self.mesh and self.mesh.role == "leader":
                        self.mesh.broadcast_clipboard_mesh(text, source_node_id=source_node_id)
                    if self.mesh and hasattr(self.mesh, "note_remote_clipboard"):
                        self.mesh.note_remote_clipboard(text=text)

            return jsonify({"success": True})

        # --- Real-Time SSE Stream ---

        @app.route("/api/events")
        def api_events():
            """Server-Sent Events endpoint for real-time reactivity."""
            def event_stream():
                client_queue = queue.Queue(maxsize=256)
                with self._events_lock:
                    self.event_subscribers.append(client_queue)
                try:
                    # Initial heartbeat
                    yield f"data: {json.dumps({'type': 'connected'})}\n\n"
                    last_beat = time.monotonic()
                    while not self._stopping.is_set():
                        try:
                            msg = client_queue.get(timeout=1.0)
                            yield f"data: {msg}\n\n"
                            last_beat = time.monotonic()
                        except queue.Empty:
                            if time.monotonic() - last_beat >= 25.0:
                                last_beat = time.monotonic()
                                yield f": heartbeat\n\n"
                finally:
                    with self._events_lock:
                        if client_queue in self.event_subscribers:
                            self.event_subscribers.remove(client_queue)

            return Response(
                event_stream(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        # --- QR Code Endpoint ---

        @app.route("/api/qr")
        def api_qr():
            """Returns QR code image for connection URL (or a Guest Drop Zone link when ?drop=<token> is set)."""
            urls = get_connection_urls(self.port)
            drop_token = request.args.get("drop", "").strip()
            if drop_token:
                if not _is_valid_drop_token(drop_token):
                    return jsonify({"error": "Drop link expired or invalid"}), 404
                is_leader = not self.mesh or self.mesh.role == "leader"
                target_url = f"{urls['nearbeam_url'] if is_leader else urls['primary_url']}/drop/{drop_token}"
            else:
                target_url = urls["primary_url"]
            png_bytes = generate_qr_png_bytes(target_url)
            if png_bytes:
                return Response(png_bytes, mimetype="image/png")
            return jsonify({"error": "QR code unavailable"}), 500


# Backward-compatible alias for existing tests and scripts
LandropServer = NearBeamServer

