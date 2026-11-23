# NearBeam — Parallel Multi-Stream Uploads, Native Windows Shell Thumbnails, and Auto-Organize Received

**Date:** 2026-11-23
**Status:** Approved Design (Pending Spec Review)

## Overview

This specification defines three cohesive improvements to NearBeam's transfer performance, browsing experience, and received-file management:

1. **Parallel Multi-Stream Uploads (`#7`)** — Order-independent, multi-stream chunked uploads for large files and multi-file concurrency for small files across the Web App, Guest Drop Zone, and Desktop PC-to-PC transfers, while preserving full backward compatibility with sequential clients and iOS Shortcuts.
2. **Native Windows Shell Thumbnails (`#2`)** — Zero-dependency Windows COM `IShellItemImageFactory` thumbnail generation on a dedicated STA worker pool, extending thumbnails from standard images to videos, PDFs, HEIC/HEIF, RAW photos, and Office documents.
3. **Auto-Organize Received (`#5`)** — Opt-in (`"off"` by default) received-file organization by File Type, Date, or Device, while preserving uploaded folder trees and Guest Drops and keeping all organized files visible/downloadable in the Web **Received** tab and Desktop Control Panel.

---

## 1. Parallel Multi-Stream Uploads (`#7`)

### 1.1 Goals & Non-Goals
- **Goals:**
  - Maximize LAN upload throughput for large single files (e.g., 4K videos, archives, disk images) via concurrent byte-range chunk streams written out-of-order into a pre-allocated temporary file.
  - Keep small-file batch uploads fast without splitting tiny files across multiple HTTP requests, while respecting browser HTTP/1.1 per-origin connection limits (6 sockets/host, keeping SSE `/api/events` responsive).
  - Eliminate sequential single-file and per-chunk TCP handshake bottlenecks in Desktop PC-to-PC transfers (`ui/transfer.py`).
  - Enable true missing-chunk resume on pause/retry and race-free cancellation and finalization on Windows NTFS.
- **Non-Goals:**
  - Changing `/api/upload` (multipart POST used by iOS Shortcuts and legacy tools).
  - Breaking older/sequential chunked clients that omit `X-Parallel: 1`.

### 1.2 Server Protocol (`core/server.py`)

Both `POST /api/upload/chunk` and `POST /api/drop/<token>/upload/chunk` route through `NearBeamServer._receive_chunk(...)`.

- **Sequential / Legacy Path (default when `X-Parallel` != `"1"`):** Unchanged.
- **Parallel Path (activated when header `X-Parallel: 1` is sent):**
  - **Required headers:** `X-Session-Id`, `X-Filename`, `X-Chunk-Index`, `X-Total-Chunks`, `X-Chunk-Offset`, `X-File-Size`. Missing or invalid values return `400`.
  - **Per-session state (`self._parallel_sessions[session_id]`):**
    - `temp_path: str` — `.<session_id>.tmp` inside the upload's temp root (`save_directory` or `save_directory/Guest Drops`).
    - `dest_dir: str` — Resolved final destination directory (incorporating `relative_dir` and Auto-Organize rules).
    - `filename: str` — Sanitized final filename (`safe_filename`).
    - `file_size: int`, `total_chunks: int` — Locked on first chunk; subsequent chunks with mismatched `file_size`, `total_chunks`, or `filename` return `400`. `dest_dir` is locked on the first chunk and reused for the rest of the session (so a transfer crossing midnight in `"date"` mode finishes cleanly in its starting folder).
    - `received_chunks: set[int]` — Chunk indices completely written and closed.
    - `chunk_progress: dict[int, int]` — Bytes written per chunk (for accurate aggregate SSE progress).
    - `active_writers: int` — Number of threads currently holding an open handle to `temp_path`.
    - `finalized: bool`, `created_at: float`.
  - **Pre-allocation:** On the first chunk for a `session_id` (while briefly holding the per-session lock):
    - Create `temp_path` (`"wb"`) and call `f.truncate(file_size)` so NTFS pre-sizes the file once and concurrent `r+b` writers can seek/write disjoint ranges safely.
    - Handle `file_size == 0` cleanly (single 0-byte chunk finalizes immediately).
  - **Lock-free streaming:**
    - Increment `active_writers += 1` under `session_lock`, then release `session_lock`.
    - Open `temp_path` in `"r+b"` mode (`buffering=4 * 1024 * 1024`), `seek(offset)`, and stream 64 KB blocks from the request stream.
    - Enforce `offset + written <= file_size`; if a chunk overflows `file_size`, abort and return `400`.
    - Never call `f.truncate()` in parallel mode.
    - Periodically (every ~0.4 s) update `chunk_progress[chunk_index] = written` under `session_lock` and emit SSE `upload_progress` with `received_bytes = sum(chunk_progress.values())` (monotonic across parallel streams).
    - Check `_is_upload_cancelled(session_id)` inside the streaming loop; break immediately if cancelled.
  - **Safe Windows finalization:**
    - Close the file handle **before** reacquiring `session_lock` (`try ... finally` guarantees `active_writers -= 1` even on socket disconnect/error).
    - Under `session_lock`:
      - Decrement `active_writers -= 1`.
      - If cancelled: when `active_writers == 0`, delete `temp_path`, pop `_parallel_sessions[session_id]`, and return `499 {"success": False, "cancelled": True}`.
      - If chunk read failed/aborted (e.g., client disconnected mid-chunk or overflowed): do not add `chunk_index` to `received_chunks`; reset `chunk_progress[chunk_index] = 0`.
      - Otherwise, add `chunk_index` to `received_chunks` and record `chunk_progress[chunk_index] = written`.
      - If `len(received_chunks) == total_chunks and active_writers == 0 and not finalized`:
        - Verify `os.path.getsize(temp_path) == file_size`.
        - Set `finalized = True`, run `finalize_upload(temp_path, dest_dir, filename)`, store in `finished_uploads` & history, emit SSE `file_received`, pop `_parallel_sessions[session_id]`, and return `200 {"success": True, "completed": True, "file": file_info}`.
      - Otherwise return `200 {"success": True, "completed": False, "chunk_index": chunk_index}`.
  - **Cancel & Status endpoints:**
    - `POST /api/upload/cancel`: marks `session_id` in `_cancelled_uploads`. If a parallel session has `active_writers == 0`, deletes `temp_path` and pops `_parallel_sessions[session_id]` immediately; if `active_writers > 0`, the last active writer deletes `temp_path` upon closing its handle.
    - `GET /api/upload/status/<session_id>`: returns `received_bytes`, `received_chunks` (sorted list of confirmed chunk indices when a parallel session is active), and `completed` (`True` if in `finished_uploads`).
  - **Stale session cleanup:** Periodically prune entries from `_parallel_sessions` older than 6 hours with `active_writers == 0`, removing orphaned `.tmp` files.

### 1.3 Web App & Guest Drop Zone (`static/js/app.js`)
- **Thresholds & concurrency budget:**
  - `LARGE_FILE_THRESHOLD = 16 * 1024 * 1024` (16 MB)
  - `PARALLEL_CHUNK_SIZE = 8 * 1024 * 1024` (8 MB)
  - `MAX_STREAMS_PER_LARGE_FILE = 4`
  - `MAX_TOTAL_UPLOAD_STREAMS = 5` (leaves at least 1 of the browser's 6 HTTP/1.1 connections free for SSE `/api/events` and interactive API requests)
  - Small files (`< 16 MB`): single stream (`chunkSize = file.size || 1`), up to 5 concurrent files.
  - Large files (`>= 16 MB`): 8 MB chunks with `X-Parallel: 1`, up to 4 concurrent streams per file (1 large file at a time, or coexisting within the 5-stream budget).
- **Per-transfer state in `TransferManager`:**
  - `pendingChunks: number[]` — queue of chunk indices remaining.
  - `completedChunks: Set<number>` — server-confirmed chunk indices.
  - `inFlightXhrs: Map<number, XMLHttpRequest>` — active XHR per chunk index.
  - `chunkLoaded: Map<number, number>` — live uploaded bytes per chunk index.
  - Aggregate uploaded bytes = sum of completed chunk sizes + current `chunkLoaded` for in-flight chunks, driving smooth monotonic progress bar, EMA speed, and ETA.
- **Pause / Resume / Retry / Cancel:**
  - **Pause:** Sets `t.paused = true`, aborts every XHR in `inFlightXhrs`, returns those indices to the front of `pendingChunks`, preserves `completedChunks`.
  - **Resume:** Clears `t.paused`, resets speed sampling baseline, and launches up to `streamCount` chunk workers.
  - **Failure & Retry:** On any chunk failure (non-2xx or network error) when not paused/cancelled, aborts sibling in-flight XHRs, moves the transfer to `failedTransfers` preserving `completedChunks`. Clicking Retry queries `GET /api/upload/status/<sessionId>` (if same-origin/supported), reconciles `completedChunks`, and only uploads missing chunks.
  - **Cancel:** Sets `t.cancelled = true`, aborts all `inFlightXhrs`, removes card, and calls `POST /api/upload/cancel`.

### 1.4 Desktop PC-to-PC Transfers (`ui/transfer.py`, `gui.py`)
- **`OutgoingTransfer` (`ui/transfer.py`):**
  - Replace per-chunk `urllib.request.urlopen` with thread-local persistent `http.client.HTTPConnection` (HTTP/1.1 Keep-Alive, `TCP_NODELAY`, auto-reconnect on stale socket / `RemoteDisconnected`).
  - Keep existing constructor signature and methods (`start`, `run`, `pause`, `resume`, `cancel`, `join`, `on_progress`, `on_done`, `chunk_size`) backward-compatible.
  - Use parallel multi-stream mode (`X-Parallel: 1`, `max_workers = 4`, `8 MB` default chunks when caller didn't override `chunk_size`) whenever `self.total >= 16 * 1024 * 1024` and `total_chunks > 1`.
  - Track aggregate `sent` bytes safely across worker threads using a lock + per-chunk byte map; emit monotonic `on_progress(sent, total, bps)`.
  - On `pause()`: workers pause cleanly between chunks without counting pause duration in speed.
  - On `cancel()`: set `_cancelled = True`, wake paused workers, abort streaming `_pieces` mid-chunk, close connections, and call `_abort_remote()` once if any chunk started.
- **Multi-file concurrency (`gui.py` `_start_outgoing`):**
  - Dispatch queued files on a small worker pool: up to **3 concurrent small files** (`< 16 MB`) in parallel, while large multi-stream files (`>= 16 MB`) run 1 at a time (each using 4 streams) so the LAN pipe stays saturated without disk thrashing.

---

## 2. Native Windows Shell Thumbnails (`#2`)

### 2.1 Architecture (`core/thumbnails.py`)
- Create a dedicated module `core/thumbnails.py` exporting:
  - `THUMB_PX = 320`
  - `PILLOW_IMAGE_EXTS` — `{".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}`
  - `SHELL_THUMB_EXTS` — `{".mp4", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".m4v", ".pdf", ".heic", ".heif", ".avif", ".tif", ".tiff", ".psd", ".cr2", ".nef", ".arw", ".dng", ".docx", ".xlsx", ".pptx"}`
  - `is_thumbnailable(filename: str) -> bool` — True for `PILLOW_IMAGE_EXTS | SHELL_THUMB_EXTS` (on non-Windows platforms, only `PILLOW_IMAGE_EXTS`).
  - `make_thumbnail(path: str, mtime_ns: int, size: int) -> bytes` — LRU-cached (`maxsize=512`), returns JPEG bytes or raises/returns empty bytes `b""` when unavailable.
- **Generation pipeline:**
  1. If extension is in `PILLOW_IMAGE_EXTS`: attempt Pillow (`Image.draft` + `ImageOps.exif_transpose` + `thumbnail((320, 320))` + JPEG q=75).
  2. Otherwise (or if Pillow fails on Windows): submit to a dedicated **2-thread Windows COM STA worker pool** (`_WindowsShellThumbnailPool`):
     - Each worker thread calls `ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)` once on startup.
     - Calls `shell32.SHCreateItemFromParsingName(os.path.abspath(path), None, IID_IShellItemImageFactory, byref(factory))`.
     - Invokes `IShellItemImageFactory::GetImage(SIZE(320, 320), SIIGBF_THUMBNAILONLY | SIIGBF_BIGGERSIZEOK, byref(hbitmap))`.
     - Reads pixel buffer via `gdi32.GetObjectW` + `gdi32.GetDIBits` (top-down 32-bit `BI_RGB` BGRA bitmap), composites alpha onto clean white background if needed (e.g. PDFs), resizes/encodes as JPEG q=75 with Pillow.
     - Always releases `hbitmap` (`gdi32.DeleteObject`) and COM `factory` (`Release`) in `finally` blocks.
     - Enforces a 3-second future timeout per request so a hung codec cannot stall Flask.
     - Returns `b""` on any COM failure (`HRESULT != 0`, missing handler, timeout) — cached in `lru_cache` so scrolling past unsupported formats doesn't re-invoke Shell.

### 2.2 Server & Packaging Integration
- **`core/server.py`:**
  - Import `make_thumbnail` and `is_thumbnailable` from `core.thumbnails` (keep `core.server.make_thumbnail` re-exported for backward compatibility with existing tests).
  - `/api/safelist/thumb` and `/api/batches/<id>/thumb/<item_id>` check `is_thumbnailable(path)` instead of `category == "image"`, returning `404` when `make_thumbnail` returns `b""` or raises.
  - `/api/safelist` (for single shared files), `/api/safelist/browse`, and `BatchStore` include `"has_thumb": is_thumbnailable(name)` on file items. `BatchStore` `preview_thumbs` includes up to 4 thumbnailable items (images/videos/PDFs).
- **`NearBeam.spec`:** Verify `core.thumbnails` is picked up automatically (standard local package under `core/`).

### 2.3 Web UI Integration (`static/js/app.js`, `static/css/style.css`)
- Render `<img>` thumbnails for any item where `browsePrefs.thumbs && (item.has_thumb || item.category === "image")`, keeping the `error` handler that removes `<img>` to fall back cleanly to the SVG category icon.
- Add a subtle centered play-triangle badge (`.video-thumb-badge`) over video thumbnails so videos are immediately distinguishable from photos in both Grid and List views.

---

## 3. Auto-Organize Received (`#5`)

### 3.1 Configuration (`core/config.py`)
- Add `VALID_ORGANIZE_MODES = ("off", "type", "date", "device")`.
- Default in `ConfigManager`: `"auto_organize_mode": "off"`.
- Sanitize invalid/non-string values back to `"off"` in `load()`.
- Expose `auto_organize_mode` property and `set_auto_organize_mode(mode: str)` (locked + persisted).

### 3.2 Destination Resolution (`core/server.py`)
Add `resolve_receive_dir(base_save_dir: str, filename: str, relative_dir: str, peer_name: str, is_guest_drop: bool = False) -> str`:
- **Guest Drops (`is_guest_drop=True`):** Always stay inside `<base_save_dir>/Guest Drops` (plus `<relative_dir>` if a folder was dropped), unaffected by `auto_organize_mode`.
- **Mode `"off"`:** `<base_save_dir>` (plus `<relative_dir>` if provided).
- **Mode `"type"`:**
  - If `relative_dir` is present (folder upload): `<base_save_dir>/<relative_dir>` (preserves folder integrity; never splits a folder's contents into separate category folders).
  - If loose file: `<base_save_dir>/<category_folder>`, where:
    - `image` -> `Photos`
    - `video` -> `Videos`
    - `document` -> `Documents`
    - `audio` -> `Audio`
    - `archive` -> `Archives`
    - `code` -> `Code`
    - `file` -> `Other`
- **Mode `"date"`:** `<base_save_dir>/<YYYY-MM-DD>` (plus `<relative_dir>` if folder upload).
- **Mode `"device"`:** `<base_save_dir>/<safe_filename(peer_label) or "Other Devices">` (plus `<relative_dir>` if folder upload), where `peer_label` is derived from `connected_clients[remote_addr]["name"]`, `X-Device-Name` header (sent by `OutgoingTransfer` / desktop peers), or fallback UA classification (`iPhone`, `iPad`, `Android Phone`, `Mac Browser`, `Windows Browser`, `Linux Browser`, or `remote_addr`).
- Always verify final `dest_dir` with `inside_folder(os.path.realpath(base_save_dir), dest_dir)`.
- Apply identically across `/api/upload/chunk` and `/api/upload` (including iOS Shortcut uploads).

### 3.3 Web Received Tab & Recursive Listing (`core/server.py`, `static/js/app.js`)
- `GET /api/received`:
  - Recursively walks `config.save_directory` (pruning external symlinks/junctions via `inside_folder`, skipping dotfiles and partial `.tmp` files).
  - Each entry returns: `filename` (basename), `rel_path` (POSIX relative path from `save_directory`, e.g., `"Photos/IMG_001.jpg"` or `"note.txt"`), `folder` (parent relative path, e.g., `"Photos"` or `""`), `size`, `formatted_size`, `modified`, `mtime` (epoch float for accurate newest-first sorting), `category`, `has_thumb`.
  - Sorts newest-first by `mtime`.
- `GET /api/received/download/<path:filepath>` and `GET /api/received/preview/<path:filepath>`:
  - Normalize path segments, reject `..`, dotfiles, or anything failing `inside_folder(real_save_dir, target)`, and stream the file.
  - Existing flat URLs (`/api/received/download/note.txt`) remain 100% compatible.
- `static/js/app.js` `renderReceivedFiles()`:
  - Uses `file.rel_path || file.filename` for preview/download URLs (encoding each path segment cleanly with `encodeURI` / segment-wise `encodeURIComponent`).
  - Displays a subtle folder tag (`file.folder`) in the metadata line when the file lives in a subfolder (e.g., `2.4 MB • Photos • 2026-11-23 16:40`).

### 3.4 Desktop Control Panel Settings (`ui/pages.py`, `gui.py`)
- Add an **"Auto-organize received files"** dropdown (`QComboBox`) to `SettingsPage`'s **Receiving** card right below the `Auto-save incoming files` switch:
  - `Off (save in one folder)` (`"off"`)
  - `By file type (Photos, Videos, Documents...)` (`"type"`)
  - `By date (YYYY-MM-DD)` (`"date"`)
  - `By device (iPhone, Laptop...)` (`"device"`)
- Wire `currentIndexChanged` in `MainWindow` to `config.set_auto_organize_mode(mode)` and log to Activity log.
- Desktop "Show in folder" already reveals `saved_path`, so clicking it opens Explorer directly inside the organized subfolder.

---

## 4. Verification & Testing Plan

- **Parallel Multi-Stream Uploads (`tests/test_parallel_uploads.py`):**
  - Out-of-order chunk arrival (e.g., chunks `3, 1, 0, 2`) produces byte-identical file and finalizes only after all chunks close.
  - Concurrent multi-thread chunk upload of a multi-MB payload succeeds with zero Windows `PermissionError` or data corruption.
  - Missing-chunk status (`/api/upload/status/<id>`) reports `received_chunks` accurately; retrying only missing chunks completes the file.
  - Mid-transfer cancel cleans up `.tmp` after active writers close and never finalizes.
  - Header mismatch / overflow chunk returns `400`.
  - Sequential clients (without `X-Parallel: 1`) continue passing all existing upload tests.
  - `OutgoingTransfer` multi-stream (>= 16 MB) & keep-alive tests: byte-identical delivery, pause/resume, cancel mid-chunk, and concurrent small-file batch.
- **Native Windows Shell Thumbnails (`tests/test_thumbnails.py`):**
  - Unit tests for `is_thumbnailable()`, Pillow fast-path, Windows Shell STA worker fallback (mocked COM + real Windows verification on a sample image/file when on Windows), timeout handling, negative-hit caching, and `404` for unsupported files.
  - Verify `/api/safelist/thumb` and `/api/batches/<id>/thumb/<item_id>` serve non-image thumbnailable types and reject path traversal.
- **Auto-Organize Received (`tests/test_auto_organize.py`):**
  - Config defaults to `"off"`, persists valid modes, sanitizes invalid modes.
  - Loose uploads route properly in `"off"`, `"type"`, `"date"`, and `"device"` modes for both `/api/upload` and `/api/upload/chunk`.
  - Explicit folder uploads (`relative_dir`) stay intact (not split across type folders); Guest Drops remain in `Guest Drops/`.
  - `/api/received`, `/api/received/download/<path>`, `/api/received/preview/<path>` list, serve, and sandbox files inside organized subfolders.
  - Offscreen PySide6 test verifying `SettingsPage` dropdown reflects and persists `auto_organize_mode`.
- **Full regression gate:** `node --check static/js/app.js` and full `pytest -v` with 0 failures.
