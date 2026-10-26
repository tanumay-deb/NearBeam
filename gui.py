"""
PySide6 Desktop Application for NearBeam.
Refined, perfectly scaled Windows 11 Dark theme control panel.
Features High-DPI support, scrollable responsive cards, no-wrap buttons,
and clean visual alignment.
"""

import datetime
import html
import json
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Enable High DPI scaling before importing Qt GUI
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from werkzeug.serving import WSGIRequestHandler, make_server

# Give large transfers time to recover from brief Wi-Fi stalls.
WSGIRequestHandler.timeout = 60

from core.autostart import is_autostart_enabled, set_autostart
from core.config import config
from core.network import (
    find_available_port,
    generate_qr_png_bytes,
    get_connection_urls,
    get_hostname,
    get_local_ip,
)
from core.server import NearBeamServer, LandropServer, NearBeamRequestHandler, format_size
from core.shell_integration import is_shell_integration_enabled, set_shell_integration
from core.version import APP_VERSION, RELEASES_URL, check_for_updates
from ui import theme
from ui.pages import ClipboardPage, HomePage, SettingsPage, SharedFoldersPage, TransfersPage
from ui.transfer import OutgoingTransfer
from ui.widgets import (
    DEVICE_ICONS,
    LiveDot,
    NavButton,
    TransferRow,
    button,
    clear_layout,
    device_role,
    fade_in,
    icon,
    icon_button,
    icon_label,
    label,
    repolish,
)


def get_resource_path(relative_path: str) -> Path:
    """Get absolute path to resource, works for dev and for PyInstaller bundle."""
    if getattr(sys, "frozen", False):
        base_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base_dir = Path(__file__).resolve().parent
    return base_dir / relative_path


class ServerThread(threading.Thread):
    def __init__(self, app, host: str, port: int):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.server = make_server(
            self.host,
            self.port,
            app,
            threaded=True,
            request_handler=NearBeamRequestHandler,
        )

    def run(self):
        try:
            self.server.serve_forever()
        except Exception as e:
            print(f"Server error: {e}")

    def shutdown(self):
        if self.server:
            self.server.shutdown()


class ActivityBridge(QObject):
    """Bridge for thread-safe UI updates from Flask server to Qt."""
    new_activity = Signal(str, str)
    update_checked = Signal(dict)
    clipboard_received = Signal(str)
    clipboard_image_received = Signal(bytes, str)
    mesh_updated = Signal(str, list)
    transfer_progress = Signal(str, object, object, float)
    transfer_done = Signal(str, bool, str)


class QRDialog(QDialog):
    """Clean, properly scaled dialog showing QR code for mobile scanning."""
    def __init__(self, urls, initial_url: str = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NearBeam - Mobile Connect & QR Code")
        self.setFixedSize(380, 450)
        self.setStyleSheet("""
            QDialog {
                background-color: #0b0f19;
                color: #f8fafc;
            }
            QLabel {
                color: #f8fafc;
            }
            QPushButton {
                background-color: #1e293b;
                color: #f8fafc;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 8px;
                padding: 6px 12px;
                font-weight: 700;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #334155;
                border-color: #38bdf8;
            }
        """)

        if isinstance(urls, dict):
            self.primary_url = urls.get("primary_url", "")
            self.nearbeam_url = urls.get("nearbeam_url", urls.get("landrop_url", ""))
            self.mdns_url = urls.get("mdns_url", self.nearbeam_url)
        else:
            self.primary_url = str(urls)
            self.nearbeam_url = str(urls)
            self.mdns_url = str(urls)

        self.initial_url = initial_url or self.nearbeam_url

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        info_lbl = QLabel("Scan with your phone or tablet camera:")
        info_lbl.setAlignment(Qt.AlignCenter)
        info_lbl.setStyleSheet("font-size: 12px; color: #94a3b8; font-weight: 600;")
        layout.addWidget(info_lbl)

        # 3-way toggle row: Permanent mDNS vs Hostname vs Direct IP
        mode_layout = QHBoxLayout()
        mode_layout.setSpacing(6)
        self.btn_mode_mdns = QPushButton("⭐ Permanent")
        self.btn_mode_host = QPushButton("🏷️ Hostname")
        self.btn_mode_direct = QPushButton("📱 Direct IP")

        self.btn_mode_mdns.setFixedHeight(28)
        self.btn_mode_host.setFixedHeight(28)
        self.btn_mode_direct.setFixedHeight(28)

        self.btn_mode_mdns.clicked.connect(self._select_mdns)
        self.btn_mode_host.clicked.connect(self._select_host)
        self.btn_mode_direct.clicked.connect(self._select_direct)

        mode_layout.addWidget(self.btn_mode_mdns)
        mode_layout.addWidget(self.btn_mode_host)
        mode_layout.addWidget(self.btn_mode_direct)
        layout.addLayout(mode_layout)

        self.url_box = QLabel()
        self.url_box.setAlignment(Qt.AlignCenter)
        self.url_box.setStyleSheet("""
            background-color: #131d31;
            border: 1px solid rgba(56, 189, 248, 0.35);
            border-radius: 6px;
            padding: 6px 10px;
            font-size: 13px;
            font-weight: bold;
            color: #38bdf8;
        """)
        self.url_box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.url_box)

        self.img_container = QLabel()
        self.img_container.setAlignment(Qt.AlignCenter)
        self.img_container.setStyleSheet("background-color: #ffffff; border-radius: 10px; padding: 10px;")
        layout.addWidget(self.img_container)

        # Initial selection
        if getattr(self, "initial_url", None) == self.primary_url:
            self._select_direct()
        elif getattr(self, "initial_url", None) == getattr(self, "mdns_url", None):
            self._select_host()
        else:
            self._select_mdns()

        btn_row = QHBoxLayout()
        btn_copy = QPushButton("Copy URL")
        btn_copy.setFixedHeight(30)
        btn_copy.clicked.connect(self._copy_current)
        btn_row.addWidget(btn_copy)

        close_btn = QPushButton("Close")
        close_btn.setFixedHeight(30)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _select_mdns(self):
        active_btn = "background-color: #0284c7; color: #ffffff; border: 1px solid #38bdf8; border-radius: 6px; font-weight: 600; font-size: 11px;"
        inactive_btn = "background-color: #1e293b; color: #94a3b8; border: 1px solid rgba(255,255,255,0.12); border-radius: 6px; font-weight: 500; font-size: 11px;"
        self.btn_mode_mdns.setStyleSheet(active_btn)
        self.btn_mode_host.setStyleSheet(inactive_btn)
        self.btn_mode_direct.setStyleSheet(inactive_btn)
        self.url_box.setText(self.nearbeam_url)
        self._update_qr(self.nearbeam_url)

    def _select_host(self):
        active_btn = "background-color: #0284c7; color: #ffffff; border: 1px solid #38bdf8; border-radius: 6px; font-weight: 600; font-size: 11px;"
        inactive_btn = "background-color: #1e293b; color: #94a3b8; border: 1px solid rgba(255,255,255,0.12); border-radius: 6px; font-weight: 500; font-size: 11px;"
        self.btn_mode_host.setStyleSheet(active_btn)
        self.btn_mode_mdns.setStyleSheet(inactive_btn)
        self.btn_mode_direct.setStyleSheet(inactive_btn)
        url = getattr(self, "mdns_url", self.nearbeam_url)
        self.url_box.setText(url)
        self._update_qr(url)

    def _select_direct(self):
        active_btn = "background-color: #0284c7; color: #ffffff; border: 1px solid #38bdf8; border-radius: 6px; font-weight: 600; font-size: 11px;"
        inactive_btn = "background-color: #1e293b; color: #94a3b8; border: 1px solid rgba(255,255,255,0.12); border-radius: 6px; font-weight: 500; font-size: 11px;"
        self.btn_mode_direct.setStyleSheet(active_btn)
        self.btn_mode_mdns.setStyleSheet(inactive_btn)
        self.btn_mode_host.setStyleSheet(inactive_btn)
        self.url_box.setText(self.primary_url)
        self._update_qr(self.primary_url)

    def _update_qr(self, url: str):
        qr_bytes = generate_qr_png_bytes(url)
        pix = QPixmap()
        if qr_bytes:
            pix.loadFromData(qr_bytes)
        self.img_container.setPixmap(pix.scaled(180, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _copy_current(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.url_box.text())


class MainWindow(QMainWindow):
    def __init__(self, send_paths: list[str] = None):
        super().__init__()
        self.initial_send_paths = send_paths or []
        self.setWindowTitle("NearBeam")
        self.resize(1000, 700)
        self.setMinimumSize(860, 600)
        self.setAcceptDrops(True)
        self.setStyleSheet(theme.QSS)

        # Setup icon
        self.icon_path = get_resource_path("assets/icon.ico")
        if not self.icon_path.exists():
            self.icon_path = get_resource_path("assets/icon.png")
        if self.icon_path.exists():
            self.setWindowIcon(QIcon(str(self.icon_path)))

        # Setup activity bridge (worker threads -> UI thread)
        self.bridge = ActivityBridge()
        self.bridge.new_activity.connect(self._append_log)
        self.bridge.update_checked.connect(self._on_update_checked)
        self.bridge.clipboard_received.connect(self._on_remote_clipboard_received)
        self.bridge.clipboard_image_received.connect(self._on_remote_clipboard_image_received)
        self.bridge.mesh_updated.connect(self._on_mesh_updated)
        self.bridge.transfer_progress.connect(self._on_transfer_progress)
        self.bridge.transfer_done.connect(self._on_transfer_done)
        self.current_mesh_nodes = []
        self.mesh_role = "leader"
        self._device_signature = None

        # Transfers: id -> {"row": TransferRow, "tx": OutgoingTransfer | None, ...}
        self.active_transfers = {}
        self.server_events = queue.Queue()

        # Clipboard auto-sync tracking state
        self._suppress_clipboard_echo = False
        self._last_synced_clipboard = ""
        self._last_synced_image_hash = ""
        self._clip_last = None
        QApplication.clipboard().dataChanged.connect(self._on_local_clipboard_changed)

        # Server setup
        self.server_thread = None
        self.server_instance = None
        self.port = config.data.get("port", 5000)
        self.current_url = f"http://nearbeam.local:{self.port}"
        self.ip_url = f"http://127.0.0.1:{self.port}"

        self._build_ui()
        self._setup_tray()
        self.start_server()
        self._refresh_history()

        # Periodic refresh for connected browser/mobile clients in Nearby Devices
        self.nearby_timer = QTimer(self)
        self.nearby_timer.timeout.connect(self._refresh_nearby_devices_ui)
        self.nearby_timer.start(4000)

        # Incoming transfer progress arrives on the same event stream the web app uses
        self.events_timer = QTimer(self)
        self.events_timer.timeout.connect(self._drain_server_events)
        self.events_timer.start(250)

        if self.initial_send_paths:
            QTimer.singleShot(400, lambda: self._handle_shared_paths(self.initial_send_paths, source="Explorer"))

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        root_layout.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        self.stack.setObjectName("pageStack")
        root_layout.addWidget(self.stack, 1)

        self.home = HomePage()
        self.transfers_page = TransfersPage()
        self.shared_page = SharedFoldersPage()
        self.clipboard_page = ClipboardPage(config.clipboard_sync_enabled)
        self.settings_page = SettingsPage(
            config.save_directory,
            config.auto_save_enabled,
            config.clipboard_sync_enabled,
            is_shell_integration_enabled(),
            is_autostart_enabled(),
        )
        for page in (self.home, self.transfers_page, self.shared_page, self.clipboard_page, self.settings_page):
            self.stack.addWidget(page)
        self.stack.currentChanged.connect(lambda i: fade_in(self.stack.widget(i), 240, rise=10))

        # Home
        self.home.drop_zone.files_dropped.connect(lambda paths: self._handle_shared_paths(paths, source="Drop zone"))
        self.home.btn_send.clicked.connect(self._send_file_to_node_dialog)
        self.home.btn_received.clicked.connect(self._open_save_folder)
        self.home.btn_view_all.clicked.connect(lambda: self._go(1))

        # Transfers
        self.log_view = self.transfers_page.log_view
        self.transfers_page.btn_clear_history.clicked.connect(self._clear_history)

        # Shared folders
        self.safe_table = self.shared_page.table
        self.shared_page.btn_add.clicked.connect(self._add_safe_folder)
        self.shared_page.btn_remove.clicked.connect(self._remove_safe_folder)

        # Clipboard (two switches, one setting)
        self.clipboard_page.copy_clicked.connect(self._copy_last_clipboard)

        # Settings — existing handlers keep their original attribute names
        s = self.settings_page
        self.txt_save_dir = s.txt_save_dir
        s.btn_change_dir.clicked.connect(self._choose_save_directory)
        s.btn_open_dir.clicked.connect(self._open_save_folder)
        self.chk_autosave = s.autosave.switch
        self.chk_autosave.toggled.connect(self._toggle_autosave)
        self.chk_clipboard_sync = s.clipboard.switch
        self.chk_clipboard_sync.toggled.connect(lambda on: self._set_clipboard_switches(on))
        self.clipboard_page.sync_row.switch.toggled.connect(lambda on: self._set_clipboard_switches(on))
        self.chk_shell_menu = s.shell_menu.switch
        self.chk_shell_menu.toggled.connect(self._toggle_shell_integration)
        self.chk_autostart = s.autostart.switch
        self.chk_autostart.toggled.connect(self._toggle_autostart)
        s.btn_firewall.clicked.connect(self._configure_firewall)

        self.nav_buttons[0].setChecked(True)
        self._refresh_safe_table()
        self.transfers_page.refresh_empty_states()

    def _build_sidebar(self) -> QFrame:
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(228)
        col = QVBoxLayout(side)
        col.setContentsMargins(14, 18, 14, 16)
        col.setSpacing(4)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        if self.icon_path.exists():
            logo = QLabel()
            logo.setPixmap(QIcon(str(self.icon_path)).pixmap(30, 30))
            brand.addWidget(logo)
        name = QLabel("NearBeam")
        name.setObjectName("brand")
        brand.addWidget(name)
        brand.addStretch()
        col.addLayout(brand)
        col.addSpacing(18)

        self.nav_buttons = []
        for i, (text, icon_name) in enumerate([
            ("Home", "home"),
            ("Transfers", "transfers"),
            ("Shared folders", "folder"),
            ("Clipboard", "clipboard"),
            ("Settings", "settings"),
        ]):
            btn = NavButton(text, icon_name)
            btn.clicked.connect(lambda _=False, idx=i: self._go(idx))
            col.addWidget(btn)
            self.nav_buttons.append(btn)
        col.addStretch()

        # The one place the phone address lives
        phone = QFrame()
        phone.setObjectName("phoneBox")
        pv = QVBoxLayout(phone)
        pv.setContentsMargins(12, 12, 12, 12)
        pv.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(icon_label("phone", theme.ACCENT, 16))
        head.addWidget(label("Open on your phone"))
        head.addStretch()
        pv.addLayout(head)
        # Always-visible QR (encodes the IP address: it works on every phone, scanning
        # already means you're on the same Wi-Fi). Click it for the larger dialog.
        self.btn_show_qr = QPushButton()
        self.btn_show_qr.setObjectName("qrButton")
        self.btn_show_qr.setCursor(Qt.PointingHandCursor)
        self.btn_show_qr.setToolTip("Scan with your phone's camera. Click for a larger code.")
        self.btn_show_qr.setAccessibleName("Connection QR code")
        self.btn_show_qr.setIconSize(QSize(112, 112))
        self.btn_show_qr.setFixedSize(124, 124)
        self.btn_show_qr.clicked.connect(self._show_qr)
        pv.addWidget(self.btn_show_qr, 0, Qt.AlignHCenter)

        # Both addresses, always shown
        self.addr_labels = {}
        self.addr_copy_buttons = {}
        for key, tag, tip in (
            ("name", "Name", "Permanent address. Works on iPhone, iPad, Mac and Windows."),
            ("ip", "IP", "Direct IP address, for devices that can't open .local names."),
        ):
            row = QHBoxLayout()
            row.setSpacing(6)
            addr = label("", "monoSmall")
            addr.setProperty("tip", tip)
            addr.setTextInteractionFlags(Qt.TextSelectableByMouse)
            addr.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            row.addWidget(addr, 1)
            copy_btn = icon_button("copy", f"Copy {tag.lower()} address")
            copy_btn.setFixedSize(26, 26)
            copy_btn.clicked.connect(lambda _=False, k=key: self._copy_url(k))
            row.addWidget(copy_btn)
            pv.addLayout(row)
            self.addr_labels[key] = addr
            self.addr_copy_buttons[key] = copy_btn
        col.addWidget(phone)
        col.addSpacing(10)

        status = QHBoxLayout()
        status.setSpacing(4)
        self.live_dot = LiveDot()
        status.addWidget(self.live_dot)
        self.status_label = label("Online on Wi-Fi", "success")
        status.addWidget(self.status_label)
        status.addStretch()
        col.addLayout(status)
        self.btn_update = button(f"v{APP_VERSION} · Up to date", kind="updateBtn")
        self.btn_update.setToolTip("Check for updates")
        self.btn_update.clicked.connect(lambda: self._check_updates_async(manual=True))
        col.addWidget(self.btn_update)
        return side

    def _go(self, index: int):
        self.nav_buttons[index].setChecked(True)
        self.stack.setCurrentIndex(index)

    def _setup_tray(self):
        """Sets up the Windows system tray icon with custom app icon."""
        self.tray_icon = QSystemTrayIcon(self)
        if self.icon_path.exists():
            self.tray_icon.setIcon(QIcon(str(self.icon_path)))
        else:
            self.tray_icon.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))

        tray_menu = QMenu(self)
        show_action = QAction("Open NearBeam Control Panel", self)
        show_action.triggered.connect(self.showNormal)
        open_web_action = QAction("Open Web App in Browser", self)
        open_web_action.triggered.connect(self._open_browser)
        update_action = QAction(f"Check for Updates (v{APP_VERSION})...", self)
        update_action.triggered.connect(lambda: self._check_updates_async(manual=True))
        quit_action = QAction("Exit NearBeam", self)
        quit_action.triggered.connect(self._clean_exit)

        tray_menu.addAction(show_action)
        tray_menu.addAction(open_web_action)
        tray_menu.addAction(update_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

    # ------------------------------------------------------------------
    # Server, address, updates
    # ------------------------------------------------------------------
    def start_server(self):
        try:
            actual_port = find_available_port(self.port)
            self.port = actual_port
            self.server_instance = NearBeamServer(port=actual_port)

            # Register activity callback
            self.server_instance.add_activity_callback(
                lambda msg, etype: self.bridge.new_activity.emit(msg, etype)
            )

            # Register remote clipboard callback
            self.server_instance.add_clipboard_callback(
                lambda text: self.bridge.clipboard_received.emit(text)
            )
            self.server_instance.add_clipboard_image_callback(
                lambda img_bytes, mime: self.bridge.clipboard_image_received.emit(img_bytes, mime)
            )

            # Register mesh cluster callback
            self.server_instance.add_mesh_callback(
                lambda role, nodes: self.bridge.mesh_updated.emit(role, nodes)
            )
            self.server_instance.event_subscribers.append(self.server_events)

            self.server_thread = ServerThread(self.server_instance.app, "0.0.0.0", actual_port)
            self.server_thread.start()

            urls = get_connection_urls(actual_port)
            self.active_urls = urls
            self.active_nearbeam_url = urls["nearbeam_url"]
            self.active_primary_url = urls["primary_url"]
            self.active_mdns_url = urls["mdns_url"]

            self._show_addresses()
            self._append_log(f"Server started on port {actual_port}. Permanent address: {urls['nearbeam_url']}.", "info")

            # Initialize mesh status display
            if self.server_instance.mesh:
                self._on_mesh_updated(
                    self.server_instance.mesh.role,
                    self.server_instance.mesh.get_nodes_list(),
                )
            else:
                self._refresh_nearby_devices_ui()

            # Check for updates in background
            self._check_updates_async(manual=False)
        except Exception as e:
            self.live_dot.set_color(theme.DANGER)
            self.status_label.setText("Server offline")
            self.status_label.setProperty("role", "warning")
            repolish(self.status_label)
            QMessageBox.critical(self, "Server Error", f"Failed to start server: {e}")

    def _show_addresses(self):
        """Fills the sidebar's name + IP addresses and the QR code."""
        self.current_url = getattr(self, "active_nearbeam_url", f"http://nearbeam.local:{self.port}")
        self.ip_url = getattr(self, "active_primary_url", f"http://127.0.0.1:{self.port}")
        for key, url in (("name", self.current_url), ("ip", self.ip_url)):
            self.addr_labels[key].setText(url.replace("http://", ""))
            self.addr_labels[key].setToolTip(url + "\n" + self.addr_labels[key].property("tip"))
        qr = QPixmap()
        if qr.loadFromData(generate_qr_png_bytes(self.ip_url)):
            self.btn_show_qr.setIcon(QIcon(qr))

    def _configure_firewall(self):
        """Allows NearBeam through Windows Firewall with a single click."""
        bat_path = get_resource_path("allow_firewall.bat")
        if bat_path.exists():
            cmd = f'powershell -Command "Start-Process cmd -ArgumentList \'/c \"\"{bat_path}\"\"\' -Verb RunAs"'
            os.system(cmd)
            self._append_log("Invoked Windows Firewall auto-configuration script with Administrator privileges.", "info")
            QMessageBox.information(
                self,
                "Windows Firewall Configuration",
                "A Windows User Account Control (UAC) prompt will appear.\n\n"
                "Click 'Yes' to add permanent inbound firewall rules for Port 5000 & mDNS.\n"
                "Once allowed, Windows will never prompt you again!",
            )
        else:
            ps_cmd = (
                'powershell -Command "Start-Process powershell -ArgumentList \'-Command '
                '\\\"New-NetFirewallRule -DisplayName \\\'NearBeam Wi-Fi File Transfer\\\' -Direction Inbound -LocalPort 5000 -Protocol TCP -Action Allow; '
                'New-NetFirewallRule -DisplayName \\\'NearBeam Wi-Fi File Transfer UDP\\\' -Direction Inbound -LocalPort 5000 -Protocol UDP -Action Allow; '
                'New-NetFirewallRule -DisplayName \\\'NearBeam mDNS Discovery\\\' -Direction Inbound -LocalPort 5353 -Protocol UDP -Action Allow; '
                'New-NetFirewallRule -DisplayName \\\'NearBeam Mesh Peer Discovery\\\' -Direction Inbound -LocalPort 5005 -Protocol UDP -Action Allow\\\"\' -Verb RunAs"'
            )
            os.system(ps_cmd)
            QMessageBox.information(
                self,
                "Windows Firewall",
                "Firewall configuration commands have been sent. Confirm the administrator prompt to apply.",
            )

    def _check_updates_async(self, manual: bool = False):
        self.manual_update_check = manual
        t = threading.Thread(target=self._run_update_check, daemon=True)
        t.start()

    def _run_update_check(self):
        res = check_for_updates()
        self.bridge.update_checked.emit(res)

    def _on_update_checked(self, res: dict):
        if res.get("update_available"):
            latest = res.get("latest_version")
            self.btn_update.setText(f"Update to v{latest} available")
            self.btn_update.setProperty("available", True)
            repolish(self.btn_update)
            self.btn_update.setToolTip(f"Open the v{latest} release page on GitHub")
            try:
                self.btn_update.clicked.disconnect()
            except Exception:
                pass
            self.btn_update.clicked.connect(lambda: webbrowser.open(res.get("release_url", RELEASES_URL)))

            if getattr(self, "manual_update_check", False):
                reply = QMessageBox.question(
                    self,
                    "Update Available",
                    f"A new version of NearBeam is available: v{latest}!\n\nWould you like to open the GitHub release page to download it?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply == QMessageBox.Yes:
                    webbrowser.open(res.get("release_url", RELEASES_URL))
        else:
            self.btn_update.setText(f"v{APP_VERSION} · Up to date")
            self.btn_update.setToolTip("Check for updates")
            if getattr(self, "manual_update_check", False):
                QMessageBox.information(
                    self,
                    "NearBeam Up to Date",
                    f"You are currently using the latest release: v{APP_VERSION}.",
                )

    def _append_log(self, msg: str, event_type: str = "info"):
        """Appends to the Activity log on the Transfers page (UI thread only)."""
        if hasattr(self, "log_view") and self.log_view:
            color = {"upload": theme.SUCCESS, "error": theme.DANGER, "warning": theme.WARNING}.get(event_type, theme.MUTED)
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            self.log_view.append(f'<span style="color: {theme.DIM};">{stamp}</span>&nbsp; <span style="color: {color};">{html.escape(msg)}</span>')
            scrollbar = self.log_view.verticalScrollBar()
            if scrollbar:
                scrollbar.setValue(scrollbar.maximum())
        if "connected from" in msg.lower():
            self._refresh_nearby_devices_ui()

    def _copy_url(self, key: str = "name"):
        url = self.current_url if key == "name" else self.ip_url
        QApplication.clipboard().setText(url)
        btn = self.addr_copy_buttons[key]
        btn.setIcon(icon("check", theme.SUCCESS, 16))
        QTimer.singleShot(1200, lambda: btn.setIcon(icon("copy", theme.MUTED, 16)))
        self._append_log(f"Copied URL {url} to clipboard.", "info")

    def _open_browser(self):
        webbrowser.open(self.current_url)

    def _show_qr(self):
        urls = getattr(self, "active_urls", self.current_url)
        dlg = QRDialog(urls, initial_url=self.ip_url, parent=self)
        dlg.exec()

    # ------------------------------------------------------------------
    # Nearby devices
    # ------------------------------------------------------------------
    def _on_mesh_updated(self, role: str, nodes: list):
        """Updates GUI when mesh role changes or nodes join/leave."""
        self.current_mesh_nodes = nodes or []
        self.mesh_role = role
        self._refresh_nearby_devices_ui()

    def _nearby_devices(self) -> list[dict]:
        devices = []
        if self.server_instance:
            try:
                devices = self.server_instance.get_nearby_devices()
            except Exception:
                devices = []
        if not devices:
            devices = [{"id": "self", "kind": "host", "name": get_hostname(), "device_type": "desktop", "is_self": True}]
        # This PC first, then other PCs, then phones/browsers
        return sorted(devices, key=lambda d: (not d.get("is_self"), d.get("kind") != "host", d.get("name", "")))

    def _refresh_nearby_devices_ui(self):
        """Rebuilds the device cards when the set of nearby devices changes."""
        if not hasattr(self, "home"):
            return
        devices = self._nearby_devices()
        others = [d for d in devices if not d.get("is_self")]
        role = "Primary host" if self.mesh_role == "leader" else "Joined another host"
        count = len(others)
        detail = f"{role}  ·  {count} other device{'s' if count != 1 else ''} online"
        self.home.set_status(get_hostname(), detail)
        if hasattr(self, "tray_icon") and self.tray_icon:
            self.tray_icon.setToolTip(f"NearBeam v{APP_VERSION} — {count} device{'s' if count != 1 else ''} online")

        signature = tuple((d.get("id"), d.get("name"), d.get("kind")) for d in devices)
        if signature == self._device_signature:
            return
        self._device_signature = signature
        self.home.set_devices(devices, self._send_to_device, self._pick_and_send_to_device)

    def _pick_and_send_to_device(self, device: dict):
        file_paths, _ = QFileDialog.getOpenFileNames(self, f"Send files to {device.get('name')}", "", "All Files (*.*)")
        if file_paths:
            self._send_to_device(device, file_paths)

    def _send_to_device(self, device: dict, paths: list[str]):
        """A PC gets the files pushed to it; a phone or browser gets them shared to download."""
        if device.get("kind") == "host" and not device.get("is_self"):
            files = [p for p in paths if os.path.isfile(p)]
            skipped = len(paths) - len(files)
            if skipped:
                self._append_log(f"Skipped {skipped} folder(s): only files can be sent straight to another PC.", "warning")
            if files:
                self._start_outgoing(f"{device['url']}/api/upload", device.get("name", "PC"), files)
                self._go(1)
        else:
            self._handle_shared_paths(paths, source=f"Shared for {device.get('name', 'device')}", send_to_peers=False)

    def _choose_target(self, anchor) -> dict | None:
        """Menu of where files can go: each other PC by name, or shared for phones/browsers."""
        pcs = [d for d in self._nearby_devices() if d.get("kind") == "host" and not d.get("is_self")]
        share = {"kind": "share", "name": "phones and browsers"}
        if not pcs:
            return share
        menu = QMenu(self)
        chosen = [None]
        for d in pcs:
            role, _ = device_role(d)
            act = menu.addAction(icon(DEVICE_ICONS.get(d.get("device_type"), "laptop"), theme.ACCENT, 16), f"Send to {d.get('name')}  ·  {role}")
            act.triggered.connect(lambda _=False, dev=d: chosen.__setitem__(0, dev))
        menu.addSeparator()
        act = menu.addAction(icon("phone", theme.SUCCESS, 16), "Share with phones and browsers")
        act.triggered.connect(lambda _=False: chosen.__setitem__(0, share))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        return chosen[0]

    def _send_file_to_node_dialog(self):
        """Pick a destination first, then the files."""
        target = self._choose_target(self.home.btn_send)
        if target is None:
            return
        title = "Choose files to share" if target["kind"] == "share" else f"Send files to {target.get('name')}"
        file_paths, _ = QFileDialog.getOpenFileNames(self, title, "", "All Files (*.*)")
        if not file_paths:
            return
        if target["kind"] == "share":
            self._handle_shared_paths(file_paths, source="Send button", send_to_peers=False)
        else:
            self._send_to_device(target, file_paths)

    def _handle_shared_paths(self, paths: list[str], source: str = "Drag & Drop", send_to_peers: bool = True):
        """Stages dropped/shared files & folders in Safe List and optionally beams to online peer computers."""
        valid_paths = [os.path.abspath(p) for p in paths if p and os.path.exists(p)]
        if not valid_paths:
            return

        added_items = []
        file_only_paths = []
        for p in valid_paths:
            try:
                entry = config.add_safe_folder(p)
                added_items.append(entry["name"])
            except Exception:
                pass
            if os.path.isfile(p):
                file_only_paths.append(p)

        self._refresh_safe_table()
        if hasattr(self, "server_instance") and self.server_instance:
            self.server_instance.broadcast_event({
                "type": "safelist_updated",
                "items": added_items,
                "message": f"Shared via {source}: {', '.join(added_items)}",
            })

        self._append_log(f"Shared {len(valid_paths)} item(s) via {source}: {', '.join(added_items)}", "upload")
        if hasattr(self, "tray_icon") and self.tray_icon and not send_to_peers:
            self.tray_icon.showMessage("Ready on your phone", f"{len(valid_paths)} item(s) are now in Shared folders.", QSystemTrayIcon.Information, 2500)

        # If a peer NearBeam computer is online and files were selected, offer/send directly
        peers = [n for n in getattr(self, "current_mesh_nodes", []) if not n.get("is_self")]
        if send_to_peers and peers and file_only_paths:
            target_node = peers[0]
            if len(peers) > 1:
                menu = QMenu(self)
                selected_box = [None]
                for p in peers:
                    action = menu.addAction(icon("laptop", theme.ACCENT, 16), f"Send to {p['name']}  ·  {'Host' if p.get('is_leader') else 'Peer PC'}  ({p['ip']})")
                    action.triggered.connect(lambda checked=False, node=p: selected_box.__setitem__(0, node))
                menu.exec(self.mapToGlobal(self.rect().center()))
                if not selected_box[0]:
                    return
                target_node = selected_box[0]
            self._start_outgoing(f"{target_node['url']}/api/upload", target_node["name"], file_only_paths)

    def dragEnterEvent(self, event):
        """Accept files and folders dragged anywhere onto the NearBeam desktop window (NB-04)."""
        if event.mimeData() and event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """Handle files and folders dropped anywhere onto the NearBeam desktop window (NB-04)."""
        if not event.mimeData() or not event.mimeData().hasUrls():
            return
        dropped_paths = []
        for url in event.mimeData().urls():
            local_path = url.toLocalFile()
            if local_path and os.path.exists(local_path):
                dropped_paths.append(local_path)
        if dropped_paths:
            event.acceptProposedAction()
            self._handle_shared_paths(dropped_paths, source="Window Drag & Drop")

    # ------------------------------------------------------------------
    # Transfers
    # ------------------------------------------------------------------
    def _start_outgoing(self, upload_url: str, target_name: str, file_paths: list[str]):
        """Queues files to a peer PC; they're sent one after another on a worker thread."""
        batch = []
        for fp in file_paths:
            tx = OutgoingTransfer(
                upload_url,
                target_name,
                fp,
            )
            tid = tx.id
            tx.on_progress = lambda sent, total, bps, tid=tid: self.bridge.transfer_progress.emit(tid, sent, total, bps)
            tx.on_done = lambda ok, msg, tid=tid: self.bridge.transfer_done.emit(tid, ok, msg)
            row = TransferRow(tx.filename, "sent", target_name, tx.total, controls=True)
            row.set_queued()
            row.pause_toggled.connect(lambda paused, t=tx: t.pause() if paused else t.resume())
            row.cancel_clicked.connect(tx.cancel)
            self._add_active_row(tid, row, tx=tx)
            batch.append(tx)
        self._append_log(f"Sending {len(batch)} file(s) to {target_name}...", "info")
        threading.Thread(target=lambda: [tx.run() for tx in batch], daemon=True).start()

    def _add_active_row(self, tid: str, row, tx=None, **extra):
        self.transfers_page.active_box.addWidget(row)
        fade_in(row, 200)
        self.active_transfers[tid] = {"row": row, "tx": tx, **extra}
        self._update_transfer_badge()

    def _remove_active_row(self, tid: str):
        entry = self.active_transfers.pop(tid, None)
        if entry:
            self.transfers_page.active_box.removeWidget(entry["row"])
            entry["row"].deleteLater()
        self._update_transfer_badge()

    def _update_transfer_badge(self):
        self.nav_buttons[1].set_badge(len(self.active_transfers))
        self.transfers_page.refresh_empty_states()

    def _on_transfer_progress(self, tid: str, sent, total, bps: float):
        entry = self.active_transfers.get(tid)
        if entry:
            entry["row"].set_progress(int(sent), int(total), bps)

    def _on_transfer_done(self, tid: str, ok: bool, msg: str):
        entry = self.active_transfers.get(tid)
        if not entry:
            return
        tx, row = entry["tx"], entry["row"]
        status = "completed" if ok else ("cancelled" if msg == "Cancelled" else "failed")
        row.set_finished(ok, "Sent" if ok else msg)
        config.add_transfer_history({
            "filename": tx.filename,
            "size": tx.total,
            "formatted_size": format_size(tx.total),
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "direction": "sent",
            "peer": tx.target_name,
            "status": status,
        })
        if ok:
            self._append_log(f"Sent '{tx.filename}' to {tx.target_name}.", "upload")
        elif status == "failed":
            self._append_log(f"Failed sending '{tx.filename}' to {tx.target_name}: {msg}", "error")
        QTimer.singleShot(1500 if ok else 3000, lambda: (self._remove_active_row(tid), self._refresh_history()))

    def _peer_name(self, ip: str) -> str:
        for d in self._nearby_devices():
            if d.get("ip") == ip:
                return d.get("name") or ip
        return ip or "a device"

    def _drain_server_events(self):
        """Turns the server's live upload events into incoming transfer rows."""
        changed_history = False
        while True:
            try:
                payload = self.server_events.get_nowait()
            except queue.Empty:
                break
            try:
                ev = json.loads(payload)
            except (TypeError, ValueError):
                continue
            etype = ev.get("type")
            tid = f"in:{ev.get('session_id')}"
            if etype == "upload_progress":
                received = int(ev.get("received_bytes") or 0)
                pct = float(ev.get("percent") or 0)
                total = int(received * 100 / pct) if pct > 0 else received
                entry = self.active_transfers.get(tid)
                now = time.monotonic()
                if not entry:
                    row = TransferRow(ev.get("filename", "file"), "received", self._peer_name(ev.get("from_addr", "")), total)
                    self._add_active_row(tid, row, last=(now, received))
                    row.set_progress(received, total, 0)
                    continue
                last_t, last_b = entry.get("last", (now, received))
                bps = (received - last_b) / (now - last_t) if now > last_t else 0
                entry["last"] = (now, received)
                entry["row"].set_progress(received, total, max(bps, 0))
            elif etype == "file_received":
                entry = self.active_transfers.get(tid)
                if entry:
                    entry["row"].set_finished(True, "Received")
                    QTimer.singleShot(1500, lambda t=tid: self._remove_active_row(t))
                changed_history = True
            elif etype == "upload_cancelled":
                self._remove_active_row(tid)
            elif etype == "download_progress":
                # A phone/browser pulling a shared file from this PC
                did = ev.get("download_id")
                sent, total = int(ev.get("sent") or 0), int(ev.get("total") or 0)
                now = time.monotonic()
                entry = self.active_transfers.get(did)
                if not entry:
                    row = TransferRow(ev.get("filename", "file"), "sent", self._peer_name(ev.get("to_addr", "")), total)
                    self._add_active_row(did, row, last=(now, sent))
                    row.set_progress(sent, total, 0)
                    continue
                last_t, last_b = entry.get("last", (now, sent))
                bps = (sent - last_b) / (now - last_t) if now > last_t and sent >= last_b else 0
                entry["last"] = (now, sent)
                entry["row"].set_progress(sent, total, bps)
            elif etype == "download_finished":
                did = ev.get("download_id")
                entry = self.active_transfers.get(did)
                if ev.get("completed"):
                    if entry:
                        entry["row"].set_finished(True, "Downloaded")
                        QTimer.singleShot(1500, lambda t=did: self._remove_active_row(t))
                    changed_history = True
                elif entry:
                    # Video players stream in ranges; a stopped range isn't a failure.
                    if ev.get("partial_range"):
                        self._remove_active_row(did)
                    else:
                        entry["row"].set_finished(False, "Stopped")
                        QTimer.singleShot(3000, lambda t=did: self._remove_active_row(t))
        if changed_history:
            self._refresh_history()

    def _history_row(self, rec: dict):
        direction = "sent" if rec.get("direction") == "sent" else "received"
        row = TransferRow(rec.get("filename", "file"), direction, rec.get("peer", ""), int(rec.get("size") or 0), openable=direction == "received")
        status = rec.get("status", "completed")
        when = (rec.get("timestamp") or "")[5:16].replace("-", "/")
        row.set_finished(status == "completed", when if status == "completed" else f"{status.capitalize()} · {when}")
        row.bar.hide()
        row.pct.hide()
        row.open_clicked.connect(lambda name=rec.get("filename", ""): self._reveal_received(name))
        return row

    def _refresh_history(self):
        history = config.get_transfer_history()
        clear_layout(self.transfers_page.history_box)
        for rec in history[:50]:
            self.transfers_page.history_box.addWidget(self._history_row(rec))
        clear_layout(self.home.recent_box)
        for rec in history[:3]:
            self.home.recent_box.addWidget(self._history_row(rec))
        if not history:
            self.home.recent_box.addWidget(label("Nothing yet. Drop files above to get started.", "dim"))
        self.transfers_page.refresh_empty_states()

    def _clear_history(self):
        config.clear_transfer_history()
        self._refresh_history()

    def _reveal_received(self, filename: str):
        path = os.path.join(config.save_directory, filename)
        if filename and os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            self._open_save_folder()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def _toggle_autosave(self, checked: bool):
        config.set_auto_save(checked)
        state_str = "ENABLED" if checked else "DISABLED"
        self._append_log(f"Auto-Save setting updated: {state_str}", "info")

    def _set_clipboard_switches(self, on: bool):
        """Keeps the Settings and Clipboard page switches in step."""
        for sw in (self.chk_clipboard_sync, self.clipboard_page.sync_row.switch):
            if sw.isChecked() != on:
                sw.blockSignals(True)
                sw.setChecked(on)
                sw._animate(on)
                sw.blockSignals(False)
        if config.clipboard_sync_enabled != on:
            self._toggle_clipboard_sync(on)

    def _toggle_clipboard_sync(self, checked: bool):
        config.set_clipboard_sync(checked)
        state_str = "ENABLED" if checked else "DISABLED"
        self._append_log(f"Clipboard Auto-Sync: {state_str}", "info")
        if checked:
            self._on_local_clipboard_changed()

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------
    def _show_clip(self, kind: str, value, source: str):
        stamp = datetime.datetime.now().strftime("%H:%M")
        meta = f"{source}  ·  {stamp}"
        self._clip_last = (kind, value)
        if kind == "text":
            self.clipboard_page.show_text(value, meta)
        else:
            self.clipboard_page.show_image(QPixmap.fromImage(value), meta)

    def _copy_last_clipboard(self):
        if not self._clip_last:
            return
        kind, value = self._clip_last
        self._suppress_clipboard_echo = True
        try:
            if kind == "text":
                QApplication.clipboard().setText(value)
            else:
                QApplication.clipboard().setImage(value)
        finally:
            self._suppress_clipboard_echo = False
        self.clipboard_page.btn_copy.setText("Copied")
        QTimer.singleShot(1200, lambda: self.clipboard_page.btn_copy.setText("Copy text" if kind == "text" else "Copy image"))
    def _on_local_clipboard_changed(self):
        """Called whenever text or a screenshot/image is copied anywhere on Windows (Ctrl+C / Win+Shift+S)."""
        if not getattr(self, "chk_clipboard_sync", None) or not self.chk_clipboard_sync.isChecked():
            return
        if getattr(self, "_suppress_clipboard_echo", False):
            return

        try:
            clipboard = QApplication.clipboard()
            mime_data = clipboard.mimeData()

            # 1. Check for copied screenshot or image first (NB-07)
            if mime_data and mime_data.hasImage():
                qimg = clipboard.image()
                if not qimg.isNull():
                    byte_array = QByteArray()
                    buffer = QBuffer(byte_array)
                    buffer.open(QIODevice.WriteOnly)
                    qimg.save(buffer, "PNG")
                    buffer.close()
                    png_bytes = bytes(byte_array)
                    if png_bytes:
                        import hashlib
                        img_hash = hashlib.sha256(png_bytes).hexdigest()
                        if img_hash != getattr(self, "_last_synced_image_hash", ""):
                            self._last_synced_image_hash = img_hash
                            if hasattr(self, "server_instance") and self.server_instance:
                                self.server_instance.set_clipboard_image(
                                    png_bytes,
                                    mime_type="image/png",
                                    notify_local_os=False,
                                    propagate_mesh=True,
                                )
                                kb = len(png_bytes) / 1024.0
                                self._append_log(
                                    f"Auto-synced Windows screenshot/image ({kb:.1f} KB) to shared clipboard",
                                    "upload",
                                )
                                self._show_clip("image", qimg, "From this PC")
                return

            # 2. Check for copied text
            text = clipboard.text()
            if text and text != getattr(self, "_last_synced_clipboard", ""):
                self._last_synced_clipboard = text
                if hasattr(self, "server_instance") and self.server_instance:
                    import datetime
                    self.server_instance.clipboard_content = text
                    self.server_instance.clipboard_updated_at = datetime.datetime.now().strftime("%H:%M:%S")
                    self.server_instance.broadcast_event({
                        "type": "clipboard_updated",
                        **self.server_instance.get_clipboard_state(),
                    })
                    if self.server_instance.mesh:
                        self.server_instance.mesh.broadcast_clipboard_mesh(text)
                    snippet = (text[:30] + "...") if len(text) > 30 else text
                    self._append_log(f"Auto-synced Windows clipboard to network: '{snippet}'", "info")
                    self._show_clip("text", text, "From this PC")
        except Exception as e:
            print(f"Error in _on_local_clipboard_changed: {e}")

    def _on_remote_clipboard_received(self, text: str):
        """Called when a mobile device or web client updates the shared text clipboard."""
        if not getattr(self, "chk_clipboard_sync", None) or not self.chk_clipboard_sync.isChecked():
            return
        if not text or text == getattr(self, "_last_synced_clipboard", ""):
            return

        try:
            self._suppress_clipboard_echo = True
            self._last_synced_clipboard = text
            QApplication.clipboard().setText(text)
            self._suppress_clipboard_echo = False
            snippet = (text[:30] + "...") if len(text) > 30 else text
            self._append_log(f"Received and copied mobile clipboard to Windows: '{snippet}'", "info")
            self._show_clip("text", text, "From another device")
        except Exception as e:
            self._suppress_clipboard_echo = False
            print(f"Error in _on_remote_clipboard_received: {e}")

    def _on_remote_clipboard_image_received(self, img_bytes: bytes, mime_type: str):
        """Called when a mobile device, browser, or peer PC shares an image into the clipboard (NB-07)."""
        if not getattr(self, "chk_clipboard_sync", None) or not self.chk_clipboard_sync.isChecked():
            return
        if not img_bytes:
            return

        try:
            import hashlib
            img_hash = hashlib.sha256(img_bytes).hexdigest()
            if img_hash == getattr(self, "_last_synced_image_hash", ""):
                return

            qimg = QImage.fromData(img_bytes)
            if qimg.isNull():
                return

            self._suppress_clipboard_echo = True
            self._last_synced_image_hash = img_hash
            QApplication.clipboard().setImage(qimg)
            self._suppress_clipboard_echo = False
            kb = len(img_bytes) / 1024.0
            self._append_log(
                f"Copied incoming image ({kb:.1f} KB) to the Windows clipboard",
                "upload",
            )
            self._show_clip("image", qimg, "From another device")
        except Exception as e:
            self._suppress_clipboard_echo = False
            print(f"Error in _on_remote_clipboard_image_received: {e}")

    def _toggle_shell_integration(self, checked: bool):
        success = set_shell_integration(checked)
        config.data["shell_integration_enabled"] = bool(checked)
        config.save()
        if success:
            state_str = "ENABLED" if checked else "DISABLED"
            self._append_log(f"Windows Explorer 'Send with NearBeam' context menu: {state_str}", "info")
        else:
            self.chk_shell_menu.blockSignals(True)
            self.chk_shell_menu.setChecked(not checked)
            self.chk_shell_menu.blockSignals(False)
            QMessageBox.warning(self, "Explorer Menu Error", "Could not update Windows Explorer context menu registry.")

    def _toggle_autostart(self, checked: bool):
        success = set_autostart(checked)
        if success:
            state_str = "ENABLED" if checked else "DISABLED"
            self._append_log(f"Windows startup autostart: {state_str}", "info")
        else:
            self.chk_autostart.setChecked(not checked)
            QMessageBox.warning(self, "Startup Error", "Could not update Windows startup registry.")

    def _choose_save_directory(self):
        new_dir = QFileDialog.getExistingDirectory(self, "Select Auto-Save Folder", config.save_directory)
        if new_dir:
            config.set_save_directory(new_dir)
            self.txt_save_dir.setText(new_dir)
            self._append_log(f"Auto-Save directory set to: {new_dir}", "info")

    def _open_save_folder(self):
        save_dir = config.save_directory
        if os.path.exists(save_dir):
            os.startfile(save_dir)
        else:
            QMessageBox.warning(self, "Folder Not Found", f"Directory does not exist: {save_dir}")

    def _refresh_safe_table(self):
        folders = config.get_safe_folders()
        self.safe_table.setRowCount(len(folders))
        for row, folder in enumerate(folders):
            p = folder.get("path", "")
            name_item = QTableWidgetItem(icon("file" if (p and os.path.isfile(p)) else "folder", theme.ACCENT, 16), folder.get("name", "Unnamed"))
            path_item = QTableWidgetItem(p)
            name_item.setFlags(name_item.flags() ^ Qt.ItemIsEditable)
            path_item.setFlags(path_item.flags() ^ Qt.ItemIsEditable)
            self.safe_table.setItem(row, 0, name_item)
            self.safe_table.setItem(row, 1, path_item)

    def _add_safe_folder(self):
        folder_path = QFileDialog.getExistingDirectory(self, "Select Folder to Add to Safe List")
        if folder_path:
            try:
                entry = config.add_safe_folder(folder_path)
                self._refresh_safe_table()
                self._append_log(f"Added '{entry['name']}' to Safe List.", "info")
            except Exception as e:
                QMessageBox.warning(self, "Add Folder Error", str(e))

    def _remove_safe_folder(self):
        current_row = self.safe_table.currentRow()
        if current_row < 0:
            QMessageBox.information(self, "Selection Required", "Please select a folder from the table to remove.")
            return

        folders = config.get_safe_folders()
        if 0 <= current_row < len(folders):
            folder = folders[current_row]
            confirm = QMessageBox.question(
                self,
                "Confirm Removal",
                f"Remove '{folder['name']}' from the Safe List?\n(No files on your computer will be deleted).",
            )
            if confirm == QMessageBox.Yes:
                config.remove_safe_folder(folder["id"])
                self._refresh_safe_table()
                self._append_log(f"Removed '{folder['name']}' from Safe List.", "info")

    def _clean_exit(self):
        if self.server_instance:
            self.server_instance.shutdown()
        if self.server_thread:
            self.server_thread.shutdown()
        QApplication.quit()

    def closeEvent(self, event):
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Close NearBeam")
        msg_box.setText("Do you want to run NearBeam in the background?")
        msg_box.setInformativeText("Minimizing to tray keeps NearBeam active for background file transfers.")
        
        minimize_btn = msg_box.addButton("Minimize to Tray", QMessageBox.AcceptRole)
        exit_btn = msg_box.addButton("Exit App", QMessageBox.DestructiveRole)
        cancel_btn = msg_box.addButton(QMessageBox.Cancel)
        
        msg_box.exec()
        
        if msg_box.clickedButton() == minimize_btn:
            self.hide()
            self.tray_icon.showMessage(
                "NearBeam is still running",
                "NearBeam has been minimized to the system tray and is running in the background.",
                QSystemTrayIcon.Information,
                2000
            )
            event.ignore()
        elif msg_box.clickedButton() == exit_btn:
            self._clean_exit()
            event.accept()
        else:
            event.ignore()


def run_gui(start_minimized: bool = False, send_paths: list[str] = None):
    app = QApplication(sys.argv)
    app.setApplicationName("NearBeam")
    window = MainWindow(send_paths=send_paths)
    if not start_minimized:
        window.show()
        window.raise_()
        window.activateWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    run_gui()
