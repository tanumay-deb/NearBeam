"""
Configuration and persistence management for NearBeam.
Handles Safe List folders, Auto-Save preferences, and settings.
"""

import datetime
import hashlib
import hmac
import json
import os
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path


def user_config_path() -> Path:
    """One settings file per user, shared by every build. Installers and updates never touch it."""
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "NearBeam" / "config.json"
    return Path.home() / ".config" / "nearbeam" / "config.json"


def legacy_config_paths() -> list[Path]:
    """Where versions up to 1.6.0 kept settings: next to the installed exe, or in the source folder."""
    paths = []
    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).parent / "config.json")
    paths.append(Path(__file__).resolve().parent.parent / "config.json")
    return paths


VALID_ORGANIZE_MODES = ("off", "type", "date", "device")


class ConfigManager:
    VALID_ORGANIZE_MODES = VALID_ORGANIZE_MODES

    def __init__(self, config_path=None):
        if config_path is None:
            self.config_path = user_config_path()
            if not self.config_path.exists():
                self._migrate_legacy()
        else:
            self.config_path = Path(config_path)

        # Default save directory in user's Downloads folder
        downloads_dir = Path.home() / "Downloads" / "NearBeam_Received"
        self.default_save_dir = str(downloads_dir)

        # Upload threads write history concurrently; serialize mutation + save.
        self._lock = threading.RLock()
        self.data = {
            "port": 5000,
            "auto_save": True,
            "auto_organize_mode": "off",
            "save_directory": self.default_save_dir,
            "safe_folders": [],
            "transfer_history": [],
            "trusted_devices": [],
            "pin_enabled": False,
            "pin": "",
        }
        self._trust_last_touch: dict[str, float] = {}

        self.load()
        self.ensure_save_directory()

    def _migrate_legacy(self):
        """One-time copy of settings from where older versions kept them (the old file stays as a backup)."""
        for old in legacy_config_paths():
            if old.exists():
                try:
                    self.config_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(old, self.config_path)
                except OSError as e:
                    print(f"Could not copy settings from {old}: {e}")
                return

    def ensure_save_directory(self):
        """Creates the auto-save directory if it does not exist."""
        save_dir = self.data.get("save_directory", self.default_save_dir)
        try:
            os.makedirs(save_dir, exist_ok=True)
        except Exception as e:
            print(f"Warning: Could not create save directory {save_dir}: {e}")

    def load(self):
        """Loads configuration from JSON file if it exists."""
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data.update(loaded)
                if not isinstance(self.data.get("safe_folders"), list):
                    self.data["safe_folders"] = []
                if not isinstance(self.data.get("transfer_history"), list):
                    self.data["transfer_history"] = []
                raw_trusted = self.data.get("trusted_devices")
                if not isinstance(raw_trusted, list):
                    self.data["trusted_devices"] = []
                else:
                    self.data["trusted_devices"] = [
                        d for d in raw_trusted
                        if isinstance(d, dict)
                        and isinstance(d.get("id"), str) and d["id"]
                        and isinstance(d.get("token_hash"), str) and len(d["token_hash"]) == 64
                    ]
                if not isinstance(self.data.get("save_directory"), str) or not self.data["save_directory"].strip():
                    self.data["save_directory"] = self.default_save_dir
                if not isinstance(self.data.get("port"), int) or isinstance(self.data.get("port"), bool):
                    self.data["port"] = 5000
                if self.data.get("auto_organize_mode") not in VALID_ORGANIZE_MODES:
                    self.data["auto_organize_mode"] = "off"
            except Exception as e:
                print(f"Error loading config from {self.config_path}: {e}")

    def save(self):
        """Persists current configuration to JSON file."""
        try:
            with self._lock:
                text = json.dumps(self.data, indent=2, ensure_ascii=False)
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                # Write-then-rename so a crash mid-write never leaves a truncated config.
                tmp_path = f"{self.config_path}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp_path, self.config_path)
        except Exception as e:
            print(f"Error saving config to {self.config_path}: {e}")

    # --- Auto-Save Settings ---

    @property
    def auto_save_enabled(self) -> bool:
        return bool(self.data.get("auto_save", True))

    def set_auto_save(self, enabled: bool):
        with self._lock:
            self.data["auto_save"] = bool(enabled)
            self.save()

    @property
    def auto_organize_mode(self) -> str:
        mode = self.data.get("auto_organize_mode", "off")
        return mode if mode in VALID_ORGANIZE_MODES else "off"

    def set_auto_organize_mode(self, mode: str) -> str:
        clean = mode if mode in VALID_ORGANIZE_MODES else "off"
        with self._lock:
            self.data["auto_organize_mode"] = clean
            self.save()
        return clean

    @property
    def clipboard_sync_enabled(self) -> bool:
        return bool(self.data.get("clipboard_sync", True))

    def set_clipboard_sync(self, enabled: bool):
        with self._lock:
            self.data["clipboard_sync"] = bool(enabled)
            self.save()

    @property
    def save_directory(self) -> str:
        return self.data.get("save_directory", self.default_save_dir)

    def set_save_directory(self, path: str):
        abs_path = os.path.abspath(path)
        os.makedirs(abs_path, exist_ok=True)
        with self._lock:
            self.data["save_directory"] = abs_path
            self.save()

    def get_unique_filepath(self, filename: str) -> tuple[str, str]:
        """
        Returns a unique filepath in the save directory to prevent overwriting
        existing files.
        Example: 'photo.jpg' -> 'photo (1).jpg' if 'photo.jpg' exists.
        Returns: (full_path, final_filename)
        """
        save_dir = self.save_directory
        self.ensure_save_directory()

        # Sanitize basic filename
        clean_name = os.path.basename(filename).strip()
        if not clean_name:
            clean_name = f"unnamed_file_{uuid.uuid4().hex[:8]}"

        target_path = os.path.join(save_dir, clean_name)
        if not os.path.exists(target_path):
            return target_path, clean_name

        name_part, ext_part = os.path.splitext(clean_name)
        counter = 1
        while True:
            new_name = f"{name_part} ({counter}){ext_part}"
            new_path = os.path.join(save_dir, new_name)
            if not os.path.exists(new_path):
                return new_path, new_name
            counter += 1

    # --- Safe List (Shared Folders) Management ---

    def get_safe_folders(self) -> list[dict]:
        """Returns a thread-safe shallow copy of the Safe List folders."""
        with self._lock:
            return list(self.data.get("safe_folders") or [])

    def add_safe_folder(self, folder_path: str, display_name: str = None) -> dict:
        """
        Adds a directory or file to the Safe List.
        Returns the created entry.
        """
        abs_path = os.path.realpath(folder_path)
        if not os.path.exists(abs_path):
            raise ValueError(f"Path does not exist: {folder_path}")

        # Upload threads save history concurrently: check-and-add under the save lock.
        with self._lock:
            for item in self.get_safe_folders():
                if os.path.realpath(item["path"]) == abs_path:
                    return item

            name = display_name if display_name else os.path.basename(abs_path) or abs_path
            folder_entry = {
                "id": uuid.uuid4().hex[:12],
                "name": name,
                "path": abs_path,
            }
            self.data.setdefault("safe_folders", []).append(folder_entry)
            self.save()
        return folder_entry

    def remove_safe_folder(self, folder_id: str) -> bool:
        """Removes a folder from the Safe List by its ID."""
        with self._lock:
            folders = self.get_safe_folders()
            initial_len = len(folders)
            self.data["safe_folders"] = [f for f in folders if f.get("id") != folder_id]
            if len(self.data["safe_folders"]) != initial_len:
                self.save()
                return True
        return False

    def add_safe_folders(self, paths: list[str]) -> list[dict]:
        """Adds multiple folders or files to the Safe List in a single atomic config save."""
        added = []
        with self._lock:
            folders = self.data.setdefault("safe_folders", [])
            existing_paths = {os.path.realpath(item["path"]) for item in folders if item.get("path")}
            for raw_path in paths or []:
                if not raw_path or not os.path.exists(raw_path):
                    continue
                abs_path = os.path.realpath(raw_path)
                if abs_path in existing_paths:
                    continue
                name = os.path.basename(abs_path) or abs_path
                entry = {
                    "id": uuid.uuid4().hex[:12],
                    "name": name,
                    "path": abs_path,
                }
                folders.append(entry)
                existing_paths.add(abs_path)
                added.append(entry)
            if added:
                self.save()
        return added

    def remove_safe_folders(self, folder_ids: list[str]) -> int:
        """Removes multiple Safe List entries by ID in a single atomic config save."""
        id_set = {fid for fid in (folder_ids or []) if fid}
        if not id_set:
            return 0
        with self._lock:
            folders = self.get_safe_folders()
            kept = [f for f in folders if f.get("id") not in id_set]
            removed = len(folders) - len(kept)
            if removed > 0:
                self.data["safe_folders"] = kept
                self.save()
            return removed

    def get_safe_folder_by_id(self, folder_id: str) -> dict | None:
        """Retrieves a safe folder by its ID."""
        for item in self.get_safe_folders():
            if item.get("id") == folder_id:
                return item
        return None

    def resolve_safe_path(self, folder_id: str, subpath: str = "") -> str:
        """
        Securely resolves a subpath inside a Safe List folder (or single shared file).
        Validates that the target path is strictly within the safe folder's boundary
        to prevent directory traversal attacks (e.g. '../').
        """
        folder = self.get_safe_folder_by_id(folder_id)
        if not folder:
            raise KeyError(f"Safe folder with ID '{folder_id}' not found.")

        base_real = os.path.realpath(folder["path"])
        if not os.path.exists(base_real):
            raise FileNotFoundError(f"Safe folder path does not exist: {base_real}")

        if subpath is None:
            subpath = ""
        elif not isinstance(subpath, str):
            raise PermissionError("Access denied: Invalid subpath type.")
        clean_subpath = subpath.replace("\\", "/").strip("/")
        if any(":" in part for part in clean_subpath.split("/") if part):
            raise PermissionError("Access denied: Invalid path syntax.")
        if os.path.isfile(base_real):
            if not clean_subpath or clean_subpath == os.path.basename(base_real):
                return base_real
            raise PermissionError("Access denied: Invalid subpath for single file.")

        if clean_subpath:
            target_real = os.path.realpath(os.path.join(base_real, clean_subpath))
        else:
            target_real = base_real

        # Sandboxing check: target must be inside base_real
        try:
            common = os.path.commonpath([base_real, target_real])
        except ValueError:
            # Different drives on Windows (e.g. C: vs D:)
            raise PermissionError("Access denied: Target path outside safe boundary.")

        if common != base_real:
            raise PermissionError("Access denied: Directory traversal detected.")

        return target_real

    # --- Persistent Transfer History ---

    def get_transfer_history(self) -> list[dict]:
        """Returns persistent transfer history records."""
        return list(self.data.get("transfer_history", []))

    def add_transfer_history(self, entry: dict) -> dict:
        """Prepends a completed or failed transfer entry to persistent history (capped at 100)."""
        record = {
            "id": entry.get("id") or uuid.uuid4().hex[:12],
            "filename": entry.get("filename", "Unknown"),
            "size": entry.get("size", 0),
            "formatted_size": entry.get("formatted_size", "0 B"),
            "timestamp": entry.get("timestamp", ""),
            "direction": entry.get("direction", "received"),
            "peer": entry.get("peer", "Local Network"),
            "status": entry.get("status", "completed"),
            "category": entry.get("category", "file"),
        }
        if isinstance(entry.get("saved_path"), str) and entry["saved_path"]:
            record["saved_path"] = entry["saved_path"]
        with self._lock:
            history = self.data.setdefault("transfer_history", [])
            history.insert(0, record)
            if len(history) > 100:
                del history[100:]
            self.save()
        return record

    def clear_transfer_history(self):
        """Clears all saved transfer history records."""
        with self._lock:
            self.data["transfer_history"] = []
            self.save()

    # --- Trusted Devices (One-Time Pairing for Safe List Delete) ---

    @staticmethod
    def hash_trust_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @staticmethod
    def _public_trusted_device(entry: dict) -> dict:
        return {
            "id": entry.get("id", ""),
            "name": entry.get("name", "Trusted Device"),
            "paired_at": entry.get("paired_at", ""),
            "last_used": entry.get("last_used", ""),
            "ip": entry.get("ip", ""),
        }

    def get_trusted_devices(self) -> list[dict]:
        """Returns public metadata for all paired trusted devices (never includes token_hash)."""
        with self._lock:
            return [self._public_trusted_device(d) for d in self.data.get("trusted_devices", [])]

    def add_trusted_device(self, name: str, raw_token: str, ip: str = "") -> dict:
        """Stores a newly paired trusted device using SHA-256 of its raw token."""
        if not isinstance(raw_token, str) or len(raw_token) < 16:
            raise ValueError("Invalid trust token")
        clean_name = (name or "").strip()[:60] or "Trusted Device"
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = {
            "id": uuid.uuid4().hex[:12],
            "name": clean_name,
            "token_hash": self.hash_trust_token(raw_token),
            "paired_at": now_str,
            "last_used": now_str,
            "ip": (ip or "").strip()[:64],
        }
        with self._lock:
            self.data.setdefault("trusted_devices", []).append(entry)
            self._trust_last_touch[entry["id"]] = time.monotonic()
            self.save()
        return self._public_trusted_device(entry)

    def verify_trusted_token(self, raw_token: str, touch_ip: str = "") -> dict | None:
        """Verifies a raw cookie token in constant time; returns public device metadata if valid."""
        if not isinstance(raw_token, str) or len(raw_token) < 16:
            return None
        cand_hash = self.hash_trust_token(raw_token)
        with self._lock:
            matched = None
            for entry in self.data.get("trusted_devices", []):
                stored = entry.get("token_hash", "")
                if isinstance(stored, str) and hmac.compare_digest(stored, cand_hash):
                    matched = entry
                    break
            if matched is None:
                return None
            now_mono = time.monotonic()
            dev_id = matched["id"]
            clean_ip = (touch_ip or "").strip()[:64]
            needs_save = False
            if clean_ip and matched.get("ip") != clean_ip:
                matched["ip"] = clean_ip
                needs_save = True
            if now_mono - self._trust_last_touch.get(dev_id, 0.0) >= 60.0:
                matched["last_used"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                self._trust_last_touch[dev_id] = now_mono
                needs_save = True
            if needs_save:
                self.save()
            return self._public_trusted_device(matched)

    def revoke_trusted_device(self, device_id: str) -> bool:
        """Revokes a single trusted device by ID."""
        if not device_id:
            return False
        with self._lock:
            devices = self.data.get("trusted_devices", [])
            kept = [d for d in devices if d.get("id") != device_id]
            if len(kept) != len(devices):
                self.data["trusted_devices"] = kept
                self._trust_last_touch.pop(device_id, None)
                self.save()
                return True
        return False

    def revoke_trusted_token(self, raw_token: str) -> bool:
        """Revokes whichever trusted device matches raw_token."""
        if not isinstance(raw_token, str) or len(raw_token) < 16:
            return False
        cand_hash = self.hash_trust_token(raw_token)
        with self._lock:
            devices = self.data.get("trusted_devices", [])
            kept = []
            removed = False
            for d in devices:
                stored = d.get("token_hash", "")
                if not removed and isinstance(stored, str) and hmac.compare_digest(stored, cand_hash):
                    self._trust_last_touch.pop(d.get("id", ""), None)
                    removed = True
                else:
                    kept.append(d)
            if removed:
                self.data["trusted_devices"] = kept
                self.save()
            return removed

    def revoke_all_trusted_devices(self) -> int:
        """Revokes all trusted devices."""
        with self._lock:
            count = len(self.data.get("trusted_devices", []))
            if count > 0:
                self.data["trusted_devices"] = []
                self._trust_last_touch.clear()
                self.save()
            return count


# Global default instance
config = ConfigManager()
