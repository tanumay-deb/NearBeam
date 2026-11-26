"""
Windows Recycle Bin helper for NearBeam (zero third-party dependencies).
Uses Win32 SHFileOperationW with FOF_ALLOWUNDO so deleted files remain recoverable.
Never falls back to permanent deletion (os.remove).
"""

import ctypes
import os
import stat
import sys

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400

_WIN_LINK_REPARSE_TAGS = (
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
)


class RecycleError(OSError):
    """Raised when moving a file to the Windows Recycle Bin fails."""


def _is_link_or_junction(path: str) -> bool:
    try:
        if os.path.islink(path):
            return True
        if sys.platform == "win32":
            st = os.lstat(path)
            return getattr(st, "st_reparse_tag", 0) in _WIN_LINK_REPARSE_TAGS
    except OSError:
        return True
    return False


def _sh_file_operation(path_str: str, flags: int) -> tuple[int, bool]:
    """Invokes Win32 SHFileOperationW with a double-NUL-terminated wide string."""
    from ctypes import wintypes

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        if ctypes.sizeof(ctypes.c_void_p) == 4:
            _pack_ = 1
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    # SHFileOperationW requires pFrom to be double-NUL terminated.
    # create_unicode_buffer(s + "\0", len(s) + 2) guarantees two trailing NULs
    # while avoiding Python 3.10+ c_wchar_p embedded-null errors.
    buf = ctypes.create_unicode_buffer(path_str + "\0", len(path_str) + 2)
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
    op.pTo = None
    op.fFlags = flags
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None

    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int

    rc = shell32.SHFileOperationW(ctypes.byref(op))
    return int(rc), bool(op.fAnyOperationsAborted)


def send_to_recycle_bin(path: str) -> None:
    """
    Moves a single regular local file to the Windows Recycle Bin.
    Refuses directories, symbolic links/junctions, UNC network paths, and non-Windows platforms.
    Never falls back to permanent deletion.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Invalid file path")

    cleaned = path.strip()
    if cleaned.startswith("\\\\?\\"):
        cleaned = cleaned[4:]
    if cleaned.startswith("\\\\") or cleaned.startswith("//"):
        raise RecycleError("Cannot move a network share file to the Recycle Bin")

    abs_path = os.path.abspath(cleaned)
    if abs_path.startswith("\\\\"):
        raise RecycleError("Cannot move a network share file to the Recycle Bin")

    if not os.path.lexists(abs_path):
        raise FileNotFoundError(f"File not found: {abs_path}")
    if _is_link_or_junction(abs_path):
        raise PermissionError("Refusing to recycle symbolic link or junction")
    if os.path.isdir(abs_path):
        raise PermissionError("Refusing to recycle directory; only files are allowed")
    if not os.path.isfile(abs_path):
        raise PermissionError("Target is not a regular file")

    if sys.platform != "win32":
        raise RecycleError("Recycle Bin deletion is only supported on Windows")

    flags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    try:
        rc, aborted = _sh_file_operation(abs_path, flags)
    except Exception as exc:
        raise RecycleError(f"Recycle Bin operation failed: {exc}") from exc

    if rc != 0 or aborted:
        raise RecycleError(f"Windows Recycle Bin refused file (code={rc}, aborted={aborted})")
    if os.path.lexists(abs_path):
        raise RecycleError("File still exists after Recycle Bin request")
