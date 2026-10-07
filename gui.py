"""
PySide6 Desktop Application for NearBeam.
Refined, perfectly scaled Windows 11 Dark theme control panel.
Features High-DPI support, scrollable responsive cards, no-wrap buttons,
and clean visual alignment.
"""

import os
import sys
import threading
import webbrowser
from pathlib import Path

# Enable High DPI scaling before importing Qt GUI
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
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
from core.server import NearBeamServer, LandropServer
from core.version import APP_VERSION, RELEASES_URL, check_for_updates


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
        self.server = make_server(self.host, self.port, app, threaded=True)

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
    mesh_updated = Signal(str, list)


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
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NearBeam - Local Wi-Fi File Sharing & Explorer")
        self.resize(860, 740)
        self.setMinimumSize(780, 620)

        # Apply clean, properly-scaled dark theme
        qss = """
            QMainWindow, QWidget#contentWidget {
                background-color: #080c14;
            }
            QWidget {
                color: #f8fafc;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            QScrollArea {
                border: none;
                background-color: transparent;
                background: transparent;
            }
            QScrollArea > QWidget > QWidget {
                background-color: transparent;
                background: transparent;
            }
            QFrame.card {
                background-color: #0f172a;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 12px;
            }
            QLabel {
                background-color: transparent;
                background: transparent;
            }
            QLabel.card-title {
                background-color: transparent;
                background: transparent;
                font-size: 13px;
                font-weight: 700;
                color: #38bdf8;
            }
            QLabel.card-subtitle {
                background-color: transparent;
                background: transparent;
                font-size: 11px;
                color: #94a3b8;
                line-height: 1.3;
            }
            QLineEdit {
                background-color: #131d31;
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 7px;
                color: #f8fafc;
                padding: 7px 10px;
                font-size: 12px;
            }
            QLineEdit:focus {
                border: 1px solid #38bdf8;
            }
            QPushButton {
                background-color: #1a263d;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 7px;
                color: #f8fafc;
                padding: 7px 14px;
                font-weight: 600;
                font-size: 12px;
                white-space: nowrap;
            }
            QPushButton:hover {
                background-color: #243452;
                border-color: #38bdf8;
                color: #ffffff;
            }
            QPushButton#primaryBtn {
                background-color: #0284c7;
                color: #ffffff;
                border: none;
            }
            QPushButton#primaryBtn:hover {
                background-color: #0369a1;
            }
            QPushButton#dangerBtn {
                background-color: rgba(239, 68, 68, 0.12);
                border: 1px solid rgba(239, 68, 68, 0.25);
                color: #fca5a5;
            }
            QPushButton#dangerBtn:hover {
                background-color: rgba(239, 68, 68, 0.25);
                border-color: #ef4444;
                color: #ffffff;
            }
            QTableWidget {
                background-color: #131d31;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 7px;
                gridline-color: rgba(255, 255, 255, 0.03);
                selection-background-color: rgba(56, 189, 248, 0.2);
                selection-color: #ffffff;
            }
            QHeaderView::section {
                background-color: #18233a;
                color: #94a3b8;
                border: none;
                border-bottom: 1px solid rgba(255, 255, 255, 0.1);
                padding: 7px 10px;
                font-weight: 700;
                font-size: 11px;
            }
            QCheckBox {
                color: #f8fafc;
                font-size: 12px;
                font-weight: 600;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1.5px solid rgba(255, 255, 255, 0.25);
                background-color: #131d31;
            }
            QCheckBox::indicator:hover {
                border-color: #38bdf8;
            }
            QCheckBox::indicator:checked {
                background-color: #10b981;
                border-color: #10b981;
                image: url("__CHECKMARK_URL__");
            }
            QScrollBar:vertical {
                background: transparent;
                width: 6px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.15);
                min-height: 20px;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255, 255, 255, 0.25);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """
        chk_icon = get_resource_path("assets/checkmark.png").as_posix()
        self.setStyleSheet(qss.replace("__CHECKMARK_URL__", chk_icon))

        # Setup icon
        self.icon_path = get_resource_path("assets/icon.ico")
        if not self.icon_path.exists():
            self.icon_path = get_resource_path("assets/icon.png")
        if self.icon_path.exists():
            self.setWindowIcon(QIcon(str(self.icon_path)))

        # Setup activity bridge
        self.bridge = ActivityBridge()
        self.bridge.new_activity.connect(self._append_log)
        self.bridge.update_checked.connect(self._on_update_checked)
        self.bridge.clipboard_received.connect(self._on_remote_clipboard_received)
        self.bridge.mesh_updated.connect(self._on_mesh_updated)
        self.current_mesh_nodes = []

        # Clipboard auto-sync tracking state
        self._suppress_clipboard_echo = False
        self._last_synced_clipboard = ""
        QApplication.clipboard().dataChanged.connect(self._on_local_clipboard_changed)

        # Server setup
        self.server_thread = None
        self.server_instance = None
        self.port = config.data.get("port", 5000)

        self._build_ui()
        self._setup_tray()
        self.start_server()

    def _build_ui(self):
        # Scroll area container to guarantee responsive scaling on any display DPI
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        self.setCentralWidget(scroll_area)

        content_widget = QWidget()
        content_widget.setObjectName("contentWidget")
        scroll_area.setWidget(content_widget)

        root_layout = QVBoxLayout(content_widget)
        root_layout.setContentsMargins(24, 20, 24, 20)
        root_layout.setSpacing(14)

        # ==========================================
        # 0. Modern Top Header Bar (No in-window menu)
        # ==========================================
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 4)
        header_layout.setSpacing(10)
        header_layout.setAlignment(Qt.AlignVCenter)

        if self.icon_path.exists():
            icon_lbl = QLabel()
            icon_pix = QPixmap(str(self.icon_path)).scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon_lbl.setPixmap(icon_pix)
            header_layout.addWidget(icon_lbl)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)

        app_title = QLabel("NearBeam")
        app_title.setStyleSheet("font-size: 20px; font-weight: 800; color: #ffffff; letter-spacing: -0.5px;")
        title_col.addWidget(app_title)

        app_desc = QLabel("High-Speed Local Wi-Fi File Sharing & Safe List Explorer")
        app_desc.setStyleSheet("font-size: 11px; color: #94a3b8; font-weight: 500;")
        title_col.addWidget(app_desc)
        header_layout.addLayout(title_col)

        header_layout.addStretch()

        self.btn_update = QPushButton(f"✓ v{APP_VERSION} Latest")
        self.btn_update.setFixedHeight(30)
        self.btn_update.setStyleSheet("""
            background-color: rgba(56, 189, 248, 0.12);
            border: 1px solid rgba(56, 189, 248, 0.3);
            color: #38bdf8;
            font-weight: 700;
            border-radius: 8px;
            padding: 0px 12px;
            font-size: 11px;
        """)
        self.btn_update.setCursor(Qt.PointingHandCursor)
        self.btn_update.setToolTip("Click to check for latest updates")
        self.btn_update.clicked.connect(lambda: self._check_updates_async(manual=True))
        header_layout.addWidget(self.btn_update)

        self.status_badge = QLabel("● Online on Wi-Fi")
        self.status_badge.setFixedHeight(30)
        self.status_badge.setAlignment(Qt.AlignCenter)
        self.status_badge.setStyleSheet("""
            background-color: rgba(16, 185, 129, 0.12);
            border: 1px solid rgba(16, 185, 129, 0.35);
            color: #34d399;
            padding: 0px 12px;
            border-radius: 8px;
            font-size: 11px;
            font-weight: 700;
        """)
        header_layout.addWidget(self.status_badge)

        btn_header_web = QPushButton("Open Web App")
        btn_header_web.setObjectName("primaryBtn")
        btn_header_web.setFixedHeight(30)
        btn_header_web.setStyleSheet("""
            background-color: #0284c7;
            color: #ffffff;
            border: none;
            border-radius: 8px;
            padding: 0px 14px;
            font-weight: 600;
            font-size: 12px;
        """)
        btn_header_web.setMinimumWidth(115)
        btn_header_web.clicked.connect(self._open_browser)
        header_layout.addWidget(btn_header_web)

        root_layout.addLayout(header_layout)

        # ==========================================
        # 1. Connection Card (Hero)
        # ==========================================
        conn_card = QFrame()
        conn_card.setProperty("class", "card")
        conn_layout = QVBoxLayout(conn_card)
        conn_layout.setContentsMargins(18, 16, 18, 16)
        conn_layout.setSpacing(10)

        card_title_row = QHBoxLayout()
        card_title = QLabel("📡 Connection Address")
        card_title.setProperty("class", "card-title")
        card_title_row.addWidget(card_title)

        perm_badge = QLabel("Permanent mDNS + Direct IP")
        perm_badge.setStyleSheet("background-color: rgba(56, 189, 248, 0.15); color: #38bdf8; font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 8px;")
        card_title_row.addWidget(perm_badge)
        card_title_row.addStretch()
        conn_layout.addLayout(card_title_row)

        card_subtitle = QLabel("Open the link or scan the QR code on your phone/tablet to transfer files immediately:")
        card_subtitle.setProperty("class", "card-subtitle")
        conn_layout.addWidget(card_subtitle)

        # 3-Mode Selector Row: Permanent vs Hostname vs Direct IP
        mode_btn_row = QHBoxLayout()
        mode_btn_row.setSpacing(8)

        self.btn_addr_perm = QPushButton("⭐ Permanent (nearbeam.local)")
        self.btn_addr_perm.setFixedHeight(28)
        self.btn_addr_perm.setCursor(Qt.PointingHandCursor)
        self.btn_addr_perm.clicked.connect(lambda: self._set_addr_mode("perm"))
        mode_btn_row.addWidget(self.btn_addr_perm)

        self.btn_addr_host = QPushButton("🏷️ Hostname (.local)")
        self.btn_addr_host.setFixedHeight(28)
        self.btn_addr_host.setCursor(Qt.PointingHandCursor)
        self.btn_addr_host.clicked.connect(lambda: self._set_addr_mode("host"))
        mode_btn_row.addWidget(self.btn_addr_host)

        self.btn_addr_ip = QPushButton("📱 Direct IP")
        self.btn_addr_ip.setFixedHeight(28)
        self.btn_addr_ip.setCursor(Qt.PointingHandCursor)
        self.btn_addr_ip.clicked.connect(lambda: self._set_addr_mode("ip"))
        mode_btn_row.addWidget(self.btn_addr_ip)

        mode_btn_row.addStretch()
        conn_layout.addLayout(mode_btn_row)

        # Input + Buttons in a clean, non-wrapping row
        addr_row = QHBoxLayout()
        addr_row.setSpacing(8)

        self.url_label = QLineEdit("http://nearbeam.local:5000")
        self.url_label.setReadOnly(True)
        self.url_label.setStyleSheet("""
            background-color: #131d31;
            border: 1px solid rgba(56, 189, 248, 0.35);
            border-radius: 7px;
            color: #38bdf8;
            font-size: 14px;
            font-weight: bold;
            font-family: 'Consolas', 'Courier New', monospace;
            padding: 7px 10px;
        """)
        addr_row.addWidget(self.url_label, 1)

        self.btn_copy_url = QPushButton("Copy Link")
        self.btn_copy_url.setMinimumWidth(85)
        self.btn_copy_url.clicked.connect(self._copy_url)
        addr_row.addWidget(self.btn_copy_url)

        self.btn_open_browser = QPushButton("Open in Browser")
        self.btn_open_browser.setObjectName("primaryBtn")
        self.btn_open_browser.setMinimumWidth(125)
        self.btn_open_browser.clicked.connect(self._open_browser)
        addr_row.addWidget(self.btn_open_browser)

        self.btn_show_qr = QPushButton("Show QR")
        self.btn_show_qr.setMinimumWidth(75)
        self.btn_show_qr.clicked.connect(self._show_qr)
        addr_row.addWidget(self.btn_show_qr)

        conn_layout.addLayout(addr_row)

        self.ip_subtext = QLabel()
        self.ip_subtext.setStyleSheet("color: #64748b; font-size: 11px; margin-top: 2px;")
        conn_layout.addWidget(self.ip_subtext)

        root_layout.addWidget(conn_card)

        # ==========================================
        # 1.5 LAN Mesh & Connected Hosts Card
        # ==========================================
        mesh_card = QFrame()
        mesh_card.setProperty("class", "card")
        mesh_layout = QVBoxLayout(mesh_card)
        mesh_layout.setContentsMargins(18, 14, 18, 14)
        mesh_layout.setSpacing(8)

        mesh_header_row = QHBoxLayout()
        mesh_title = QLabel("🌐 LAN Mesh & Connected Hosts")
        mesh_title.setProperty("class", "card-title")
        mesh_header_row.addWidget(mesh_title)

        self.mesh_role_badge = QLabel("🟢 Primary Leader")
        self.mesh_role_badge.setStyleSheet("""
            background-color: rgba(16, 185, 129, 0.12);
            border: 1px solid rgba(16, 185, 129, 0.35);
            color: #34d399;
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 700;
        """)
        mesh_header_row.addWidget(self.mesh_role_badge)
        mesh_header_row.addStretch()

        self.btn_send_to_node = QPushButton("📤 Send to Peer Host...")
        self.btn_send_to_node.setStyleSheet("""
            background-color: rgba(56, 189, 248, 0.12);
            border: 1px solid rgba(56, 189, 248, 0.35);
            color: #38bdf8;
            font-weight: 700;
            padding: 5px 12px;
        """)
        self.btn_send_to_node.clicked.connect(self._send_file_to_node_dialog)
        mesh_header_row.addWidget(self.btn_send_to_node)
        mesh_layout.addLayout(mesh_header_row)

        self.mesh_nodes_summary = QLabel("Searching for other NearBeam computers on Wi-Fi...")
        self.mesh_nodes_summary.setStyleSheet("color: #94a3b8; font-size: 11px;")
        mesh_layout.addWidget(self.mesh_nodes_summary)

        root_layout.addWidget(mesh_card)

        # ==========================================
        # 2. Auto-Save Settings Card
        # ==========================================
        autosave_card = QFrame()
        autosave_card.setProperty("class", "card")
        autosave_layout = QVBoxLayout(autosave_card)
        autosave_layout.setContentsMargins(18, 16, 18, 16)
        autosave_layout.setSpacing(10)

        autosave_title = QLabel("📥 Auto-Save & App Settings")
        autosave_title.setProperty("class", "card-title")
        autosave_layout.addWidget(autosave_title)

        options_row = QHBoxLayout()
        options_row.setSpacing(24)

        self.chk_autosave = QCheckBox("Auto-Save incoming files without prompts")
        self.chk_autosave.setChecked(config.auto_save_enabled)
        self.chk_autosave.toggled.connect(self._toggle_autosave)
        options_row.addWidget(self.chk_autosave)

        self.chk_clipboard_sync = QCheckBox("Keep syncing clipboard automatically")
        self.chk_clipboard_sync.setChecked(config.clipboard_sync_enabled)
        self.chk_clipboard_sync.toggled.connect(self._toggle_clipboard_sync)
        options_row.addWidget(self.chk_clipboard_sync)

        self.chk_autostart = QCheckBox("Start with Windows")
        self.chk_autostart.setChecked(is_autostart_enabled())
        self.chk_autostart.toggled.connect(self._toggle_autostart)
        options_row.addWidget(self.chk_autostart)

        options_row.addStretch()
        autosave_layout.addLayout(options_row)

        dir_row = QHBoxLayout()
        dir_row.setSpacing(8)

        dir_lbl = QLabel("Save Folder:")
        dir_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: 600;")
        dir_row.addWidget(dir_lbl)

        self.txt_save_dir = QLineEdit(config.save_directory)
        self.txt_save_dir.setReadOnly(True)
        dir_row.addWidget(self.txt_save_dir, 1)

        btn_change_dir = QPushButton("Change Folder")
        btn_change_dir.setMinimumWidth(110)
        btn_change_dir.clicked.connect(self._choose_save_directory)
        dir_row.addWidget(btn_change_dir)

        btn_open_dir = QPushButton("Open in Explorer")
        btn_open_dir.setMinimumWidth(115)
        btn_open_dir.clicked.connect(self._open_save_folder)
        dir_row.addWidget(btn_open_dir)

        autosave_layout.addLayout(dir_row)

        fw_row = QHBoxLayout()
        fw_row.setSpacing(10)

        fw_lbl = QLabel("🛡️ Windows Firewall:")
        fw_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: 600;")
        fw_row.addWidget(fw_lbl)

        fw_desc = QLabel("Fix repeated 'Allow Python to connect' network pop-ups on this PC")
        fw_desc.setStyleSheet("color: #64748b; font-size: 11px;")
        fw_row.addWidget(fw_desc, 1)

        btn_firewall = QPushButton("Allow in Windows Firewall (1-Click)")
        btn_firewall.setMinimumWidth(210)
        btn_firewall.clicked.connect(self._configure_firewall)
        fw_row.addWidget(btn_firewall)

        autosave_layout.addLayout(fw_row)
        root_layout.addWidget(autosave_card)

        # ==========================================
        # 3. Safe List (Shared PC Folders) Card
        # ==========================================
        safelist_card = QFrame()
        safelist_card.setProperty("class", "card")
        safelist_layout = QVBoxLayout(safelist_card)
        safelist_layout.setContentsMargins(18, 16, 18, 16)
        safelist_layout.setSpacing(8)

        safelist_title = QLabel("🔒 Safe List Folders (Shared with Wi-Fi)")
        safelist_title.setProperty("class", "card-title")
        safelist_layout.addWidget(safelist_title)

        safelist_sub = QLabel("Only folders explicitly added here can be browsed by devices on your Wi-Fi network:")
        safelist_sub.setProperty("class", "card-subtitle")
        safelist_layout.addWidget(safelist_sub)

        self.safe_table = QTableWidget(0, 2)
        self.safe_table.setHorizontalHeaderLabels(["Shared Folder Name", "Path on Computer"])
        self.safe_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.safe_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.safe_table.horizontalHeader().resizeSection(0, 220)
        self.safe_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.safe_table.setSelectionMode(QTableWidget.SingleSelection)
        self.safe_table.setMinimumHeight(130)
        safelist_layout.addWidget(self.safe_table)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        btn_add_safe = QPushButton("+ Add Folder to Safe List")
        btn_add_safe.setObjectName("primaryBtn")
        btn_add_safe.setMinimumWidth(165)
        btn_add_safe.clicked.connect(self._add_safe_folder)
        btn_row.addWidget(btn_add_safe)

        btn_remove_safe = QPushButton("Remove Selected Folder")
        btn_remove_safe.setObjectName("dangerBtn")
        btn_remove_safe.setMinimumWidth(160)
        btn_remove_safe.clicked.connect(self._remove_safe_folder)
        btn_row.addWidget(btn_remove_safe)

        btn_row.addStretch()
        safelist_layout.addLayout(btn_row)

        root_layout.addWidget(safelist_card)

        # ==========================================
        # 4. Live Activity Console Card
        # ==========================================
        log_card = QFrame()
        log_card.setProperty("class", "card")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(18, 14, 18, 14)
        log_layout.setSpacing(8)

        log_top = QHBoxLayout()
        log_title = QLabel("⚡ Live Activity Log")
        log_title.setProperty("class", "card-title")
        log_top.addWidget(log_title)
        log_top.addStretch()

        btn_clear_log = QPushButton("Clear")
        btn_clear_log.setStyleSheet("padding: 3px 10px; font-size: 11px;")
        btn_clear_log.clicked.connect(lambda: self.log_view.clear())
        log_top.addWidget(btn_clear_log)
        log_layout.addLayout(log_top)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(100)
        self.log_view.setStyleSheet("""
            background-color: #0b1120;
            border: 1px solid rgba(255, 255, 255, 0.07);
            border-radius: 7px;
            font-family: 'Consolas', 'Courier New', monospace;
            font-size: 11px;
            padding: 8px 10px;
        """)
        log_layout.addWidget(self.log_view)

        root_layout.addWidget(log_card)

        self._refresh_safe_table()

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

            # Register mesh cluster callback
            self.server_instance.add_mesh_callback(
                lambda role, nodes: self.bridge.mesh_updated.emit(role, nodes)
            )

            self.server_thread = ServerThread(self.server_instance.app, "0.0.0.0", actual_port)
            self.server_thread.start()

            urls = get_connection_urls(actual_port)
            self.active_urls = urls
            self.active_nearbeam_url = urls["nearbeam_url"]
            self.active_primary_url = urls["primary_url"]
            self.active_mdns_url = urls["mdns_url"]

            self.btn_addr_host.setText(f"🏷️ {urls['hostname']}.local")
            self.btn_addr_ip.setText(f"📱 Direct IP ({urls['primary_ip']})")
            self._set_addr_mode("perm")
            self._append_log(f"Server started on port {actual_port}. Permanent address: {urls['nearbeam_url']}.", "info")

            # Initialize mesh status display
            if self.server_instance.mesh:
                self._on_mesh_updated(
                    self.server_instance.mesh.role,
                    self.server_instance.mesh.get_nodes_list(),
                )

            # Check for updates in background
            self._check_updates_async(manual=False)
        except Exception as e:
            QMessageBox.critical(self, "Server Error", f"Failed to start server: {e}")

    def _set_addr_mode(self, mode: str):
        """Switches displayed URL between Permanent (nearbeam.local), Hostname, and Direct IP."""
        self.current_addr_mode = mode
        active_btn = "background-color: #0284c7; color: #ffffff; border: 1px solid #38bdf8; border-radius: 6px; font-weight: 600; font-size: 11px; padding: 2px 10px;"
        inactive_btn = "background-color: #131d31; color: #94a3b8; border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; font-weight: 500; font-size: 11px; padding: 2px 10px;"

        self.btn_addr_perm.setStyleSheet(active_btn if mode == "perm" else inactive_btn)
        self.btn_addr_host.setStyleSheet(active_btn if mode == "host" else inactive_btn)
        self.btn_addr_ip.setStyleSheet(active_btn if mode == "ip" else inactive_btn)

        if mode == "perm":
            url = getattr(self, "active_nearbeam_url", "http://nearbeam.local:5000")
            desc = "⭐ Permanent address: Never changes even if router reassigns your IP. Works on iPhone, iPad, Mac & Windows."
        elif mode == "host":
            url = getattr(self, "active_mdns_url", "http://nearbeam.local:5000")
            desc = "🏷️ Hostname address: Permanent local address mapped to your computer name via native Windows mDNS."
        else:
            url = getattr(self, "active_primary_url", "http://127.0.0.1:5000")
            desc = "📱 Direct numeric IP: Use if an older device does not support .local mDNS names."

        self.url_label.setText(url)
        self.ip_subtext.setText(desc)

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
            self.btn_update.setText(f"🚀 Update v{latest} Available")
            self.btn_update.setFixedHeight(30)
            self.btn_update.setStyleSheet("""
                background-color: #f59e0b;
                color: #0b0f19;
                font-weight: 800;
                border-radius: 8px;
                padding: 0px 12px;
                font-size: 11px;
                border: 1px solid #fbbf24;
            """)
            self.btn_update.setToolTip(f"New update v{latest} available! Click to download on GitHub.")
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
            self.btn_update.setText(f"✓ v{APP_VERSION} Latest")
            self.btn_update.setFixedHeight(30)
            self.btn_update.setStyleSheet("""
                background-color: rgba(56, 189, 248, 0.12);
                border: 1px solid rgba(56, 189, 248, 0.3);
                color: #38bdf8;
                font-weight: 700;
                border-radius: 8px;
                padding: 0px 12px;
                font-size: 11px;
            """)
            self.btn_update.setToolTip(f"You are running the latest version (v{APP_VERSION}). Click to re-check.")
            if getattr(self, "manual_update_check", False):
                QMessageBox.information(
                    self,
                    "NearBeam Up to Date",
                    f"You are currently using the latest release: v{APP_VERSION}.",
                )

    def _append_log(self, msg: str, event_type: str = "info"):
        """Thread-safe UI append to Live Activity Log with color formatting."""
        if hasattr(self, "log_view") and self.log_view:
            if event_type == "upload":
                color = "#34d399"
            elif event_type == "error":
                color = "#f87171"
            elif event_type == "warning":
                color = "#fbbf24"
            else:
                color = "#94a3b8"
            self.log_view.append(f'<span style="color: {color};">{msg}</span>')
            scrollbar = self.log_view.verticalScrollBar()
            if scrollbar:
                scrollbar.setValue(scrollbar.maximum())

    def _copy_url(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.url_label.text())
        self._append_log(f"Copied URL {self.url_label.text()} to clipboard.", "info")

    def _open_browser(self):
        webbrowser.open(self.url_label.text())

    def _show_qr(self):
        urls = getattr(self, "active_urls", self.url_label.text())
        dlg = QRDialog(urls, initial_url=self.url_label.text(), parent=self)
        dlg.exec()

    def _on_mesh_updated(self, role: str, nodes: list):
        """Updates GUI when mesh role changes or nodes join/leave."""
        self.current_mesh_nodes = nodes or []
        is_leader = (role == "leader")

        if is_leader:
            self.mesh_role_badge.setText("🟢 Primary Leader (nearbeam.local)")
            self.mesh_role_badge.setStyleSheet("""
                background-color: rgba(16, 185, 129, 0.12);
                border: 1px solid rgba(16, 185, 129, 0.35);
                color: #34d399;
                padding: 3px 10px;
                border-radius: 12px;
                font-size: 11px;
                font-weight: 700;
            """)
        else:
            leader_node = next((n for n in self.current_mesh_nodes if n.get("is_leader")), None)
            leader_ip = leader_node.get("ip") if leader_node else "Connected"
            self.mesh_role_badge.setText(f"🔵 Secondary Node (Leader: {leader_ip})")
            self.mesh_role_badge.setStyleSheet("""
                background-color: rgba(56, 189, 248, 0.12);
                border: 1px solid rgba(56, 189, 248, 0.35);
                color: #38bdf8;
                padding: 3px 10px;
                border-radius: 12px;
                font-size: 11px;
                font-weight: 700;
            """)

        peers = [n for n in self.current_mesh_nodes if not n.get("is_self")]
        if peers:
            peer_descs = [f"💻 {p.get('name', 'Peer')} ({p.get('ip')})" for p in peers]
            self.mesh_nodes_summary.setText(f"Active Cluster ({len(self.current_mesh_nodes)} Hosts Online):  " + "  •  ".join(peer_descs))
            self.btn_send_to_node.setEnabled(True)
            self.btn_send_to_node.setText(f"📤 Send to Peer ({len(peers)} Online)...")
        else:
            self.mesh_nodes_summary.setText("No other NearBeam computers detected on Wi-Fi yet. (Waiting for peer beacons)")
            self.btn_send_to_node.setEnabled(False)
            self.btn_send_to_node.setText("📤 Send to Peer Host (None Online)")

    def _send_file_to_node_dialog(self):
        """Allows 1-click native file sending from desktop to another running NearBeam computer."""
        peers = [n for n in getattr(self, "current_mesh_nodes", []) if not n.get("is_self")]
        if not peers:
            QMessageBox.information(
                self,
                "No Peer Hosts Online",
                "No secondary NearBeam laptops or PCs are currently detected on your Wi-Fi.\n\n"
                "Launch NearBeam on another computer and it will appear here automatically!",
            )
            return

        target_node = peers[0]
        if len(peers) > 1:
            menu = QMenu(self)
            selected_box = [None]
            for p in peers:
                action = menu.addAction(f"💻 {p['name']} ({p['ip']})")
                action.triggered.connect(lambda checked=False, node=p: selected_box.__setitem__(0, node))
            menu.exec(self.btn_send_to_node.mapToGlobal(self.btn_send_to_node.rect().bottomLeft()))
            if not selected_box[0]:
                return
            target_node = selected_box[0]

        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            f"Select File(s) to Send to {target_node['name']}",
            "",
            "All Files (*.*)",
        )
        if not file_paths:
            return

        target_url = f"{target_node['url']}/api/upload"
        t = threading.Thread(
            target=self._send_files_async,
            args=(target_url, target_node['name'], file_paths),
            daemon=True,
        )
        t.start()

    def _send_files_async(self, upload_url: str, target_name: str, file_paths: list[str]):
        """Pushes files directly to peer computer using pure Python standard library multipart HTTP."""
        import urllib.request
        import uuid

        boundary = f"----NearBeamBoundary{uuid.uuid4().hex}"
        self._append_log(f"Sending {len(file_paths)} file(s) directly to {target_name} ({upload_url})...", "info")

        try:
            body = bytearray()
            for fp in file_paths:
                fname = os.path.basename(fp)
                body.extend(f"--{boundary}\r\n".encode("utf-8"))
                body.extend(f'Content-Disposition: form-data; name="files"; filename="{fname}"\r\n'.encode("utf-8"))
                body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
                with open(fp, "rb") as f:
                    body.extend(f.read())
                body.extend(b"\r\n")
            body.extend(f"--{boundary}--\r\n".encode("utf-8"))

            req = urllib.request.Request(
                upload_url,
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                if resp.status == 200:
                    self._append_log(f"✓ Successfully sent {len(file_paths)} file(s) to {target_name}!", "upload")
                else:
                    self._append_log(f"Send failed with HTTP status {resp.status}", "error")
        except Exception as e:
            self._append_log(f"Failed sending files to {target_name}: {e}", "error")

    def _toggle_autosave(self, checked: bool):
        config.set_auto_save(checked)
        state_str = "ENABLED" if checked else "DISABLED"
        self._append_log(f"Auto-Save setting updated: {state_str}", "info")

    def _toggle_clipboard_sync(self, checked: bool):
        config.set_clipboard_sync(checked)
        state_str = "ENABLED" if checked else "DISABLED"
        self._append_log(f"Clipboard Auto-Sync: {state_str}", "info")
        if checked:
            self._on_local_clipboard_changed()

    def _on_local_clipboard_changed(self):
        """Called whenever text is copied anywhere on Windows (Ctrl+C)."""
        if not getattr(self, "chk_clipboard_sync", None) or not self.chk_clipboard_sync.isChecked():
            return
        if getattr(self, "_suppress_clipboard_echo", False):
            return

        try:
            clipboard = QApplication.clipboard()
            text = clipboard.text()
            if text and text != getattr(self, "_last_synced_clipboard", ""):
                self._last_synced_clipboard = text
                if hasattr(self, "server_instance") and self.server_instance:
                    import datetime
                    self.server_instance.clipboard_content = text
                    self.server_instance.clipboard_updated_at = datetime.datetime.now().strftime("%H:%M:%S")
                    self.server_instance.broadcast_event({
                        "type": "clipboard_updated",
                        "text": text,
                        "updated_at": self.server_instance.clipboard_updated_at,
                    })
                    snippet = (text[:30] + "...") if len(text) > 30 else text
                    self._append_log(f"Auto-synced Windows clipboard to network: '{snippet}'", "info")
        except Exception as e:
            print(f"Error in _on_local_clipboard_changed: {e}")

    def _on_remote_clipboard_received(self, text: str):
        """Called when a mobile device or web client updates the shared clipboard."""
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
        except Exception as e:
            self._suppress_clipboard_echo = False
            print(f"Error in _on_remote_clipboard_received: {e}")

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
            name_item = QTableWidgetItem(f"📁  {folder.get('name', 'Unnamed')}")
            path_item = QTableWidgetItem(folder.get("path", ""))
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
        # Clean shutdown when user closes the window
        if self.server_instance:
            self.server_instance.shutdown()
        if self.server_thread:
            self.server_thread.shutdown()
        event.accept()


def run_gui(start_minimized: bool = False):
    app = QApplication(sys.argv)
    app.setApplicationName("NearBeam")
    window = MainWindow()
    if not start_minimized:
        window.show()
        window.raise_()
        window.activateWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    run_gui()
