# NearBeam: Send Batches, Multi-Select Explorer, Sorting, Thumbnails & Bulk Shared Folders

**Date:** 2026-11-20
**Status:** Draft for Review
**Scope:** Desktop Control Panel (`gui.py`, `ui/pages.py`), Config (`core/config.py`), Server API (`core/server.py`, `core/batches.py`), and Web App (`templates/index.html`, `static/js/app.js`, `static/css/style.css`).

---

## 1. Problem & Goals

When a user sends 100 photos (or mixed files) from their host PC to a phone, tablet, or other browser today:
1. NearBeam appends all 100 files individually into permanent `safe_folders` (`config.json`), cluttering the PC's **Shared folders** list.
2. On the receiving device (iPhone, Android, laptop browser), each photo appears at the Safe List root with a generic folder icon and requires 100 separate download taps.
3. On the PC Control Panel, `SharedFoldersPage` only allows single-folder add and single-row removal with a confirmation popup per row.
4. Inside the Web Explorer, users can only sort by `Newest first` or `Name A–Z`, cannot toggle thumbnails on/off, and cannot multi-select a subset of files to download as a single ZIP.

### Goals
1. **Universal PC $\rightarrow$ Devices "Send Batches":** Separate temporary one-off PC sends from permanent Shared folders so sending 100 files of any type creates **1 ephemeral Send Batch** that any device (iPhone, iPad, Android, Mac, Windows, Linux) can grab in 1 tap or inspect individually.
2. **Multi-Select Mode ("Select" button):** Allow selecting arbitrary subsets of files/folders inside **both** Shared folders and Send Batches and downloading them as 1 ZIP (or direct download when 1 file is selected).
3. **Expanded Sorting & Thumbnail Toggle:** Add 6 sort modes (including size largest/smallest), add a Show/Hide Thumbnails toggle, and fix single shared image files at the Safe List root to display real thumbnails and file sizes.
4. **Bulk Add & Bulk Remove on PC:** Enable multi-select, multi-file/drag-drop add, batch removal, and clear-all on `SharedFoldersPage` with single-write config batching.
5. **iOS/iPadOS Share Sheet Upload Polish:** Support multi-file `file` form field alongside `files` on `/api/upload`, and include the step-by-step **iPhone/iPad "Send to PC" Shortcut** card in Help & FAQ.

---

## 2. Architecture & Component Boundaries

### New Focused Module: `core/batches.py` (`BatchStore`)
To keep `core/server.py` from growing further, all Send Batch state, path expansion, deduplication, Explorer rapid-drop merging, and allowlisted item lookup live in a small, thread-safe, independently testable class `BatchStore`:

- **Lifetime:** Ephemeral, in-memory only (auto-clears on PC restart; zero writes to `config.json`, zero file copies on disk).
- **Data Model:**
  ```python
  BatchItem = {
      "id": str,           # 10-char hex unique within batch
      "path": str,         # canonical os.path.realpath()
      "name": str,         # basename
      "rel_path": str,     # archive path inside ZIP (preserves dropped folder structure; deduped on collisions)
      "size": int,
      "formatted_size": str,
      "modified": str,     # "YYYY-MM-DD HH:MM"
      "mtime": float,
      "category": str,     # get_file_type_category(name)
  }

  SendBatch = {
      "id": str,           # 12-char hex
      "label": str,        # e.g. "report.pdf" (1 file) or "100 photos" / "12 files" / dropped folder name
      "source": str,       # e.g. "Drop zone", "Send button", "Explorer"
      "created_at": str,   # "HH:MM"
      "created_ts": float, # monotonic timestamp for merge window
      "items": list[BatchItem],
      "items_by_id": dict[str, BatchItem],
      "file_count": int,
      "total_bytes": int,
      "formatted_size": str,
      "download_count": int,
  }
  ```
- **Path Expansion & Security:**
  - Single files become 1 `BatchItem` with `rel_path = basename` (if duplicate basenames are dropped from different folders into one batch, suffix `name (1).ext` in `rel_path` so ZIP entries never collide).
  - Dropped directories are walked with `os.walk`, skipping entries where `inside_folder(root_real, file_path)` is `False` (prevents symlink/junction escapes) and preserving `<folder_name>/<relative_subpath>` in `rel_path`.
  - Every download, preview, thumbnail, and ZIP lookup resolves strictly by `item_id` in `batch["items_by_id"]` — no user-supplied path string is ever joined to the filesystem.
- **Windows Explorer Multi-Select Merge (`merge_window=2.0` seconds):**
  - When Windows Explorer invokes `NearBeam --send "%1"` for multiple selected files, each file arrives as a separate call within milliseconds.
  - `BatchStore.create_or_merge(paths, source="Explorer", merge_window=2.0)` checks if the newest batch has `source == "Explorer"` and `monotonic() - batch.created_ts <= merge_window`; if so, it appends any new canonical paths (deduplicated by `realpath`) into that batch and refreshes `label`, `file_count`, `total_bytes`, and `created_ts`.
- **Cap:** Keep at most the 20 most recent active batches (evicting oldest if exceeded).

---

## 3. REST API & SSE Contract (`core/server.py`)

All new routes remain protected by existing `only_lan_hostnames()` DNS-rebinding middleware.

### 3.1 Send Batch Endpoints
| Method & Path | Access | Behaviour |
| :--- | :--- | :--- |
| `GET /api/batches` | LAN | Returns `{"batches": [...]}` (newest first). Each summary includes `id`, `label`, `source`, `created_at`, `file_count`, `total_bytes`, `formatted_size`, `download_count`, `single_item` (item object when `file_count == 1`, else `None`), and `preview_thumbs` (up to 4 image `item_id`s). |
| `GET /api/batches/<batch_id>/items` | LAN | Returns `{"batch": summary, "items": [...]}` (each item carries `id`, `name`, `rel_path`, `size`, `formatted_size`, `modified`, `category`, `is_dir: False`). `404` if batch not found. |
| `GET /api/batches/<batch_id>/download/<item_id>` | LAN | Streams original file via `_track_download(send_file(..., as_attachment=True, conditional=True))`, increments `download_count`, and broadcasts `{"type": "batches_updated"}`. Returns `404` if batch/item missing or file removed from disk. |
| `GET /api/batches/<batch_id>/preview/<item_id>` | LAN | Streams inline (`send_file(..., conditional=True)`) for the existing preview modal. |
| `GET /api/batches/<batch_id>/thumb/<item_id>` | LAN | Returns cached JPEG thumbnail via `make_thumbnail()` for `category == "image"` (`Cache-Control: private, max-age=86400`), `404` otherwise. |
| `POST /api/batches/<batch_id>/prepare-zip` | LAN | Body: `{"item_ids": [...]}` (optional; if omitted or empty, packages the entire batch). Validates all `item_ids`, checks files still exist on disk, spawns existing `_run_zip_worker` / `self.zip_jobs`, increments `download_count`, broadcasts `{"type": "batches_updated"}`, and returns `{success, job_id, folder_name, total_files, total_bytes}` — reusing `/api/safelist/zip-progress`, `/api/safelist/cancel-zip`, and `/api/safelist/download-zip-file` unchanged! |
| `DELETE /api/batches/<batch_id>` | LAN | Removes batch from `BatchStore`, broadcasts SSE `{"type": "batches_updated"}`. |
| `POST /api/batches/clear` | LAN | Clears all active batches, broadcasts SSE `{"type": "batches_updated"}`. |

### 3.2 Updated Existing Endpoints
1. **`POST /api/shell-send` (localhost-only):**
   - Routes dropped/Explorer paths into `self.batch_store.create_or_merge(paths, source="Explorer")` instead of adding them to `config.safe_folders` or copying them with `shutil.copy2`.
   - Broadcasts `{"type": "batches_updated", "batch_id": batch["id"]}` and returns `{"success": True, "added_count": len(added_paths), "batch": summary}`.
2. **`POST /api/safelist/prepare-zip`:**
   - Adds optional JSON key `items: list[str]` (list of relative subpaths inside `folder_id` — files or subfolders).
   - When `items` is provided and non-empty:
     - Each subpath is resolved via `config.resolve_safe_path(folder_id, item_subpath)` (raises `PermissionError` -> `403` on traversal).
     - Files are added directly; directories are walked with `os.walk` + `inside_folder()` check, preserving folder structure relative to the current `subpath` base.
     - Archive name becomes `<folder_name>-selected` (or `<folder_name>` when `items` is omitted).
   - When `items` is omitted/empty, existing full-folder behaviour is unchanged.
3. **`GET /api/safelist`:**
   - For single-file entries (`is_file: True`), also include `size`, `formatted_size`, `modified`, and `category` so the web root view can show real thumbnails and file sizes instead of a generic folder icon.
4. **`POST /api/upload`:**
   - Change `request.files.getlist("files") or [request.files.get("file")]` to `request.files.getlist("files") or request.files.getlist("file")` so multi-file `file` uploads from iOS Shortcuts / curl work cleanly.

---

## 4. Web App UI (`templates/index.html`, `static/js/app.js`, `static/css/style.css`)

### 4.1 "Incoming from PC" Send Batches Banner (Universal, Any Tab)
- Placed prominently near the top of `<main>` (above the tab panes, hidden when `activeBatches.length === 0`).
- Populated on load via `GET /api/batches` and kept live via SSE `batches_updated` (plus a toast notification when a new batch arrives while the page is open).
- Each batch card displays:
  - Title & stats: e.g. `100 photos · 420 MB · 14:32` (or `contract.pdf · 2.4 MB`).
  - Up to 4 mini thumbnail previews (respecting the Thumbnails toggle) when the batch contains images.
  - Actions:
    - **If 1 file:** `Download` (direct file download, no ZIP), `Preview`, `Dismiss`.
    - **If 2+ files:** `Download All (ZIP)` (triggers `POST /api/batches/<id>/prepare-zip` + existing ZIP progress modal), `View files` (switches to Explorer tab and opens the batch in full browse view with Grid/List, Sort, Type chips, Thumbnails, and Select Mode), `Dismiss` (`DELETE /api/batches/<id>`).

### 4.2 Explorer Toolbar: Sort, Thumbnail Toggle & Select Mode
Inside `#browseToolbar` (shown whenever browsing a Shared Folder OR a Send Batch):
1. **Sort dropdown (`#safeSortSelect`):**
   - `newest` — Newest first (default)
   - `oldest` — Oldest first
   - `size-desc` — Largest first
   - `size-asc` — Smallest first
   - `name-asc` — Name A–Z
   - `name-desc` — Name Z–A
   - Saved to `localStorage["nearbeam.browseSort"]` (gracefully maps legacy `"name"` to `"name-asc"`). Folders remain grouped above files; within files, `name-asc` acts as tie-breaker.
2. **Show / Hide Thumbnails toggle (`#btnToggleThumbs`):**
   - Compact icon/label toggle button (`aria-pressed="true|false"`), saved to `localStorage["nearbeam.showThumbs"]` (default `"true"`).
   - When `false`, neither `renderSafeItems()` nor `showAllSafeFolders()` nor the Incoming batch strip creates `<img>` thumbnail elements (zero `/thumb` network calls; renders crisp category icons).
   - When `true`, single shared image files at the Safe List root (`showAllSafeFolders()`) render real thumbnails via `/api/safelist/thumb?folder_id=<id>&subpath=` and show `formatted_size · Shared file` instead of `"1 items • Shared PC Folder"`.
3. **Select Mode (`#btnToggleSelect`):**
   - Toggles `selectMode` (`true`/`false`), resetting selection automatically when leaving a folder/batch or turning Select mode off.
   - While `selectMode === true`:
     - Each file/folder card gets a clean circular checkbox badge in the corner and `aria-selected` styling; clicking a card toggles its selection (instead of opening preview/folder).
     - A sticky bottom **Selection Bar** (`#selectionActionBar`) slides up showing:
       - Counter: `N selected · 124.5 MB` (sum of known file sizes).
       - `Select all` (selects all currently visible filtered items; switches to `Deselect all` when all visible items are selected).
       - `Cancel` (exits Select mode).
       - Primary CTA: `Download (N)` — if exactly 1 non-directory file is selected, triggers a direct download without ZIP; otherwise calls `POST /api/safelist/prepare-zip` with `{folder_id, subpath, items}` (or `POST /api/batches/<id>/prepare-zip` with `{item_ids}` when inside a batch) and opens the live ZIP progress modal.

### 4.3 Help & FAQ Addition
- Add a collapsible FAQ card in `#tab-faq`: **"Send files straight from iPhone / iPad Share Sheet (Apple Shortcuts)"** containing the exact 7-step recipe targeting `POST http://nearbeam.local:5000/api/upload` (with a 1-tap copy button for the live upload URL).

---

## 5. Desktop Control Panel (`gui.py`, `ui/pages.py`, `core/config.py`)

### 5.1 Config Batch Operations (`core/config.py`)
- Add `add_safe_folders(paths: list[str]) -> list[dict]`: acquires `self._lock`, validates & deduplicates all paths, appends all new entries, and calls `self.save()` **once**.
- Add `remove_safe_folders(folder_ids: list[str]) -> int`: acquires `self._lock`, filters out all matching IDs in one pass, calls `self.save()` **once** if changed, and returns removed count.

### 5.2 Shared Folders Page Bulk Add & Bulk Remove (`ui/pages.py`, `gui.py`)
- `SharedFoldersPage.table`:
  - `setSelectionMode(QTableWidget.ExtendedSelection)` (supports Ctrl+Click, Shift+Click, drag-select, Ctrl+A).
  - Support `Delete` key shortcut to remove selected rows.
  - Enable `setAcceptDrops(True)` (and handle in `MainWindow.dropEvent` when `self.stack.currentWidget() is self.shared_page`) so dragging multiple folders or files onto the **Shared folders** page bulk-adds them to permanent Shared folders, with a subtitle hint (`"Drag & drop multiple folders or files here to add them at once"`).
- Action toolbar under the table:
  - **Add folder** (`btn_add`, existing single folder picker)
  - **Add files...** (`btn_add_files`, `QFileDialog.getOpenFileNames` multi-file picker using `config.add_safe_folders`)
  - **Select all** (`btn_select_all`)
  - **Remove selected** (`btn_remove`, dynamically updates label to `Remove selected (N)` and disables when 0 rows selected; removes all selected IDs with **1 confirmation dialog**)
  - **Clear all** (`btn_clear_all`, disabled when table is empty; removes all entries with **1 confirmation dialog**)
- Broadcasts a single `{"type": "safelist_updated"}` SSE event after any bulk add/remove/clear.

### 5.3 Home Page: "Ready for your devices" Send Batches Section (`ui/pages.py`, `gui.py`)
- Add a clean `batches_section` container on `HomePage` (between the DropZone and Recent transfers, hidden when no active Send Batches exist):
  - Header: **"Ready for your devices"** + **"Clear all"** link button.
  - One compact row per active batch showing: batch label, file count & total size, download count (e.g. `100 files · 420 MB · Downloaded 1×`), and an inline **Clear** (`x`) button.
- Routing PC drops & Send button in `gui.py`:
  - **Shared folders page drop** $\rightarrow$ `config.add_safe_folders(paths)` (permanent).
  - **Drop on a peer PC's DeviceCard** (or Send button $\rightarrow$ peer PC) $\rightarrow$ pushes files straight to that peer PC only (no Send Batch created).
  - **Drop on a Phone/Browser DeviceCard** or Send button $\rightarrow$ **"Share with phones and browsers"** $\rightarrow$ creates a temporary Send Batch only (never modifies `safe_folders`).
  - **Home DropZone / Window drop (other pages) / Explorer "Send with NearBeam"** $\rightarrow$ creates a temporary Send Batch (never modifies `safe_folders`), and when `send_to_peers=True` with peer NearBeam PCs online, also pushes files to the selected peer PC just as before.
  - Shows a tray toast `"Ready on your devices — N files ready to download"` when creating a Send Batch for phones/browsers.
- Listen for `batches_updated` in `_drain_server_events()` so Home updates live when a phone downloads or dismisses a batch (or when Explorer `--send` arrives via another instance).

---

## 6. Error Handling & Edge Cases

- **File deleted/moved on PC after creating a Send Batch:**
  - Individual download/preview returns `404` with a clear error message.
  - `prepare-zip` skips missing/unreadable files gracefully; if 0 readable files remain, returns `400` (`"Files are no longer available on the PC"`).
- **Empty folders dropped into a Send Batch:**
  - If a drop yields 0 files (e.g. empty folder only), return an informative log/toast without creating an empty ghost batch.
- **Duplicate filenames from different folders in one Send Batch:**
  - `rel_path` disambiguates collisions (`photo.jpg`, `photo (1).jpg`) so ZIP archives never overwrite entries.
- **Large Send Batches (e.g. 500+ photos):**
  - `GET /api/batches` only returns summary metadata + up to 4 thumb IDs (tiny JSON payload).
  - Images inside the batch load lazily (`loading="lazy"`) via cached 320px JPEGs (`make_thumbnail`), and users can flip Thumbnails OFF anytime.
  - ZIP packaging uses `ZIP_STORED` for already-compressed media (photos/videos/archives) and `compresslevel=1` for text/docs, running asynchronously with cancel support.
- **Backward compatibility:**
  - Existing permanent `safe_folders` in `config.json` continue working identically, with improved single-file rendering at the root.
  - Existing ZIP endpoints (`/api/safelist/zip-progress`, `/cancel-zip`, `/download-zip-file`) work unchanged for folder ZIPs, selective ZIPs, and Send Batch ZIPs.

---

## 7. Verification & Test Plan

1. **Backend unit & integration tests (`tests/test_backend.py`):**
   - `BatchStore` + `/api/batches`: create single-file & multi-file & directory batches, verify symlink escape skipped, verify Explorer rapid-send merging within 2 s window, verify `safe_folders` untouched, test item download, preview, thumb, full batch ZIP, selective `item_ids` ZIP, `DELETE /api/batches/<id>`, `POST /api/batches/clear`, and deleted-file `404` handling.
   - `/api/safelist/prepare-zip` with `items: [...]` (files + nested subfolders) and path traversal `403` rejection inside `items`.
   - `/api/safelist` single-file metadata (`category`, `size`, `formatted_size`).
   - `/api/upload` with multiple files under `file` field.
   - `ConfigManager.add_safe_folders` and `remove_safe_folders` deduplication & single persistence.
2. **Frontend static & DOM checks:**
   - `node --check static/js/app.js` passes cleanly.
   - DOM element assertions in `tests/test_backend.py` for `#incomingBatchesSection`, `#btnToggleThumbs`, `#btnToggleSelect`, `#selectionActionBar`, expanded `#safeSortSelect` options, and the iOS Shortcuts FAQ card.
3. **Desktop GUI offscreen verification (`QT_QPA_PLATFORM=offscreen`):**
   - `SharedFoldersPage` supports `ExtendedSelection`, bulk add (`_add_safe_paths`), bulk remove of multiple selected rows, and clear all.
   - `HomePage` shows/clears active Send Batches without modifying `config.get_safe_folders()`.
