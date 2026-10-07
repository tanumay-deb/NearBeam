"""
Configuration and persistence management for NearBeam.
Handles Safe List folders, Auto-Save preferences, and settings.
"""

import json
import os
import sys
import uuid
from pathlib import Path


class ConfigManager:
    def __init__(self, config_path=None):
        if config_path is None:
            if getattr(sys, "frozen", False):
                base_dir = Path(sys.executable).parent
            else:
                base_dir = Path(__file__).resolve().parent.parent
            self.config_path = base_dir / "config.json"
        else:
            self.config_path = Path(config_path)

        # Default save directory in user's Downloads folder
        downloads_dir = Path.home() / "Downloads" / "NearBeam_Received"
        self.default_save_dir = str(downloads_dir)

        self.data = {
            "port": 5000,
            "auto_save": True,
            "save_directory": self.default_save_dir,
            "safe_folders": [],
            "pin_enabled": False,
            "pin": "",
        }

        self.load()
        self.ensure_save_directory()

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
                    self.data.update(loaded)
            except Exception as e:
                print(f"Error loading config from {self.config_path}: {e}")

    def save(self):
        """Persists current configuration to JSON file."""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving config to {self.config_path}: {e}")

    # --- Auto-Save Settings ---

    @property
    def auto_save_enabled(self) -> bool:
        return bool(self.data.get("auto_save", True))

    def set_auto_save(self, enabled: bool):
        self.data["auto_save"] = bool(enabled)
        self.save()

    @property
    def clipboard_sync_enabled(self) -> bool:
        return bool(self.data.get("clipboard_sync", True))

    def set_clipboard_sync(self, enabled: bool):
        self.data["clipboard_sync"] = bool(enabled)
        self.save()

    @property
    def save_directory(self) -> str:
        return self.data.get("save_directory", self.default_save_dir)

    def set_save_directory(self, path: str):
        abs_path = os.path.abspath(path)
        os.makedirs(abs_path, exist_ok=True)
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
        """Returns the list of safe folders."""
        return self.data.get("safe_folders", [])

    def add_safe_folder(self, folder_path: str, display_name: str = None) -> dict:
        """
        Adds a directory to the Safe List.
        Returns the created folder entry.
        """
        abs_path = os.path.realpath(folder_path)
        if not os.path.isdir(abs_path):
            raise ValueError(f"Path is not a valid directory: {folder_path}")

        # Check if already in safe list
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
        folders = self.get_safe_folders()
        initial_len = len(folders)
        self.data["safe_folders"] = [f for f in folders if f.get("id") != folder_id]
        if len(self.data["safe_folders"]) != initial_len:
            self.save()
            return True
        return False

    def get_safe_folder_by_id(self, folder_id: str) -> dict | None:
        """Retrieves a safe folder by its ID."""
        for item in self.get_safe_folders():
            if item.get("id") == folder_id:
                return item
        return None

    def resolve_safe_path(self, folder_id: str, subpath: str = "") -> str:
        """
        Securely resolves a subpath inside a Safe List folder.
        Validates that the target path is strictly within the safe folder's boundary
        to prevent directory traversal attacks (e.g. '../').
        """
        folder = self.get_safe_folder_by_id(folder_id)
        if not folder:
            raise KeyError(f"Safe folder with ID '{folder_id}' not found.")

        base_real = os.path.realpath(folder["path"])
        if not os.path.exists(base_real):
            raise FileNotFoundError(f"Safe folder path does not exist: {base_real}")

        # Normalize subpath
        clean_subpath = subpath.replace("\\", "/").strip("/")
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


# Global default instance
config = ConfigManager()
