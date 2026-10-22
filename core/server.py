"""
Flask Server for NearBeam.
Provides REST and SSE endpoints for Safe List folder exploration,
Auto-Save file uploads, clipboard synchronization, and media previews.
"""

import datetime
import io
import json
import mimetypes
import os
import queue
import re
import sys
import tempfile
import threading
import time
import uuid
import zipfile
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
from werkzeug.serving import WSGIRequestHandler
from werkzeug.wsgi import FileWrapper
import socket

# Enable HTTP/1.1 Keep-Alive, 4 MB file wrapper blocks, and TCP_NODELAY for high-speed LAN transfers
WSGIRequestHandler.protocol_version = "HTTP/1.1"
WSGIRequestHandler.timeout = 60


def _high_speed_file_wrapper(file_obj, buffer_size: int = 4 * 1024 * 1024):
    return FileWrapper(file_obj, buffer_size=buffer_size)


class NearBeamRequestHandler(WSGIRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        except Exception:
            pass

    def make_environ(self):
        env = super().make_environ()
        env["wsgi.file_wrapper"] = _high_speed_file_wrapper
        return env

PRECOMPRESSED_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz", ".iso",
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"
}

from core.config import config
from core.mesh import MeshManager
from core.network import (
    ZeroconfBroadcaster,
    generate_qr_png_bytes,
    get_connection_urls,
    get_hostname,
    get_local_ip,
)
from core.version import APP_VERSION, check_for_updates

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


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
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".heic"}:
        return "image"
    elif ext in {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv"}:
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
        CORS(self.app)

        # In-memory shared state
        self.clipboard_content = ""
        self.clipboard_updated_at = None
        self.clipboard_image_bytes: Optional[bytes] = None
        self.clipboard_image_mime: str = "image/png"
        self.clipboard_image_id: str = ""
        self.received_files_history = []
        self.connected_clients: Dict[str, Dict[str, Any]] = {}
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

        # Active background ZIP packaging jobs
        self.zip_jobs: Dict[str, Dict[str, Any]] = {}
        self.zip_jobs_lock = threading.Lock()

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
        existing = self.connected_clients.get(ip)
        is_new = existing is None or (now - existing.get("last_seen", 0) > 60)
        info = self._parse_client_info(ip, ua, custom_name)
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
        stale_ips = []
        for ip, c in list(self.connected_clients.items()):
            if now - c.get("last_seen", 0) > 900:
                stale_ips.append(ip)
                continue
            if ip not in mesh_ips:
                devices.append(c)

        for ip in stale_ips:
            self.connected_clients.pop(ip, None)

        return devices

    def shutdown(self):
        """Stops background workers and unregisters mDNS services."""
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
                    temp_path = job.get("temp_path")
                    if temp_path and os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass
                self.zip_jobs.clear()

    def _cleanup_old_zip_jobs(self):
        """Removes zip jobs older than 30 minutes to prevent temp disk buildup."""
        now = time.time()
        to_delete = []
        with self.zip_jobs_lock:
            for jid, job in self.zip_jobs.items():
                if now - job.get("created_at", now) > 1800:
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
                            zinfo = zipfile.ZipInfo.from_file(file_path, archive_name)
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

            with self.zip_jobs_lock:
                job = self.zip_jobs.get(job_id)
                if job:
                    if job.get("cancel_event", threading.Event()).is_set():
                        job["status"] = "cancelled"
                        try:
                            if os.path.exists(temp_zip_path):
                                os.remove(temp_zip_path)
                        except Exception:
                            pass
                    else:
                        job["status"] = "completed"
                        job["percent"] = 100.0
                        job["processed_files"] = len(files_list)
                        job["processed_bytes"] = job["total_bytes"]
                        job["current_file"] = "Complete"
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
        self.log_activity(f"Mesh role established: {role.upper()}", "info")
        if role == "leader":
            if hasattr(self, "broadcaster") and self.broadcaster and not self.broadcaster.zc:
                self.broadcaster.start()
        elif role == "node":
            if hasattr(self, "broadcaster") and self.broadcaster and self.broadcaster.zc:
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
        if text and text != self.clipboard_content:
            self.clipboard_content = text
            self.clipboard_updated_at = datetime.datetime.now().strftime("%H:%M:%S")
            self.broadcast_event({
                "type": "clipboard_updated",
                "text": text,
                "updated_at": self.clipboard_updated_at,
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
        has_img = bool(self.clipboard_image_bytes)
        img_size = len(self.clipboard_image_bytes) if self.clipboard_image_bytes else 0
        return {
            "text": self.clipboard_content,
            "updated_at": self.clipboard_updated_at,
            "has_image": has_img,
            "image_id": self.clipboard_image_id if has_img else None,
            "image_url": f"/api/clipboard/image?id={self.clipboard_image_id}" if has_img else None,
            "image_mime": self.clipboard_image_mime if has_img else None,
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

        self.clipboard_image_bytes = image_bytes
        self.clipboard_image_mime = mime_type or "image/png"
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
                    cb(image_bytes, self.clipboard_image_mime)
                except Exception as e:
                    print(f"Error in clipboard image callback: {e}")

        if propagate_mesh and self.mesh and image_bytes:
            b64_str = base64.b64encode(image_bytes).decode("ascii")
            self.mesh.broadcast_clipboard_mesh(
                text=self.clipboard_content,
                source_node_id=source_node_id,
                image_base64=b64_str,
                image_mime=self.clipboard_image_mime,
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
        for q in list(self.event_subscribers):
            try:
                q.put_nowait(payload)
            except Exception:
                if q in self.event_subscribers:
                    self.event_subscribers.remove(q)

    def _receive_chunk(self, save_dir: str, peer: str, log_template: str):
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
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid chunk headers"}), 400
        if chunk_index < 0 or total_chunks < 1 or chunk_index >= total_chunks or (offset is not None and offset < 0):
            return jsonify({"error": "Invalid chunk headers"}), 400

        # Drop any directory parts smuggled into the name; Windows accepts both separators.
        filename = os.path.basename(unquote(filename_raw).replace("\\", "/")).strip()
        if filename in ("", ".", ".."):
            filename = f"unnamed_file_{uuid.uuid4().hex[:8]}"
        relative_dir = unquote(request.headers.get("X-Relative-Dir", request.form.get("relative_dir", ""))).strip()

        dest_dir = save_dir
        if relative_dir:
            dest_dir = os.path.normpath(os.path.join(save_dir, relative_dir))
            root = os.path.realpath(save_dir)
            try:
                inside = os.path.commonpath([root, os.path.realpath(dest_dir)]) == root
            except ValueError:  # different drive on Windows
                inside = False
            if not inside:
                return jsonify({"error": "Invalid relative directory"}), 400

        os.makedirs(save_dir, exist_ok=True)
        temp_path = os.path.join(save_dir, f".{session_id}.tmp")
        legacy_temp = os.path.join(tempfile.gettempdir(), "nearbeam_uploads", f"{session_id}.tmp")
        if chunk_index > 0 and os.path.exists(legacy_temp) and not os.path.exists(temp_path):
            temp_path = legacy_temp

        if offset is None:
            # Legacy clients without X-Chunk-Offset: append-only.
            mode = "ab" if chunk_index > 0 else "wb"
        else:
            have = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
            if offset > have:
                return jsonify({"error": "Chunk offset beyond received data", "received_bytes": have}), 409
            mode = "r+b" if have else "wb"

        buf_size = 8 * 1024 * 1024
        with open(temp_path, mode, buffering=buf_size) as f:
            if offset is not None:
                # A resent chunk overwrites its own range instead of being appended twice.
                f.seek(offset)
                f.truncate()
            stream = request.files["file"].stream if request.files.get("file") else request.stream
            while True:
                data = stream.read(buf_size)
                if not data:
                    break
                f.write(data)

        # Broadcast live incoming progress so the host web app (and any
        # other connected device) can show a bar for large transfers.
        if total_chunks > 1:
            try:
                received_bytes = os.path.getsize(temp_path)
            except OSError:
                received_bytes = 0
            self.broadcast_event({
                "type": "upload_progress",
                "session_id": session_id,
                "filename": filename,
                "relative_dir": relative_dir,
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "percent": round(((chunk_index + 1) / total_chunks) * 100, 1),
                "received_bytes": received_bytes,
                "from_addr": request.remote_addr,
            })

        if chunk_index < total_chunks - 1:
            return jsonify({"success": True, "completed": False, "chunk_index": chunk_index})

        os.makedirs(dest_dir, exist_ok=True)
        final_path = os.path.join(dest_dir, filename)
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(final_path):
            final_path = os.path.join(dest_dir, f"{base} ({counter}){ext}")
            counter += 1
        final_name = os.path.basename(final_path)

        try:
            os.replace(temp_path, final_path)
        except OSError:
            import shutil
            shutil.move(temp_path, final_path)
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
        return jsonify({"success": True, "completed": True, "file": file_info})

    def _register_routes(self):
        app = self.app

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
            data = request.get_json(silent=True) or {}
            device_name = data.get("device_name", "")
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
                if exists and not is_file:
                    try:
                        item_count = len(os.listdir(folder_path))
                    except Exception:
                        item_count = 0

                result.append({
                    "id": folder["id"],
                    "name": folder["name"],
                    "path": folder_path,
                    "exists": exists,
                    "is_file": is_file,
                    "item_count": item_count,
                })
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
                        "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        "category": get_file_type_category(fname),
                    }],
                })

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
                            mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")

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
            return send_file(real_target, as_attachment=True, download_name=filename, conditional=True)

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

        @app.route("/api/safelist/prepare-zip", methods=["POST"])
        def api_safelist_prepare_zip():
            """Starts background job to package a Safe List folder or subfolder into a ZIP."""
            data = request.get_json(silent=True) or request.form or {}
            folder_id = data.get("folder_id", "")
            subpath = data.get("subpath", "")

            try:
                real_target = config.resolve_safe_path(folder_id, subpath)
            except Exception as e:
                return jsonify({"error": str(e)}), 403

            if not os.path.isdir(real_target):
                return jsonify({"error": "Not a directory"}), 400

            self._cleanup_old_zip_jobs()

            folder_name = os.path.basename(real_target) or "shared_folder"

            # Pre-scan directory to determine total file count and byte size
            files_list = []
            total_bytes = 0
            for root, _, filenames in os.walk(real_target):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    try:
                        f_size = os.path.getsize(file_path)
                    except OSError:
                        f_size = 0
                    archive_name = os.path.relpath(file_path, real_target)
                    files_list.append((file_path, archive_name, f_size))
                    total_bytes += f_size

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

            # Spawn background thread to create the zip file with live progress tracking
            worker_thread = threading.Thread(
                target=self._run_zip_worker,
                args=(job_id, real_target, temp_zip_path, files_list),
                daemon=True,
            )
            worker_thread.start()

            return jsonify({
                "success": True,
                "job_id": job_id,
                "folder_name": folder_name,
                "total_files": len(files_list),
                "total_bytes": total_bytes,
            })

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
            data = request.get_json(silent=True) or request.form or {}
            job_id = data.get("job_id", "")
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

            self.log_activity(f"Folder downloaded as ZIP: {folder_name} by {request.remote_addr}")

            def delayed_cleanup():
                time.sleep(120)  # Wait 2 minutes for download stream to finish
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception:
                    pass
                with self.zip_jobs_lock:
                    self.zip_jobs.pop(job_id, None)

            threading.Thread(target=delayed_cleanup, daemon=True).start()

            return send_file(
                temp_path,
                mimetype="application/zip",
                as_attachment=True,
                download_name=f"{folder_name}.zip",
            )

        @app.route("/api/safelist/download-zip")
        def api_safelist_download_zip():
            """Packs a Safe List folder or subfolder into a ZIP and sends it (synchronous / fallback)."""
            folder_id = request.args.get("folder_id", "")
            subpath = request.args.get("subpath", "")

            try:
                real_target = config.resolve_safe_path(folder_id, subpath)
            except Exception as e:
                return jsonify({"error": str(e)}), 403

            if not os.path.isdir(real_target):
                return jsonify({"error": "Not a directory"}), 400

            folder_name = os.path.basename(real_target) or "shared_folder"
            temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            temp_zip.close()

            try:
                with zipfile.ZipFile(temp_zip.name, "w", allowZip64=True, compresslevel=1) as zip_file:
                    for root, _, filenames in os.walk(real_target):
                        for filename in filenames:
                            file_path = os.path.join(root, filename)
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

                def cleanup_sync_temp():
                    time.sleep(120)
                    try:
                        if os.path.exists(temp_zip.name):
                            os.remove(temp_zip.name)
                    except Exception:
                        pass

                threading.Thread(target=cleanup_sync_temp, daemon=True).start()
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

        self.active_drop_zones = {}

        @app.route("/api/drop/create", methods=["POST"])
        def api_drop_create():
            if not config.auto_save_enabled:
                return jsonify({"error": "Auto-Save is disabled on host"}), 403
            token = str(uuid.uuid4())
            # Token valid for 24 hours
            expires_at = time.time() + (24 * 3600)
            self.active_drop_zones[token] = {"expires_at": expires_at}
            
            urls = get_connection_urls(self.port)
            drop_url = f"{urls['nearbeam_url'] or urls['primary_url']}/drop/{token}"
            return jsonify({"success": True, "token": token, "url": drop_url})

        @app.route("/drop/<token>")
        def drop_page(token):
            if token not in self.active_drop_zones or time.time() > self.active_drop_zones[token]["expires_at"]:
                return "Drop link expired or invalid", 404
            return render_template("drop.html", token=token)

        @app.route("/api/drop/<token>/upload/chunk", methods=["POST"])
        def api_drop_upload_chunk(token):
            if token not in self.active_drop_zones or time.time() > self.active_drop_zones[token]["expires_at"]:
                return jsonify({"error": "Drop link expired"}), 403
            return self._receive_chunk(
                os.path.join(config.save_directory, "Guest Drops"),
                f"Guest Drop ({request.remote_addr})",
                "Guest dropped '{name}' ({size})",
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

            uploaded_files = request.files.getlist("files") or [request.files.get("file")]
            saved_list = []

            for f in uploaded_files:
                if not f or not f.filename:
                    continue

                original_name = f.filename
                dest_path, final_name = config.get_unique_filepath(original_name)

                # Stream save to disk in 8 MB buffered chunks for maximum LAN throughput
                total_bytes = 0
                buf_size = 8 * 1024 * 1024
                with open(dest_path, "wb", buffering=buf_size) as out_file:
                    while True:
                        chunk = f.stream.read(buf_size)
                        if not chunk:
                            break
                        out_file.write(chunk)
                        total_bytes += len(chunk)

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
            if ".." in session_id or "/" in session_id or "\\" in session_id:
                return jsonify({"received_bytes": 0})
            candidates = [
                os.path.join(config.save_directory, f".{session_id}.tmp"),
                os.path.join(config.save_directory, "Guest Drops", f".{session_id}.tmp"),
                os.path.join(tempfile.gettempdir(), "nearbeam_uploads", f"{session_id}.tmp"),
            ]
            for temp_path in candidates:
                if os.path.exists(temp_path):
                    size = os.path.getsize(temp_path)
                    return jsonify({"received_bytes": size})
            return jsonify({"received_bytes": 0})

        @app.route("/api/upload/cancel", methods=["POST"])
        def api_upload_cancel():
            data = request.get_json(silent=True) or {}
            session_id = data.get("session_id", "")
            removed_tmp = False
            if session_id and ".." not in session_id and "/" not in session_id and "\\" not in session_id:
                candidates = [
                    os.path.join(config.save_directory, f".{session_id}.tmp"),
                    os.path.join(config.save_directory, "Guest Drops", f".{session_id}.tmp"),
                    os.path.join(tempfile.gettempdir(), "nearbeam_uploads", f"{session_id}.tmp"),
                ]
                for temp_path in candidates:
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                            removed_tmp = True
                        except OSError:
                            pass
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
            self.received_files_history.clear()
            return jsonify({"success": True})

        @app.route("/api/shell-send", methods=["POST"])
        def api_shell_send():
            """
            Handles files or folders sent from Windows Explorer right-click ('Send with NearBeam')
            or desktop drag-and-drop.
            """
            import shutil
            data = request.get_json(silent=True) or {}
            paths = data.get("paths", [])
            if not paths:
                return jsonify({"error": "No paths provided"}), 400

            staged_files = []
            added_folders = []

            for raw_path in paths:
                if not raw_path or not os.path.exists(raw_path):
                    continue
                real_p = os.path.realpath(raw_path)
                if os.path.isdir(real_p):
                    entry = config.add_safe_folder(real_p)
                    added_folders.append(entry)
                    self.log_activity(f"Shared folder via Windows Shell: '{entry['name']}'", "info")
                elif os.path.isfile(real_p):
                    entry = config.add_safe_folder(real_p)
                    added_folders.append(entry)
                    fname = os.path.basename(real_p)
                    save_dir_real = os.path.realpath(config.save_directory)
                    if os.path.dirname(real_p) == save_dir_real:
                        dest_path, final_name = real_p, fname
                    else:
                        dest_path, final_name = config.get_unique_filepath(fname)
                        shutil.copy2(real_p, dest_path)

                    total_bytes = os.path.getsize(dest_path)
                    file_info = {
                        "filename": final_name,
                        "original_name": fname,
                        "size": total_bytes,
                        "formatted_size": format_size(total_bytes),
                        "saved_path": dest_path,
                        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "category": get_file_type_category(final_name),
                    }
                    staged_files.append(file_info)
                    self.received_files_history.insert(0, file_info)
                    config.add_transfer_history({
                        **file_info,
                        "direction": "sent",
                        "peer": "Shared from PC",
                        "status": "completed",
                    })
                    self.log_activity(f"Shared '{final_name}' ({format_size(total_bytes)}) via NearBeam Shell", "upload")
                    self.broadcast_event({
                        "type": "file_received",
                        "file": file_info,
                        "source": "shell_send",
                    })

            if added_folders:
                self.broadcast_event({"type": "safelist_updated"})

            return jsonify({
                "success": True,
                "added_count": len(added_folders),
                "staged_files": staged_files,
                "added_folders": added_folders,
            })

        @app.route("/api/received")
        def api_received():
            """Lists files in the auto-save directory."""
            save_dir = config.save_directory
            files = []
            if os.path.exists(save_dir) and os.path.isdir(save_dir):
                try:
                    with os.scandir(save_dir) as entries:
                        for entry in entries:
                            if entry.is_file() and not entry.name.startswith("."):
                                stat = entry.stat()
                                size = stat.st_size
                                files.append({
                                    "filename": entry.name,
                                    "size": size,
                                    "formatted_size": format_size(size),
                                    "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                                    "category": get_file_type_category(entry.name),
                                })
                except Exception as e:
                    print(f"Error scanning save directory: {e}")

            files.sort(key=lambda x: x["modified"], reverse=True)
            return jsonify({"files": files, "save_directory": save_dir})

        @app.route("/api/received/download/<filename>")
        def api_received_download(filename: str):
            """Downloads an auto-saved file from the save directory."""
            save_dir = config.save_directory
            safe_name = os.path.basename(filename)
            target = os.path.join(save_dir, safe_name)
            if not os.path.isfile(target):
                return jsonify({"error": "File not found"}), 404
            return send_file(target, as_attachment=True, download_name=safe_name, conditional=True)

        @app.route("/api/received/preview/<filename>")
        def api_received_preview(filename: str):
            """Streams an auto-saved file inline for preview."""
            save_dir = config.save_directory
            safe_name = os.path.basename(filename)
            target = os.path.join(save_dir, safe_name)
            if not os.path.isfile(target):
                return jsonify({"error": "File not found"}), 404
            mime_type, _ = mimetypes.guess_type(target)
            return send_file(target, mimetype=mime_type or "application/octet-stream", conditional=True)

        # --- Shared Clipboard Endpoints (Text + Rich Images / Screenshots NB-07) ---

        @app.route("/api/clipboard", methods=["GET", "POST"])
        def api_clipboard():
            if request.method == "POST":
                data = request.get_json(silent=True) or {}
                text = data.get("text")
                image_b64 = data.get("image_base64")
                image_mime = data.get("image_mime", "image/png")

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
                            cb(self.clipboard_content)
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
                    data = request.get_json(silent=True) or {}
                    b64 = data.get("image_base64", "")
                    mime_type = data.get("image_mime", "image/png")
                    if b64:
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

            if not self.clipboard_image_bytes:
                return jsonify({"error": "No image in clipboard"}), 404

            import io
            ext = "png"
            if "jpeg" in (self.clipboard_image_mime or "") or "jpg" in (self.clipboard_image_mime or ""):
                ext = "jpg"
            elif "webp" in (self.clipboard_image_mime or ""):
                ext = "webp"
            elif "gif" in (self.clipboard_image_mime or ""):
                ext = "gif"

            as_download = request.args.get("download") == "1"
            return send_file(
                io.BytesIO(self.clipboard_image_bytes),
                mimetype=self.clipboard_image_mime or "image/png",
                as_attachment=as_download,
                download_name=f"nearbeam_clipboard_{self.clipboard_image_id or 'image'}.{ext}",
            )

        @app.route("/api/clipboard/image/clear", methods=["POST"])
        def api_clipboard_image_clear():
            """Clears the current image from the shared clipboard."""
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
            data = request.get_json(silent=True) or {}
            if not data.get("node_id") or not data.get("ip"):
                return jsonify({"error": "Invalid node registration data"}), 400

            if self.mesh:
                self.mesh.register_node(data)
                self.log_activity(f"Secondary Node joined cluster: {data.get('name')} ({data.get('ip')})", "info")
                self.broadcast_event({
                    "type": "mesh_nodes_changed",
                    "nodes": self.mesh.get_nodes_list(),
                })
                return jsonify({
                    "success": True,
                    "leader_name": self.mesh.device_name,
                    "leader_role": self.mesh.role,
                })
            return jsonify({"error": "Mesh not enabled on host"}), 503

        @app.route("/api/mesh/heartbeat", methods=["POST"])
        def api_mesh_heartbeat():
            """Heartbeat ping from active secondary nodes."""
            data = request.get_json(silent=True) or {}
            node_id = data.get("node_id")
            if not node_id:
                return jsonify({"error": "Missing node_id"}), 400

            if self.mesh:
                updated = self.mesh.update_heartbeat(node_id)
                return jsonify({"status": "ok", "updated": updated})
            return jsonify({"error": "Mesh not enabled"}), 503

        @app.route("/api/mesh/clipboard-sync", methods=["POST"])
        def api_mesh_clipboard_sync():
            """Receives cross-node clipboard text or image update."""
            data = request.get_json(silent=True) or {}
            text = data.get("text", "")
            image_b64 = data.get("image_base64")
            image_mime = data.get("image_mime", "image/png")
            source_node_id = data.get("source_node_id")

            if image_b64:
                import base64
                try:
                    img_bytes = base64.b64decode(image_b64)
                    self.set_clipboard_image(
                        img_bytes,
                        mime_type=image_mime,
                        notify_local_os=True,
                        propagate_mesh=(self.mesh is not None and self.mesh.role == "leader"),
                        source_node_id=source_node_id,
                    )
                    self.log_activity(f"Clipboard image ({format_size(len(img_bytes))}) synced from peer host", "info")
                except Exception:
                    pass

            if text and text != self.clipboard_content:
                self.clipboard_content = text
                self.clipboard_updated_at = datetime.datetime.now().strftime("%H:%M:%S")
                snippet = (text[:30] + "...") if len(text) > 30 else text
                self.log_activity(f"Clipboard synced from peer host: '{snippet}'", "info")

                self.broadcast_event({
                    "type": "clipboard_updated",
                    **self.get_clipboard_state(),
                })

                for cb in self.clipboard_callbacks:
                    try:
                        cb(text)
                    except Exception:
                        pass

                # If Leader, forward to other secondary nodes
                if self.mesh and self.mesh.role == "leader":
                    self.mesh.broadcast_clipboard_mesh(text, source_node_id=source_node_id)

            return jsonify({"success": True})

        # --- Real-Time SSE Stream ---

        @app.route("/api/events")
        def api_events():
            """Server-Sent Events endpoint for real-time reactivity."""
            def event_stream():
                client_queue = queue.Queue()
                self.event_subscribers.append(client_queue)
                try:
                    # Initial heartbeat
                    yield f"data: {json.dumps({'type': 'connected'})}\n\n"
                    while True:
                        try:
                            msg = client_queue.get(timeout=25)
                            yield f"data: {msg}\n\n"
                        except queue.Empty:
                            # Keep-alive heartbeat
                            yield f": heartbeat\n\n"
                except GeneratorExit:
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
            """Returns QR code image for connection URL."""
            urls = get_connection_urls(self.port)
            target_url = urls["primary_url"]
            png_bytes = generate_qr_png_bytes(target_url)
            if png_bytes:
                return Response(png_bytes, mimetype="image/png")
            return jsonify({"error": "QR code unavailable"}), 500


# Backward-compatible alias for existing tests and scripts
LandropServer = NearBeamServer

