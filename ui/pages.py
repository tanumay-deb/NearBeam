"""Control Panel pages. Pages only lay out widgets; MainWindow owns the behaviour."""

import html

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui import theme
from ui.widgets import (
    Card,
    DeviceCard,
    DropZone,
    ToggleRow,
    button,
    clear_layout,
    icon,
    icon_button,
    icon_label,
    label,
)


class Page(QScrollArea):
    """Scrollable page with a title; content goes into self.body."""

    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        inner.setObjectName("page")
        self.setWidget(inner)
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(32, 28, 32, 28)
        self.body.setSpacing(16)
        self.header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        self.title = label(title, "h1")
        titles.addWidget(self.title)
        self.subtitle = label(subtitle, "muted", wrap=True)
        self.subtitle.setVisible(bool(subtitle))
        titles.addWidget(self.subtitle)
        self.header.addLayout(titles, 1)
        self.body.addLayout(self.header)


def section_header(title: str, action=None) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(label(title, "h2"))
    row.addStretch()
    if action is not None:
        row.addWidget(action)
    return row


def empty_state(text: str) -> QLabel:
    lbl = label(text, "dim", wrap=True)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setMinimumHeight(56)
    return lbl


class HomePage(Page):
    def __init__(self):
        super().__init__("This PC")

        self.body.addLayout(section_header("Nearby devices"))
        self.devices_grid = QGridLayout()
        self.devices_grid.setSpacing(10)
        self.body.addLayout(self.devices_grid)

        self.drop_zone = DropZone()
        drop = QVBoxLayout(self.drop_zone)
        drop.setContentsMargins(24, 28, 24, 28)
        drop.setSpacing(8)
        drop.addWidget(icon_label("upload", theme.ACCENT, 36), 0, Qt.AlignHCenter)
        drop.addWidget(label("Drop files here"), 0, Qt.AlignHCenter)
        hint = label("Drop files to send to a nearby PC or share as a 1-tap bundle for your phone.", "dim", wrap=True)
        hint.setAlignment(Qt.AlignCenter)
        drop.addWidget(hint)
        actions = QHBoxLayout()
        actions.addStretch()
        self.btn_send = button("Send files", "send", "primaryBtn")
        actions.addWidget(self.btn_send)
        self.btn_received = button("Open received folder", "folder-open", "linkBtn", icon_color=theme.ACCENT)
        actions.addWidget(self.btn_received)
        actions.addStretch()
        drop.addSpacing(6)
        drop.addLayout(actions)
        self.body.addWidget(self.drop_zone)

        self.batches_section = QWidget()
        batches_outer = QVBoxLayout(self.batches_section)
        batches_outer.setContentsMargins(0, 0, 0, 0)
        batches_outer.setSpacing(8)
        self.btn_clear_batches = button("Clear all", kind="linkBtn")
        batches_outer.addLayout(section_header("Ready for your devices", self.btn_clear_batches))
        self.batches_box = QVBoxLayout()
        self.batches_box.setSpacing(8)
        batches_outer.addLayout(self.batches_box)
        self.batches_section.hide()
        self.body.addWidget(self.batches_section)

        self.btn_view_all = button("View all", kind="linkBtn")
        self.body.addLayout(section_header("Recent transfers", self.btn_view_all))
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(8)
        self.body.addLayout(self.recent_box)
        self.body.addStretch()

    def set_status(self, name: str, detail: str):
        self.title.setText(name)
        self.subtitle.setText(detail)
        self.subtitle.setVisible(True)

    def set_devices(self, devices: list[dict], on_drop, on_send):
        clear_layout(self.devices_grid)
        cols = 3
        for i, dev in enumerate(devices):
            card = DeviceCard(dev)
            card.files_dropped.connect(lambda paths, d=dev: on_drop(d, paths))
            card.send_clicked.connect(lambda d=dev: on_send(d))
            self.devices_grid.addWidget(card, i // cols, i % cols)
        for c in range(cols):
            self.devices_grid.setColumnStretch(c, 1)

    def set_batches(self, batches: list[dict], on_clear):
        clear_layout(self.batches_box)
        if not batches:
            self.batches_section.hide()
            return
        self.batches_section.show()
        for batch in batches:
            card = Card(padding=14, spacing=4)
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(icon_label("send", theme.ACCENT, 18), 0, Qt.AlignVCenter)
            info = QVBoxLayout()
            info.setSpacing(2)
            batch_label = batch.get("label", "Files")
            title_lbl = label(batch_label)
            title_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            title_lbl.setToolTip(f"<span>{html.escape(batch_label)}</span>")
            info.addWidget(title_lbl)
            fcount = batch.get("file_count", 0)
            fword = "1 file" if fcount == 1 else f"{fcount} files"
            dl = int(batch.get("download_count") or 0)
            dl_part = f" · Downloaded {dl}×" if dl > 0 else ""
            meta_txt = f"{fword} · {batch.get('formatted_size', '0 B')} · Sent {batch.get('created_at', '')}{dl_part}"
            meta_lbl = label(meta_txt, "dim")
            meta_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            meta_lbl.setToolTip(f"<span>{html.escape(meta_txt)}</span>")
            info.addWidget(meta_lbl)
            row.addLayout(info, 1)
            clear_btn = icon_button("x", f"<span>{html.escape('Remove ' + batch_label)}</span>")
            clear_btn.setFixedSize(28, 28)
            bid = batch.get("id", "")
            clear_btn.clicked.connect(lambda _=False, batch_id=bid: on_clear(batch_id))
            row.addWidget(clear_btn, 0, Qt.AlignVCenter)
            card.body.addLayout(row)
            self.batches_box.addWidget(card)


class TransfersPage(Page):
    def __init__(self):
        super().__init__("Transfers")

        self.body.addLayout(section_header("Active"))
        self.active_box = QVBoxLayout()
        self.active_box.setSpacing(8)
        self.body.addLayout(self.active_box)
        self.active_empty = empty_state("Nothing in progress. Drop files on the Home page to send them.")
        self.body.addWidget(self.active_empty)

        self.btn_clear_history = button("Clear history", "trash")
        self.body.addLayout(section_header("History", self.btn_clear_history))
        self.history_box = QVBoxLayout()
        self.history_box.setSpacing(8)
        self.body.addLayout(self.history_box)
        self.history_empty = empty_state("Files you send and receive will show up here.")
        self.body.addWidget(self.history_empty)

        self.btn_log = button("Activity log", "chevron-right", "linkBtn", icon_color=theme.ACCENT)
        self.btn_log.setCheckable(True)
        self.btn_log.toggled.connect(self._toggle_log)
        log_row = QHBoxLayout()
        log_row.addWidget(self.btn_log)
        log_row.addStretch()
        self.btn_clear_log = button("Clear", kind="linkBtn")
        self.btn_clear_log.hide()
        log_row.addWidget(self.btn_clear_log)
        self.body.addSpacing(8)
        self.body.addLayout(log_row)
        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(180)
        self.log_view.hide()
        self.btn_clear_log.clicked.connect(self.log_view.clear)
        self.body.addWidget(self.log_view)
        self.body.addStretch()

    def _toggle_log(self, shown: bool):
        self.log_view.setVisible(shown)
        self.btn_clear_log.setVisible(shown)
        self.btn_log.setIcon(icon("chevron-down" if shown else "chevron-right", theme.ACCENT, 16))

    def refresh_empty_states(self):
        self.active_empty.setVisible(self.active_box.count() == 0)
        self.history_empty.setVisible(self.history_box.count() == 0)


class SharedFoldersPage(Page):
    def __init__(self):
        super().__init__(
            "Shared folders",
            "Devices on your Wi-Fi can browse and download only what's listed here. Drag & drop multiple folders or files here to add them at once.",
        )
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Name", "Location on this PC"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().resizeSection(0, 220)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setIconSize(QSize(16, 16))
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setMinimumHeight(260)
        self.body.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_add = button("Add folder", "plus", "primaryBtn")
        row.addWidget(self.btn_add)
        self.btn_add_files = button("Add files...", "file")
        row.addWidget(self.btn_add_files)
        self.btn_select_all = button("Select all", "check")
        row.addWidget(self.btn_select_all)
        self.btn_remove = button("Remove selected", "trash", "dangerBtn")
        self.btn_remove.setEnabled(False)
        row.addWidget(self.btn_remove)
        row.addStretch()
        self.btn_clear_all = button("Clear all", kind="linkBtn")
        self.btn_clear_all.setEnabled(False)
        row.addWidget(self.btn_clear_all)
        self.body.addLayout(row)


class ClipboardPage(Page):
    copy_clicked = Signal()

    def __init__(self, sync_enabled: bool):
        super().__init__("Clipboard", "Copy on one device, paste on another.")
        self.sync_row = ToggleRow("Live clipboard sync", "Share text and screenshots you copy with your other devices.", sync_enabled)
        self.body.addWidget(self.sync_row)

        card = Card()
        head = QHBoxLayout()
        head.addWidget(label("Last synced", "h2"))
        head.addStretch()
        self.meta = label("", "dim")
        head.addWidget(self.meta)
        card.body.addLayout(head)
        self.empty = empty_state("Nothing synced yet. Copy some text or take a screenshot (Win+Shift+S).")
        card.body.addWidget(self.empty)
        self.text_view = QPlainTextEdit()
        self.text_view.setReadOnly(True)
        self.text_view.setMinimumHeight(140)
        self.text_view.hide()
        card.body.addWidget(self.text_view)
        self.image_view = QLabel()
        self.image_view.setAlignment(Qt.AlignCenter)
        self.image_view.setMinimumHeight(140)
        self.image_view.hide()
        card.body.addWidget(self.image_view)
        actions = QHBoxLayout()
        actions.addStretch()
        self.btn_copy = button("Copy", "copy")
        self.btn_copy.clicked.connect(self.copy_clicked.emit)
        self.btn_copy.hide()
        actions.addWidget(self.btn_copy)
        card.body.addLayout(actions)
        self.body.addWidget(card)
        self.body.addStretch()

    def show_text(self, text: str, meta: str):
        self.empty.hide()
        self.image_view.hide()
        self.text_view.setPlainText(text)
        self.text_view.show()
        self.btn_copy.setText("Copy text")
        self.btn_copy.show()
        self.meta.setText(meta)

    def show_image(self, pm: QPixmap, meta: str):
        self.empty.hide()
        self.text_view.hide()
        self.image_view.setPixmap(pm.scaled(560, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.image_view.show()
        self.btn_copy.setText("Copy image")
        self.btn_copy.show()
        self.meta.setText(meta)


class SettingsPage(Page):
    def __init__(
        self,
        save_dir: str,
        autosave: bool,
        clipboard: bool,
        shell_menu: bool,
        autostart: bool,
        organize_mode: str = "off",
    ):
        super().__init__("Settings")

        receiving = Card(spacing=8)
        receiving.body.addWidget(label("Receiving", "h2"))
        receiving.body.addWidget(label("Save incoming files to", "dim"))
        dir_row = QHBoxLayout()
        self.txt_save_dir = QLineEdit(save_dir)
        self.txt_save_dir.setReadOnly(True)
        dir_row.addWidget(self.txt_save_dir, 1)
        self.btn_change_dir = button("Change")
        dir_row.addWidget(self.btn_change_dir)
        self.btn_open_dir = button("Open", "folder-open")
        dir_row.addWidget(self.btn_open_dir)
        receiving.body.addLayout(dir_row)
        self.autosave = ToggleRow("Auto-save incoming files", "Save files from your devices straight into this folder.", autosave)
        receiving.body.addWidget(self.autosave)

        org_row = QHBoxLayout()
        org_text = QVBoxLayout()
        org_text.setSpacing(2)
        org_text.addWidget(label("Auto-organize received files"))
        org_text.addWidget(label("Sort loose incoming files into subfolders automatically. Folder uploads stay intact.", "dim", wrap=True))
        org_row.addLayout(org_text, 1)
        self.cmb_organize = QComboBox()
        self.cmb_organize.setMinimumWidth(220)
        for text_label, mode_val in (
            ("Off (single folder)", "off"),
            ("By file type (Photos, Videos...)", "type"),
            ("By date (YYYY-MM-DD)", "date"),
            ("By device (iPhone, Laptop...)", "device"),
        ):
            self.cmb_organize.addItem(text_label, mode_val)
        sel_idx = self.cmb_organize.findData(organize_mode)
        self.cmb_organize.setCurrentIndex(sel_idx if sel_idx >= 0 else 0)
        org_row.addWidget(self.cmb_organize, 0, Qt.AlignVCenter)
        receiving.body.addLayout(org_row)
        self.body.addWidget(receiving)

        clip = Card(spacing=4)
        clip.body.addWidget(label("Clipboard", "h2"))
        self.clipboard = ToggleRow("Live clipboard sync", "Share text and screenshots you copy with your other devices.", clipboard)
        clip.body.addWidget(self.clipboard)
        self.body.addWidget(clip)

        win = Card(spacing=4)
        win.body.addWidget(label("Windows", "h2"))
        self.shell_menu = ToggleRow("Explorer right-click menu", "Adds “Send with NearBeam” when you right-click files.", shell_menu)
        win.body.addWidget(self.shell_menu)
        self.autostart = ToggleRow("Start with Windows", "Run NearBeam in the tray when you sign in.", autostart)
        win.body.addWidget(self.autostart)
        self.body.addWidget(win)

        net = Card(spacing=8)
        net.body.addWidget(label("Network", "h2"))
        fw = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(label("Windows Firewall"))
        text.addWidget(label("Stops repeated “allow network access” pop-ups. Needs administrator approval.", "dim", wrap=True))
        fw.addLayout(text, 1)
        self.btn_firewall = button("Allow in firewall", "shield")
        fw.addWidget(self.btn_firewall, 0, Qt.AlignVCenter)
        net.body.addLayout(fw)
        self.body.addWidget(net)
        self.body.addStretch()
