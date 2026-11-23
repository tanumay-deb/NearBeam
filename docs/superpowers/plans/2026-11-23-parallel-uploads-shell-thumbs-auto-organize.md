# Parallel Multi-Stream Uploads, Native Windows Shell Thumbnails, and Auto-Organize Received Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver order-independent parallel multi-stream uploads across Web, Guest Drop, and Desktop PC-to-PC (`#7`), native Windows Shell thumbnails for videos/PDFs/HEIC/RAW/Office files (`#2`), and opt-in Auto-Organize Received by Type/Date/Device (`#5`).

**Architecture:** We extend `core/config.py` with `auto_organize_mode`, add a pre-allocated random-access parallel chunk protocol (`X-Parallel: 1`) and recursive received-file serving to `core/server.py`, upgrade both `ui/transfer.py` (with HTTP keep-alive + 4-stream `ThreadPoolExecutor`) and `static/js/app.js` (`TransferManager` 4-stream large-file uploads within a 5-stream browser budget), add a dedicated zero-dependency Windows COM STA thumbnail worker module `core/thumbnails.py`, and surface the Auto-Organize picker in `ui/pages.py` + `gui.py`.

**Tech Stack:** Python 3.10+, Flask, Werkzeug, `http.client`, `concurrent.futures`, Pillow, Windows `ctypes` COM (`shell32`, `ole32`, `gdi32`), PySide6, Vanilla ES2020 JS, CSS3, `pytest`.

**Spec:** [docs/superpowers/specs/2026-11-23-parallel-uploads-shell-thumbs-auto-organize-design.md](../specs/2026-11-23-parallel-uploads-shell-thumbs-auto-organize-design.md)

## Global Constraints

- Requires Python `>=3.10`; **zero** new pip dependencies (use stdlib `ctypes`/`http.client`/`concurrent.futures` + existing Pillow/Flask/PySide6).
- Full backward compatibility with sequential chunk clients (omitting `X-Parallel: 1`) and iOS Shortcut multipart `/api/upload`.
- Browser concurrent upload streams capped at `<= 5` total so HTTP/1.1's 6-connection host limit always leaves a slot for SSE `/api/events`.
- `auto_organize_mode` defaults to `"off"` (opt-in); Guest Drops always remain under `Guest Drops/`; explicit folder uploads (`relative_dir`) never split across file-type folders.
- Windows Shell thumbnails must pass `SIIGBF_THUMBNAILONLY | SIIGBF_BIGGERSIZEOK` on dedicated COM STA threads, release all `HBITMAP`/COM objects in `finally`, and fall back cleanly to `404` (built-in SVG icon) when unavailable.
- All commits authored with `git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit ...`.

## Review Focus

1. **Cross-midnight parallel upload in `"date"` mode** — a large upload starting at 23:59:58 and finishing at 00:00:05 must reuse the session's locked `dest_dir` rather than rejecting or splitting across dates (pinned in Task 2).
2. **Duplicate/retried chunk delivery in parallel mode** — resending an already-confirmed chunk index (or an already-finalized session's chunk) must be idempotent and never corrupt `received_chunks` or double-finalize (pinned in Task 2).
3. **Zero-byte file uploaded with `X-Parallel: 1` (`X-File-Size: 0`)** — empty files must pre-allocate and finalize cleanly without errors or divide-by-zero (pinned in Task 2).
4. **Slow or hung Windows Shell thumbnail codec** — must time out within 3 seconds and return `b""` / `404` without deadlocking Flask or exhausting the STA pool (pinned in Task 5).
5. **Path traversal or partial `.tmp` access via recursive `/api/received/download/<path>`** — requests like `Photos/../../cfg.json` or `.abc.tmp` must be rejected with `403`/`404` (pinned in Task 1).

---

## File Structure

| File | Responsibility |
|---|---|
| `core/config.py` | Persist & validate `auto_organize_mode` (`"off"`, `"type"`, `"date"`, `"device"`). |
| `core/thumbnails.py` *(new)* | Pillow fast-path + Windows COM STA `IShellItemImageFactory` thumbnail worker pool & LRU cache. |
| `core/batches.py` | Expose `has_thumb` on batch items and broaden `preview_thumbs` to thumbnailable media. |
| `core/server.py` | `resolve_receive_dir()`, parallel out-of-order chunk receiver (`X-Parallel: 1`), enriched `/api/upload/status`, recursive `/api/received*`, rich thumbnail routes. |
| `ui/transfer.py` | `OutgoingTransfer` with thread-local `http.client` keep-alive + 4-stream parallel chunks (`>= 16 MB`) + `X-Device-Name`. |
| `ui/pages.py` | Add "Auto-organize received files" dropdown to `SettingsPage` Receiving card. |
| `gui.py` | Wire Auto-Organize dropdown to `config.set_auto_organize_mode()`; run small outgoing files concurrently (up to 3) in `_start_outgoing`. |
| `static/js/app.js` | Parallel multi-stream `TransferManager` (4 streams for `>= 16 MB`, 5-stream budget, missing-chunk retry), organized Received folder badge + `rel_path` URLs, video/PDF thumbnail rendering + play badge. |
| `static/css/style.css` | Video thumbnail play badge (`.video-thumb-badge`) and Received subfolder badge styles. |
| `tests/test_auto_organize.py` *(new)* | Config, routing, recursive `/api/received*` security, and Desktop Settings dropdown tests. |
| `tests/test_parallel_uploads.py` *(new)* | Server out-of-order/concurrent/resume/cancel/midnight/0-byte tests, `OutgoingTransfer` parallel & keep-alive tests, and Web JS contract checks. |
| `tests/test_thumbnails.py` | Extended tests for `core/thumbnails.py` STA worker, timeout, caching, and video/PDF thumbnail routes. |

---

### Task 1: Auto-Organize Config, Destination Routing, and Recursive `/api/received`

**Files:**
- Modify: `core/config.py:45-145`
- Modify: `core/server.py:925-1100,1750-2030`
- Test: `tests/test_auto_organize.py` (create)

**Interfaces:**
- Consumes: `core.server.safe_filename`, `core.server.inside_folder`, `core.server.get_file_type_category`.
- Produces:
  - `ConfigManager.VALID_ORGANIZE_MODES = ("off", "type", "date", "device")`
  - `ConfigManager.auto_organize_mode -> str`
  - `ConfigManager.set_auto_organize_mode(mode: str) -> str`
  - `core.server.resolve_receive_dir(base_save_dir: str, filename: str, relative_dir: str, peer_name: str, mode: str, is_guest_drop: bool = False) -> str`
  - Recursive `GET /api/received` returning `{filename, rel_path, folder, size, formatted_size, modified, mtime, category, has_thumb}` and sandboxed `GET /api/received/download/<path:filepath>` & `GET /api/received/preview/<path:filepath>`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auto_organize.py`:

```python
"""Tests for Auto-Organize Received (#5): config, routing, and recursive /api/received."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

import core.server as srv_mod
from core.config import ConfigManager
from core.server import NearBeamServer


class TestAutoOrganizeConfigAndRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nb_org_")
        self.cfg = ConfigManager(os.path.join(self.tmp, "cfg.json"))
        self.cfg.set_save_directory(os.path.join(self.tmp, "recv"))
        self.cfg.set_auto_save(True)
        self.orig = srv_mod.config
        srv_mod.config = self.cfg
        self.server = NearBeamServer(port=5971, enable_mesh=False)
        self.client = self.server.app.test_client()

    def tearDown(self):
        self.server.shutdown()
        srv_mod.config = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_defaults_off_persists_and_sanitizes(self):
        self.assertEqual(self.cfg.auto_organize_mode, "off")
        self.cfg.set_auto_organize_mode("type")
        reloaded = ConfigManager(self.cfg.config_path)
        self.assertEqual(reloaded.auto_organize_mode, "type")
        with open(self.cfg.config_path, "w", encoding="utf-8") as f:
            json.dump({"auto_organize_mode": "invalid_mode"}, f)
        sanitized = ConfigManager(self.cfg.config_path)
        self.assertEqual(sanitized.auto_organize_mode, "off")

    def test_mode_type_organizes_loose_files_and_preserves_folder_uploads_and_guest_drops(self):
        self.cfg.set_auto_organize_mode("type")
        # Loose image via multipart /api/upload (iOS Shortcut compatible)
        r_img = self.client.post(
            "/api/upload",
            data={"file": (io.BytesIO(b"jpg"), "sunset.jpg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r_img.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, "Photos", "sunset.jpg")))

        # Loose video via chunk upload
        r_vid = self.client.post(
            "/api/upload/chunk",
            data=b"mp4data",
            headers={
                "X-Session-Id": "org-vid-1",
                "X-Filename": "clip.mp4",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "7",
            },
        )
        self.assertEqual(r_vid.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, "Videos", "clip.mp4")))

        # Explicit folder upload MUST NOT split across Photos/Documents
        r_dir = self.client.post(
            "/api/upload/chunk",
            data=b"pdfdata",
            headers={
                "X-Session-Id": "org-dir-1",
                "X-Filename": "plan.pdf",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "7",
                "X-Relative-Dir": "Vacation/Docs",
            },
        )
        self.assertEqual(r_dir.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, "Vacation", "Docs", "plan.pdf")))

        # Guest Drop stays under Guest Drops/
        token = self.client.post("/api/drop/create").get_json()["token"]
        r_guest = self.client.post(
            f"/api/drop/{token}/upload/chunk",
            data=b"guestimg",
            headers={
                "X-Session-Id": "org-guest-1",
                "X-Filename": "snap.png",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "8",
            },
        )
        self.assertEqual(r_guest.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, "Guest Drops", "snap.png")))

    def test_mode_date_and_device_routing(self):
        today = datetime.date.today().isoformat()
        self.cfg.set_auto_organize_mode("date")
        r_date = self.client.post(
            "/api/upload",
            data={"file": (io.BytesIO(b"hello"), "notes.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r_date.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, today, "notes.txt")))

        self.cfg.set_auto_organize_mode("device")
        r_dev = self.client.post(
            "/api/upload/chunk",
            data=b"abc",
            headers={
                "X-Session-Id": "org-dev-1",
                "X-Filename": "shot.jpg",
                "X-Chunk-Index": "0",
                "X-Total-Chunks": "1",
                "X-Chunk-Offset": "0",
                "X-File-Size": "3",
                "X-Device-Name": "My Laptop",
            },
        )
        self.assertEqual(r_dev.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.cfg.save_directory, "My Laptop", "shot.jpg")))

    def test_recursive_received_listing_download_and_traversal_blocking(self):
        self.cfg.set_auto_organize_mode("type")
        self.client.post(
            "/api/upload",
            data={"file": (io.BytesIO(b"photo-bytes"), "pic.jpg")},
            content_type="multipart/form-data",
        )
        # Drop a hidden partial upload inside Photos/ — must stay hidden and un-downloadable
        with open(os.path.join(self.cfg.save_directory, "Photos", ".partial.tmp"), "wb") as f:
            f.write(b"secret-partial")

        listed = self.client.get("/api/received").get_json()["files"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["filename"], "pic.jpg")
        self.assertEqual(listed[0]["rel_path"], "Photos/pic.jpg")
        self.assertEqual(listed[0]["folder"], "Photos")

        dl = self.client.get("/api/received/download/Photos/pic.jpg")
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.data, b"photo-bytes")
        dl.close()

        pv = self.client.get("/api/received/preview/Photos/pic.jpg")
        self.assertEqual(pv.status_code, 200)
        self.assertEqual(pv.data, b"photo-bytes")
        pv.close()

        # Traversal and hidden dotfile MUST be refused
        self.assertIn(self.client.get("/api/received/download/Photos/../../cfg.json").status_code, (403, 404))
        self.assertEqual(self.client.get("/api/received/download/Photos/.partial.tmp").status_code, 404)
        self.assertEqual(self.client.get("/api/received/preview/Photos/.partial.tmp").status_code, 404)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_auto_organize.py -v`
Expected: FAIL (`AttributeError: 'ConfigManager' object has no attribute 'auto_organize_mode'`).

- [ ] **Step 3: Implement `auto_organize_mode` in `core/config.py`**

In `core/config.py`:
- Add module constant `VALID_ORGANIZE_MODES = ("off", "type", "date", "device")`.
- Add `"auto_organize_mode": "off"` to `self.data` defaults in `ConfigManager.__init__`.
- In `ConfigManager.load()`: sanitize `if self.data.get("auto_organize_mode") not in VALID_ORGANIZE_MODES: self.data["auto_organize_mode"] = "off"`.
- Add property `auto_organize_mode` and method `set_auto_organize_mode(self, mode: str) -> str`.

- [ ] **Step 4: Implement `resolve_receive_dir` and recursive `/api/received*` in `core/server.py`**

In `core/server.py`:
- Add category folder map:
  ```python
  ORGANIZE_TYPE_FOLDERS = {
      "image": "Photos",
      "video": "Videos",
      "document": "Documents",
      "audio": "Audio",
      "archive": "Archives",
      "code": "Code",
      "file": "Other",
  }
  ```
- Add `resolve_receive_dir(base_save_dir: str, filename: str, relative_dir: str, peer_name: str, mode: str, is_guest_drop: bool = False) -> str`:
  - `base_root = os.path.realpath(base_save_dir)`
  - When `is_guest_drop`: target starts at `base_save_dir` (already `.../Guest Drops`), joined with `relative_dir` parts if non-empty.
  - Else:
    - If `mode == "type"`: if `relative_dir` is non-empty, keep `relative_dir` directly under `base_save_dir`; else put under `ORGANIZE_TYPE_FOLDERS.get(get_file_type_category(filename), "Other")`.
    - If `mode == "date"`: prefix with `datetime.date.today().isoformat()`, then `relative_dir` if present.
    - If `mode == "device"`: prefix with `safe_filename(peer_name) if peer_name else "Other Devices"`, then `relative_dir` if present.
    - Else (`"off"`): `base_save_dir` (+ `relative_dir` if present).
  - Verify `inside_folder(base_root, candidate)`, else fall back to `base_save_dir`.
- Add helper `NearBeamServer._resolve_peer_device_label(self) -> str`:
  - Priority: `request.headers.get("X-Device-Name")` -> `connected_clients[remote_addr]["name"]` -> `_parse_client_info(remote_addr, ua)["name"]` -> `remote_addr or "Other Devices"`.
- Wire `resolve_receive_dir(...)` into both `_receive_chunk(..., is_guest_drop=...)` (passing `is_guest_drop=True` from `api_drop_upload_chunk`) and `api_upload()`.
- Update `/api/received` to walk `config.save_directory` recursively (pruning external links via `inside_folder`, skipping hidden directories/files starting with `.`), returning `filename`, `rel_path`, `folder`, `size`, `formatted_size`, `modified`, `mtime`, `category`, sorted by `mtime` descending.
- Update `/api/received/download/<path:filename>` and `/api/received/preview/<path:filename>` to normalize POSIX path segments, reject empty/`..`/dot-prefixed segments, verify `inside_folder(os.path.realpath(save_dir), target)`, and serve with `os.path.basename(target)`.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/test_auto_organize.py tests/test_backend.py tests/test_bugfixes.py tests/test_review_fixes.py -v`
Expected: PASS (0 failures).

```bash
git add core/config.py core/server.py tests/test_auto_organize.py
git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit -m "feat(organize): add opt-in auto-organize received modes and recursive /api/received"
```

---

### Task 2: Server-Side Order-Independent Parallel Multi-Stream Chunk Receiver

**Files:**
- Modify: `core/server.py:310-370,940-1120,1860-1925`
- Test: `tests/test_parallel_uploads.py` (create)

**Interfaces:**
- Consumes: `resolve_receive_dir`, `finalize_upload`, `safe_filename`, `inside_folder`.
- Produces:
  - `POST /api/upload/chunk` & `POST /api/drop/<token>/upload/chunk` supporting `X-Parallel: 1` with out-of-order concurrent byte-range writes, `active_writers`-guarded Windows-safe finalization, locked `dest_dir` per session, and 0-byte support.
  - `GET /api/upload/status/<session_id>` returning `{"received_bytes": int, "received_chunks": list[int], "completed": bool}`.
  - `POST /api/upload/cancel` safely deferring `.tmp` deletion to the last active writer when parallel writers are in-flight.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_parallel_uploads.py` with server tests covering:
- Out-of-order chunks (`[3, 1, 0, 2]`) -> byte-identical finalized file, intermediate chunks return `completed: False`, last missing chunk returns `completed: True`.
- Concurrent multi-thread parallel upload of 4 MB split into 8 × 512 KB chunks across 4 threads (using Werkzeug `make_server` live HTTP server) -> byte-identical file, zero `PermissionError` on Windows.
- Duplicate/retried chunk index is idempotent and does not corrupt count or double-finalize.
- Zero-byte file with `X-Parallel: 1`, `X-File-Size: 0` finalizes cleanly.
- Header mismatch (`X-File-Size` / `X-Total-Chunks` changed mid-session) and chunk overflow (`offset + len(data) > file_size`) return `400`.
- Missing-chunk status (`/api/upload/status/<session_id>`) returns `received_chunks: [0, 2]` and `completed: False`; completing chunk `1` flips `completed: True`.
- Mid-transfer cancel cleans up `.tmp` and prevents late chunks from finalizing.
- Cross-midnight `"date"` mode stability: mock `datetime.date.today()` to Day 1 on chunk 0 and Day 2 on chunk 1 -> file lands cleanly in Day 1's folder.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_parallel_uploads.py -v`
Expected: FAIL (currently sequential receiver rejects out-of-order chunk 3 with `409`).

- [ ] **Step 3: Implement parallel chunk receiver in `core/server.py`**

In `NearBeamServer`:
- In `__init__`: add `self._parallel_sessions: Dict[str, dict] = {}`.
- Add `_prune_stale_parallel_sessions(self, max_age: float = 6 * 3600)` removing abandoned sessions with `active_writers == 0`.
- In `_receive_chunk(...)`:
  - Check `is_parallel = (request.headers.get("X-Parallel") == "1")`.
  - If `not is_parallel`: keep existing sequential implementation intact (zero regression risk!).
  - If `is_parallel`: dispatch to `self._receive_parallel_chunk(...)`:
    1. Require `offset is not None` and `file_size is not None` (`400` otherwise), and `offset <= file_size`.
    2. Acquire `session_lock`:
       - Check `_cancelled_uploads` -> `499`.
       - Check `finished_uploads` -> `200 {success: True, completed: True, file: finished}`.
       - Look up or create `sess = self._parallel_sessions.get(session_id)`:
         - If creating: `os.makedirs(save_dir, exist_ok=True)`, pre-allocate `temp_path = os.path.join(save_dir, f".{session_id}.tmp")` via `with open(temp_path, "wb") as f: f.truncate(file_size)`, store `dest_dir` (locked for session!), `filename`, `file_size`, `total_chunks`, `received_chunks=set()`, `chunk_progress={}`, `active_writers=0`, `finalized=False`, `created_at=time.time()`.
         - If existing: validate `sess["file_size"] == file_size`, `sess["total_chunks"] == total_chunks`, `sess["filename"] == filename` (`400` on mismatch). Reuse `sess["dest_dir"]`.
       - Increment `sess["active_writers"] += 1`.
    3. Outside `session_lock`:
       - Open `temp_path` in `"r+b"` mode (`buffering=4 * 1024 * 1024`), `seek(offset)`, read 64 KB blocks checking `_is_upload_cancelled(session_id)` and `offset + written <= file_size` (on overflow set `overflow = True; break`).
       - Every ~0.4 s update `sess["chunk_progress"][chunk_index] = written` under `session_lock` and broadcast SSE `upload_progress` with `sum(sess["chunk_progress"].values())`.
    4. Ensure handle is closed (`with open(...)` exited), then in `finally:` acquire `session_lock`:
       - Decrement `sess["active_writers"] -= 1`.
       - If cancelled: if `sess["active_writers"] == 0`, remove `temp_path` and pop `_parallel_sessions[session_id]`; return `499`.
       - If overflow or stream error: reset `sess["chunk_progress"][chunk_index] = 0`; return `400` (overflow) or re-raise.
       - Record `sess["received_chunks"].add(chunk_index)` and `sess["chunk_progress"][chunk_index] = written`.
       - If `len(sess["received_chunks"]) == sess["total_chunks"] and sess["active_writers"] == 0 and not sess["finalized"]`:
         - Set `sess["finalized"] = True`, call `finalize_upload(temp_path, sess["dest_dir"], sess["filename"])`, record in `finished_uploads` & history, emit `file_received`, pop `_parallel_sessions[session_id]`, return `200 {success: True, completed: True, file: file_info}`.
       - Return `200 {success: True, completed: False, chunk_index: chunk_index}`.
- Update `api_upload_status(session_id)`:
  - Check `finished_uploads` -> return `{"received_bytes": info["size"], "received_chunks": [], "completed": True}`.
  - Check `_parallel_sessions` under `session_lock` -> return `{"received_bytes": sum(chunk_progress.values()), "received_chunks": sorted(received_chunks), "completed": False}`.
  - Fallback to existing `.tmp` size scan returning `{"received_bytes": size, "received_chunks": [], "completed": False}`.
- Update `api_upload_cancel()`:
  - If `session_id` is in `_parallel_sessions`, check `active_writers == 0` under `session_lock` before deleting `.tmp` and popping session (if `active_writers > 0`, active writers clean up on exit).
- In `shutdown()`: clear `_parallel_sessions` and delete any leftover temp files whose `active_writers == 0`.

- [ ] **Step 4: Run tests and commit**

Run: `python -m pytest tests/test_parallel_uploads.py tests/test_bugfixes.py tests/test_review_fixes.py tests/test_bug_sweep.py -v`
Expected: PASS (0 failures).

```bash
git add core/server.py tests/test_parallel_uploads.py
git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit -m "feat(server): add order-independent parallel multi-stream chunk upload protocol"
```

---

### Task 3: Desktop PC-to-PC Multi-Stream `OutgoingTransfer` with HTTP Keep-Alive & Multi-File Pool

**Files:**
- Modify: `ui/transfer.py:1-165`
- Modify: `gui.py:980-1030`
- Test: `tests/test_parallel_uploads.py` (append desktop transfer tests)

**Interfaces:**
- Consumes: `POST /api/upload/chunk` with `X-Parallel: 1`, `X-Device-Name`, `POST /api/upload/cancel`.
- Produces:
  - `OutgoingTransfer(..., max_streams: int = 4, parallel_threshold: int = 16 * 1024 * 1024, device_name: Optional[str] = None)`
  - Thread-local persistent `http.client.HTTPConnection` with `TCP_NODELAY` and automatic retry on stale keep-alive socket.
  - Multi-file concurrent dispatch in `MainWindow._start_outgoing` (up to 3 concurrent small files, 1 large multi-stream file at a time).

- [ ] **Step 1: Add failing tests in `tests/test_parallel_uploads.py`**

Add `TestOutgoingTransferParallel`:
- `test_large_file_uses_parallel_multi_stream_and_delivers_byte_identical`: 20 MB file with `chunk_size=2 MB`, `parallel_threshold=4 MB`, `max_streams=4` -> completes byte-identically with monotonic `on_progress` and `X-Parallel: 1` header sent.
- `test_keep_alive_reuses_tcp_connection`: multi-chunk transfer over single stream verifies persistent `HTTPConnection` reuse (`max_streams=1`, 4 chunks).
- `test_parallel_pause_resume_and_mid_chunk_cancel`: pause mid-parallel transfer -> stops advancing -> resume -> byte-identical; cancel mid-parallel transfer -> stops cleanly and removes `.tmp`.
- `test_device_name_header_integrates_with_auto_organize_device_mode`: `OutgoingTransfer(..., device_name="Studio-PC")` with server in `"device"` mode lands in `Studio-PC/<filename>`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_parallel_uploads.py::TestOutgoingTransferParallel -v`
Expected: FAIL (`TypeError: unexpected keyword argument 'parallel_threshold'` / `'device_name'`).

- [ ] **Step 3: Upgrade `ui/transfer.py` and `gui.py`**

In `ui/transfer.py`:
- Add `PARALLEL_THRESHOLD = 16 * 1024 * 1024`, `PARALLEL_CHUNK_SIZE = 8 * 1024 * 1024`, `DEFAULT_MAX_STREAMS = 4`.
- Keep `CHUNK_SIZE = 4 * 1024 * 1024` constant exported; when caller passes default `chunk_size=None`, choose `PARALLEL_CHUNK_SIZE` for `self.total >= self.parallel_threshold` else `CHUNK_SIZE`.
- Parse `self.chunk_url` via `urllib.parse.urlsplit` (`scheme`, `hostname`, `port`, `path`).
- Implement `_post_chunk(conn_holder, idx, total_chunks, offset, data, parallel: bool)` using thread-local `http.client.HTTPConnection` (`HTTP/1.1`, `Connection: keep-alive`, `TCP_NODELAY` via `conn.sock.setsockopt`), streaming `self._pieces(data)` via `putrequest`/`putheader`/`endheaders`/`send`, reading & draining response body so the socket stays reusable! Auto-reconnect once if a pooled connection raises `http.client.RemoteDisconnected` or `BrokenPipeError`/`ConnectionResetError` before sending body or when retrying cleanly.
- Include `"X-Device-Name": urllib.parse.quote(self.device_name)` when set (defaults to `socket.gethostname()`).
- For parallel mode (`self.total >= self.parallel_threshold and total_chunks > 1 and self.max_streams > 1`):
  - Track `self._chunk_bytes: dict[int, int]` and `self._sent_lock` for monotonic aggregate `self.sent` and EMA speed calculation.
  - Dispatch chunk indices `0..total_chunks-1` across `ThreadPoolExecutor(max_workers=min(self.max_streams, total_chunks))`.
  - Each worker waits on `self._resume.wait()` before opening/reading its slice, aborts immediately on `self._cancelled`, closes its thread-local `HTTPConnection` in `finally`.
- In `gui.py` `_start_outgoing`:
  - Split `batch` into small files (`tx.total < tx.parallel_threshold`) and large files (`tx.total >= tx.parallel_threshold`).
  - On the background dispatcher thread: run small files through `ThreadPoolExecutor(max_workers=3)` and large files sequentially (each using 4 internal parallel streams).

- [ ] **Step 4: Run tests and commit**

Run: `python -m pytest tests/test_parallel_uploads.py tests/test_gui_transfer.py tests/test_gui_smoke.py -v`
Expected: PASS (0 failures).

```bash
git add ui/transfer.py gui.py tests/test_parallel_uploads.py
git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit -m "feat(desktop): add keep-alive and 4-stream parallel OutgoingTransfer with multi-file queue"
```

---

### Task 4: Web App & Guest Drop Zone Multi-Stream `TransferManager` + Organized Received UI

**Files:**
- Modify: `static/js/app.js:1985-2560`
- Test: `tests/test_parallel_uploads.py` (append JS contract + syntax tests)

**Interfaces:**
- Consumes: `POST /api/upload/chunk` & `POST /api/drop/<token>/upload/chunk` (`X-Parallel: 1`), `GET /api/upload/status/<sessionId>`, `GET /api/received` (`rel_path`, `folder`).
- Produces: Smart hybrid `TransferManager` in `static/js/app.js` + subfolder-aware `renderReceivedFiles()`.

- [ ] **Step 1: Add failing contract test in `tests/test_parallel_uploads.py`**

Add `TestWebParallelAndReceivedContracts`:
- Verify `static/js/app.js` contains `LARGE_FILE_THRESHOLD`, `X-Parallel`, `inFlightXhrs`, `completedChunks`, `/api/upload/status/`, and `renderReceivedFiles` uses `file.rel_path` and `file.folder`.
- Verify `node --check static/js/app.js` returns 0.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_parallel_uploads.py::TestWebParallelAndReceivedContracts -v`
Expected: FAIL (`AssertionError: 'X-Parallel' not found`).

- [ ] **Step 3: Implement multi-stream `TransferManager` and `renderReceivedFiles` updates in `static/js/app.js`**

In `static/js/app.js`:
- Update `TransferManager`:
  - `this.smallFileThreshold = 16 * 1024 * 1024;` (16 MB)
  - `this.parallelChunkSize = 8 * 1024 * 1024;` (8 MB)
  - `this.maxStreamsPerLargeFile = 4;`
  - `this.maxTotalStreams = 5;` (keeps 1 of browser's 6 HTTP/1.1 slots free for SSE!)
  - `addTransfer(sessionId, filename, file, uploadUrl, targetName, relativeDir, completedChunks = null)`:
    - `isLarge = file.size >= this.smallFileThreshold`
    - `chunkSize = isLarge ? this.parallelChunkSize : Math.max(file.size, 1)`
    - `totalChunks = Math.ceil(file.size / chunkSize) || 1`
    - `useParallel = isLarge && totalChunks > 1`
    - `completed = new Set(Array.isArray(completedChunks) ? completedChunks : [])`
    - `pendingChunks = []` containing all `0..totalChunks-1` not in `completed`
    - `inFlightXhrs = new Map()`, `chunkLoaded = new Map()`, `maxStreams = useParallel ? Math.min(this.maxStreamsPerLargeFile, totalChunks) : 1`.
  - Helper `totalActiveStreams()` sums `t.maxStreams` across `this.activeTransfers.values()`.
  - `processQueue()` starts queued transfers whenever `totalActiveStreams() + candidateStreams <= this.maxTotalStreams` (or `this.activeTransfers.size === 0` so a 4-stream large file always starts immediately).
  - `pumpChunks(sessionId)` launches `uploadChunk(sessionId, chunkIndex)` while `!t.paused && !t.cancelled && t.inFlightXhrs.size < t.maxStreams && t.pendingChunks.length > 0`.
  - `uploadChunk(sessionId, chunkIndex)`:
    - Sets `X-Parallel: 1` when `t.useParallel`.
    - Tracks per-chunk progress in `t.chunkLoaded.set(chunkIndex, e.loaded)` and computes monotonic aggregate `totalLoaded` across `completedChunks` + `chunkLoaded` for progress bar, EMA speed, and ETA.
    - On HTTP 2xx: removes from `inFlightXhrs`, adds to `completedChunks`, sets `chunkLoaded.set(chunkIndex, chunk.size)`. If `resp.completed`, finishes transfer (removing card, scheduling 5s delayed `myUploadSessions.delete`, toast, history, `fetchReceivedFiles`); else calls `pumpChunks(sessionId)`.
    - On non-2xx / network error (when `!t.paused && !t.cancelled`): aborts all sibling `inFlightXhrs`, pushes uncompleted in-flight indices back to `pendingChunks`, calls `markTransferFailed(t, reason)`.
  - `togglePause(sessionId)`: aborts all XHRs in `t.inFlightXhrs`, pushes their indices back to front of `t.pendingChunks`, clears `chunkLoaded` for aborted chunks; on resume calls `pumpChunks(sessionId)`.
  - `cancelTransfer(sessionId)`: sets `t.cancelled = true`, aborts all XHRs in `t.inFlightXhrs`, clears card, calls `${cancelBaseUrl}/api/upload/cancel`.
  - `retryFailedTransfer(sessionId)`: fetches `${cancelBaseUrl}/api/upload/status/${encodeURIComponent(sessionId)}` (ignoring errors if cross-origin), merges returned `received_chunks` into `item.completedChunks` (if `completed: true`, completes immediately without re-uploading!), and re-adds only missing chunks!
- Update `renderReceivedFiles(files)`:
  - Encode `relPath = file.rel_path || file.filename` segment-by-segment (`relPath.split("/").map(encodeURIComponent).join("/")`) for `/api/received/download/...` and `/api/received/preview/...`.
  - Include `file.folder` in `meta.innerText` when non-empty (e.g., `${file.formatted_size} • ${file.folder} • ${file.modified}`).

- [ ] **Step 4: Run syntax check, tests, and commit**

Run: `node --check static/js/app.js; python -m pytest tests/test_parallel_uploads.py tests/test_backend.py tests/test_bug_sweep.py -v`
Expected: PASS (0 failures).

```bash
git add static/js/app.js tests/test_parallel_uploads.py
git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit -m "feat(web): add 4-stream parallel TransferManager and organized Received folder display"
```

---

### Task 5: Native Windows Shell Thumbnails Module (`core/thumbnails.py`)

**Files:**
- Create: `core/thumbnails.py`
- Test: `tests/test_thumbnails.py`

**Interfaces:**
- Consumes: `PIL.Image`, `PIL.ImageOps`, Windows `ctypes` (`ole32`, `shell32`, `gdi32`).
- Produces:
  - `THUMB_PX = 320`
  - `PILLOW_IMAGE_EXTS`, `SHELL_THUMB_EXTS`
  - `is_thumbnailable(filename: str) -> bool`
  - `make_thumbnail(path: str, mtime_ns: int, size: int) -> bytes` (LRU-cached, returns JPEG bytes or `b""` on unavailable/unsupported)
  - `shutdown_thumbnail_pool()` (clean shutdown hook called from `NearBeamServer.shutdown()` if needed; daemon workers).

- [ ] **Step 1: Write failing unit tests in `tests/test_thumbnails.py`**

Extend `tests/test_thumbnails.py` with `TestNativeThumbnailsModule`:
- `test_is_thumbnailable_extensions`: `.jpg`, `.png` always True; `.mp4`, `.pdf`, `.heic`, `.docx` True on Windows; `.txt`, `.zip`, `.bin` always False.
- `test_pillow_fast_path_and_negative_cache`: valid JPEG returns small JPEG bytes; corrupt JPEG returns `b""` (not unhandled crash when using `make_thumbnail`).
- `test_windows_shell_sta_pool_converts_bgra_and_releases_resources`: mock the COM/GDI extraction function (and verify real BGRA->JPEG alpha-compositing helper `_bgra_to_jpeg`) returns valid JPEG <= 320px.
- `test_windows_shell_timeout_returns_empty_without_hanging`: mock shell extraction sleeping 5 s with `timeout=0.1` -> returns `b""` in < 0.5 s!
- `test_real_windows_shell_extractor_handles_real_image_and_unsupported_file` (runs on `sys.platform == "win32"`): directly invokes `_extract_windows_shell_thumbnail` on a real `.jpg` (returns valid JPEG bytes!) and a `.txt` file with `SIIGBF_THUMBNAILONLY` (returns `b""` — no generic icon!).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_thumbnails.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'core.thumbnails'`).

- [ ] **Step 3: Implement `core/thumbnails.py`**

Create `core/thumbnails.py`:
- Constants:
  - `THUMB_PX = 320`
  - `SHELL_TIMEOUT_SEC = 3.0`
  - `PILLOW_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})`
  - `SHELL_THUMB_EXTS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".m4v", ".pdf", ".heic", ".heif", ".avif", ".tif", ".tiff", ".psd", ".cr2", ".nef", ".arw", ".dng", ".docx", ".xlsx", ".pptx"})`
- `is_thumbnailable(filename: str) -> bool`:
  - `ext = os.path.splitext(filename or "")[1].lower()`
  - Return `ext in PILLOW_IMAGE_EXTS` or (`sys.platform == "win32"` and `ext in SHELL_THUMB_EXTS`).
- `_make_pillow_thumbnail(path: str) -> bytes`: existing Pillow `draft` + `exif_transpose` + `thumbnail((THUMB_PX, THUMB_PX))` + JPEG q=75.
- `_bgra_to_jpeg(raw_bgra: bytes, width: int, height: int) -> bytes`: builds `Image.frombuffer("RGBA", (width, height), raw_bgra, "raw", "BGRA", 0, 1)`, composites alpha onto white `(255, 255, 255)` if alpha is not opaque, thumbnails to `(THUMB_PX, THUMB_PX)`, encodes JPEG q=75.
- Windows `ctypes` COM STA worker (`_WindowsShellThumbnailPool` with 2 daemon STA threads):
  - Each worker thread initializes COM once with `ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)` (0x2).
  - Pulls `(path, result_future)` tasks from a `queue.Queue`.
  - `_extract_shell_hbitmap_jpeg(path: str) -> bytes`:
    - `SHCreateItemFromParsingName(os.path.abspath(path), None, byref(IID_IShellItemImageFactory), byref(factory_ptr))` (`IID_IShellItemImageFactory = "{bcc18b79-ba16-442f-80c4-8a59c30c463b}"`).
    - Reads vtable pointer from `factory_ptr`, calls slot 3 (`GetImage(this, SIZE(320, 320), SIIGBF_THUMBNAILONLY | SIIGBF_BIGGERSIZEOK, byref(hbitmap))`), noting Windows x64 passes 8-byte `SIZE` struct (`c_long cx, c_long cy`) by value in a single 64-bit register (`c_uint64` packed `(cy << 32) | (cx & 0xFFFFFFFF)` works identically and cleanly across x64 & x86 via `SIZE` ctypes Structure!). Let's verify on this exact 64-bit Windows machine in Step 4!
    - Extracts dimensions with `gdi32.GetObjectW(hbitmap, sizeof(BITMAP), byref(bmp))`, allocates 32-bit top-down `BITMAPINFOHEADER` (`biHeight = -abs(height)`, `biBitCount = 32`, `biCompression = BI_RGB`), calls `gdi32.GetDIBits(...)` using a screen DC (`user32.GetDC(0)` / `ReleaseDC(0, hdc)`).
    - Always calls `gdi32.DeleteObject(hbitmap)` and COM `Release` (vtable slot 2) in `finally` blocks!
    - Converts buffer via `_bgra_to_jpeg(...)`.
  - `submit(path, timeout=SHELL_TIMEOUT_SEC)` waits on `future.result(timeout=timeout)` and returns `b""` on `TimeoutError` or any exception.
- `@lru_cache(maxsize=512) def make_thumbnail(path: str, mtime_ns: int, size: int) -> bytes`:
  - Checks `ext = os.path.splitext(path)[1].lower()`.
  - If `ext in PILLOW_IMAGE_EXTS`: try Pillow first; if Pillow succeeds return bytes.
  - If `sys.platform == "win32"` and `is_thumbnailable(path)`: try `_get_shell_pool().generate(path)`.
  - Return `b""` if neither produced a thumbnail (negative hit cached cleanly!).

- [ ] **Step 4: Run tests (including real Windows COM verification!) and commit**

Run: `python -m pytest tests/test_thumbnails.py -v`
Expected: PASS (0 failures, including real Windows `IShellItemImageFactory` test!).

```bash
git add core/thumbnails.py tests/test_thumbnails.py
git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit -m "feat(thumbs): add native Windows Shell COM STA thumbnail engine"
```

---

### Task 6: Wire Rich Media Thumbnails into Server, Batches, and Web Explorer

**Files:**
- Modify: `core/server.py:72-85,270-290,1190-1395,1475-1545`
- Modify: `core/batches.py:130-176`
- Modify: `static/js/app.js:350-385,1445-1475`
- Modify: `static/css/style.css:2740-2770`
- Test: `tests/test_thumbnails.py`

**Interfaces:**
- Consumes: `core.thumbnails.is_thumbnailable`, `core.thumbnails.make_thumbnail`.
- Produces:
  - `/api/safelist/thumb` and `/api/batches/<id>/thumb/<item_id>` serving videos, PDFs, HEIC/RAW, Office docs in addition to images.
  - `has_thumb: bool` on file entries from `/api/safelist`, `/api/safelist/browse`, `/api/batches/<id>/items`, and `/api/received`.
  - Video play-badge overlay (`.video-thumb-badge`) in Web Explorer Grid & List views.

- [ ] **Step 1: Add failing integration tests in `tests/test_thumbnails.py`**

Add tests to `TestThumbnails`:
- `test_video_and_pdf_expose_has_thumb_and_use_thumbnail_engine`: create `demo.mp4` and `doc.pdf` in shared folder, check `/api/safelist/browse` returns `has_thumb: True` (on Windows), mock `core.server.make_thumbnail` to return valid JPEG bytes -> `/api/safelist/thumb?subpath=demo.mp4` returns `200 image/jpeg`, and batch `/api/batches/<id>/thumb/<item_id>` returns `200 image/jpeg` + `preview_thumbs` includes the video item ID!
- `test_empty_thumbnail_returns_404`: when `make_thumbnail` returns `b""`, `/api/safelist/thumb` returns `404`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_thumbnails.py -v`
Expected: FAIL (`demo.mp4` rejected with 404 because `/api/safelist/thumb` checks `category != "image"`).

- [ ] **Step 3: Wire `core.thumbnails` across server, batches, JS, and CSS**

- In `core/server.py`:
  - Import `THUMB_PX, is_thumbnailable, make_thumbnail` from `core.thumbnails` (remove duplicate Pillow-only `make_thumbnail` definition).
  - Add `"has_thumb": is_thumbnailable(...)` to single-file entries in `api_safelist`, file items in `api_safelist_browse`, and entries in `api_received`.
  - Update `api_safelist_thumb` and `api_batches_thumb_item` to check `is_thumbnailable(path)` and return `404` when `not data`.
  - Pass `thumbnailable_fn=is_thumbnailable` to `BatchStore(...)`.
- In `core/batches.py`:
  - Accept optional `thumbnailable_fn: Callable[[str], bool]` in `BatchStore.__init__` (defaulting to `lambda name: self._category(name) == "image"`).
  - Include `"has_thumb": bool(self._thumbnailable(fname))` in `_make_item` & `_public_item`.
  - Build `preview_thumbs` in `_summary` using `[it["id"] for it in items if it.get("has_thumb")][:4]`.
- In `static/js/app.js`:
  - Render `<img class="file-thumb">` when `browsePrefs.thumbs && (item.has_thumb || item.category === "image")` in both `showAllSafeFolders` (root shared files) and `renderSafeItems`.
  - For `item.category === "video"`, append `<span class="video-thumb-badge" aria-hidden="true">▶</span>` inside `iconDiv` when thumbnail loads (and remove it if thumbnail fails to load so the clean video SVG icon shows!).
- In `static/css/style.css`:
  - Add `.video-thumb-badge` centered dark translucent circular play badge over `.file-thumb`.

- [ ] **Step 4: Run syntax check, tests, and commit**

Run: `node --check static/js/app.js; python -m pytest tests/test_thumbnails.py tests/test_batches.py tests/test_backend.py -v`
Expected: PASS (0 failures).

```bash
git add core/server.py core/batches.py static/js/app.js static/css/style.css tests/test_thumbnails.py
git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit -m "feat(thumbs): serve Windows Shell thumbnails for videos/PDFs/HEIC in Safe List, Batches, and Web UI"
```

---

### Task 7: Desktop Settings UI for Auto-Organize Received + Full Regression Verification

**Files:**
- Modify: `ui/pages.py:310-340`
- Modify: `gui.py:390-455,1200-1225`
- Modify: `ui/theme.py` (only if `QComboBox` styling needs refinement — inspect first)
- Test: `tests/test_auto_organize.py` (append offscreen PySide6 test)

**Interfaces:**
- Consumes: `config.auto_organize_mode`, `config.set_auto_organize_mode(mode)`.
- Produces: `SettingsPage.cmb_organize: QComboBox` wired to `MainWindow._change_auto_organize(index)`.

- [ ] **Step 1: Add failing offscreen GUI test in `tests/test_auto_organize.py`**

Add `TestAutoOrganizeSettingsGui`:
- Launch `MainWindow` offscreen with throwaway `ConfigManager`.
- Assert `win.settings_page.cmb_organize.currentData() == "off"`.
- Select `"type"` via `cmb_organize.setCurrentIndex(...)` -> assert `cfg.auto_organize_mode == "type"`.
- Select `"date"` -> assert `cfg.auto_organize_mode == "date"`.
- Clean up `win._clean_exit()` + `win.deleteLater()`.

- [ ] **Step 2: Run test to verify it fails**

Run: `$env:QT_QPA_PLATFORM="offscreen"; python -m pytest tests/test_auto_organize.py::TestAutoOrganizeSettingsGui -v`
Expected: FAIL (`AttributeError: 'SettingsPage' object has no attribute 'cmb_organize'`).

- [ ] **Step 3: Add Auto-Organize dropdown to `ui/pages.py` and wire in `gui.py`**

- In `ui/pages.py` `SettingsPage.__init__(..., organize_mode: str = "off")`:
  - Below `self.autosave` in the `receiving` `Card`, add a clean horizontal row:
    - Left: `label("Auto-organize received files")` + `label("Sort loose incoming files into subfolders automatically. Folder uploads stay intact.", "dim", wrap=True)`.
    - Right: `self.cmb_organize = QComboBox()` populated with:
      - `("Off (single folder)", "off")`
      - `("By file type (Photos, Videos...)", "type")`
      - `("By date (YYYY-MM-DD)", "date")`
      - `("By device (iPhone, Laptop...)", "device")`
    - Select the item whose `itemData` matches `organize_mode`.
- In `gui.py`:
  - Pass `config.auto_organize_mode` to `SettingsPage(...)`.
  - Store `self.cmb_organize = s.cmb_organize` and connect `currentIndexChanged` to `self._change_auto_organize`.
  - Implement `_change_auto_organize(self, _index: int)`: calls `config.set_auto_organize_mode(mode)` and logs to Activity log.
- Check `ui/theme.py` to ensure `QComboBox` has dark theme styling matching `QLineEdit` / buttons; add clean dark `QComboBox` + `QComboBox QAbstractItemView` rules if missing.

- [ ] **Step 4: Run full verification gate and commit**

Run:
1. `node --check static/js/app.js`
2. `$env:QT_QPA_PLATFORM="offscreen"; python -m pytest -v`
Expected: **0 failures across the entire test suite**!

```bash
git add ui/pages.py ui/theme.py gui.py tests/test_auto_organize.py
git -c user.name="Tanumay Goswami" -c user.email="tanumaygoswami2001@gmail.com" commit -m "feat(gui): add Auto-Organize Received mode selector in Desktop Settings"
```
