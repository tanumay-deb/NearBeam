"""Colour tokens and the single application stylesheet for the Control Panel."""

BG = "#0f1833"
SIDEBAR = "#121d3b"
CARD = "#17233f"
RAISED = "#1e2c4f"
BORDER = "rgba(148, 163, 255, 0.14)"
BORDER_STRONG = "rgba(148, 163, 255, 0.28)"
TEXT = "#f1f5f9"
MUTED = "#a9b8d0"
DIM = "#7d8db0"
ACCENT = "#4cc9ff"
ACCENT_FILL = "#0284c7"
ACCENT_FILL_HOVER = "#0369a1"
SUCCESS = "#34d399"
WARNING = "#fbbf24"
DANGER = "#f87171"

QSS = f"""
QMainWindow, QWidget#pageStack, QWidget#page {{ background-color: {BG}; }}
QWidget {{ color: {TEXT}; font-family: 'Segoe UI Variable Text', 'Segoe UI', Arial, sans-serif; font-size: 13px; }}
QLabel {{ background: transparent; }}
QToolTip {{ background-color: {RAISED}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 4px 8px; }}

QFrame#sidebar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #16234a, stop:1 {SIDEBAR});
    border-right: 1px solid {BORDER};
}}
QLabel#brand {{ font-size: 17px; font-weight: 600; }}
QPushButton#navButton {{
    background: transparent; border: none; border-radius: 8px;
    color: {MUTED}; text-align: left; padding: 9px 12px; font-size: 13px; font-weight: 500;
}}
QPushButton#navButton:hover {{ background-color: rgba(255, 255, 255, 0.05); color: {TEXT}; }}
QPushButton#navButton:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(56, 189, 248, 0.20), stop:1 rgba(99, 102, 241, 0.06));
    color: {ACCENT};
}}
QLabel#navBadge {{
    background-color: {ACCENT_FILL}; color: #ffffff; border-radius: 9px;
    font-size: 11px; font-weight: 600; padding: 1px 7px;
}}

QFrame[card="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1c2a4d, stop:1 {CARD});
    border: 1px solid {BORDER}; border-top-color: rgba(255, 255, 255, 0.16); border-radius: 14px;
}}
QFrame[row="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #22325a, stop:1 {RAISED});
    border: 1px solid {BORDER}; border-top-color: rgba(255, 255, 255, 0.14); border-radius: 12px;
}}
QFrame[row="true"]:hover {{
    border-color: rgba(76, 201, 255, 0.55);
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #283b6a, stop:1 #213157);
}}
QFrame[row="true"][host="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(76, 201, 255, 0.22), stop:1 rgba(129, 140, 248, 0.10));
    border-color: rgba(76, 201, 255, 0.45);
}}
QLabel[pill="host"] {{ background-color: rgba(76, 201, 255, 0.18); color: {ACCENT}; border-radius: 8px; padding: 1px 7px; font-size: 11px; font-weight: 600; }}
QLabel[pill="peer"] {{ background-color: rgba(129, 140, 248, 0.18); color: #a5b4fc; border-radius: 8px; padding: 1px 7px; font-size: 11px; font-weight: 600; }}
QLabel[pill="phone"] {{ background-color: rgba(52, 211, 153, 0.16); color: {SUCCESS}; border-radius: 8px; padding: 1px 7px; font-size: 11px; font-weight: 600; }}
QPushButton#qrButton {{ background-color: #ffffff; border: 2px solid rgba(76, 201, 255, 0.35); border-radius: 12px; padding: 4px; }}
QPushButton#qrButton:hover {{ border-color: {ACCENT}; }}
QFrame#phoneBox {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(76, 201, 255, 0.18), stop:1 rgba(129, 140, 248, 0.10));
    border: 1px solid rgba(76, 201, 255, 0.35); border-radius: 14px;
}}
QFrame#dropZone {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(76, 201, 255, 0.14), stop:0.6 {CARD}, stop:1 rgba(129, 140, 248, 0.12));
    border: 1px dashed rgba(76, 201, 255, 0.40); border-radius: 16px;
}}
QFrame#dropZone:hover {{ border-color: rgba(56, 189, 248, 0.45); }}
QFrame#dropZone[dragOver="true"], QFrame[row="true"][dragOver="true"] {{ border: 1px dashed {ACCENT}; background-color: rgba(56, 189, 248, 0.08); }}

QLabel[role="h1"] {{ font-size: 20px; font-weight: 600; }}
QLabel[role="h2"] {{ font-size: 14px; font-weight: 600; }}
QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel[role="dim"] {{ color: {DIM}; font-size: 12px; }}
QLabel[role="mono"] {{ color: {ACCENT}; font-family: 'Cascadia Mono', Consolas, monospace; font-size: 12px; }}
QLabel[role="monoSmall"] {{ color: {ACCENT}; font-family: 'Cascadia Mono', Consolas, monospace; font-size: 11px; }}
QLabel[role="success"] {{ color: {SUCCESS}; font-size: 12px; }}
QLabel[role="warning"] {{ color: {WARNING}; font-size: 12px; }}

QPushButton {{
    background-color: {RAISED}; border: 1px solid {BORDER_STRONG}; border-radius: 8px;
    color: {TEXT}; padding: 7px 14px; font-weight: 500;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background-color: {CARD}; }}
QPushButton#primaryBtn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0ea5e9, stop:1 #6366f1);
    border: 1px solid rgba(255, 255, 255, 0.18); color: #ffffff; font-weight: 600; padding: 8px 18px;
}}
QPushButton#primaryBtn:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #38bdf8, stop:1 #818cf8); }}
QPushButton#primaryBtn:pressed {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT_FILL}, stop:1 #4f46e5); }}
QPushButton#dangerBtn {{ background-color: rgba(248, 113, 113, 0.10); border: 1px solid rgba(248, 113, 113, 0.30); color: {DANGER}; }}
QPushButton#dangerBtn:hover {{ border-color: {DANGER}; }}
QPushButton#linkBtn {{ background: transparent; border: none; color: {ACCENT}; padding: 2px 4px; }}
QPushButton#linkBtn:hover {{ text-decoration: underline; }}
QPushButton#iconBtn {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 5px; }}
QPushButton#iconBtn:hover {{ background-color: rgba(255, 255, 255, 0.06); border-color: {BORDER_STRONG}; }}
QPushButton#updateBtn {{ background: transparent; border: none; color: {DIM}; font-size: 12px; padding: 2px 0; text-align: left; }}
QPushButton#updateBtn:hover {{ color: {TEXT}; }}
QPushButton#updateBtn[available="true"] {{ color: {WARNING}; }}
QPushButton#segBtn {{ background: transparent; border: 1px solid {BORDER}; border-radius: 6px; color: {DIM}; font-size: 11px; padding: 2px 8px; }}
QPushButton#segBtn:checked {{ border-color: {ACCENT}; color: {ACCENT}; }}

QLineEdit {{
    background-color: {RAISED}; border: 1px solid {BORDER}; border-radius: 8px;
    color: {TEXT}; padding: 7px 10px; selection-background-color: {ACCENT_FILL};
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QTextEdit, QPlainTextEdit {{
    background-color: {RAISED}; border: 1px solid {BORDER}; border-radius: 8px;
    color: {TEXT}; padding: 8px 10px; selection-background-color: {ACCENT_FILL};
}}
QTextEdit#logView {{ font-family: 'Cascadia Mono', Consolas, monospace; font-size: 11px; background-color: {BG}; }}

QProgressBar {{ background-color: rgba(255, 255, 255, 0.08); border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0ea5e9, stop:1 #818cf8); border-radius: 3px; }}
QProgressBar[state="done"]::chunk {{ background-color: {SUCCESS}; }}
QProgressBar[state="paused"]::chunk {{ background-color: {DIM}; }}

QTableWidget {{
    background-color: {CARD}; border: 1px solid {BORDER}; border-radius: 10px;
    gridline-color: transparent; selection-background-color: rgba(56, 189, 248, 0.15); selection-color: {TEXT};
}}
QTableWidget::item {{ padding: 6px 8px; border-bottom: 1px solid {BORDER}; }}
QHeaderView::section {{
    background-color: {CARD}; color: {DIM}; border: none; border-bottom: 1px solid {BORDER};
    padding: 8px; font-size: 12px; font-weight: 500;
}}

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: rgba(255, 255, 255, 0.14); min-height: 24px; border-radius: 4px; }}
QScrollBar::handle:vertical:hover {{ background: rgba(255, 255, 255, 0.24); }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 0; }}

QMenu {{ background-color: {RAISED}; border: 1px solid {BORDER_STRONG}; padding: 4px; }}
QMenu::item {{ padding: 6px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background-color: rgba(56, 189, 248, 0.15); }}
QMessageBox {{ background-color: {CARD}; }}
"""
