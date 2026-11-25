"""
Thumbnail generation for NearBeam.
- Pillow fast-path for standard web/photo images (JPEG, PNG, GIF, WebP, BMP).
- Native Windows Shell COM (IShellItemImageFactory) on a dedicated STA worker pool
  for videos, PDFs, HEIC/HEIF, RAW photos, and Office documents with zero pip deps.
"""

from collections import OrderedDict
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import ctypes
import io
import os
import queue
import sys
import threading
from typing import Callable, Optional
import uuid

from PIL import Image, ImageOps

THUMB_PX = 320
SHELL_TIMEOUT_SEC = 3.0

PILLOW_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
SHELL_THUMB_EXTS = frozenset({
    # Videos
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".m4v",
    # Documents & Office
    ".pdf", ".docx", ".xlsx", ".pptx",
    # Modern & RAW camera formats
    ".heic", ".heif", ".avif", ".tif", ".tiff", ".psd",
    ".cr2", ".nef", ".arw", ".dng",
})


def is_thumbnailable(filename: str) -> bool:
    """Returns True if NearBeam can attempt thumbnail extraction for this filename."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in PILLOW_IMAGE_EXTS:
        return True
    if sys.platform == "win32" and ext in SHELL_THUMB_EXTS:
        return True
    return False


def _make_pillow_thumbnail(path: str) -> bytes:
    """Fast Pillow thumbnailer for standard raster images."""
    try:
        with Image.open(path) as img:
            if img.format == "JPEG":
                img.draft("RGB", (THUMB_PX * 2, THUMB_PX * 2))
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.getchannel("A"))
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((THUMB_PX, THUMB_PX), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75, optimize=True)
            return buf.getvalue()
    except Exception:
        return b""


def _bgra_to_jpeg(raw_bgra: bytes, width: int, height: int) -> bytes:
    """Converts a 32-bit top-down BGRA pixel buffer into an RGB JPEG <= THUMB_PX."""
    if width <= 0 or height <= 0 or len(raw_bgra) < width * height * 4:
        return b""
    img = Image.frombuffer("RGBA", (width, height), raw_bgra, "raw", "BGRA", 0, 1).copy()
    alpha_min, alpha_max = img.getchannel("A").getextrema()
    if alpha_max == 0:
        # Many Win32 thumbnail handlers leave the 4th byte zeroed (RGBX, not transparent)
        rgb = img.convert("RGB")
    elif alpha_min < 255:
        rgb = Image.new("RGB", img.size, (255, 255, 255))
        rgb.paste(img, mask=img.getchannel("A"))
    else:
        rgb = img.convert("RGB")
    rgb.thumbnail((THUMB_PX, THUMB_PX), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=75, optimize=True)
    return buf.getvalue()


if sys.platform == "win32":
    from ctypes import wintypes

    _COINIT_APARTMENTTHREADED = 0x2
    _SIIGBF_BIGGERSIZEOK = 0x00000001
    _SIIGBF_THUMBNAILONLY = 0x00000008
    _BI_RGB = 0
    _DIB_RGB_COLORS = 0

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class _SIZE(ctypes.Structure):
        _fields_ = [
            ("cx", ctypes.c_long),
            ("cy", ctypes.c_long),
        ]

    class _BITMAP(ctypes.Structure):
        _fields_ = [
            ("bmType", wintypes.LONG),
            ("bmWidth", wintypes.LONG),
            ("bmHeight", wintypes.LONG),
            ("bmWidthBytes", wintypes.LONG),
            ("bmPlanes", wintypes.WORD),
            ("bmBitsPixel", wintypes.WORD),
            ("bmBits", ctypes.c_void_p),
        ]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    _IID_IShellItemImageFactory = _GUID.from_buffer_copy(
        uuid.UUID("{bcc18b79-ba16-442f-80c4-8a59c30c463b}").bytes_le
    )

    _ole32 = ctypes.windll.ole32
    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _ole32.CoInitializeEx.restype = ctypes.c_long
    _ole32.CoUninitialize.argtypes = []
    _ole32.CoUninitialize.restype = None

    _shell32 = ctypes.windll.shell32
    _shell32.SHCreateItemFromParsingName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _shell32.SHCreateItemFromParsingName.restype = ctypes.c_long

    _gdi32 = ctypes.windll.gdi32
    _gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    _gdi32.GetObjectW.restype = ctypes.c_int
    _gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    _gdi32.GetDIBits.restype = ctypes.c_int
    _gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    _gdi32.DeleteObject.restype = wintypes.BOOL

    _user32 = ctypes.windll.user32
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.ReleaseDC.restype = ctypes.c_int

    _ReleaseFn = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
    _GetImageFn = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        _SIZE,
        ctypes.c_int,
        ctypes.POINTER(wintypes.HBITMAP),
    )

    def _extract_windows_shell_thumbnail(path: str) -> bytes:
        """Extracts a thumbnail via IShellItemImageFactory on an initialized COM STA thread."""
        abs_path = os.path.normpath(os.path.abspath(path))
        if not os.path.isfile(abs_path):
            return b""
        factory_ptr = ctypes.c_void_p()
        hr = _shell32.SHCreateItemFromParsingName(
            abs_path,
            None,
            ctypes.byref(_IID_IShellItemImageFactory),
            ctypes.byref(factory_ptr),
        )
        if hr < 0 or not factory_ptr.value:
            return b""

        hbitmap = wintypes.HBITMAP()
        vtable = ctypes.cast(
            factory_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        try:
            get_image = _GetImageFn(vtable[3])
            flags = _SIIGBF_THUMBNAILONLY | _SIIGBF_BIGGERSIZEOK
            hr = get_image(factory_ptr, _SIZE(THUMB_PX, THUMB_PX), flags, ctypes.byref(hbitmap))
            if hr < 0 or not hbitmap.value:
                return b""

            bmp = _BITMAP()
            if not _gdi32.GetObjectW(hbitmap, ctypes.sizeof(bmp), ctypes.byref(bmp)):
                return b""
            width = int(bmp.bmWidth)
            height = int(abs(bmp.bmHeight))
            if width <= 0 or height <= 0 or width > 4096 or height > 4096:
                return b""

            bmi = _BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.biWidth = width
            bmi.biHeight = -height  # negative -> top-down scanlines
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = _BI_RGB

            buf = ctypes.create_string_buffer(width * height * 4)
            hdc = _user32.GetDC(0)
            if not hdc:
                return b""
            try:
                lines = _gdi32.GetDIBits(
                    hdc,
                    hbitmap,
                    0,
                    height,
                    buf,
                    ctypes.byref(bmi),
                    _DIB_RGB_COLORS,
                )
            finally:
                _user32.ReleaseDC(0, hdc)
            if not lines:
                return b""
            return _bgra_to_jpeg(buf.raw, width, height)
        except Exception:
            return b""
        finally:
            if hbitmap.value:
                try:
                    _gdi32.DeleteObject(hbitmap)
                except Exception:
                    pass
            try:
                release = _ReleaseFn(vtable[2])
                release(factory_ptr)
            except Exception:
                pass
else:
    def _extract_windows_shell_thumbnail(path: str) -> bytes:
        return b""


class _WindowsShellThumbnailPool:
    """Dedicated COM STA worker pool so Flask threads never touch COM or hang on stalled codecs."""

    def __init__(
        self,
        workers: int = 2,
        extractor: Optional[Callable[[str], bytes]] = None,
        init_com: bool = True,
    ):
        self._extractor = extractor or _extract_windows_shell_thumbnail
        self._init_com = init_com and (sys.platform == "win32")
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._state_lock = threading.Lock()
        self._closed = False
        self._threads = [
            threading.Thread(target=self._worker_loop, name=f"nearbeam-thumb-sta-{i}", daemon=True)
            for i in range(max(1, workers))
        ]
        for th in self._threads:
            th.start()

    def _worker_loop(self):
        com_inited = False
        if self._init_com:
            try:
                hr = _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
                com_inited = hr in (0, 1)  # S_OK or S_FALSE
            except Exception:
                com_inited = False
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                path, fut = item
                if fut.cancelled() or fut.done():
                    continue
                try:
                    data = self._extractor(path)
                except Exception:
                    data = b""
                if not fut.done():
                    try:
                        fut.set_result(data if isinstance(data, bytes) else b"")
                    except Exception:
                        pass
        finally:
            if com_inited:
                try:
                    _ole32.CoUninitialize()
                except Exception:
                    pass

    def generate(self, path: str, timeout: float = SHELL_TIMEOUT_SEC) -> Optional[bytes]:
        """Returns JPEG bytes, b"" on definitive unavailability, or None on transient timeout/backpressure."""
        fut: Future = Future()
        with self._state_lock:
            if self._closed:
                return None
            try:
                self._queue.put_nowait((path, fut))
            except queue.Full:
                return None
        try:
            return fut.result(timeout=timeout)
        except (FutureTimeoutError, Exception):
            fut.cancel()
            return None

    def shutdown(self):
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            # Drain queued futures so stop sentinels always fit cleanly and no callers hang.
            while True:
                try:
                    queued = self._queue.get_nowait()
                    if queued is not None:
                        _, fut = queued
                        if not fut.done():
                            fut.cancel()
                except queue.Empty:
                    break
            for _ in self._threads:
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    break


_shell_pool_lock = threading.Lock()
_shell_pool: Optional[_WindowsShellThumbnailPool] = None


def _get_shell_pool() -> _WindowsShellThumbnailPool:
    global _shell_pool
    if _shell_pool is None:
        with _shell_pool_lock:
            if _shell_pool is None:
                _shell_pool = _WindowsShellThumbnailPool(workers=2)
    return _shell_pool


def shutdown_thumbnail_pool():
    global _shell_pool
    with _shell_pool_lock:
        pool = _shell_pool
        _shell_pool = None
    if pool is not None:
        pool.shutdown()


_THUMB_CACHE_MAXSIZE = 512
_thumb_cache_lock = threading.Lock()
_thumb_cache: "OrderedDict[tuple[str, int, int], bytes]" = OrderedDict()


def _thumb_cache_clear():
    with _thumb_cache_lock:
        _thumb_cache.clear()


def make_thumbnail(path: str, mtime_ns: int, size: int) -> bytes:
    """
    Returns cached JPEG thumbnail bytes (<= 320px), or b"" when unavailable.
    Keyed on (path, mtime_ns, size) so edits invalidate automatically.
    Definitive failures (corrupt / unsupported files) are cached as b"", while
    transient Windows Shell queue/timeout failures are NOT permanently cached.
    """
    key = (path, mtime_ns, size)
    with _thumb_cache_lock:
        cached = _thumb_cache.get(key)
        if cached is not None:
            _thumb_cache.move_to_end(key)
            return cached

    ext = os.path.splitext(path or "")[1].lower()
    result: Optional[bytes]
    if ext in PILLOW_IMAGE_EXTS:
        result = _make_pillow_thumbnail(path)
    elif sys.platform == "win32" and ext in SHELL_THUMB_EXTS:
        result = _get_shell_pool().generate(path)
    else:
        result = b""

    if result is None:
        # Transient timeout or backpressure: do not poison cache
        return b""

    with _thumb_cache_lock:
        _thumb_cache[key] = result
        _thumb_cache.move_to_end(key)
        while len(_thumb_cache) > _THUMB_CACHE_MAXSIZE:
            _thumb_cache.popitem(last=False)
    return result


make_thumbnail.cache_clear = _thumb_cache_clear
