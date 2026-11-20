# Send Batches, Multi-Select Explorer, Sorting, Thumbnails & Bulk Shared Folders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the 100-photo Shared Folders clutter by introducing universal ephemeral PC $\rightarrow$ Devices Send Batches, add Select Mode multi-file ZIP downloads, expand sorting to 6 modes, add a Show/Hide Thumbnails toggle with root single-file thumbnail support, and add bulk add/remove on the Desktop Control Panel's Shared folders page.

**Architecture:** Extract ephemeral batch management into a focused, thread-safe `core/batches.py` (`BatchStore`) consumed by `NearBeamServer` (`core/server.py`) and reusing the existing async `zip_jobs` / `_run_zip_worker` / progress-ring pipeline for both Send Batch ZIPs and Safe List multi-select ZIPs. Keep permanent `safe_folders` in `core/config.py` with atomic batch add/remove methods, expose clean REST/SSE endpoints to the mobile/desktop Web App (`templates/index.html`, `static/js/app.js`, `static/css/style.css`), and wire bulk table operations and the Home "Ready for your devices" batch list in PySide6 (`ui/pages.py`, `gui.py`).

**Tech Stack:** Python 3.10+, Flask, Werkzeug, Pillow (thumbnails), PySide6 (desktop GUI), Vanilla ES6 JS / HTML5 / CSS3 (web app), `unittest` (backend & GUI tests), `node --check` (JS syntax verification).

**Spec:** `docs/superpowers/specs/2026-11-20-send-batches-explorer-bulk-shared-design.md`

## Global Constraints

- Ephemeral Send Batches live in memory only (never written to `config.json`, never copy source files to disk, auto-clear on PC restart, capped at 20 batches).
- Universal device & file support: Send Batches and Select Mode must work identically for all file types and all browsers (iPhone, iPad, Android, Mac, Windows, Linux).
- All LAN HTTP endpoints remain guarded by `only_lan_hostnames()`; `POST /api/shell-send` remains strictly localhost-only (`127.0.0.1`, `::1`).
- Every file lookup inside a Send Batch resolves strictly via immutable `item_id -> realpath` map; every Safe List path lookup resolves strictly via `config.resolve_safe_path()` + `inside_folder()`.
- Existing ZIP progress/cancel/download endpoints (`/api/safelist/zip-progress`, `/api/safelist/cancel-zip`, `/api/safelist/download-zip-file`) are reused without breaking existing contracts.
- All commits authored under `Tanumay Goswami <tanumaygoswami2001@gmail.com>`.

## Review Focus

- **Duplicate filenames from different folders in one Send Batch:** Two files named `IMG_0001.jpg` from separate directories dropped into one batch must get distinct `rel_path` entries (`IMG_0001.jpg`, `IMG_0001 (1).jpg`) so ZIP packaging preserves both files (pinned in Task 2).
- **Symlink / junction escape in dropped folder or selective ZIP:** A symlink inside a dropped directory or a `../` item inside `prepare-zip`'s `items` list must be skipped or rejected with `403` and never archived (pinned in Tasks 2 & 3).
- **Source file deleted from disk after batch creation:** Downloading a deleted item returns `404`; preparing a ZIP skips missing files and returns `400` if zero readable files remain rather than raising `500` (pinned in Task 3).
- **Windows Explorer rapid multi-file `--send` calls:** Separate `--send` invocations within 2.0 seconds from `"Explorer"` merge into one batch without duplicate paths, while a different source or later drop creates a new batch (pinned in Tasks 2 & 3).
- **Empty folder or nonexistent paths dropped:** Dropping only empty folders or nonexistent paths creates no ghost 0-file batch and leaves existing batches and `safe_folders` intact (pinned in Tasks 2, 3 & 5).

---

## File Structure

| File | Responsibility |
| :--- | :--- |
| `core/config.py` | Add single-save bulk `add_safe_folders(paths)` and `remove_safe_folders(folder_ids)` |
| `core/batches.py` *(new)* | Thread-safe `BatchStore` for ephemeral PC $\rightarrow$ Devices Send Batches, directory expansion, symlink filtering, Explorer rapid-drop merge, and `item_id` lookup |
| `core/server.py` | Mount `BatchStore`, add `/api/batches/*` routes, route `/api/shell-send` to Send Batches, add optional `items` to `/api/safelist/prepare-zip`, enrich single-file metadata on `/api/safelist`, accept `getlist("file")` on `/api/upload` |
| `templates/index.html` | Add `#incomingBatchesSection`, expand `#safeSortSelect` to 6 modes, add `#btnToggleThumbs` and `#btnToggleSelect` in `#browseToolbar`, add sticky `#selectionActionBar`, add iPhone/iPad Shortcut FAQ item |
| `static/js/app.js` | Implement Send Batch banner + batch browse view, 6-way sort, Show/Hide Thumbnails preference + root single-file thumbnails, Select Mode + selective ZIP/single-file download, SSE `batches_updated` + fix `fetchSafeFolders()` call |
| `static/css/style.css` | Style `#incomingBatchesSection`, thumbnail/select toolbar buttons, selectable cards, and sticky `#selectionActionBar` |
| `ui/pages.py` | Upgrade `SharedFoldersPage` (`ExtendedSelection`, bulk buttons, drag-and-drop hint) and add `batches_section` ("Ready for your devices") to `HomePage` |
| `gui.py` | Wire bulk add/remove/clear/drop on `SharedFoldersPage`, route PC drops & Send-to-phone to `create_send_batch`, render & clear active batches on `HomePage`, react to `batches_updated` SSE |
| `tests/test_batches.py` *(new)* | Isolated unit tests for `BatchStore` |
| `tests/test_backend.py` | Unit/integration tests for config bulk methods, `/api/batches/*`, `/api/shell-send`, selective `prepare-zip`, `/api/safelist` single-file metadata, multi-file `/api/upload`, and DOM assertions |
| `tests/test_gui_features.py` *(new)* | Offscreen PySide6 tests for bulk Shared Folders add/remove/clear and Home Send Batch display/clear |

---

### Task 1: Config Bulk Safe Folder Add & Remove (`core/config.py`)

**Files:**
- Modify: `core/config.py:163-202`
- Test: `tests/test_backend.py`

**Interfaces:**
- Consumes: `ConfigManager._lock`, `ConfigManager.save()`, `ConfigManager.get_safe_folders()`
- Produces:
  - `ConfigManager.add_safe_folders(self, paths: list[str]) -> list[dict]` — returns list of newly added entries (deduplicated against existing `safe_folders` and within `paths`; skips nonexistent paths; saves once).
  - `ConfigManager.remove_safe_folders(self, folder_ids: list[str]) -> int` — removes all matching IDs in one pass, saves once if count > 0, returns removed count.

- [ ] **Step 1: Write the failing test in `tests/test_backend.py`**

Add test method `test_bulk_add_and_remove_safe_folders` to `TestLandrop` in `tests/test_backend.py`:

```python
    def test_bulk_add_and_remove_safe_folders(self):
        dir_a = os.path.join(self.test_dir, "bulk_a")
        dir_b = os.path.join(self.test_dir, "bulk_b")
        file_c = os.path.join(self.test_dir, "bulk_c.txt")
        os.makedirs(dir_a, exist_ok=True)
        os.makedirs(dir_b, exist_ok=True)
        with open(file_c, "w") as f:
            f.write("bulk file")

        save_calls = []
        orig_save = self.cfg.save
        self.cfg.save = lambda: (save_calls.append(1), orig_save())
        try:
            added = self.cfg.add_safe_folders([
                dir_a,
                dir_b,
                dir_a,  # duplicate within batch
                file_c,
                os.path.join(self.test_dir, "does_not_exist"),
                self.safe_share_dir,  # already in safe_folders
            ])
            self.assertEqual(len(added), 3)
            self.assertEqual(len(save_calls), 1)

            ids = [e["id"] for e in added]
            removed = self.cfg.remove_safe_folders([ids[0], ids[2], "nonexistent_id"])
            self.assertEqual(removed, 2)
            self.assertEqual(len(save_calls), 2)

            remaining_ids = {f["id"] for f in self.cfg.get_safe_folders()}
            self.assertNotIn(ids[0], remaining_ids)
            self.assertIn(ids[1], remaining_ids)
            self.assertNotIn(ids[2], remaining_ids)

            # Removing no matching IDs must not write disk
            removed_zero = self.cfg.remove_safe_folders(["still_missing"])
            self.assertEqual(removed_zero, 0)
            self.assertEqual(len(save_calls), 2)
        finally:
            self.cfg.save = orig_save
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_backend.TestLandrop.test_bulk_add_and_remove_safe_folders -v`
Expected: FAIL with `AttributeError: 'ConfigManager' object has no attribute 'add_safe_folders'`

- [ ] **Step 3: Implement `add_safe_folders` and `remove_safe_folders` in `core/config.py`**

Add directly below `remove_safe_folder` in `core/config.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_backend.TestLandrop.test_bulk_add_and_remove_safe_folders -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/config.py tests/test_backend.py
git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit -m "feat(config): add single-save bulk add_safe_folders and remove_safe_folders"
```

---

### Task 2: Ephemeral `BatchStore` Module (`core/batches.py`)

**Files:**
- Create: `core/batches.py`
- Create: `tests/test_batches.py`

**Interfaces:**
- Consumes: `format_size`, `get_file_type_category`, `inside_folder` (passed or imported cleanly from small shared helpers — to avoid circular imports with `core/server.py`, `core/batches.py` defines clean helper imports or accepts `format_size`/`category_fn`, OR keeps self-contained `_inside_folder` and imports `format_size`/`get_file_type_category` lazily or defines `core/batches.py` cleanly without circular import). Better: `core/batches.py` accepts `format_size` and `get_category` callables with clean built-in defaults so `core/batches.py` has **zero** dependency on `core/server.py`!
- Produces (`core/batches.py`):
  - `BatchStore(max_batches=20, format_size_fn=..., category_fn=...)`
  - `create_or_merge(paths: list[str], source: str = "Drop zone", merge_window: float = 0.0) -> dict | None` — returns public summary dict or `None` if 0 readable files found.
  - `list_batches() -> list[dict]` — newest first summary dicts (`id`, `label`, `source`, `created_at`, `file_count`, `total_bytes`, `formatted_size`, `download_count`, `single_item`, `preview_thumbs`).
  - `get_batch_summary(batch_id: str) -> dict | None`
  - `get_batch_items(batch_id: str) -> tuple[dict, list[dict]] | None` — returns `(summary, public_items)`.
  - `get_item(batch_id: str, item_id: str) -> dict | None` — returns internal item dict including `"path"` and `"rel_path"`.
  - `collect_zip_files(batch_id: str, item_ids: list[str] | None = None) -> tuple[str, list[tuple[str, str, int]], int] | None` — returns `(archive_label, [(path, rel_path, size), ...], total_bytes)` skipping files deleted since creation.
  - `increment_downloads(batch_id: str) -> int | None`
  - `delete_batch(batch_id: str) -> bool`
  - `clear() -> int`

- [ ] **Step 1: Write failing unit tests in `tests/test_batches.py`**

Covers all Review Focus items for `BatchStore`: duplicate filenames across folders, symlink escape inside dropped folder, Explorer rapid-send merge within 2.0 s window (with deduplication & source isolation), empty folder / nonexistent paths returning `None`, 20-batch cap eviction, selective `item_ids` ZIP collection, and deleted-file skipping:

```python
"""Unit tests for core.batches.BatchStore."""

import os
import shutil
import tempfile
import time
import unittest

from core.batches import BatchStore


class TestBatchStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nearbeam_batches_test_")
        self.store = BatchStore(max_batches=3)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_file(self, rel_path: str, data: bytes = b"hello") -> str:
        full = os.path.join(self.tmp, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        return full

    def test_single_and_multi_file_batches_with_duplicate_basenames(self):
        p1 = self._make_file("dir1/IMG_0001.jpg", b"jpg-one")
        p2 = self._make_file("dir2/IMG_0001.jpg", b"jpg-two-longer")

        summary = self.store.create_or_merge([p1, p2], source="Drop zone")
        self.assertIsNotNone(summary)
        self.assertEqual(summary["file_count"], 2)
        self.assertEqual(summary["total_bytes"], len(b"jpg-one") + len(b"jpg-two-longer"))
        self.assertEqual(summary["label"], "2 photos")
        self.assertIsNone(summary["single_item"])
        self.assertEqual(len(summary["preview_thumbs"]), 2)

        _, items = self.store.get_batch_items(summary["id"])
        rel_paths = [it["rel_path"] for it in items]
        self.assertEqual(len(set(rel_paths)), 2)
        self.assertIn("IMG_0001.jpg", rel_paths)
        self.assertIn("IMG_0001 (1).jpg", rel_paths)

        # Single file batch exposes single_item and uses filename as label
        p3 = self._make_file("report.pdf", b"pdf-bytes")
        s_single = self.store.create_or_merge([p3], source="Send button")
        self.assertEqual(s_single["label"], "report.pdf")
        self.assertIsNotNone(s_single["single_item"])
        self.assertEqual(s_single["single_item"]["name"], "report.pdf")

    def test_directory_expansion_and_empty_folder_ignored(self):
        empty_dir = os.path.join(self.tmp, "empty_dir")
        os.makedirs(empty_dir, exist_ok=True)
        self.assertIsNone(self.store.create_or_merge([empty_dir, os.path.join(self.tmp, "missing.bin")]))
        self.assertEqual(len(self.store.list_batches()), 0)

        self._make_file("album/a.png", b"111")
        self._make_file("album/sub/b.txt", b"2222")
        album_dir = os.path.join(self.tmp, "album")
        summary = self.store.create_or_merge([album_dir], source="Drop zone")
        self.assertEqual(summary["file_count"], 2)
        self.assertEqual(summary["label"], "album")
        _, items = self.store.get_batch_items(summary["id"])
        rels = {it["rel_path"].replace("\\", "/") for it in items}
        self.assertEqual(rels, {"album/a.png", "album/sub/b.txt"})

    def test_explorer_rapid_send_merges_within_window(self):
        p1 = self._make_file("f1.txt", b"aaa")
        p2 = self._make_file("f2.txt", b"bbbb")
        p3 = self._make_file("f3.txt", b"ccccc")

        b1 = self.store.create_or_merge([p1], source="Explorer", merge_window=2.0)
        b2 = self.store.create_or_merge([p2, p1], source="Explorer", merge_window=2.0)  # p1 duplicate ignored
        self.assertEqual(b1["id"], b2["id"])
        self.assertEqual(b2["file_count"], 2)
        self.assertEqual(len(self.store.list_batches()), 1)

        # Different source does not merge
        b3 = self.store.create_or_merge([p3], source="Drop zone", merge_window=0.0)
        self.assertNotEqual(b3["id"], b1["id"])
        self.assertEqual(len(self.store.list_batches()), 2)

    def test_max_batches_cap_and_delete_clear(self):
        ids = []
        for i in range(4):
            p = self._make_file(f"file_{i}.txt", b"x")
            ids.append(self.store.create_or_merge([p], source="Drop zone")["id"])
        # max_batches=3 -> oldest (ids[0]) evicted
        listed_ids = [b["id"] for b in self.store.list_batches()]
        self.assertEqual(len(listed_ids), 3)
        self.assertNotIn(ids[0], listed_ids)
        self.assertEqual(listed_ids[0], ids[3])  # newest first

        self.assertTrue(self.store.delete_batch(ids[3]))
        self.assertFalse(self.store.delete_batch("unknown"))
        self.assertEqual(self.store.clear(), 2)
        self.assertEqual(self.store.list_batches(), [])

    def test_collect_zip_files_selective_and_missing_disk_file(self):
        p1 = self._make_file("keep.txt", b"keep-data")
        p2 = self._make_file("vanish.txt", b"gone-soon")
        summary = self.store.create_or_merge([p1, p2], source="Drop zone")
        _, items = self.store.get_batch_items(summary["id"])
        id_by_name = {it["name"]: it["id"] for it in items}

        # Delete second file from disk
        os.remove(p2)
        label, files_list, total = self.store.collect_zip_files(summary["id"])
        self.assertEqual(len(files_list), 1)
        self.assertEqual(files_list[0][1], "keep.txt")
        self.assertEqual(total, len(b"keep-data"))

        # Selective item_ids with only the deleted file yields empty list
        _, empty_files, empty_total = self.store.collect_zip_files(summary["id"], [id_by_name["vanish.txt"]])
        self.assertEqual(empty_files, [])
        self.assertEqual(empty_total, 0)

    def test_symlink_escape_skipped_during_directory_walk(self):
        outside = self._make_file("secret.txt", b"top-secret")
        safe_dir = os.path.join(self.tmp, "shared")
        os.makedirs(safe_dir, exist_ok=True)
        self._make_file("shared/ok.txt", b"public")
        link_path = os.path.join(safe_dir, "escape.txt")
        try:
            os.symlink(outside, link_path)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation requires elevated privileges on this Windows host")

        summary = self.store.create_or_merge([safe_dir], source="Drop zone")
        _, items = self.store.get_batch_items(summary["id"])
        names = [it["name"] for it in items]
        self.assertIn("ok.txt", names)
        self.assertNotIn("escape.txt", names)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests/test_batches.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.batches'`

- [ ] **Step 3: Create `core/batches.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests/test_batches.py -v`
Expected: PASS (all 6 tests pass)

- [ ] **Step 5: Commit**

```bash
git add core/batches.py tests/test_batches.py
git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit -m "feat(batches): add thread-safe ephemeral BatchStore for PC-to-device sends"
```

---

### Task 3: Server REST APIs — Send Batches, Selective Safe-List ZIP, Root Single-File Metadata & Multi-File Upload (`core/server.py`)

**Files:**
- Modify: `core/server.py` (imports, `NearBeamServer.__init__`, helper `_start_zip_job`, `/api/safelist`, `/api/safelist/prepare-zip`, `/api/batches/*`, `/api/upload`, `/api/shell-send`)
- Test: `tests/test_backend.py`

**Interfaces:**
- Consumes: `BatchStore` (`core/batches.py`), `NearBeamServer._run_zip_worker`, `make_thumbnail`, `_track_download`, `inside_folder`, `config.resolve_safe_path`
- Produces:
  - `NearBeamServer.create_send_batch(self, paths: list[str], source: str = "Drop zone", merge_window: float = 0.0) -> dict | None`
  - `GET /api/batches`, `GET /api/batches/<batch_id>/items`, `GET /api/batches/<batch_id>/download/<item_id>`, `GET /api/batches/<batch_id>/preview/<item_id>`, `GET /api/batches/<batch_id>/thumb/<item_id>`, `POST /api/batches/<batch_id>/prepare-zip`, `DELETE /api/batches/<batch_id>`, `POST /api/batches/clear`
  - `POST /api/safelist/prepare-zip` accepting optional `items: list[str]`
  - `GET /api/safelist` single-file metadata: `size`, `formatted_size`, `modified`, `category`
  - `POST /api/shell-send` creating/merging a Send Batch (`{"success": True, "added_count": N, "batch": summary}`) without touching `safe_folders`
  - `POST /api/upload` accepting multiple `file` fields via `getlist("file")`

- [ ] **Step 1: Write failing API tests in `tests/test_backend.py`**

Update `test_shell_send_file_and_folder` (split single-file Safe List check into `test_single_file_safelist_metadata_and_download`, and verify `/api/shell-send` creates/merges a Send Batch without adding to `safe_folders`) and add tests `test_send_batches_api_end_to_end`, `test_safelist_prepare_zip_selective_items_and_traversal`, and `test_upload_multi_file_field`:

```python
    def test_single_file_safelist_metadata_and_download(self):
        single_file = os.path.join(self.test_dir, "direct_share.png")
        with open(single_file, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake_png_bytes")
        self.cfg.add_safe_folder(single_file)

        sl_res = self.client.get("/api/safelist").get_json()
        file_entry = next((x for x in sl_res["folders"] if x["name"] == "direct_share.png"), None)
        self.assertIsNotNone(file_entry)
        self.assertTrue(file_entry["is_file"])
        self.assertEqual(file_entry["category"], "image")
        self.assertEqual(file_entry["size"], len(b"\x89PNG\r\n\x1a\nfake_png_bytes"))
        self.assertTrue(file_entry["formatted_size"])

        dl_res = self.client.get(f"/api/safelist/download?folder_id={file_entry['id']}&subpath=")
        self.assertEqual(dl_res.status_code, 200)
        self.assertEqual(dl_res.data, b"\x89PNG\r\n\x1a\nfake_png_bytes")
        dl_res.close()

    def test_shell_send_creates_and_merges_batch_without_cluttering_safelist(self):
        f1 = os.path.join(self.test_dir, "photo1.jpg")
        f2 = os.path.join(self.test_dir, "photo2.jpg")
        with open(f1, "wb") as f:
            f.write(b"jpg1")
        with open(f2, "wb") as f:
            f.write(b"jpg2")

        before_safe_count = len(self.cfg.get_safe_folders())
        r1 = self.client.post("/api/shell-send", json={"paths": [f1]})
        self.assertEqual(r1.status_code, 200)
        d1 = r1.get_json()
        self.assertTrue(d1["success"])
        self.assertIn("batch", d1)

        # Second rapid call merges into the same Explorer batch
        r2 = self.client.post("/api/shell-send", json={"paths": [f2]})
        self.assertEqual(r2.status_code, 200)
        d2 = r2.get_json()
        self.assertEqual(d1["batch"]["id"], d2["batch"]["id"])
        self.assertEqual(d2["batch"]["file_count"], 2)

        # Permanent Safe List stays untouched
        self.assertEqual(len(self.cfg.get_safe_folders()), before_safe_count)

    def test_send_batches_api_end_to_end(self):
        import time, zipfile
        f1 = os.path.join(self.test_dir, "doc1.txt")
        f2 = os.path.join(self.test_dir, "doc2.txt")
        with open(f1, "w") as f:
            f.write("batch file one")
        with open(f2, "w") as f:
            f.write("batch file two")

        batch = self.server.create_send_batch([f1, f2], source="Drop zone")
        self.assertIsNotNone(batch)
        bid = batch["id"]

        # List & items
        listed = self.client.get("/api/batches").get_json()["batches"]
        self.assertEqual(len(listed), 1)
        items_res = self.client.get(f"/api/batches/{bid}/items").get_json()
        self.assertEqual(len(items_res["items"]), 2)
        item1 = next(it for it in items_res["items"] if it["name"] == "doc1.txt")
        item2 = next(it for it in items_res["items"] if it["name"] == "doc2.txt")

        # Single item download + preview + invalid item_id 404
        dl = self.client.get(f"/api/batches/{bid}/download/{item1['id']}")
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.data, b"batch file one")
        dl.close()
        self.assertEqual(self.client.get(f"/api/batches/{bid}/download/bogus").status_code, 404)

        # Selective ZIP of just item2
        prep = self.client.post(f"/api/batches/{bid}/prepare-zip", json={"item_ids": [item2["id"]]})
        self.assertEqual(prep.status_code, 200)
        job_id = prep.get_json()["job_id"]
        for _ in range(60):
            if self.client.get(f"/api/safelist/zip-progress?job_id={job_id}").get_json()["status"] == "completed":
                break
            time.sleep(0.05)
        zdl = self.client.get(f"/api/safelist/download-zip-file?job_id={job_id}")
        self.assertEqual(zdl.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(zdl.data), "r") as zf:
            self.assertEqual(zf.namelist(), ["doc2.txt"])
            self.assertEqual(zf.read("doc2.txt"), b"batch file two")
        zdl.close()

        # Deleted source file returns 404 on download and 400 on ZIP when no files remain
        os.remove(f1)
        os.remove(f2)
        self.assertEqual(self.client.get(f"/api/batches/{bid}/download/{item1['id']}").status_code, 404)
        self.assertEqual(self.client.post(f"/api/batches/{bid}/prepare-zip", json={}).status_code, 400)

        # Delete & clear
        self.assertEqual(self.client.delete(f"/api/batches/{bid}").status_code, 200)
        self.assertEqual(self.client.get("/api/batches").get_json()["batches"], [])

    def test_safelist_prepare_zip_selective_items_and_traversal(self):
        import time, zipfile
        extra = os.path.join(self.safe_share_dir, "skip_me.txt")
        with open(extra, "w") as f:
            f.write("should not be in zip")
        folder_id = self.safe_entry["id"]

        # Traversal inside items list MUST be rejected with 403
        bad = self.client.post(
            "/api/safelist/prepare-zip",
            json={"folder_id": folder_id, "subpath": "", "items": ["../test_config.json"]},
        )
        self.assertEqual(bad.status_code, 403)

        # Select only hello.txt and subfolder/ (excluding skip_me.txt)
        prep = self.client.post(
            "/api/safelist/prepare-zip",
            json={"folder_id": folder_id, "subpath": "", "items": ["hello.txt", "subfolder"]},
        )
        self.assertEqual(prep.status_code, 200)
        job_id = prep.get_json()["job_id"]
        for _ in range(60):
            if self.client.get(f"/api/safelist/zip-progress?job_id={job_id}").get_json()["status"] == "completed":
                break
            time.sleep(0.05)
        zdl = self.client.get(f"/api/safelist/download-zip-file?job_id={job_id}")
        self.assertEqual(zdl.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(zdl.data), "r") as zf:
            names = {n.replace("\\", "/") for n in zf.namelist()}
            self.assertEqual(names, {"hello.txt", "subfolder/nested.txt"})
        zdl.close()

    def test_upload_multi_file_field(self):
        res = self.client.post(
            "/api/upload",
            data={"file": [(io.BytesIO(b"one"), "a.txt"), (io.BytesIO(b"two"), "b.txt")]},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["saved_count"], 2)
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `python -m unittest tests.test_backend.TestLandrop.test_send_batches_api_end_to_end tests.test_backend.TestLandrop.test_safelist_prepare_zip_selective_items_and_traversal tests.test_backend.TestLandrop.test_upload_multi_file_field -v`
Expected: FAIL (`AttributeError` / `404` / assertion failures)

- [ ] **Step 3: Implement Server API changes in `core/server.py`**

1. Import `BatchStore` from `core.batches` and initialize `self.batch_store = BatchStore(format_size_fn=format_size, category_fn=get_file_type_category)` in `NearBeamServer.__init__`.
2. Add helper methods on `NearBeamServer`:
   - `create_send_batch(self, paths: list[str], source: str = "Drop zone", merge_window: float = 0.0) -> Optional[dict]` — calls `self.batch_store.create_or_merge(...)`, logs activity, broadcasts `{"type": "batches_updated", "batch_id": batch["id"], "label": batch["label"], "file_count": batch["file_count"]}`, returns summary.
   - `_start_zip_job(self, folder_name: str, files_list: list[tuple[str, str, int]], total_bytes: int) -> dict` — shared by `/api/safelist/prepare-zip` and `/api/batches/<batch_id>/prepare-zip` so ZIP job creation is DRY.
3. Enrich `GET /api/safelist`: when `is_file and exists`, stat the file and include `size`, `formatted_size`, `modified`, `category`.
4. Extend `POST /api/safelist/prepare-zip`: when `data.get("items")` is a non-empty list, resolve each item subpath via `config.resolve_safe_path(folder_id, item_subpath)` (returning `403` on `PermissionError`), collect files (walking subdirectories with `inside_folder(base_real, fpath)` check) with archive names relative to `real_target`, and call `self._start_zip_job(f"{folder_name}-selected", files_list, total_bytes)`.
5. Add Send Batch routes:
   - `GET /api/batches`
   - `GET /api/batches/<batch_id>/items`
   - `GET /api/batches/<batch_id>/download/<item_id>`
   - `GET /api/batches/<batch_id>/preview/<item_id>`
   - `GET /api/batches/<batch_id>/thumb/<item_id>`
   - `POST /api/batches/<batch_id>/prepare-zip`
   - `DELETE /api/batches/<batch_id>`
   - `POST /api/batches/clear`
6. Update `POST /api/shell-send`: call `self.create_send_batch(paths, source="Explorer", merge_window=2.0)` (returns `400` if no readable files found; never touches `config.safe_folders` or copies files).
7. Update `POST /api/upload`: `uploaded_files = request.files.getlist("files") or request.files.getlist("file")`.

- [ ] **Step 4: Run backend test suite to verify all tests pass**

Run: `python -m unittest tests/test_batches.py tests/test_backend.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/server.py tests/test_backend.py
git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit -m "feat(server): add Send Batches API, selective Safe List ZIP, single-file metadata, and multi-file upload field"
```

---

### Task 4: Web App UI — Incoming Send Batches, Select Mode, 6-Way Sort, Thumbnail Toggle, Root Single-File Thumbnails & iOS Shortcut FAQ (`templates/index.html`, `static/js/app.js`, `static/css/style.css`)

**Files:**
- Modify: `templates/index.html`
- Modify: `static/js/app.js`
- Modify: `static/css/style.css`
- Test: `tests/test_backend.py` (`test_frontend_dom_and_shell_integration`) + `node --check static/js/app.js`

**Interfaces:**
- Consumes: `/api/batches*`, `/api/safelist`, `/api/safelist/prepare-zip` (`items`), `/api/safelist/thumb`, SSE `batches_updated` & `safelist_updated`
- Produces:
  - HTML IDs: `#incomingBatchesSection`, `#incomingBatchesList`, `#btnClearAllBatches`, `#safeSortSelect` (6 options: `newest`, `oldest`, `size-desc`, `size-asc`, `name-asc`, `name-desc`), `#btnToggleThumbs`, `#btnToggleSelect`, `#selectionActionBar`, `#selectionCountLabel`, `#btnSelectAllVisible`, `#btnCancelSelect`, `#btnDownloadSelected`, `#btnCopyShortcutUrl`
  - `localStorage` keys: `nearbeam.browseSort`, `nearbeam.browseView`, `nearbeam.showThumbs`

- [ ] **Step 1: Add DOM assertions to `test_frontend_dom_and_shell_integration` in `tests/test_backend.py`**

Extend `required_ids` in `test_frontend_dom_and_shell_integration` with:
```python
            "incomingBatchesSection",
            "incomingBatchesList",
            "btnToggleThumbs",
            "btnToggleSelect",
            "selectionActionBar",
            "btnDownloadSelected",
            "btnSelectAllVisible",
            "btnCancelSelect",
```
And verify sort options (`size-desc`, `size-asc`, `oldest`, `name-desc`) and `Send to PC (NearBeam)` FAQ copy exist in `index_html`.

- [ ] **Step 2: Run DOM test to verify it fails**

Run: `python -m unittest tests.test_backend.TestLandrop.test_frontend_dom_and_shell_integration -v`
Expected: FAIL with missing DOM element `#incomingBatchesSection`

- [ ] **Step 3: Update `templates/index.html`**

1. Insert `#incomingBatchesSection` (hidden by default) right below `#nearbyDashboardCard` and above `#tab-safelist`:
   ```html
   <!-- INCOMING SEND BATCHES FROM PC -->
   <section class="incoming-batches-card hidden" id="incomingBatchesSection">
     <div class="incoming-batches-header">
       <div class="incoming-batches-title-wrap">
         <span class="mesh-dot"></span>
         <h3 class="incoming-batches-title">Incoming from PC</h3>
         <span class="nearby-role-tag" id="incomingBatchesCountBadge">0</span>
       </div>
       <button type="button" class="card-action-btn" id="btnClearAllBatches">Clear all</button>
     </div>
     <div class="incoming-batches-list" id="incomingBatchesList"></div>
   </section>
   ```
2. Update `#browseToolbar` in `#tab-safelist`:
   - Expand `#safeSortSelect` to:
     - `newest`: Newest first
     - `oldest`: Oldest first
     - `size-desc`: Largest first
     - `size-asc`: Smallest first
     - `name-asc`: Name A–Z
     - `name-desc`: Name Z–A
   - Add `#btnToggleThumbs` (`aria-pressed="true"`, title `"Show/hide thumbnails"`) and `#btnToggleSelect` (`aria-pressed="false"`, `"Select"`).
3. Add sticky `#selectionActionBar` (hidden by default) right inside `<main>` (or bottom of `#tab-safelist`):
   ```html
   <div class="selection-bar hidden" id="selectionActionBar" role="region" aria-label="Selected files actions">
     <div class="selection-bar-info">
       <span id="selectionCountLabel">0 selected</span>
     </div>
     <div class="selection-bar-actions">
       <button type="button" class="card-action-btn" id="btnSelectAllVisible">Select all</button>
       <button type="button" class="card-action-btn" id="btnCancelSelect">Cancel</button>
       <button type="button" class="primary-btn btn-xs" id="btnDownloadSelected" disabled>Download</button>
     </div>
   </div>
   ```
4. Add collapsible iPhone/iPad Share Sheet Shortcut FAQ item inside `#tab-faq` with `#shortcutUploadUrl` and `#btnCopyShortcutUrl`.

- [ ] **Step 4: Implement JS logic in `static/js/app.js`**

1. Add state: `let activeBatches = []; let currentBatchId = ""; let currentBatchMeta = null; let selectMode = false; const selectedKeys = new Set(); let lastRenderedItems = [];`.
2. Update `loadBrowsePrefs()` / `saveBrowsePrefs()`:
   - Support 6 sort keys (`newest`, `oldest`, `size-desc`, `size-asc`, `name-asc`, `name-desc`; migrate legacy `"name"` -> `"name-asc"`).
   - Add `thumbs: localStorage.getItem("nearbeam.showThumbs") !== "false"`.
3. Update `compareBrowseItems(a, b)`:
   - Folders always first; handle all 6 sort modes (`size-desc`, `size-asc`, `newest`, `oldest`, `name-asc`, `name-desc`) with `name-asc` tie-breaker.
4. Wire `#btnToggleThumbs`:
   - Toggles `browsePrefs.thumbs`, saves pref, updates `aria-pressed`, re-renders active view (folder/batch/root + incoming batches strip).
5. Fix `showAllSafeFolders()`:
   - Exits batch view (`currentBatchId = ""`) and Select mode.
   - For entries with `f.is_file`: render file icon (`f.category || "file"`), thumbnail via `/api/safelist/thumb?folder_id=${encodeURIComponent(f.id)}&subpath=` when `browsePrefs.thumbs && f.category === "image"`, meta `${f.formatted_size || ""} · Shared file`, and click opens preview modal (or download button downloads directly).
6. Implement **Send Batches** functions:
   - `fetchSendBatches(showNewToast = false)` -> `GET /api/batches`, updates `activeBatches`, calls `renderIncomingBatches()`.
   - `renderIncomingBatches()` -> shows/hides `#incomingBatchesSection`, renders cards with up to 4 mini thumbs (only when `browsePrefs.thumbs`), **Download** (direct when `file_count === 1`) or **Download All (ZIP)** (`startBatchZipDownload(batch.id, batch.label)`), **View files** (`openSendBatch(batch.id)`), and **Dismiss** (`DELETE /api/batches/<id>`).
   - `openSendBatch(batchId)` -> switches to `#tab-safelist`, sets `currentBatchId = batchId`, `currentFolderId = ""`, fetches `GET /api/batches/<id>/items`, renders breadcrumbs (`Safe List / Incoming: <label>`), populates `safeItemsCache`, enables `#browseToolbar`, calls `applyBrowseView()`.
   - Update `safeItemUrl(kind, item)` so when `currentBatchId` is set, it returns `/api/batches/${encodeURIComponent(currentBatchId)}/${kind}/${encodeURIComponent(item.id)}`; otherwise `/api/safelist/${kind}?folder_id=...&subpath=...`.
   - `startBatchZipDownload(batchId, label, itemIds = null)` -> posts to `/api/batches/<id>/prepare-zip` and hands `job_id` straight to `pollZipProgress(...)`!
7. Implement **Select Mode**:
   - `setSelectMode(on)` toggles `selectMode`, clears `selectedKeys`, updates `#btnToggleSelect` `aria-pressed`, updates `#selectionActionBar` visibility, and re-renders cards.
   - Each card in `renderSafeItems()` uses key `currentBatchId ? item.id : item.subpath`; when `selectMode` is active, adds `.selectable` / `.selected` + checkbox pill, and card click toggles key in `selectedKeys` + calls `updateSelectionBar()`.
   - `updateSelectionBar()` computes selected count and byte total, toggles Select all / Deselect all button label, enables/disables `#btnDownloadSelected`.
   - `#btnDownloadSelected` click:
     - If 1 file (non-dir) selected -> triggers direct file download (`safeItemUrl("download", item)`), exits Select mode.
     - Otherwise -> calls `startBatchZipDownload(currentBatchId, label, Array.from(selectedKeys))` (in batch mode) or `startFolderZipDownload(currentFolderId, currentSubpath, Array.from(selectedKeys))` (in folder mode, passing `items` to `POST /api/safelist/prepare-zip`), exits Select mode.
8. Wire SSE in `setupSSE()`:
   - Fix `safelist_updated` to call `fetchSafeFolders()` (replacing broken `fetchSafelist()`).
   - Handle `batches_updated`: call `fetchSendBatches(true)` (and if currently viewing a batch that was dismissed, return to `showAllSafeFolders()`).
   - Call `fetchSendBatches(false)` on `DOMContentLoaded`.
   - Populate `#shortcutUploadUrl` in `fetchServerInfo()` and wire `#btnCopyShortcutUrl`.

- [ ] **Step 5: Add CSS styles in `static/css/style.css`**

Add clean styles matching existing design tokens (`--surface-card`, `--accent-cyan`, `--border-subtle`, `--radius-md`) for:
- `.incoming-batches-card`, `.incoming-batch-row`, `.batch-mini-thumbs`, `.batch-actions`
- `.tool-toggle-btn` (`aria-pressed="true"` highlight) for Thumbnails & Select buttons
- `.file-card.selectable`, `.file-card.selected`, `.select-check-badge`
- `.selection-bar` (sticky bottom bar with backdrop blur, responsive on mobile screens)

- [ ] **Step 6: Verify JS syntax and DOM tests pass**

Run: `node --check static/js/app.js && python -m unittest tests.test_backend.TestLandrop.test_frontend_dom_and_shell_integration -v`
Expected: `0` exit code and `OK`

- [ ] **Step 7: Commit**

```bash
git add templates/index.html static/js/app.js static/css/style.css tests/test_backend.py
git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit -m "feat(web): add Incoming Send Batches, Select Mode ZIP, 6-way sort, thumbnail toggle, root thumbs, and iOS Shortcut FAQ"
```

---

### Task 5: Desktop Control Panel — Bulk Shared Folders & Home Send Batches (`ui/pages.py`, `gui.py`)

**Files:**
- Modify: `ui/pages.py:74-200`
- Modify: `gui.py` (`SharedFoldersPage` wiring, `HomePage` batch rendering, `_handle_shared_paths`, `dropEvent`, `_drain_server_events`, `_add_safe_files`, `_remove_safe_folder`, `_clear_safe_folders`)
- Create: `tests/test_gui_features.py`

**Interfaces:**
- Consumes: `ConfigManager.add_safe_folders`, `ConfigManager.remove_safe_folders`, `NearBeamServer.create_send_batch`, `BatchStore.list_batches`, `BatchStore.delete_batch`, `BatchStore.clear`
- Produces:
  - `SharedFoldersPage`: `table` (`ExtendedSelection`), `btn_add`, `btn_add_files`, `btn_select_all`, `btn_remove`, `btn_clear_all`
  - `HomePage`: `batches_section` widget (`batches_box`, `btn_clear_batches`, `set_batches(batches, on_clear)`)
  - `MainWindow`: `_add_safe_paths(paths)`, `_add_safe_files()`, `_remove_safe_folder()` (bulk), `_clear_safe_folders()`, `_refresh_batches_ui()`

- [ ] **Step 1: Write failing offscreen PySide6 tests in `tests/test_gui_features.py`**

```python
"""Offscreen PySide6 GUI tests for bulk Shared Folders and Home Send Batches."""

import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTableWidget

from core.config import ConfigManager


class TestGuiFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nearbeam_gui_test_")
        self.cfg = ConfigManager(os.path.join(self.tmp, "cfg.json"))
        self.cfg.set_save_directory(os.path.join(self.tmp, "save"))

        import core.config as cfg_mod
        import core.server as srv_mod
        import gui as gui_mod
        self._orig = (cfg_mod.config, srv_mod.config, gui_mod.config)
        cfg_mod.config = srv_mod.config = gui_mod.config = self.cfg

        self.win = gui_mod.MainWindow()

    def tearDown(self):
        self.win.server_thread.shutdown()
        self.win.server_instance.shutdown()
        self.win.close()
        import core.config as cfg_mod
        import core.server as srv_mod
        import gui as gui_mod
        cfg_mod.config, srv_mod.config, gui_mod.config = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shared_folders_bulk_add_select_and_remove(self):
        self.assertEqual(self.win.safe_table.selectionMode(), QTableWidget.ExtendedSelection)
        self.assertTrue(hasattr(self.win.shared_page, "btn_add_files"))
        self.assertTrue(hasattr(self.win.shared_page, "btn_select_all"))
        self.assertTrue(hasattr(self.win.shared_page, "btn_clear_all"))

        f1 = os.path.join(self.tmp, "one.txt")
        f2 = os.path.join(self.tmp, "two.txt")
        d1 = os.path.join(self.tmp, "dir1")
        os.makedirs(d1, exist_ok=True)
        for p in (f1, f2):
            with open(p, "w") as fh:
                fh.write("data")

        self.win._add_safe_paths([f1, f2, d1])
        self.assertEqual(self.win.safe_table.rowCount(), 3)

        # Select rows 0 and 2 and remove without modal prompt via helper
        self.win.safe_table.selectRow(0)
        self.win.safe_table.selectionModel().select(
            self.win.safe_table.model().index(2, 0),
            self.win.safe_table.selectionModel().SelectionFlag.Select
            | self.win.safe_table.selectionModel().SelectionFlag.Rows,
        )
        self.win._remove_selected_safe_rows(confirm=False)
        self.assertEqual(self.win.safe_table.rowCount(), 1)

        # Clear all
        self.win._clear_safe_folders(confirm=False)
        self.assertEqual(self.win.safe_table.rowCount(), 0)

    def test_send_to_phone_creates_batch_not_shared_folder(self):
        photos = []
        for i in range(5):
            p = os.path.join(self.tmp, f"img_{i}.jpg")
            with open(p, "wb") as fh:
                fh.write(b"fakejpg")
            photos.append(p)

        self.win._handle_shared_paths(photos, source="Drop zone", send_to_peers=False)

        # Permanent Shared folders MUST stay empty
        self.assertEqual(len(self.cfg.get_safe_folders()), 0)
        self.assertEqual(self.win.safe_table.rowCount(), 0)

        # Active Send Batch created & visible on HomePage
        batches = self.win.server_instance.batch_store.list_batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["file_count"], 5)
        self.assertFalse(self.win.home.batches_section.isHidden())

        # Empty folder drop creates no ghost batch
        empty_dir = os.path.join(self.tmp, "empty")
        os.makedirs(empty_dir, exist_ok=True)
        self.win._handle_shared_paths([empty_dir], source="Drop zone", send_to_peers=False)
        self.assertEqual(len(self.win.server_instance.batch_store.list_batches()), 1)

        # Clear all batches hides Home section
        self.win._clear_all_batches()
        self.assertEqual(len(self.win.server_instance.batch_store.list_batches()), 0)
        self.assertTrue(self.win.home.batches_section.isHidden())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run GUI test to verify it fails**

Run: `python -m unittest tests/test_gui_features.py -v`
Expected: FAIL (`AssertionError: SingleSelection != ExtendedSelection`)

- [ ] **Step 3: Update `ui/pages.py`**

1. In `HomePage`:
   - Update drop zone hint text to: `"Drop files to send to a nearby PC or share as a 1-tap bundle for your phone."`
   - Add `self.batches_section` container between `drop_zone` and `Recent transfers` (hidden when empty) with header `"Ready for your devices"` + `self.btn_clear_batches = button("Clear all", kind="linkBtn")` and `self.batches_box = QVBoxLayout()`.
   - Add `HomePage.set_batches(self, batches: list[dict], on_clear)`: hides section when empty; otherwise builds compact `Card` rows showing icon, `batch["label"]`, `"{file_count} files · {formatted_size} · Sent {created_at}"` (+ `" · Downloaded {N}×"` when `download_count > 0`), and inline Clear button (`icon_button("x", "Remove batch")`).
2. In `SharedFoldersPage`:
   - Update subtitle: `"Devices on your Wi-Fi can browse and download what's listed here. Drag & drop multiple folders or files here to add them at once."`
   - Set `self.table.setSelectionMode(QTableWidget.ExtendedSelection)`.
   - Add buttons:
     - `self.btn_add = button("Add folder", "plus", "primaryBtn")`
     - `self.btn_add_files = button("Add files...", "file")`
     - `self.btn_select_all = button("Select all", "check")`
     - `self.btn_remove = button("Remove selected", "trash", "dangerBtn")`
     - `self.btn_clear_all = button("Clear all", kind="linkBtn")`

- [ ] **Step 4: Update `gui.py`**

1. Wire `SharedFoldersPage` buttons & signals:
   - `btn_add` -> `self._add_safe_folder`
   - `btn_add_files` -> `self._add_safe_files`
   - `btn_select_all` -> `self.safe_table.selectAll`
   - `btn_remove` -> `lambda: self._remove_selected_safe_rows(confirm=True)`
   - `btn_clear_all` -> `lambda: self._clear_safe_folders(confirm=True)`
   - `self.safe_table.itemSelectionChanged.connect(self._update_safe_selection_buttons)`
   - `QShortcut(QKeySequence(Qt.Key_Delete), self.safe_table, lambda: self._remove_selected_safe_rows(confirm=True))`
2. Implement `_add_safe_paths(paths)`, `_add_safe_files()`, `_update_safe_selection_buttons()`, `_remove_selected_safe_rows(confirm=True)`, `_clear_safe_folders(confirm=True)` using `config.add_safe_folders` / `config.remove_safe_folders` and broadcasting a single `safelist_updated` SSE event.
3. Update `MainWindow.dropEvent`:
   - If `self.stack.currentWidget() is self.shared_page`: call `self._add_safe_paths(dropped_paths)`.
   - Otherwise: call `self._handle_shared_paths(dropped_paths, source="Window Drag & Drop")`.
4. Update `_handle_shared_paths(paths, source, send_to_peers=True)`:
   - Create an ephemeral Send Batch via `self.server_instance.create_send_batch(valid_paths, source=source, merge_window=2.0 if source == "Explorer" else 0.0)` instead of calling `config.add_safe_folder()`!
   - Refresh `self._refresh_batches_ui()`, log clean summary, show tray toast `"Ready on your devices — {label} ({formatted_size})"` when not sending to peers.
   - Preserve peer-PC outgoing push when `send_to_peers=True` and peer PCs are online.
5. Wire `HomePage.btn_clear_batches` -> `self._clear_all_batches`, add `_dismiss_batch(batch_id)` and `_refresh_batches_ui()`, and handle `etype == "batches_updated"` in `_drain_server_events()`.

- [ ] **Step 5: Run full test suite (all unit, API, JS, and GUI tests)**

Run:
```powershell
node --check static/js/app.js
python -m unittest tests/test_batches.py tests/test_backend.py tests/test_gui_features.py -v
```
Expected: ALL PASS

- [ ] **Step 6: Commit & push**

```bash
git add ui/pages.py gui.py tests/test_gui_features.py
git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit -m "feat(gui): add bulk add/remove on Shared folders and Home Ready-for-devices Send Batches"
```
