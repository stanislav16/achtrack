"""Two palettes - Graphite (dark) and Porcelain (light) - and the stylesheet built from them.

The app follows the desktop's light/dark setting unless Settings overrides it. Detection tries Qt
first (which reads the desktop portal), then the portal directly, then GNOME's and COSMIC's own
settings, so it works on COSMIC, GNOME and KDE alike.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

GRAPHITE = {
    "scheme": "dark",
    "bg": "#0E0E10",          # window
    "side": "#141416",        # sidebar
    "card": "#17171A",        # rows, panels
    "card_hover": "#1C1C20",
    "sel": "#212126",         # selected row
    "toast": "#1B1B1FF2",     # popup (last two digits = alpha)
    "field": "#18181B",       # inputs
    "hair": "#2A2A2E",        # hairline borders
    "text": "#F5F5F7",
    "text2": "#D6D6DA",
    "muted": "#8A8A8F",
    "accent": "#E0913C",      # amber
    "accent_soft": "#C67E33",
    "on_accent": "#1A1206",
    "track": "#26262A",       # progress background
    "icon_off": "#1E1E22",
    "shadow": 110,
}

PORCELAIN = {
    "scheme": "light",
    "bg": "#F6F5F3",
    "side": "#EFEEEB",
    "card": "#FFFFFF",
    "card_hover": "#FAF9F7",
    "sel": "#E4E3DF",
    "toast": "#FFFFFFF7",
    "field": "#FFFFFF",
    "hair": "#DEDCD7",
    "text": "#1C1C1E",
    "text2": "#3A3A3C",
    "muted": "#86847F",
    "accent": "#B0702C",      # copper
    "accent_soft": "#9A6126",
    "on_accent": "#FFFFFF",
    "track": "#E4E2DD",
    "icon_off": "#E8E6E1",
    "shadow": 45,
}

FONTS = '"SF Pro Text", "Inter", "Noto Sans", "Cantarell", "Ubuntu", "DejaVu Sans", sans-serif'


def _portal_scheme() -> str | None:
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.portal.Desktop",
             "--object-path", "/org/freedesktop/portal/desktop", "--method",
             "org.freedesktop.portal.Settings.Read", "org.freedesktop.appearance", "color-scheme"],
            capture_output=True, timeout=3).stdout.decode()
    except (OSError, subprocess.SubprocessError):
        return None
    if "uint32 1" in out:
        return "dark"
    if "uint32 2" in out:
        return "light"
    return None


def _gnome_scheme() -> str | None:
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                             capture_output=True, timeout=3).stdout.decode().strip().strip("'")
    except (OSError, subprocess.SubprocessError):
        return None
    if "dark" in out:
        return "dark"
    return "light" if out and "default" not in out else None


def _cosmic_scheme() -> str | None:
    path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cosmic" / \
        "com.system76.CosmicTheme.Mode" / "v1" / "is_dark"
    try:
        return "dark" if path.read_text(encoding="utf-8").strip() == "true" else "light"
    except OSError:
        return None


def system_scheme() -> str:
    """"dark" or "light", following the desktop; defaults to dark when nothing says."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
        hints = QGuiApplication.styleHints()
        scheme = getattr(Qt, "ColorScheme", None)
        if hints is not None and scheme is not None:
            value = getattr(hints, "colorScheme", lambda: None)()
            if value == scheme.Dark:
                return "dark"
            if value == scheme.Light:
                return "light"
    except (ImportError, AttributeError):
        pass
    for probe in (_portal_scheme, _cosmic_scheme, _gnome_scheme):
        found = probe()
        if found:
            return found
    return "dark"


def palette(choice: str = "auto") -> dict:
    scheme = choice if choice in ("dark", "light") else system_scheme()
    return dict(PORCELAIN if scheme == "light" else GRAPHITE)


def stylesheet(p: dict) -> str:
    return f"""
* {{ font-family: {FONTS}; }}
QWidget {{ color: {p['text2']}; font-size: 13px; }}
QMainWindow, QDialog {{ background: {p['bg']}; }}
QMenuBar {{ background: {p['bg']}; color: {p['text2']}; padding: 2px 6px; border-bottom: 1px solid {p['hair']}; }}
QMenuBar::item {{ padding: 5px 10px; border-radius: 6px; }}
QMenuBar::item:selected {{ background: {p['sel']}; }}
QMenu {{ background: {p['card']}; border: 1px solid {p['hair']}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 24px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p['sel']}; color: {p['text']}; }}
QMenu::item:disabled {{ color: {p['muted']}; }}
QMenu::separator {{ height: 1px; background: {p['hair']}; margin: 5px 8px; }}
QSplitter::handle {{ background: {p['hair']}; }}
QStatusBar {{ color: {p['muted']}; border-top: 1px solid {p['hair']}; }}
#Sidebar {{ background: {p['side']}; border-right: 1px solid {p['hair']}; }}
#SidebarList {{ background: transparent; border: none; outline: 0; }}
#SidebarList::item {{ border-radius: 10px; margin: 2px 8px; }}
#SidebarList::item:selected {{ background: {p['sel']}; }}
#SidebarList::item:hover:!selected {{ background: {p['card_hover']}; }}
QLineEdit, QPlainTextEdit, QDoubleSpinBox, QComboBox {{
    background: {p['field']}; border: 1px solid {p['hair']}; border-radius: 8px; padding: 7px 10px;
    color: {p['text']}; selection-background-color: {p['accent']}; selection-color: {p['on_accent']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {p['accent']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {p['card']}; border: 1px solid {p['hair']}; border-radius: 8px;
    selection-background-color: {p['sel']}; selection-color: {p['text']}; padding: 4px; }}
QPushButton {{ background: {p['card']}; border: 1px solid {p['hair']}; border-radius: 8px; padding: 7px 14px;
    color: {p['text']}; }}
QPushButton:hover {{ background: {p['card_hover']}; }}
QPushButton:pressed {{ background: {p['sel']}; }}
QPushButton:disabled {{ color: {p['muted']}; }}
QPushButton#Primary {{ background: {p['accent']}; border: none; color: {p['on_accent']}; font-weight: 600; }}
QPushButton#Primary:hover {{ background: {p['accent_soft']}; }}
QProgressBar {{ background: {p['track']}; border: none; border-radius: 2px; max-height: 4px; min-height: 4px; }}
QProgressBar::chunk {{ background: {p['accent']}; border-radius: 2px; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p['hair']}; border-radius: 5px; min-height: 36px; }}
QScrollBar::handle:vertical:hover {{ background: {p['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
#AchList {{ background: transparent; border: none; outline: 0; }}
#AchList::item, #AchList::item:selected, #AchList::item:hover {{ background: transparent; }}
#Muted, #Hint {{ color: {p['muted']}; }}
#Hint {{ font-size: 12px; }}
#StatLabel {{ color: {p['muted']}; font-size: 10px; font-weight: 600; letter-spacing: 0.8px; }}
#StatValue {{ color: {p['text2']}; font-size: 13.5px; font-weight: 500; }}
#GameTitle {{ color: {p['text']}; font-size: 26px; font-weight: 700; }}
#SectionLabel {{ color: {p['muted']}; font-size: 10px; font-weight: 600; letter-spacing: 0.8px; }}
#AchRow {{ background: {p['card']}; border: 1px solid {p['hair']}; border-radius: 12px; }}
#AchRow[unlocked="true"] {{ background: {p['card']}; }}
#AchName {{ color: {p['text2']}; font-size: 14px; font-weight: 500; }}
#AchRow[unlocked="true"] #AchName {{ color: {p['text']}; }}
#AchDesc {{ color: {p['muted']}; font-size: 12px; }}
#AchDate {{ color: {p['text2']}; font-size: 11.5px; }}
#AchPct {{ color: {p['muted']}; font-size: 11.5px; }}
#AchPct[rare="true"] {{ color: {p['accent']}; font-weight: 600; }}
#Empty {{ color: {p['muted']}; font-size: 15px; }}
#Running {{ color: {p['accent']}; font-size: 11.5px; font-weight: 500; }}
#Tag {{ color: {p['muted']}; border: 1px solid {p['hair']}; border-radius: 5px; padding: 1px 6px; font-size: 10px;
    font-weight: 600; }}
#Tag[kind="rare"] {{ color: {p['accent']}; border-color: {p['accent']}; }}
#ReviewRow {{ background: {p['card']}; border: 1px solid {p['hair']}; border-radius: 12px; }}
#ReviewRow[checked="true"] {{ border-color: {p['accent']}; }}
#Found {{ color: {p['accent']}; font-size: 11.5px; }}
#SaveStatus {{ color: {p['muted']}; font-size: 11.5px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; background: {p['field']};
    border: 1px solid {p['hair']}; }}
QCheckBox::indicator:checked {{ background: {p['accent']}; border-color: {p['accent']}; }}
QToolTip {{ background: {p['card']}; color: {p['text']}; border: 1px solid {p['hair']}; border-radius: 6px;
    padding: 5px; }}
"""
