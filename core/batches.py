"""
In-memory ephemeral Send Batches for PC -> Devices transfers.
Keeps one-off drops/sends completely separate from permanent Safe List folders.
"""

from collections import OrderedDict
import datetime
import os
import threading
import time
import uuid
from typing import Callable, Optional


def _default_format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _default_category(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".heic"}:
        return "image"
    if ext in {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv"}:
        return "video"
    if ext in {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"}:
        return "audio"
    if ext in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".md", ".csv"}:
        return "document"
    if ext in {".zip", ".tar", ".gz", ".7z", ".rar", ".bz2"}:
        return "archive"
    if ext in {".py", ".js", ".html", ".css", ".json", ".ts", ".c", ".cpp", ".java", ".go", ".rs", ".sql"}:
        return "code"
    return "file"


def _inside_folder(root_real: str, path: str) -> bool:
    try:
        return os.path.commonpath([root_real, os.path.realpath(path)]) == root_real
    except ValueError:
        return False


def _unique_rel_path(candidate: str, used: set[str]) -> str:
    norm = candidate.replace("\\", "/").lstrip("/")
    if norm not in used:
        used.add(norm)
        return norm
    directory, filename = os.path.split(norm)
    base, ext = os.path.splitext(filename)
    counter = 1
    while True:
        alt_name = f"{base} ({counter}){ext}"
        alt = f"{directory}/{alt_name}" if directory else alt_name
        if alt not in used:
            used.add(alt)
            return alt
        counter += 1


def _build_batch_label(items: list[dict], dropped_roots: list[str]) -> str:
    if len(items) == 1:
        return items[0]["name"]
    if len(dropped_roots) == 1 and os.path.isdir(dropped_roots[0]):
        folder_name = os.path.basename(dropped_roots[0].rstrip("/\\"))
        if folder_name:
            return folder_name
    categories = {it["category"] for it in items}
    if categories == {"image"}:
        return f"{len(items)} photos"
    if categories == {"video"}:
        return f"{len(items)} videos"
    return f"{len(items)} files"


class BatchStore:
    """Thread-safe ephemeral store for PC -> Devices Send Batches."""

    def __init__(
        self,
        max_batches: int = 20,
        format_size_fn: Callable[[int], str] = _default_format_size,
        category_fn: Callable[[str], str] = _default_category,
    ):
        self.max_batches = max(1, int(max_batches))
        self._format_size = format_size_fn
        self._category = category_fn
        self._lock = threading.RLock()
        self._batches: "OrderedDict[str, dict]" = OrderedDict()

    def _expand_paths(self, paths: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
        """Expands files and directories into [(real_path, preferred_rel_path), ...]."""
        expanded: list[tuple[str, str]] = []
        valid_roots: list[str] = []
        for raw in paths or []:
            if not raw or not os.path.exists(raw):
                continue
            real_root = os.path.realpath(raw)
            valid_roots.append(real_root)
            if os.path.isfile(real_root):
                expanded.append((real_root, os.path.basename(real_root)))
            elif os.path.isdir(real_root):
                root_name = os.path.basename(real_root.rstrip("/\\")) or "folder"
                for walk_root, _, filenames in os.walk(real_root):
                    for fname in filenames:
                        fpath = os.path.join(walk_root, fname)
                        if not _inside_folder(real_root, fpath):
                            continue
                        real_file = os.path.realpath(fpath)
                        if not os.path.isfile(real_file):
                            continue
                        rel = os.path.relpath(fpath, real_root).replace("\\", "/")
                        expanded.append((real_file, f"{root_name}/{rel}"))
        return expanded, valid_roots

    def _make_item(self, real_path: str, rel_path: str) -> Optional[dict]:
        try:
            st = os.stat(real_path)
        except OSError:
            return None
        fname = os.path.basename(real_path)
        size = st.st_size
        return {
            "id": uuid.uuid4().hex[:10],
            "path": real_path,
            "name": fname,
            "rel_path": rel_path,
            "size": size,
            "formatted_size": self._format_size(size),
            "modified": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "mtime": st.st_mtime,
            "category": self._category(fname),
        }

    def _public_item(self, item: dict) -> dict:
        return {
            "id": item["id"],
            "name": item["name"],
            "rel_path": item["rel_path"],
            "size": item["size"],
            "formatted_size": item["formatted_size"],
            "modified": item["modified"],
            "category": item["category"],
            "is_dir": False,
        }

    def _summary(self, batch: dict) -> dict:
        items = batch["items"]
        thumbs = [it["id"] for it in items if it["category"] == "image"][:4]
        return {
            "id": batch["id"],
            "label": batch["label"],
            "source": batch["source"],
            "created_at": batch["created_at"],
            "file_count": len(items),
            "total_bytes": batch["total_bytes"],
            "formatted_size": self._format_size(batch["total_bytes"]),
            "download_count": batch["download_count"],
            "single_item": self._public_item(items[0]) if len(items) == 1 else None,
            "preview_thumbs": thumbs,
        }

    def create_or_merge(
        self,
        paths: list[str],
        source: str = "Drop zone",
        merge_window: float = 0.0,
    ) -> Optional[dict]:
        """Creates a new Send Batch or merges into the latest matching batch within merge_window."""
        expanded, valid_roots = self._expand_paths(paths)
        if not expanded:
            return None

        now_mono = time.monotonic()
        with self._lock:
            target = None
            if merge_window > 0 and self._batches:
                newest = next(reversed(self._batches.values()))
                if newest["source"] == source and (now_mono - newest["created_ts"]) <= merge_window:
                    target = newest

            if target is not None:
                seen_paths = target["seen_paths"]
                used_rels = target["used_rels"]
                for real_path, pref_rel in expanded:
                    if real_path in seen_paths:
                        continue
                    rel_path = _unique_rel_path(pref_rel, used_rels)
                    item = self._make_item(real_path, rel_path)
                    if not item:
                        continue
                    seen_paths.add(real_path)
                    target["items"].append(item)
                    target["items_by_id"][item["id"]] = item
                    target["total_bytes"] += item["size"]
                target["dropped_roots"].extend(valid_roots)
                target["label"] = _build_batch_label(target["items"], target["dropped_roots"])
                target["created_ts"] = now_mono
                self._batches.move_to_end(target["id"])
                return self._summary(target)

            seen_paths: set[str] = set()
            used_rels: set[str] = set()
            items: list[dict] = []
            items_by_id: dict[str, dict] = {}
            total_bytes = 0
            for real_path, pref_rel in expanded:
                if real_path in seen_paths:
                    continue
                rel_path = _unique_rel_path(pref_rel, used_rels)
                item = self._make_item(real_path, rel_path)
                if not item:
                    continue
                seen_paths.add(real_path)
                items.append(item)
                items_by_id[item["id"]] = item
                total_bytes += item["size"]

            if not items:
                return None

            batch_id = uuid.uuid4().hex[:12]
            batch = {
                "id": batch_id,
                "label": _build_batch_label(items, valid_roots),
                "source": source,
                "created_at": datetime.datetime.now().strftime("%H:%M"),
                "created_ts": now_mono,
                "items": items,
                "items_by_id": items_by_id,
                "seen_paths": seen_paths,
                "used_rels": used_rels,
                "dropped_roots": list(valid_roots),
                "total_bytes": total_bytes,
                "download_count": 0,
            }
            self._batches[batch_id] = batch
            while len(self._batches) > self.max_batches:
                self._batches.popitem(last=False)
            return self._summary(batch)

    def list_batches(self) -> list[dict]:
        with self._lock:
            return [self._summary(b) for b in reversed(self._batches.values())]

    def get_batch_summary(self, batch_id: str) -> Optional[dict]:
        with self._lock:
            b = self._batches.get(batch_id)
            return self._summary(b) if b else None

    def get_batch_items(self, batch_id: str) -> Optional[tuple[dict, list[dict]]]:
        with self._lock:
            b = self._batches.get(batch_id)
            if not b:
                return None
            return self._summary(b), [self._public_item(it) for it in b["items"]]

    def get_item(self, batch_id: str, item_id: str) -> Optional[dict]:
        with self._lock:
            b = self._batches.get(batch_id)
            if not b:
                return None
            it = b["items_by_id"].get(item_id)
            return dict(it) if it else None

    def collect_zip_files(
        self, batch_id: str, item_ids: Optional[list[str]] = None
    ) -> Optional[tuple[str, list[tuple[str, str, int]], int]]:
        with self._lock:
            b = self._batches.get(batch_id)
            if not b:
                return None
            if item_ids:
                chosen = [b["items_by_id"][iid] for iid in item_ids if iid in b["items_by_id"]]
                label = f"{b['label']}-selected"
            else:
                chosen = list(b["items"])
                label = b["label"]

        files_list: list[tuple[str, str, int]] = []
        total_bytes = 0
        for it in chosen:
            path = it["path"]
            if not os.path.isfile(path):
                continue
            try:
                fsize = os.path.getsize(path)
            except OSError:
                continue
            files_list.append((path, it["rel_path"], fsize))
            total_bytes += fsize
        return label, files_list, total_bytes

    def increment_downloads(self, batch_id: str) -> Optional[int]:
        with self._lock:
            b = self._batches.get(batch_id)
            if not b:
                return None
            b["download_count"] += 1
            return b["download_count"]

    def delete_batch(self, batch_id: str) -> bool:
        with self._lock:
            return self._batches.pop(batch_id, None) is not None

    def clear(self) -> int:
        with self._lock:
            count = len(self._batches)
            self._batches.clear()
            return count
