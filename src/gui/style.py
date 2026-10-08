"""Centralised theming: a Theme token set plus the global Qt stylesheet.

Two neutral themes ship today: charcoal dark mode and white light mode.
Visuals flow from a ``Theme`` instance, including the Qt palette for
controls that aren't fully painted by the stylesheet.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtGui import QColor, QPalette


# Geometry is shared across themes.
RADIUS = 10
RADIUS_SM = 8


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
    bg="#111111",
    surface="#1b1b1b",
    surface_alt="#262626",
    border="#343434",
    border_strong="#484848",
    text="#f5f5f5",
    text_muted="#b3b3b3",
    text_dim="#949494",
    accent="#f0f0f0",
    accent_hover="#ffffff",
    accent_pressed="#d4d4d4",
    accent_text="#111111",
    accent_soft="rgba(240, 240, 240, 0.10)",
    accent_disabled_bg="#303030",
    accent_disabled_text="#858585",
    button_bg="#262626",
    button_hover="#333333",
    button_pressed="#1a1a1a",
    success="#b3b3b3",
    warning="#b3b3b3",
    error="#f5f5f5",
    media_bg="#000000",
    scrollbar="#484848",
    scrollbar_hover="#666666",
)


LIGHT = Theme(
    name="light",
    bg="#f5f5f5",
    surface="#ffffff",
    surface_alt="#ededed",
    border="#d8d8d8",
    border_strong="#b8b8b8",
    text="#181818",
    text_muted="#555555",
    text_dim="#6b6b6b",
    accent="#181818",
    accent_hover="#333333",
    accent_pressed="#000000",
    accent_text="#ffffff",
    accent_soft="rgba(24, 24, 24, 0.08)",
    accent_disabled_bg="#dedede",
    accent_disabled_text="#777777",
    button_bg="#ffffff",
    button_hover="#eeeeee",
    button_pressed="#e2e2e2",
    success="#555555",
    warning="#555555",
    error="#181818",
    media_bg="#000000",
    scrollbar="#b8b8b8",
    scrollbar_hover="#949494",
)


THEMES = {DARK.name: DARK, LIGHT.name: LIGHT}
DEFAULT_THEME = DARK.name


def get_theme(name: str) -> Theme:
    """Return the theme for ``name``, falling back to the default."""
    return THEMES.get(name, THEMES[DEFAULT_THEME])


def build_palette(theme: Theme) -> QPalette:
    """Keep unstyled Qt controls in sync with the chosen theme."""
    palette = QPalette()
    role = QPalette.ColorRole
    colors = {
        role.Window: theme.bg,
        role.WindowText: theme.text,
        role.Base: theme.surface,
        role.AlternateBase: theme.surface_alt,
        role.ToolTipBase: theme.surface_alt,
        role.ToolTipText: theme.text,
        role.Text: theme.text,
        role.Button: theme.button_bg,
        role.ButtonText: theme.text,
        role.BrightText: theme.accent_text,
        role.Link: theme.accent,
        role.LinkVisited: theme.text_muted,
        role.Highlight: theme.accent,
        role.HighlightedText: theme.accent_text,
        role.Accent: theme.accent,
        role.PlaceholderText: theme.text_dim,
        role.Light: theme.border_strong,
        role.Midlight: theme.surface_alt,
        role.Mid: theme.border,
        role.Dark: theme.border,
        role.Shadow: theme.media_bg,
    }
    for color_role, color in colors.items():
        palette.setColor(color_role, QColor(color))
    disabled = QPalette.ColorGroup.Disabled
    for color_role in (role.WindowText, role.Text, role.ButtonText):
        palette.setColor(disabled, color_role, QColor(theme.text_dim))
    palette.setColor(disabled, role.Highlight, QColor(theme.accent_disabled_bg))
    palette.setColor(disabled, role.HighlightedText, QColor(theme.accent_disabled_text))
    return palette


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
    QWidget#PreviewPanel, QWidget#PlayerHost, QWidget#PlayerControls,
    QWidget#MediaInfo, QWidget#DownloadControls, QWidget#AudioFormatControls,
    QStackedWidget#VideoSidebar {{
        background-color: transparent;
        border: 0;
    }}
    QFrame#PreferencesCard {{
        background-color: {t.surface};
        border: 1px solid {t.border};
        border-radius: {RADIUS}px;
    }}
    QLabel[role="preferencesTitle"], QLabel[role="aboutTitle"] {{
        font-size: 18px;
        font-weight: 600;
    }}

    /* ---------- Toolbar ---------- */
    QToolBar {{
        background-color: {t.bg};
        border: 0;
        border-bottom: 1px solid {t.border};
        padding: 6px 14px;
        spacing: 6px;
    }}
    QToolBar QToolButton {{
        background-color: transparent;
        color: {t.text_muted};
        border: 1px solid transparent;
        padding: 6px 11px;
        border-radius: {RADIUS_SM}px;
        min-height: 20px;
    }}
    QToolBar QToolButton:hover {{
        background-color: {t.surface};
        color: {t.text};
        border-color: {t.border};
    }}
    QToolBar QToolButton:pressed {{
        background-color: {t.surface_alt};
    }}
    QToolBar QToolButton:focus {{
        border-color: {t.border_strong};
    }}
    QToolBar QToolButton:disabled {{
        background-color: transparent;
        color: {t.text_dim};
        border-color: transparent;
    }}
    QToolBar::separator {{
        background-color: {t.border};
        width: 1px;
        margin: 7px 4px;
    }}
    QToolButton#ThemeToggle {{
        background-color: {t.surface};
        color: {t.text};
        border: 1px solid {t.border};
        padding: 6px 11px;
    }}
    QToolButton#ThemeToggle:hover {{
        background-color: {t.button_hover};
        border-color: {t.border_strong};
    }}
    QToolButton#MoreButton {{
        padding-right: 22px;
    }}
    QToolButton#MoreButton::menu-indicator {{
        subcontrol-origin: padding;
        subcontrol-position: right center;
        right: 8px;
        width: 9px;
        height: 6px;
    }}

    /* ---------- Popup menus ---------- */
    QMenu {{
        background-color: {t.surface};
        color: {t.text};
        border: 1px solid {t.border_strong};
        border-radius: {RADIUS_SM}px;
        padding: 5px;
        min-width: 230px;
    }}
    QMenu::item {{
        background-color: transparent;
        color: {t.text};
        padding: 10px 24px 10px 34px;
        border-radius: 5px;
    }}
    QMenu::item:selected {{
        background-color: {t.surface_alt};
        color: {t.text};
    }}
    QMenu::item:disabled {{
        color: {t.text_dim};
    }}
    QMenu::separator {{
        background-color: {t.border};
        height: 1px;
        margin: 5px 9px;
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
        color: {t.text_muted};
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
        color: {DARK.text_muted};
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
    QFrame#HSep {{
        background-color: {t.border};
        border: 0;
        min-height: 1px;
        max-height: 1px;
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
    QComboBox[lineArrow="true"]::drop-down {{
        border: 0;
        border-left: 1px solid {t.border};
        width: 26px;
        subcontrol-origin: padding;
        subcontrol-position: top right;
        border-top-right-radius: {RADIUS_SM}px;
        border-bottom-right-radius: {RADIUS_SM}px;
    }}
    QComboBox[lineArrow="true"]::down-arrow {{
        image: none;
    }}
    QComboBox {{
        padding-right: 30px;
    }}
    QComboBox:hover {{
        border-color: {t.border_strong};
    }}
    QComboBox:disabled {{
        color: {t.text_dim};
        background-color: {t.surface};
    }}
    QComboBox QAbstractItemView {{
        background-color: {t.surface};
        color: {t.text};
        border: 1px solid {t.border_strong};
        border-radius: {RADIUS_SM}px;
        padding: 4px;
        selection-background-color: {t.surface_alt};
        selection-color: {t.text};
        outline: 0;
    }}
    QComboBox QAbstractItemView::item {{
        padding: 8px 10px;
        min-height: 18px;
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
    QSpinBox[lineArrow="true"]::up-button,
    QSpinBox[lineArrow="true"]::down-button {{
        background-color: transparent;
        width: 24px;
        border-left: 1px solid {t.border};
        subcontrol-origin: padding;
        right: 1px;
    }}
    QSpinBox[lineArrow="true"]::up-arrow,
    QSpinBox[lineArrow="true"]::down-arrow {{
        image: none;
        border: 0;
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
    QPushButton:focus {{
        border-color: {t.accent};
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
        min-height: 22px;
    }}
    QPushButton#Primary:hover {{
        background-color: {t.accent_hover};
        border-color: {t.accent_hover};
    }}
    QPushButton#Primary:pressed {{
        background-color: {t.accent_pressed};
        border-color: {t.accent_pressed};
    }}
    QPushButton#Primary:focus {{
        border: 2px solid {t.border_strong};
        padding: 6px 13px;
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
    QPushButton#Link:focus {{
        background-color: {t.surface_alt};
    }}

    QPushButton#SidebarToggle {{
        background-color: transparent;
        border: 1px solid transparent;
        border-radius: 6px;
        padding: 0;
        min-height: 0;
    }}
    QPushButton#SidebarToggle:hover {{
        background-color: {t.surface_alt};
        border-color: {t.border_strong};
    }}
    QPushButton#SidebarToggle:pressed {{
        background-color: {t.button_pressed};
    }}
    QPushButton#SidebarToggle:focus {{
        border-color: {t.accent};
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
    QPushButton#Ghost:disabled {{
        background-color: {t.surface};
        color: {t.text_dim};
        border-color: {t.border};
    }}
    QPushButton#PlayerControl {{
        background-color: {t.surface_alt};
        color: {t.text};
        border: 1px solid {t.border};
        padding: 7px 10px;
        min-height: 18px;
    }}
    QPushButton#PlayerControl:hover {{
        background-color: {t.button_hover};
        border-color: {t.border_strong};
    }}
    QPushButton#PlayerControl:pressed {{
        background-color: {t.button_pressed};
    }}
    QPushButton#PlayerControl:checked {{
        background-color: {t.accent_soft};
        color: {t.text};
        border-color: {t.text_muted};
    }}
    QPushButton#PlayerControl:focus {{
        border-color: {t.accent};
    }}
    QPushButton#PlayerControl:disabled {{
        background-color: {t.surface};
        color: {t.text_dim};
        border-color: {t.border};
    }}

    /* ---------- List ---------- */
    QListWidget#VideoList {{
        background-color: {t.surface};
        border: 0;
        padding: 0;
        outline: 0;
    }}
    QListWidget#VideoList QWidget {{
        background-color: transparent;
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
        color: {t.text};
        font-size: 11px;
    }}
    QProgressBar::chunk {{
        background-color: {t.border_strong};
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
    QSlider {{
        background-color: transparent;
    }}
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
    QSlider::groove:horizontal:disabled,
    QSlider::sub-page:horizontal:disabled,
    QSlider::add-page:horizontal:disabled {{
        background: {t.border};
    }}
    QSlider::handle:horizontal:disabled {{
        background: {t.text_dim};
    }}
    QSlider::handle:horizontal:focus {{
        border: 1px solid {t.text_muted};
    }}

    /* ---------- Logs ---------- */
    QPlainTextEdit#Logs {{
        background-color: {t.surface};
        border: 1px solid {t.border};
        border-radius: {RADIUS_SM}px;
        padding: 7px 10px;
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
