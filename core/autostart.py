"""
Windows Startup (Autostart) Manager for NearBeam.
Handles adding and removing NearBeam from Windows startup via HKCU Run registry key.
Cleans up legacy LANDrop startup entries.
"""

import os
import sys
from pathlib import Path

REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "NearBeam"
LEGACY_APP_NAME = "LANDrop"


def is_windows() -> bool:
    return sys.platform == "win32"


def is_autostart_enabled() -> bool:
    """Checks if NearBeam (or legacy LANDrop) is registered in Windows startup."""
    if not is_windows():
        return False

    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_READ) as key:
            try:
                winreg.QueryValueEx(key, APP_NAME)
                return True
            except (FileNotFoundError, OSError):
                winreg.QueryValueEx(key, LEGACY_APP_NAME)
                return True
    except (FileNotFoundError, OSError):
        return False


def set_autostart(enabled: bool, start_minimized: bool = True) -> bool:
    """
    Registers or unregisters NearBeam in Windows startup registry.
    Automatically removes legacy LANDrop startup entry if present.
    """
    if not is_windows():
        return False

    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            # Always purge legacy entry
            try:
                winreg.DeleteValue(key, LEGACY_APP_NAME)
            except FileNotFoundError:
                pass

            if enabled:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    # Find pythonw.exe for windowless background execution
                    python_dir = os.path.dirname(sys.executable)
                    pythonw = os.path.join(python_dir, "pythonw.exe")
                    if not os.path.exists(pythonw):
                        pythonw = sys.executable

                    project_dir = Path(__file__).resolve().parent.parent
                    main_py = project_dir / "main.py"
                    cmd = f'"{pythonw}" "{main_py}"'

                if start_minimized:
                    cmd += " --minimized"

                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                return True
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
                return True
    except Exception as e:
        print(f"Error setting autostart: {e}")
        return False
