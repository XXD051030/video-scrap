"""Centralised theming: a Theme token set plus the global Qt stylesheet.

Two themes ship today -- a warm graphite dark mode and a Claude-style
warm-cream light mode. Everything visual flows from a ``Theme`` instance,
so switching is just ``app.setStyleSheet(build_stylesheet(theme))`` plus a
re-style of the handful of widgets that paint themselves inline.
"""

from __future__ import annotations

from dataclasses import dataclass


# Geometry is shared across themes.
RADIUS = 10
RADIUS_SM = 7


@dataclass(frozen=True)
class Theme:
    """All colour tokens for one theme."""

    name: str

    # Surfaces
    bg: str
    surface: str
    surface_alt: str
    border: str
    border_strong: str

    # Text
    text: str
    text_muted: str
    text_dim: str

    # Brand / accent
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str          # text/glyph colour on top of the accent
    accent_soft: str          # translucent accent (list selection fill)
    accent_disabled_bg: str
    accent_disabled_text: str

    # Neutral buttons
    button_bg: str
    button_hover: str
    button_pressed: str

    # State
    success: str
    warning: str
    error: str

    # Media surface (always black so letterboxing looks right)
    media_bg: str

    # Scrollbars
    scrollbar: str
    scrollbar_hover: str


DARK = Theme(
    name="dark",
    bg="#1f1e1d",
    surface="#262624",
    surface_alt="#2e2d2b",
    border="#3a3937",
    border_strong="#4a4845",
    text="#eceae3",
    text_muted="#a3a199",
    text_dim="#6f6d66",
    accent="#d97757",
    accent_hover="#e08a6b",
    accent_pressed="#c2664a",
    accent_text="#ffffff",
    accent_soft="rgba(217, 119, 87, 0.20)",
    accent_disabled_bg="#4a3a33",
    accent_disabled_text="#8a7a70",
    button_bg="#2e2d2b",
    button_hover="#36342f",
    button_pressed="#211f1d",
    success="#5fb87e",
    warning="#e2b23c",
    error="#e0685d",
    media_bg="#000000",
    scrollbar="#4a4845",
    scrollbar_hover="#5a5854",
)


LIGHT = Theme(
    name="light",
    bg="#f5f4ee",
    surface="#ffffff",
    surface_alt="#efeee6",
    border="#e3e1d7",
    border_strong="#d2cfc3",
    text="#23211e",
    text_muted="#6b6a63",
    text_dim="#95938b",
    accent="#c96442",
    accent_hover="#d97757",
    accent_pressed="#b0573a",
    accent_text="#ffffff",
    accent_soft="rgba(201, 100, 66, 0.14)",
    accent_disabled_bg="#ecd2c6",
    accent_disabled_text="#b79c90",
    button_bg="#ffffff",
    button_hover="#f2f0e8",
    button_pressed="#e8e6dc",
    success="#3e9b63",
    warning="#c8901f",
    error="#c9544b",
    media_bg="#000000",
    scrollbar="#d2cfc3",
    scrollbar_hover="#bbb8ac",
)


THEMES = {DARK.name: DARK, LIGHT.name: LIGHT}
DEFAULT_THEME = DARK.name


def get_theme(name: str) -> Theme:
    """Return the theme for ``name``, falling back to the default."""
    return THEMES.get(name, THEMES[DEFAULT_THEME])


def build_stylesheet(theme: Theme) -> str:
    t = theme
    return f"""
    /* ---------- Base ---------- */
    QWidget {{
        background-color: {t.bg};
        color: {t.text};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{
        background-color: {t.bg};
    }}
    QToolTip {{
        background-color: {t.surface_alt};
        color: {t.text};
        border: 1px solid {t.border};
        padding: 4px 8px;
        border-radius: 4px;
    }}

    /* ---------- Cards ---------- */
    QFrame#Card {{
        background-color: {t.surface};
        border: 1px solid {t.border};
        border-radius: {RADIUS}px;
    }}

    /* ---------- Toolbar ---------- */
    QToolBar {{
        background-color: {t.bg};
        border: 0;
        padding: 4px 8px;
        spacing: 4px;
    }}
    QToolBar QToolButton {{
        background-color: transparent;
        color: {t.text_muted};
        padding: 6px 10px;
        border-radius: {RADIUS_SM}px;
    }}
    QToolBar QToolButton:hover {{
        background-color: {t.surface};
        color: {t.text};
    }}
    QToolBar QToolButton:pressed {{
        background-color: {t.surface_alt};
    }}

    /* ---------- Labels ---------- */
    QLabel {{
        background-color: transparent;
        color: {t.text};
    }}
    QLabel[role="muted"] {{
        color: {t.text_muted};
    }}
    QLabel[role="dim"] {{
        color: {t.text_dim};
        font-size: 12px;
    }}
    QLabel[role="title"] {{
        font-size: 14px;
        font-weight: 600;
        color: {t.text};
    }}
    QLabel[role="section"] {{
        font-size: 12px;
        font-weight: 600;
        color: {t.text_muted};
        text-transform: uppercase;
        letter-spacing: 1px;
    }}
    QLabel[role="badge"] {{
        background-color: {t.accent_soft};
        color: {t.accent};
        padding: 2px 8px;
        border-radius: 9px;
        font-size: 11px;
        font-weight: 600;
    }}
    QLabel[role="path"] {{
        color: {t.accent};
        font-family: "SF Mono", Menlo, Consolas, monospace;
        font-size: 12px;
    }}

    /* Preview-panel widgets that used to style themselves inline. */
    QWidget#MediaStage {{
        background-color: {t.media_bg};
        border: 1px solid {t.border};
        border-radius: {RADIUS}px;
    }}
    QLabel#ThumbHint {{
        color: {t.text_muted};
        background-color: transparent;
        font-size: 14px;
    }}
    QLabel#TimeLabel {{
        color: {t.text_muted};
        font-family: "SF Mono", Menlo, Consolas, monospace;
        font-size: 12px;
    }}
    QLabel#UrlLink {{
        font-family: "SF Mono", Menlo, Consolas, monospace;
        font-size: 12px;
    }}
    QFrame#VSep {{
        background-color: {t.border};
        border: 0;
    }}

    /* ---------- Inputs ---------- */
    QLineEdit, QComboBox, QPlainTextEdit, QSpinBox, QDoubleSpinBox {{
        background-color: {t.surface_alt};
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: {RADIUS_SM}px;
        padding: 7px 10px;
        min-height: 18px;
        selection-background-color: {t.accent};
        selection-color: {t.accent_text};
    }}
    QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus,
    QSpinBox:focus, QDoubleSpinBox:focus {{
        border: 1px solid {t.accent};
    }}
    QLineEdit#UrlInput {{
        background-color: {t.bg};
        border: 1px solid {t.border};
        padding: 9px 12px;
        font-size: 13px;
    }}
    QLineEdit#UrlInput:focus {{
        border: 1px solid {t.accent};
        background-color: {t.surface_alt};
    }}
    QComboBox::drop-down {{
        border: 0;
        width: 22px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {t.surface};
        border: 1px solid {t.border};
        selection-background-color: {t.accent};
        selection-color: {t.accent_text};
        outline: 0;
    }}
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        background-color: {t.surface};
        border: 0;
        width: 18px;
    }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{
        subcontrol-origin: border;
        subcontrol-position: top right;
        border-top-right-radius: {RADIUS_SM}px;
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        subcontrol-origin: border;
        subcontrol-position: bottom right;
        border-bottom-right-radius: {RADIUS_SM}px;
    }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
        background-color: {t.border_strong};
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-bottom: 5px solid {t.text_muted};
        width: 0;
        height: 0;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {t.text_muted};
        width: 0;
        height: 0;
    }}

    /* ---------- Buttons ---------- */
    QPushButton {{
        background-color: {t.button_bg};
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: {RADIUS_SM}px;
        padding: 7px 14px;
        min-height: 18px;
    }}
    QPushButton:hover {{
        background-color: {t.button_hover};
        border-color: {t.border_strong};
    }}
    QPushButton:pressed {{
        background-color: {t.button_pressed};
    }}
    QPushButton:disabled {{
        color: {t.text_dim};
        background-color: {t.surface};
        border-color: {t.border};
    }}

    QPushButton#Primary {{
        background-color: {t.accent};
        color: {t.accent_text};
        border: 1px solid {t.accent};
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{
        background-color: {t.accent_hover};
        border-color: {t.accent_hover};
    }}
    QPushButton#Primary:pressed {{
        background-color: {t.accent_pressed};
        border-color: {t.accent_pressed};
    }}
    QPushButton#Primary:disabled {{
        background-color: {t.accent_disabled_bg};
        color: {t.accent_disabled_text};
        border-color: {t.accent_disabled_bg};
    }}

    QPushButton#Link {{
        background-color: transparent;
        color: {t.accent};
        border: 0;
        padding: 4px 6px;
        text-align: left;
    }}
    QPushButton#Link:hover {{
        color: {t.accent_hover};
    }}
    QPushButton#Link:disabled {{
        color: {t.text_dim};
    }}

    QPushButton#Ghost {{
        background-color: transparent;
        border: 1px solid {t.border};
        color: {t.text};
    }}
    QPushButton#Ghost:hover {{
        background-color: {t.surface_alt};
        border-color: {t.border_strong};
    }}

    /* ---------- List ---------- */
    QListWidget#VideoList {{
        background-color: {t.surface};
        border: 1px solid {t.border};
        border-radius: {RADIUS_SM}px;
        padding: 4px;
        outline: 0;
    }}
    QListWidget#VideoList::item {{
        background-color: transparent;
        color: {t.text};
        padding: 6px;
        margin: 2px 0;
        border-radius: {RADIUS_SM}px;
        border: 1px solid transparent;
    }}
    QListWidget#VideoList::item:hover {{
        background-color: {t.surface_alt};
    }}
    QListWidget#VideoList::item:selected {{
        background-color: {t.accent_soft};
        border: 1px solid {t.accent};
        color: {t.text};
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
        background-color: {t.surface_alt};
        border: 1px solid {t.border};
        border-radius: 6px;
        text-align: center;
        color: {t.text_muted};
        font-size: 11px;
    }}
    QProgressBar::chunk {{
        background-color: {t.accent};
        border-radius: 6px;
    }}
    QFrame#DownloadRow {{
        background-color: {t.surface};
        border: 1px solid {t.border};
        border-radius: {RADIUS_SM}px;
    }}
    QLabel[role="downloadTitle"] {{
        color: {t.text};
        font-size: 12px;
        font-weight: 600;
    }}
    QLabel[role="downloadMeta"] {{
        color: {t.text_muted};
        font-size: 11px;
        font-family: "SF Mono", Menlo, Consolas, monospace;
    }}

    /* ---------- Slider (preview seek bar) ---------- */
    QSlider::groove:horizontal {{
        height: 4px;
        background: {t.surface_alt};
        border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{
        background: {t.accent};
        border-radius: 2px;
    }}
    QSlider::add-page:horizontal {{
        background: {t.surface_alt};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {t.accent};
        width: 14px;
        margin: -6px 0;
        border-radius: 7px;
    }}
    QSlider::handle:horizontal:hover {{
        background: {t.accent_hover};
    }}

    /* ---------- Logs ---------- */
    QPlainTextEdit#Logs {{
        background-color: {t.surface};
        border: 1px solid {t.border};
        border-radius: {RADIUS_SM}px;
        font-family: "SF Mono", Menlo, Consolas, monospace;
        font-size: 12px;
        color: {t.text_muted};
    }}

    /* ---------- Status bar ---------- */
    QStatusBar {{
        background-color: {t.bg};
        color: {t.text_muted};
        border-top: 1px solid {t.border};
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
        background: {t.scrollbar};
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {t.scrollbar_hover};
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
        background: {t.scrollbar};
        border-radius: 4px;
        min-width: 24px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {t.scrollbar_hover};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
    }}
    """
