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

from core.config import config
from core.network import (
    find_available_port,
    get_connection_urls,
    get_hostname,
    get_local_ip,
)
from core.server import NearBeamServer, LandropServer


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
    print("\n  💡 Tip: Bookmark 'http://nearbeam.local:5000' on your phone.")
    print("  It automatically stays connected even if your router changes your IP!\n")
    print(f"  📁 Auto-Save Destination:  {config.save_directory}")
    print(f"  🔒 Safe List Folders:      {len(config.get_safe_folders())} folder(s) shared")
    print("=" * 60)
    print("  Press Ctrl+C to stop the server.\n")

    def cli_logger(msg, etype="info"):
        print(f"  {msg}")

    server.add_activity_callback(cli_logger)

    try:
        from werkzeug.serving import WSGIRequestHandler
        # Give large transfers time to recover from brief Wi-Fi stalls.
        WSGIRequestHandler.timeout = 60
        server.app.run(host="0.0.0.0", port=actual_port, threaded=True)
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
            scripts_exe = os.path.join(os.path.dirname(sys.executable), "Scripts", "nearbeam.exe")
            if not os.path.exists(scripts_exe):
                scripts_exe = os.path.join(os.path.dirname(sys.executable), "Scripts", "landrop.exe")
            if os.path.exists(scripts_exe):
                target = scripts_exe
            else:
                target = sys.executable
                args = f'"{os.path.abspath(__file__)}"'

            possible_icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.ico")
            if os.path.exists(possible_icon):
                icon_path = possible_icon

        os.makedirs(programs_dir, exist_ok=True)
        ps_cmd = (
            f'$ws = New-Object -ComObject WScript.Shell; '
            f'$s = $ws.CreateShortcut("{shortcut_path}"); '
            f'$s.TargetPath = "{target}"; '
        )
        if args:
            ps_cmd += f'$s.Arguments = \'{args}\'; '
        if icon_path:
            ps_cmd += f'$s.IconLocation = "{icon_path},0"; '
        ps_cmd += '$s.Save()'

        import subprocess
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd], capture_output=True, timeout=5)
    except Exception:
        pass


def main():
    ensure_windows_start_menu_shortcut()
    parser = argparse.ArgumentParser(description="NearBeam - Wi-Fi File Sharing & Explorer")
    parser.add_argument(
        "--headless",
        "--cli",
        action="store_true",
        help="Run in headless terminal mode without desktop GUI",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config.data.get("port", 5000),
        help="Port to run the server on (default: 5000)",
    )
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="Start NearBeam minimized directly to the Windows system tray",
    )
    args = parser.parse_args()

    if args.headless:
        run_headless(args.port)
    else:
        try:
            from gui import run_gui
            run_gui(start_minimized=args.minimized)
        except Exception as e:
            import traceback
            err_msg = traceback.format_exc()
            try:
                with open("nearbeam_error.log", "w", encoding="utf-8") as f:
                    f.write(err_msg)
            except Exception:
                pass
            print(f"Failed to launch GUI ({e}), falling back to headless CLI mode...\n")
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    f"NearBeam could not open the desktop window:\n\n{e}\n\nDetails saved to nearbeam_error.log",
                    "NearBeam Launch Notice",
                    0x30,
                )
            except Exception:
                pass
            run_headless(args.port)


if __name__ == "__main__":
    main()
