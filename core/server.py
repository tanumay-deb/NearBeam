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
        self.received_files_history = []
        self.event_subscribers: list[queue.Queue] = []
        self.activity_callbacks = []
        self.clipboard_callbacks = []
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
            with zipfile.ZipFile(temp_zip_path, "w", allowZip64=True) as zip_file:
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
                            zip_file.write(file_path, archive_name, compress_type=compress_type)
                            with self.zip_jobs_lock:
                                job = self.zip_jobs.get(job_id)
                                if job:
                                    job["processed_bytes"] += f_size
                                    if job["total_bytes"] > 0:
                                        job["percent"] = min(99.0, round((job["processed_bytes"] / job["total_bytes"]) * 100, 1))
                                    elif job["total_files"] > 0:
                                        job["percent"] = min(99.0, round((job["processed_files"] / job["total_files"]) * 100, 1))
                        else:
                            chunk_size = 1024 * 512
                            with open(file_path, "rb") as src, zip_file.open(archive_name, "w", compress_type=compress_type) as dst:
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
        """Registers a callback for remote clipboard updates."""
        self.clipboard_callbacks.append(callback)

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

    def _register_routes(self):
        app = self.app

        @app.route("/")
        def index():
            self.log_activity(f"Device connected from {request.remote_addr}", "info")
            return render_template("index.html")

        @app.route("/finder")
        def finder():
            self.log_activity(f"Subnet scanner accessed from {request.remote_addr}", "info")
            return render_template("finder.html")

        @app.route("/api/info")
        def api_info():
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
            })

        @app.route("/api/version")
        def api_version():
            return jsonify(check_for_updates())

        # --- Safe List Endpoints ---

        @app.route("/api/safelist")
        def api_safelist():
            """Returns list of all exposed Safe List folders."""
            result = []
            for folder in config.get_safe_folders():
                folder_path = folder["path"]
                exists = os.path.exists(folder_path) and os.path.isdir(folder_path)
                item_count = 0
                if exists:
                    try:
                        item_count = len(os.listdir(folder_path))
                    except Exception:
                        item_count = 0

                result.append({
                    "id": folder["id"],
                    "name": folder["name"],
                    "path": folder_path,
                    "exists": exists,
                    "item_count": item_count,
                })
            return jsonify({"folders": result})

        @app.route("/api/safelist/browse")
        def api_safelist_browse():
            """
            Browses files and subdirectories inside a safe folder.
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
                with zipfile.ZipFile(temp_zip.name, "w", allowZip64=True) as zip_file:
                    for root, _, filenames in os.walk(real_target):
                        for filename in filenames:
                            file_path = os.path.join(root, filename)
                            archive_name = os.path.relpath(file_path, real_target)
                            ext = os.path.splitext(filename)[1].lower()
                            c_type = zipfile.ZIP_STORED if ext in PRECOMPRESSED_EXTENSIONS else zipfile.ZIP_DEFLATED
                            try:
                                zip_file.write(file_path, archive_name, compress_type=c_type)
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
            from urllib.parse import unquote
            session_id = request.headers.get("X-Session-Id") or request.form.get("session_id")
            filename_raw = request.headers.get("X-Filename") or request.form.get("filename")
            if not session_id or not filename_raw:
                return jsonify({"error": "Missing parameters"}), 400
                
            filename = unquote(filename_raw)
            chunk_index = int(request.headers.get("X-Chunk-Index", request.form.get("chunk_index", 0)))
            total_chunks = int(request.headers.get("X-Total-Chunks", request.form.get("total_chunks", 1)))
            relative_dir = unquote(request.headers.get("X-Relative-Dir", request.form.get("relative_dir", ""))).strip()

            temp_dir = os.path.join(tempfile.gettempdir(), "nearbeam_uploads")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, f"{session_id}.tmp")

            mode = "ab" if chunk_index > 0 else "wb"
            with open(temp_path, mode) as f:
                if request.content_type == "application/octet-stream":
                    chunk_size = 1024 * 1024 * 4
                    while True:
                        data = request.stream.read(chunk_size)
                        if not data:
                            break
                        f.write(data)
                else:
                    chunk_file = request.files.get("file")
                    if chunk_file:
                        while True:
                            data = chunk_file.stream.read(1024 * 1024)
                            if not data:
                                break
                            f.write(data)

            # Broadcast live incoming progress so the host web app can show a
            # bar for large guest drops.
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

            if chunk_index == total_chunks - 1:
                save_dir = os.path.join(config.save_directory, "Guest Drops")
                os.makedirs(save_dir, exist_ok=True)
                
                if relative_dir and ".." not in relative_dir and not relative_dir.startswith("/"):
                    dest_dir = os.path.join(save_dir, relative_dir)
                    os.makedirs(dest_dir, exist_ok=True)
                    final_path = os.path.join(dest_dir, filename)
                    base, ext = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(final_path):
                        final_path = os.path.join(dest_dir, f"{base} ({counter}){ext}")
                        counter += 1
                    final_name = os.path.basename(final_path)
                else:
                    final_path = os.path.join(save_dir, filename)
                    base, ext = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(final_path):
                        final_path = os.path.join(save_dir, f"{base} ({counter}){ext}")
                        counter += 1
                    final_name = os.path.basename(final_path)

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

                msg = f"Guest dropped '{final_name}' ({format_size(total_bytes)})"
                self.log_activity(msg, event_type="upload")
                self.broadcast_event({
                    "type": "file_received",
                    "file": file_info,
                    "session_id": session_id,
                })

                return jsonify({"success": True, "completed": True, "file": file_info})
            
            return jsonify({"success": True, "completed": False, "chunk_index": chunk_index})

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

                # Stream save to disk in chunks to avoid memory overflow
                total_bytes = 0
                with open(dest_path, "wb") as out_file:
                    while True:
                        chunk = f.stream.read(1024 * 1024)
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
            from urllib.parse import unquote
            session_id = request.headers.get("X-Session-Id") or request.form.get("session_id")
            filename_raw = request.headers.get("X-Filename") or request.form.get("filename")
            if not session_id or not filename_raw:
                return jsonify({"error": "Missing parameters"}), 400
                
            filename = unquote(filename_raw)
            chunk_index = int(request.headers.get("X-Chunk-Index", request.form.get("chunk_index", 0)))
            total_chunks = int(request.headers.get("X-Total-Chunks", request.form.get("total_chunks", 1)))
            relative_dir = unquote(request.headers.get("X-Relative-Dir", request.form.get("relative_dir", ""))).strip()

            temp_dir = os.path.join(tempfile.gettempdir(), "nearbeam_uploads")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, f"{session_id}.tmp")

            mode = "ab" if chunk_index > 0 else "wb"
            with open(temp_path, mode) as f:
                if request.content_type == "application/octet-stream":
                    chunk_size = 1024 * 1024 * 4
                    while True:
                        data = request.stream.read(chunk_size)
                        if not data:
                            break
                        f.write(data)
                else:
                    chunk_file = request.files.get("file")
                    if chunk_file:
                        while True:
                            data = chunk_file.stream.read(1024 * 1024)
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

            if chunk_index == total_chunks - 1:
                save_dir = config.save_directory
                if relative_dir and ".." not in relative_dir and not relative_dir.startswith("/"):
                    dest_dir = os.path.join(save_dir, relative_dir)
                    os.makedirs(dest_dir, exist_ok=True)
                    final_path = os.path.join(dest_dir, filename)
                    base, ext = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(final_path):
                        final_path = os.path.join(dest_dir, f"{base} ({counter}){ext}")
                        counter += 1
                    final_name = os.path.basename(final_path)
                else:
                    final_path, final_name = config.get_unique_filepath(filename)

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

                msg = f"Auto-saved '{final_name}' ({format_size(total_bytes)}) from {request.remote_addr}"
                self.log_activity(msg, event_type="upload")
                self.broadcast_event({
                    "type": "file_received",
                    "file": file_info,
                    "session_id": session_id,
                })

                return jsonify({"success": True, "completed": True, "file": file_info})
            
            return jsonify({"success": True, "completed": False, "chunk_index": chunk_index})

        @app.route("/api/upload/status/<session_id>")
        def api_upload_status(session_id):
            temp_path = os.path.join(tempfile.gettempdir(), "nearbeam_uploads", f"{session_id}.tmp")
            if os.path.exists(temp_path):
                size = os.path.getsize(temp_path)
                return jsonify({"received_bytes": size})
            return jsonify({"received_bytes": 0})

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

        # --- Shared Clipboard Endpoints ---

        @app.route("/api/clipboard", methods=["GET", "POST"])
        def api_clipboard():
            if request.method == "POST":
                data = request.get_json(silent=True) or {}
                text = data.get("text", "")
                self.clipboard_content = text
                self.clipboard_updated_at = datetime.datetime.now().strftime("%H:%M:%S")

                snippet = (text[:35] + "...") if len(text) > 35 else text
                self.log_activity(f"Clipboard updated from {request.remote_addr}: '{snippet}'")
                self.broadcast_event({
                    "type": "clipboard_updated",
                    "text": self.clipboard_content,
                    "updated_at": self.clipboard_updated_at,
                })

                if self.mesh:
                    self.mesh.broadcast_clipboard_mesh(text)

                for cb in self.clipboard_callbacks:
                    try:
                        cb(self.clipboard_content)
                    except Exception as e:
                        print(f"Error in clipboard callback: {e}")

                return jsonify({"success": True, "text": self.clipboard_content})

            return jsonify({
                "text": self.clipboard_content,
                "updated_at": self.clipboard_updated_at,
            })

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
            """Receives cross-node clipboard update."""
            data = request.get_json(silent=True) or {}
            text = data.get("text", "")
            source_node_id = data.get("source_node_id")

            if text and text != self.clipboard_content:
                self.clipboard_content = text
                self.clipboard_updated_at = datetime.datetime.now().strftime("%H:%M:%S")
                snippet = (text[:30] + "...") if len(text) > 30 else text
                self.log_activity(f"Clipboard synced from peer host: '{snippet}'", "info")

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

