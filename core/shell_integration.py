"""
Windows Explorer Shell & Share Menu Integration for NearBeam.
Registers 'Send with NearBeam' in the Windows right-click context menu
(HKCU\\Software\\Classes\\*\\shell\\NearBeam and Directory\\shell\\NearBeam)
and in the Windows 'Send to' menu (%APPDATA%\\Microsoft\\Windows\\SendTo).
No administrator privileges required.
"""

import os
import subprocess
import sys
from pathlib import Path

FILE_SHELL_KEY = r"Software\Classes\*\shell\NearBeam"
DIR_SHELL_KEY = r"Software\Classes\Directory\shell\NearBeam"
LEGACY_FILE_SHELL_KEY = r"Software\Classes\*\shell\LANDrop"
LEGACY_DIR_SHELL_KEY = r"Software\Classes\Directory\shell\LANDrop"


def is_windows() -> bool:
    return sys.platform == "win32"


def _get_launch_command_and_icon() -> tuple[str, str]:
    """Returns the command template and icon path for Windows Explorer shell invocation."""
    if getattr(sys, "frozen", False):
        exe_path = sys.executable
        cmd = f'"{exe_path}" --send "%1"'
        icon = f'"{exe_path}",0'
    else:
        python_dir = os.path.dirname(sys.executable)
        pythonw = os.path.join(python_dir, "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable

        project_dir = Path(__file__).resolve().parent.parent
        main_py = project_dir / "main.py"
        icon_file = project_dir / "assets" / "icon.ico"
        cmd = f'"{pythonw}" "{main_py}" --send "%1"'
        icon = f'"{icon_file}"' if icon_file.exists() else f'"{pythonw}",0'

    return cmd, icon


def _get_sendto_shortcut_path() -> str | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "SendTo", "NearBeam.lnk")


def is_shell_integration_enabled() -> bool:
    """Checks if 'Send with NearBeam' is registered in Windows Explorer context menu."""
    if not is_windows():
        return False

    import winreg
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, f"{FILE_SHELL_KEY}\\command", 0, winreg.KEY_READ
        ) as key:
            val, _ = winreg.QueryValueEx(key, "")
            return bool(val)
    except (FileNotFoundError, OSError):
        return False


def _delete_reg_tree(root, subkey: str):
    """Deletes a registry key and its 'command' subkey if present."""
    import winreg
    for child in ("command", ""):
        path = f"{subkey}\\{child}" if child else subkey
        try:
            winreg.DeleteKey(root, path)
        except (FileNotFoundError, OSError):
            pass


def set_shell_integration(enabled: bool) -> bool:
    """
    Enables or disables Windows Explorer right-click 'Send with NearBeam'
    and Windows 'Send to -> NearBeam' integration.
    """
    if not is_windows():
        return False

    import winreg

    try:
        # Clean up legacy LANDrop shell entries
        _delete_reg_tree(winreg.HKEY_CURRENT_USER, LEGACY_FILE_SHELL_KEY)
        _delete_reg_tree(winreg.HKEY_CURRENT_USER, LEGACY_DIR_SHELL_KEY)

        sendto_path = _get_sendto_shortcut_path()

        if enabled:
            cmd, icon = _get_launch_command_and_icon()

            # 1. File context menu: Right-click any file -> "Send with NearBeam"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, FILE_SHELL_KEY) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "Send with NearBeam")
                winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{FILE_SHELL_KEY}\\command") as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd)

            # 2. Folder context menu: Right-click any folder -> "Share Folder with NearBeam"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, DIR_SHELL_KEY) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "Share Folder with NearBeam")
                winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{DIR_SHELL_KEY}\\command") as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd)

            # 3. Windows 'SendTo' menu shortcut for multi-file selection
            if sendto_path:
                try:
                    os.makedirs(os.path.dirname(sendto_path), exist_ok=True)
                    project_dir = Path(__file__).resolve().parent.parent
                    icon_ico = project_dir / "assets" / "icon.ico"
                    if getattr(sys, "frozen", False):
                        target = sys.executable
                        args = "--send"
                        icon_loc = f"{sys.executable},0"
                    else:
                        python_dir = os.path.dirname(sys.executable)
                        pythonw = os.path.join(python_dir, "pythonw.exe")
                        target = pythonw if os.path.exists(pythonw) else sys.executable
                        main_py = project_dir / "main.py"
                        args = f'"{main_py}" --send'
                        icon_loc = f"{icon_ico},0" if icon_ico.exists() else f"{target},0"

                    ps_cmd = (
                        f'$ws = New-Object -ComObject WScript.Shell; '
                        f'$s = $ws.CreateShortcut("{sendto_path}"); '
                        f'$s.TargetPath = "{target}"; '
                        f'$s.Arguments = \'{args}\'; '
                        f'$s.IconLocation = "{icon_loc}"; '
                        f'$s.Save()'
                    )
                    subprocess.run(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                        capture_output=True,
                        timeout=5,
                    )
                except Exception:
                    pass
            return True
        else:
            _delete_reg_tree(winreg.HKEY_CURRENT_USER, FILE_SHELL_KEY)
            _delete_reg_tree(winreg.HKEY_CURRENT_USER, DIR_SHELL_KEY)
            if sendto_path and os.path.exists(sendto_path):
                try:
                    os.remove(sendto_path)
                except OSError:
                    pass
            return True
    except Exception as e:
        print(f"Error configuring Windows shell integration: {e}")
        return False
