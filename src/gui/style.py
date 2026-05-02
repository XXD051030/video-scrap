"""Centralised theming: colour tokens and global Qt stylesheet.

Keeping all visual constants here makes it easier to tweak the look
without spelunking through every widget.
"""

from __future__ import annotations


class Tokens:
    """Design tokens used by both Python code and the QSS string."""

    # Surfaces
    BG = "#0f1115"           # window background
    SURFACE = "#171a21"      # cards / panels
    SURFACE_ALT = "#1d2128"  # subtle nested surface
    BORDER = "#262b36"       # 1px hairline
    BORDER_STRONG = "#323847"

    # Text
    TEXT = "#e6e8ee"
    TEXT_MUTED = "#8a93a6"
    TEXT_DIM = "#5b6273"

    # Brand / state
    ACCENT = "#5aa9ff"
    ACCENT_HOVER = "#74b6ff"
    ACCENT_PRESSED = "#3f8fe6"
    SUCCESS = "#4ade80"
    WARNING = "#facc15"
    ERROR = "#f87171"

    # Media
    MEDIA_BG = "#000000"

    # Geometry
    RADIUS = 8
    RADIUS_SM = 6


def build_stylesheet() -> str:
    t = Tokens
    return f"""
    /* ---------- Base ---------- */
    QWidget {{
        background-color: {t.BG};
        color: {t.TEXT};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{
        background-color: {t.BG};
    }}
    QToolTip {{
        background-color: {t.SURFACE_ALT};
        color: {t.TEXT};
        border: 1px solid {t.BORDER};
        padding: 4px 8px;
        border-radius: 4px;
    }}

    /* ---------- Cards ---------- */
    QFrame#Card {{
        background-color: {t.SURFACE};
        border: 1px solid {t.BORDER};
        border-radius: {t.RADIUS}px;
    }}

    /* ---------- Toolbar ---------- */
    QToolBar {{
        background-color: {t.BG};
        border: 0;
        padding: 4px 8px;
        spacing: 4px;
    }}
    QToolBar QToolButton {{
        background-color: transparent;
        color: {t.TEXT_MUTED};
        padding: 6px 10px;
        border-radius: {t.RADIUS_SM}px;
    }}
    QToolBar QToolButton:hover {{
        background-color: {t.SURFACE};
        color: {t.TEXT};
    }}
    QToolBar QToolButton:pressed {{
        background-color: {t.SURFACE_ALT};
    }}

    /* ---------- Labels ---------- */
    QLabel {{
        background-color: transparent;
        color: {t.TEXT};
    }}
    QLabel[role="muted"] {{
        color: {t.TEXT_MUTED};
    }}
    QLabel[role="dim"] {{
        color: {t.TEXT_DIM};
        font-size: 12px;
    }}
    QLabel[role="title"] {{
        font-size: 14px;
        font-weight: 600;
        color: {t.TEXT};
    }}
    QLabel[role="section"] {{
        font-size: 12px;
        font-weight: 600;
        color: {t.TEXT_MUTED};
        text-transform: uppercase;
        letter-spacing: 1px;
    }}
    QLabel[role="badge"] {{
        background-color: {t.SURFACE_ALT};
        color: {t.ACCENT};
        padding: 2px 8px;
        border-radius: 9px;
        font-size: 11px;
        font-weight: 600;
    }}
    QLabel[role="path"] {{
        color: {t.ACCENT};
        font-family: "SF Mono", Menlo, Consolas, monospace;
        font-size: 12px;
    }}

    /* ---------- Inputs ---------- */
    QLineEdit, QComboBox, QPlainTextEdit {{
        background-color: {t.SURFACE_ALT};
        color: {t.TEXT};
        border: 1px solid {t.BORDER};
        border-radius: {t.RADIUS_SM}px;
        padding: 7px 10px;
        selection-background-color: {t.ACCENT};
        selection-color: #ffffff;
    }}
    QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{
        border: 1px solid {t.ACCENT};
    }}
    QLineEdit#UrlInput {{
        background-color: {t.BG};
        border: 1px solid {t.BORDER};
        padding: 9px 12px;
        font-size: 13px;
    }}
    QLineEdit#UrlInput:focus {{
        border: 1px solid {t.ACCENT};
        background-color: {t.SURFACE_ALT};
    }}
    QComboBox::drop-down {{
        border: 0;
        width: 22px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {t.SURFACE};
        border: 1px solid {t.BORDER};
        selection-background-color: {t.ACCENT};
        selection-color: #ffffff;
        outline: 0;
    }}

    /* ---------- Buttons ---------- */
    QPushButton {{
        background-color: {t.SURFACE_ALT};
        color: {t.TEXT};
        border: 1px solid {t.BORDER};
        border-radius: {t.RADIUS_SM}px;
        padding: 7px 14px;
        min-height: 18px;
    }}
    QPushButton:hover {{
        background-color: #232834;
        border-color: {t.BORDER_STRONG};
    }}
    QPushButton:pressed {{
        background-color: #161a22;
    }}
    QPushButton:disabled {{
        color: {t.TEXT_DIM};
        background-color: {t.SURFACE};
        border-color: {t.BORDER};
    }}

    QPushButton#Primary {{
        background-color: {t.ACCENT};
        color: #0b1220;
        border: 1px solid {t.ACCENT};
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{
        background-color: {t.ACCENT_HOVER};
        border-color: {t.ACCENT_HOVER};
    }}
    QPushButton#Primary:pressed {{
        background-color: {t.ACCENT_PRESSED};
        border-color: {t.ACCENT_PRESSED};
    }}
    QPushButton#Primary:disabled {{
        background-color: #2a3a52;
        color: #6f7d92;
        border-color: #2a3a52;
    }}

    QPushButton#Link {{
        background-color: transparent;
        color: {t.ACCENT};
        border: 0;
        padding: 4px 6px;
        text-align: left;
    }}
    QPushButton#Link:hover {{
        color: {t.ACCENT_HOVER};
        text-decoration: underline;
    }}
    QPushButton#Link:disabled {{
        color: {t.TEXT_DIM};
    }}

    QPushButton#Ghost {{
        background-color: transparent;
        border: 1px solid {t.BORDER};
        color: {t.TEXT};
    }}
    QPushButton#Ghost:hover {{
        background-color: {t.SURFACE_ALT};
    }}

    /* ---------- List ---------- */
    QListWidget#VideoList {{
        background-color: {t.SURFACE};
        border: 1px solid {t.BORDER};
        border-radius: {t.RADIUS_SM}px;
        padding: 4px;
        outline: 0;
    }}
    QListWidget#VideoList::item {{
        background-color: transparent;
        color: {t.TEXT};
        padding: 6px;
        margin: 2px 0;
        border-radius: {t.RADIUS_SM}px;
        border: 1px solid transparent;
    }}
    QListWidget#VideoList::item:hover {{
        background-color: {t.SURFACE_ALT};
    }}
    QListWidget#VideoList::item:selected {{
        background-color: rgba(90, 169, 255, 0.18);
        border: 1px solid {t.ACCENT};
        color: {t.TEXT};
    }}
    QListWidget#VideoList::indicator {{
        width: 16px;
        height: 16px;
        border-radius: 4px;
        border: 1px solid {t.BORDER_STRONG};
        background-color: {t.SURFACE_ALT};
    }}
    QListWidget#VideoList::indicator:hover {{
        border-color: {t.ACCENT};
    }}
    QListWidget#VideoList::indicator:checked {{
        background-color: {t.ACCENT};
        border-color: {t.ACCENT};
        image: none;
    }}

    /* ---------- Splitter ---------- */
    QSplitter::handle {{
        background-color: transparent;
    }}
    QSplitter::handle:horizontal {{
        width: 8px;
    }}
    QSplitter::handle:vertical {{
        height: 8px;
    }}

    /* ---------- Progress bar ---------- */
    QProgressBar {{
        background-color: {t.SURFACE_ALT};
        border: 1px solid {t.BORDER};
        border-radius: 6px;
        height: 12px;
        text-align: center;
        color: {t.TEXT_MUTED};
        font-size: 11px;
    }}
    QProgressBar::chunk {{
        background-color: {t.ACCENT};
        border-radius: 5px;
    }}

    /* ---------- Slider (preview seek bar) ---------- */
    QSlider::groove:horizontal {{
        height: 4px;
        background: {t.SURFACE_ALT};
        border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{
        background: {t.ACCENT};
        border-radius: 2px;
    }}
    QSlider::add-page:horizontal {{
        background: {t.SURFACE_ALT};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {t.ACCENT};
        width: 14px;
        margin: -6px 0;
        border-radius: 7px;
    }}
    QSlider::handle:horizontal:hover {{
        background: {t.ACCENT_HOVER};
    }}

    /* ---------- Logs ---------- */
    QPlainTextEdit#Logs {{
        background-color: {t.SURFACE};
        border: 1px solid {t.BORDER};
        border-radius: {t.RADIUS_SM}px;
        font-family: "SF Mono", Menlo, Consolas, monospace;
        font-size: 12px;
        color: {t.TEXT_MUTED};
    }}

    /* ---------- Status bar ---------- */
    QStatusBar {{
        background-color: {t.BG};
        color: {t.TEXT_MUTED};
        border-top: 1px solid {t.BORDER};
    }}
    QStatusBar::item {{
        border: 0;
    }}

    /* ---------- Scrollbars ---------- */
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 4px 2px 4px 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {t.BORDER_STRONG};
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: #404758;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: transparent;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 10px;
        margin: 2px 4px 2px 4px;
    }}
    QScrollBar::handle:horizontal {{
        background: {t.BORDER_STRONG};
        border-radius: 4px;
        min-width: 24px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: #404758;
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
    }}
    """
