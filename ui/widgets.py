"""Small reusable widgets for the Control Panel."""

import os
import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import (
    QByteArray,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.server import format_size
from ui import theme


@lru_cache(maxsize=1)
def animations_enabled() -> bool:
    """Honours Windows' "Show animations in Windows" accessibility setting."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        enabled = ctypes.c_bool(True)
        SPI_GETCLIENTAREAANIMATION = 0x1042
        ctypes.windll.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0)
        return bool(enabled.value)
    except Exception:
        return True


def fade_in(widget: QWidget, duration: int = 220, rise: int = 0):
    """Fades a widget in (optionally rising a few px). The effect is removed afterwards so
    text renders crisply and nested opacity effects don't fight."""
    if not animations_enabled():
        return
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start(QPropertyAnimation.DeleteWhenStopped)
    if rise:
        end = widget.pos()
        slide = QPropertyAnimation(widget, b"pos", widget)
        slide.setDuration(duration)
        slide.setStartValue(end + QPoint(0, rise))
        slide.setEndValue(end)
        slide.setEasingCurve(QEasingCurve.OutCubic)
        slide.start(QPropertyAnimation.DeleteWhenStopped)


def glow(widget: QWidget, color: str = theme.ACCENT, blur: int = 26, alpha: int = 110):
    """Soft coloured light under a widget."""
    effect = QGraphicsDropShadowEffect(widget)
    c = QColor(color)
    c.setAlpha(alpha)
    effect.setColor(c)
    effect.setBlurRadius(blur)
    effect.setOffset(0, 4)
    widget.setGraphicsEffect(effect)


def _assets_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "assets"
    return Path(__file__).resolve().parent.parent / "assets"


@lru_cache(maxsize=None)
def pixmap(name: str, color: str = theme.MUTED, size: int = 18) -> QPixmap:
    """Renders assets/icons/<name>.svg in the given colour (the SVGs stroke with currentColor)."""
    path = _assets_dir() / "icons" / f"{name}.svg"
    try:
        svg = path.read_text(encoding="utf-8").replace("currentColor", color)
    except OSError:
        return QPixmap()
    ratio = 2  # render at 2x so icons stay crisp on high-DPI screens
    pm = QPixmap(size * ratio, size * ratio)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(painter, QRectF(0, 0, size * ratio, size * ratio))
    painter.end()
    pm.setDevicePixelRatio(ratio)
    return pm


def icon(name: str, color: str = theme.MUTED, size: int = 18, checked_color: str = None) -> QIcon:
    ic = QIcon()
    ic.addPixmap(pixmap(name, color, size), QIcon.Normal, QIcon.Off)
    if checked_color:
        ic.addPixmap(pixmap(name, checked_color, size), QIcon.Normal, QIcon.On)
    return ic


def label(text: str = "", role: str = None, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if role:
        lbl.setProperty("role", role)
    lbl.setWordWrap(wrap)
    return lbl


def icon_label(name: str, color: str = theme.MUTED, size: int = 18) -> QLabel:
    lbl = QLabel()
    lbl.setPixmap(pixmap(name, color, size))
    lbl.setFixedSize(size, size)
    return lbl


def button(text: str = "", icon_name: str = None, kind: str = None, tooltip: str = None, icon_color: str = None) -> QPushButton:
    btn = QPushButton(text)
    if kind:
        btn.setObjectName(kind)
    if icon_name:
        default = "#ffffff" if kind == "primaryBtn" else (theme.DANGER if kind == "dangerBtn" else theme.MUTED)
        btn.setIcon(icon(icon_name, icon_color or default, 16))
        btn.setIconSize(QSize(16, 16))
    if tooltip:
        btn.setToolTip(tooltip)
        btn.setAccessibleName(tooltip)
    btn.setCursor(Qt.PointingHandCursor)
    if kind == "primaryBtn":
        glow(btn, "#6366f1", 28, 120)
    return btn


def icon_button(icon_name: str, tooltip: str, color: str = theme.MUTED) -> QPushButton:
    btn = button("", icon_name, "iconBtn", tooltip, color)
    btn.setFixedSize(32, 32)
    return btn


def repolish(widget: QWidget):
    """Re-applies the stylesheet after a dynamic property change."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def clear_layout(layout: QLayout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout())


class Card(QFrame):
    def __init__(self, parent=None, padding: int = 18, spacing: int = 12):
        super().__init__(parent)
        self.setProperty("card", True)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding - 2, padding, padding)
        self.body.setSpacing(spacing)


class NavButton(QPushButton):
    def __init__(self, text: str, icon_name: str):
        super().__init__(text)
        self.setObjectName("navButton")
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIcon(icon(icon_name, theme.MUTED, 18, checked_color=theme.ACCENT))
        self.setIconSize(QSize(18, 18))
        self.setMinimumHeight(38)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 10, 0)
        row.addStretch()
        self.badge = QLabel()
        self.badge.setObjectName("navBadge")
        self.badge.setFixedHeight(18)
        self.badge.setMinimumWidth(18)
        self.badge.setAlignment(Qt.AlignCenter)
        self.badge.hide()
        row.addWidget(self.badge, 0, Qt.AlignVCenter)

    def set_badge(self, count: int):
        self.badge.setText(str(count))
        self.badge.setVisible(count > 0)


class ToggleSwitch(QAbstractButton):
    """A pill-shaped on/off switch."""

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._pos = 1.0 if checked else 0.0  # 0 = off, 1 = on; animated in between
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._set_pos)
        self.toggled.connect(self._animate)

    def _set_pos(self, v):
        self._pos = float(v)
        self.update()

    def _animate(self, on: bool):
        if not animations_enabled():
            self._set_pos(1.0 if on else 0.0)
            return
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def sizeHint(self):
        return QSize(38, 22)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1, 1, 36, 20)
        t = self._pos
        off_c, on_c = QColor(theme.RAISED), QColor(theme.ACCENT_FILL)
        track = QColor(
            int(off_c.red() + (on_c.red() - off_c.red()) * t),
            int(off_c.green() + (on_c.green() - off_c.green()) * t),
            int(off_c.blue() + (on_c.blue() - off_c.blue()) * t),
        )
        p.setPen(QColor(255, 255, 255, int(60 * (1 - t))))
        p.setBrush(track)
        p.drawRoundedRect(r, 10, 10)
        x = r.left() + 3 + (r.width() - 20) * t
        knob = QRectF(x, r.top() + 3, 14, 14)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff") if t > 0.5 else QColor(theme.MUTED))
        p.drawEllipse(knob)
        if self.hasFocus():
            p.setPen(QColor(theme.ACCENT))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 11, 11)


class LiveDot(QWidget):
    """A status dot with a soft expanding ring, so 'online' feels alive."""

    def __init__(self, color: str = theme.SUCCESS, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.setFixedSize(18, 18)
        self._phase = 0.0
        if animations_enabled():
            self._anim = QVariantAnimation(self)
            self._anim.setStartValue(0.0)
            self._anim.setEndValue(1.0)
            self._anim.setDuration(2000)
            self._anim.setLoopCount(-1)
            self._anim.valueChanged.connect(self._tick)
            self._anim.start()

    def set_color(self, color: str):
        self.color = QColor(color)
        self.update()

    def _tick(self, v):
        self._phase = float(v)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self.rect().center()
        ring = QColor(self.color)
        ring.setAlphaF(0.55 * (1 - self._phase))
        p.setPen(Qt.NoPen)
        p.setBrush(ring)
        radius = 4 + 5 * self._phase
        p.drawEllipse(QRectF(c.x() - radius + 0.5, c.y() - radius + 0.5, radius * 2, radius * 2))
        p.setBrush(self.color)
        p.drawEllipse(QRectF(c.x() - 3.5, c.y() - 3.5, 8, 8))


class ToggleRow(QWidget):
    """Title + description on the left, switch on the right."""

    def __init__(self, title: str, description: str, checked: bool):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 6, 0, 6)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(label(title))
        text.addWidget(label(description, "dim", wrap=True))
        row.addLayout(text, 1)
        self.switch = ToggleSwitch(checked)
        self.switch.setAccessibleName(title)
        row.addWidget(self.switch, 0, Qt.AlignVCenter)


class _DropMixin:
    """Accepts local file drops, highlights while dragging, emits the paths."""

    def _init_drop(self):
        self.setAcceptDrops(True)

    def _set_over(self, over: bool):
        self.setProperty("dragOver", over)
        repolish(self)

    def dragEnterEvent(self, event):
        if event.mimeData() and event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_over(True)

    def dragLeaveEvent(self, _event):
        self._set_over(False)

    def dropEvent(self, event):
        self._set_over(False)
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile() and os.path.exists(u.toLocalFile())]
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)


class DropZone(_DropMixin, QFrame):
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dropZone")
        self._init_drop()


DEVICE_ICONS = {"phone": "phone", "tablet": "phone", "desktop": "desktop", "laptop": "laptop"}


def device_role(device: dict) -> tuple[str, str]:
    """(label, pill style) — the host is the mesh leader that owns nearbeam.local."""
    if device.get("kind") == "host":
        return ("Host", "host") if device.get("is_leader") else ("Peer PC", "peer")
    dtype = device.get("device_type")
    return ({"phone": "Phone", "tablet": "Tablet"}.get(dtype, "Browser"), "phone")


class DeviceCard(_DropMixin, QFrame):
    """A nearby device. Emits files_dropped(paths) on drop and send_clicked() from its arrow."""

    files_dropped = Signal(list)
    send_clicked = Signal()

    def __init__(self, device: dict):
        super().__init__()
        self.device = device
        self.setProperty("row", True)
        self.setMinimumWidth(200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._init_drop()
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 8, 10)
        row.setSpacing(10)
        row.addWidget(icon_label(DEVICE_ICONS.get(device.get("device_type"), "laptop"), theme.ACCENT, 22))
        role, pill = device_role(device)
        if device.get("is_leader"):
            self.setProperty("host", True)
        text = QVBoxLayout()
        text.setSpacing(3)
        name = label(device.get("name", "Device"))
        name.setToolTip(device.get("name", ""))
        name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        text.addWidget(name)
        tags = QHBoxLayout()
        tags.setSpacing(6)
        tag = label(role)
        tag.setProperty("pill", pill)
        tags.addWidget(tag)
        if device.get("is_self"):
            tags.addWidget(label("This PC", "dim"))
        tags.addStretch()
        text.addLayout(tags)
        row.addLayout(text, 1)
        if not device.get("is_self"):
            tip = f"Send files to {device.get('name')}" if device.get("kind") == "host" else f"Share files with {device.get('name')}"
            send = icon_button("send", tip, theme.ACCENT)
            send.clicked.connect(self.send_clicked.emit)
            row.addWidget(send)


def format_eta(seconds: float) -> str:
    s = max(int(seconds + 0.5), 1)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


class TransferRow(QFrame):
    """One transfer: active (progress + controls) or finished (history)."""

    pause_toggled = Signal(bool)
    cancel_clicked = Signal()
    open_clicked = Signal()

    def __init__(self, filename: str, direction: str, peer: str, total: int = 0, controls: bool = False, openable: bool = False):
        super().__init__()
        self.setProperty("row", True)
        self.total = total
        self.peer = peer
        self.direction = direction
        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 10, 8, 10)
        outer.setSpacing(12)
        arrow = "arrow-up" if direction == "sent" else "arrow-down"
        outer.addWidget(icon_label(arrow, theme.ACCENT if direction == "sent" else theme.SUCCESS, 18))

        mid = QVBoxLayout()
        mid.setSpacing(4)
        top = QHBoxLayout()
        self.name = label(filename)
        self.name.setToolTip(filename)
        self.name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        top.addWidget(self.name, 1)
        self.pct = label("", "dim")
        top.addWidget(self.pct)
        mid.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self._bar_anim = QPropertyAnimation(self.bar, b"value", self)
        self._bar_anim.setDuration(350)
        self._bar_anim.setEasingCurve(QEasingCurve.OutCubic)
        mid.addWidget(self.bar)
        bottom = QHBoxLayout()
        self.meta = label("", "dim")
        bottom.addWidget(self.meta, 1)
        self.stats = label("", "dim")
        self.stats.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bottom.addWidget(self.stats)
        mid.addLayout(bottom)
        outer.addLayout(mid, 1)
        self.sent = 0
        self._bps = 0.0

        self.btn_pause = self.btn_cancel = self.btn_open = None
        if controls:
            self.btn_pause = icon_button("pause", "Pause")
            self.btn_pause.setCheckable(True)
            self.btn_pause.toggled.connect(self._on_pause)
            outer.addWidget(self.btn_pause)
            self.btn_cancel = icon_button("x", "Cancel")
            self.btn_cancel.clicked.connect(self.cancel_clicked.emit)
            outer.addWidget(self.btn_cancel)
        if openable:
            self.btn_open = icon_button("folder-open", "Show in folder")
            self.btn_open.clicked.connect(self.open_clicked.emit)
            outer.addWidget(self.btn_open)
        self.set_progress(0, total, 0)

    def _on_pause(self, paused: bool):
        self.btn_pause.setIcon(icon("play" if paused else "pause", theme.MUTED, 16))
        self.btn_pause.setToolTip("Resume" if paused else "Pause")
        self.bar.setProperty("state", "paused" if paused else "")
        repolish(self.bar)
        self._render()
        self.pause_toggled.emit(paused)

    def _meta_text(self, status: str) -> str:
        """Finished rows: size · peer · status."""
        word = "to" if self.direction == "sent" else "from"
        parts = [format_size(self.total), f"{word} {self.peer}"]
        if status:
            parts.append(status)
        return "  ·  ".join(parts)

    def set_queued(self):
        self.meta.setText(f"{'to' if self.direction == 'sent' else 'from'} {self.peer}  ·  Queued")
        self.stats.setText(f"0 B / {format_size(self.total)}")

    def set_progress(self, sent: int, total: int, bps: float):
        self.total = total or self.total
        self.sent = sent
        # Smooth the speed so the readout and time-left don't jitter between chunks.
        if bps > 0:
            self._bps = bps if self._bps <= 0 else self._bps * 0.6 + bps * 0.4
        frac = min((sent / self.total) if self.total else 0, 1.0)
        self._set_bar(int(frac * 1000))
        self.pct.setText(f"{frac * 100:.0f}%")
        self._render()

    def _render(self):
        paused = self.btn_pause is not None and self.btn_pause.isChecked()
        word = "to" if self.direction == "sent" else "from"
        self.meta.setText(f"{word} {self.peer}" + ("  ·  Paused" if paused else ""))
        parts = [f"{format_size(self.sent)} / {format_size(self.total)}"]
        if not paused and self._bps > 0 and self.sent < self.total:
            parts.append(f"{format_size(int(self._bps))}/s")
            parts.append(f"{format_eta((self.total - self.sent) / self._bps)} left")
        self.stats.setText("  ·  ".join(parts))

    def _set_bar(self, value: int):
        """Glides the bar to its new value instead of jumping chunk by chunk."""
        if not animations_enabled() or value <= self.bar.value():
            self._bar_anim.stop()
            self.bar.setValue(value)
            return
        self._bar_anim.stop()
        self._bar_anim.setStartValue(self.bar.value())
        self._bar_anim.setEndValue(value)
        self._bar_anim.start()

    def set_finished(self, ok: bool, status: str):
        if ok:
            self._set_bar(1000)
        self.bar.setProperty("state", "done" if ok else "paused")
        repolish(self.bar)
        self.pct.setText("")
        self.stats.setText("")
        self.meta.setText(self._meta_text(status))
        for b in (self.btn_pause, self.btn_cancel):
            if b:
                b.hide()
