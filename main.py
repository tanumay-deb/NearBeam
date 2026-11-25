"""
Main entry point for NearBeam.
Launches the PySide6 Desktop GUI, or runs headless CLI server with --headless / --cli.
"""

import argparse
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Set AppUserModelID so Windows taskbar shows our custom icon instead of generic Python
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "NearBeam.WiFiTransfer.Desktop.1"
        )
    except Exception:
        pass

from core.config import config, user_config_path
from core.network import (
    find_available_port,
    get_connection_urls,
    get_hostname,
    get_local_ip,
)
from core.server import NearBeamServer, LandropServer, NearBeamRequestHandler


def run_headless(port: int = 5000):
    actual_port = find_available_port(port)
    server = NearBeamServer(port=actual_port)
    urls = get_connection_urls(actual_port)

    print("\n" + "=" * 60)
    print("  🚀 NearBeam Server is ONLINE & READY ON WI-FI")
    print("=" * 60)
    print(f"\n  ⭐ Permanent URL:          {urls['nearbeam_url']}")
    print(f"  👉 Hostname URL:           {urls['mdns_url']}")
    print(f"  👉 Direct IP URL:          {urls['primary_url']}")
    print(f"  🔍 Network Auto-Finder:    {urls['primary_url']}/finder")
    print(f"\n  💡 Tip: Bookmark '{urls['nearbeam_url']}' on your phone.")
    print("  It automatically stays connected even if your router changes your IP!\n")
    print(f"  📁 Auto-Save Destination:  {config.save_directory}")
    print(f"  🔒 Safe List Folders:      {len(config.get_safe_folders())} folder(s) shared")
    print("=" * 60)
    print("  Press Ctrl+C to stop the server.\n")

    def cli_logger(msg, etype="info"):
        print(f"  {msg}")

    server.add_activity_callback(cli_logger)

    try:
        server.app.run(
            host="0.0.0.0",
            port=actual_port,
            threaded=True,
            request_handler=NearBeamRequestHandler,
        )
    except KeyboardInterrupt:
        print("\n  Shutting down NearBeam server...")
    finally:
        server.shutdown()


def ensure_windows_start_menu_shortcut():
    """Ensures NearBeam is registered in the Windows Start Menu 'All apps' list."""
    if sys.platform != "win32":
        return
    try:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return
        programs_dir = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")

        # Clean up legacy shortcut if present
        legacy_shortcut = os.path.join(programs_dir, "LANDrop.lnk")
        if os.path.exists(legacy_shortcut):
            try:
                os.remove(legacy_shortcut)
            except Exception:
                pass

        shortcut_path = os.path.join(programs_dir, "NearBeam.lnk")
        if os.path.exists(shortcut_path):
            return

        target = sys.executable
        args = ""
        icon_path = ""

        if getattr(sys, "frozen", False):
            target = sys.executable
            icon_path = target
        else:
            exe_dir = os.path.dirname(sys.executable)
            candidates = (
                os.path.join(exe_dir, "nearbeam-gui.exe"),
                os.path.join(exe_dir, "Scripts", "nearbeam-gui.exe"),
                os.path.join(exe_dir, "nearbeam.exe"),
                os.path.join(exe_dir, "Scripts", "nearbeam.exe"),
                os.path.join(exe_dir, "Scripts", "landrop.exe"),
            )
            found_exe = next((p for p in candidates if os.path.exists(p)), None)
            if found_exe:
                target = found_exe
            else:
                pythonw = os.path.join(exe_dir, "pythonw.exe")
                target = pythonw if os.path.exists(pythonw) else sys.executable
                args = f'"{os.path.abspath(__file__)}"'

            possible_icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.ico")
            if os.path.exists(possible_icon):
                icon_path = possible_icon

        os.makedirs(programs_dir, exist_ok=True)
        ps_script = (
            "$ws = New-Object -ComObject WScript.Shell; "
            "$s = $ws.CreateShortcut($env:NB_LNK); "
            "$s.TargetPath = $env:NB_TARGET; "
            "if ($env:NB_ARGS) { $s.Arguments = $env:NB_ARGS }; "
            "if ($env:NB_ICON) { $s.IconLocation = $env:NB_ICON }; "
            "$s.Save()"
        )
        ps_env = {
            **os.environ,
            "NB_LNK": shortcut_path,
            "NB_TARGET": target,
            "NB_ARGS": args,
            "NB_ICON": f"{icon_path},0" if icon_path else "",
        }

        import subprocess
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            env=ps_env,
            capture_output=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def send_paths_to_running_instance(paths: list[str], port: int = 5000) -> bool:
    """If NearBeam is already running locally, hand off shared paths via HTTP API."""
    import json
    import socket
    import urllib.request

    candidate_ports = list(dict.fromkeys((port, 5000, 5001, 5002)))
    payload = json.dumps({"paths": paths}).encode("utf-8")
    for candidate_port in candidate_ports:
        try:
            with socket.create_connection(("127.0.0.1", candidate_port), timeout=0.2):
                pass
        except OSError:
            continue
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{candidate_port}/api/shell-send",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            continue
    return False


def main():
    has_console = sys.stdout is not None
    # Non-UTF-8 consoles (cp1252, redirected output) must not crash on the emoji banner.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ensure_windows_start_menu_shortcut()
    if sys.platform == "win32" and config.data.get("shell_integration_enabled") is True:
        try:
            from core.shell_integration import is_shell_integration_enabled, set_shell_integration
            if not is_shell_integration_enabled():
                set_shell_integration(True)
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="NearBeam - Transfer files without QR codes, accounts, or cables")
    parser.add_argument(
        "--headless",
        "--cli",
        action="store_true",
        help="Run in headless terminal mode without desktop GUI",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to run the server on (default: configured port or 5000)",
    )
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="Start NearBeam minimized directly to the Windows system tray",
    )
    parser.add_argument(
        "--send",
        nargs="*",
        default=[],
        help="Share or send file(s)/folder(s) via NearBeam from Windows Explorer",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=[],
        help="Optional file or folder paths passed via Windows SendTo or drag-and-drop onto executable",
    )
    # In frozen console=False builds, sys.stdout/sys.stderr are None; bind StringIO around
    # argparse so --help or invalid arguments never crash with AttributeError.
    orig_out, orig_err = sys.stdout, sys.stderr
    captured_io = None
    if orig_out is None or orig_err is None:
        import io
        captured_io = io.StringIO()
        if sys.stdout is None:
            sys.stdout = captured_io
        if sys.stderr is None:
            sys.stderr = captured_io
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        if captured_io is not None:
            msg = captured_io.getvalue().strip()
            if msg and sys.platform == "win32":
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(0, msg, "NearBeam", 0x40 if exc.code == 0 else 0x30)
                except Exception:
                    pass
        raise
    finally:
        sys.stdout, sys.stderr = orig_out, orig_err

    effective_port = args.port if args.port is not None else config.data.get("port", 5000)
    send_paths = [os.path.abspath(p) for p in (args.send + args.paths) if p and os.path.exists(p)]

    if args.headless:
        if send_paths and send_paths_to_running_instance(send_paths, effective_port):
            return
        run_headless(effective_port)
    else:
        if send_paths:
            # Prefer waking an existing desktop GUI via QLocalSocket; fall back to HTTP for a headless server.
            try:
                from gui import INSTANCE_KEY, hand_off_to_running_instance
                if hand_off_to_running_instance(INSTANCE_KEY, {"show": True, "paths": send_paths}):
                    return
            except Exception:
                pass
            if send_paths_to_running_instance(send_paths, effective_port):
                return
        try:
            from gui import run_gui
            run_gui(start_minimized=args.minimized, send_paths=send_paths, port=args.port)
        except Exception as e:
            import traceback
            err_msg = traceback.format_exc()
            log_path = user_config_path().parent / "nearbeam_error.log"
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(err_msg)
            except Exception:
                log_path = Path("nearbeam_error.log")
            if has_console:
                print(f"Failed to launch GUI ({e}), falling back to headless CLI mode...\n")
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    f"NearBeam could not open the desktop window:\n\n{e}\n\nDetails saved to {log_path}",
                    "NearBeam Launch Notice",
                    0x30,
                )
            except Exception:
                pass
            if has_console:
                run_headless(effective_port)


if __name__ == "__main__":
    main()

