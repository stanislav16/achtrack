"""Two palettes - Graphite (dark) and Porcelain (light) - and the stylesheet built from them.

The app follows the desktop's light/dark setting unless Settings overrides it. Detection tries Qt
first (which reads the desktop portal), then the portal directly, then GNOME's and COSMIC's own
settings, so it works on COSMIC, GNOME and KDE alike.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

GRAPHITE = {
    "scheme": "dark",
    "bg": "#111112",          # window
    "side": "#18181A",        # sidebar
    "card": "#1C1C1E",        # grouped lists, panels
    "card_hover": "#232325",
    "sel": "#2C2C2E",         # selected row
    "fill": "#2A2A2C",        # buttons, fields
    "fill_hover": "#343437",
    "toast": "#1C1C1EF2",     # popup; last two digits are alpha, CSS order
    "field": "#1F1F21",       # inputs
    "hair": "#2B2B2E",        # hairline borders
    "text": "#F5F5F7",
    "text2": "#D1D1D6",
    "muted": "#8E8E93",
    "accent": "#F5F5F7",      # no hue: the artwork brings the colour
    "accent_soft": "#D1D1D6",
    "on_accent": "#111112",
    "live": "#30D158",        # playing now
    "danger": "#FF6961",
    "track": "#2C2C2E",       # progress background
    "icon_off": "#232325",
    "shadow": 110,
}

PORCELAIN = {
    "scheme": "light",
    "bg": "#F5F5F7",
    "side": "#ECECEE",
    "card": "#FFFFFF",
    "card_hover": "#FAFAFB",
    "sel": "#E2E2E6",
    "fill": "#E0E0E4",
    "fill_hover": "#D5D5DA",
    "toast": "#FFFFFFF7",
    "field": "#FFFFFF",
    "hair": "#DCDCE0",
    "text": "#1D1D1F",
    "text2": "#3A3A3C",
    "muted": "#86868B",
    "accent": "#1D1D1F",
    "accent_soft": "#3A3A3C",
    "on_accent": "#FFFFFF",
    "live": "#248A3D",
    "danger": "#D70015",
    "track": "#E2E2E6",
    "icon_off": "#E8E8EB",
    "shadow": 45,
}


def check_image(p: dict) -> str:
    """A tick for checked boxes, drawn in the palette's colours; "" when it can't be made."""
    try:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter, QPen
        folder = Path(tempfile.gettempdir()) / "achtrack-ui"
        folder.mkdir(parents=True, exist_ok=True)
        base = folder / f"check-{p['scheme']}.png"
        for path, scale in ((base, 1), (folder / f"check-{p['scheme']}@2x.png", 2)):
            img = QImage(16 * scale, 16 * scale, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            painter = QPainter(img)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.scale(scale, scale)
            pen = QPen(QColor(p["on_accent"]), 1.9)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline([QPointF(4.2, 8.4), QPointF(7.0, 11.0), QPointF(11.8, 5.2)])
            painter.end()
            img.save(str(path))
        return base.as_posix()
    except Exception:       # a missing tick must never stop the window opening
        return ""


def colour(value: str):
    """A palette colour as a QColor, reading "#RRGGBBAA" the way CSS does.

    Qt reads an eight-digit hex colour as #AARRGGBB, so "#1C1C1CF2" came out as 11% opaque with
    its blue at 242 - a washed-out blue popup instead of a near-solid grey one.
    """
    from PySide6.QtGui import QColor
    text = value.strip()
    if len(text) == 9 and text.startswith("#"):
        c = QColor(text[:7])
        c.setAlpha(int(text[7:], 16))
        return c
    return QColor(text)


def font_rule(family: str = "") -> str:
    """Use the desktop's own UI font unless Settings names one."""
    return f'* {{ font-family: "{family}"; }}\n' if family.strip() else ""


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


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def _ini_value(path: Path, key: str) -> str | None:
    """A key's value from a simple INI-ish file, whichever section it's in."""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip().lower() == key:
                return value.strip()
    except OSError:
        return None
    return None


def _kde_scheme() -> str | None:
    """Plasma (Kubuntu, Fedora KDE, SteamOS...) records the scheme in kdeglobals."""
    path = _config_home() / "kdeglobals"
    name = _ini_value(path, "colorscheme") or _ini_value(path, "lookandfeelpackage") or ""
    if "dark" in name.lower():
        return "dark"
    # Custom schemes don't say "dark" in their name, so go by the window colour itself.
    background = _ini_value(path, "backgroundnormal")
    try:
        red, green, blue = (int(part) for part in background.split(",")[:3])
    except (AttributeError, ValueError):
        return "light" if name else None
    return "dark" if (red * 299 + green * 587 + blue * 114) / 1000 < 128 else "light"


def _xfce_scheme() -> str | None:
    try:
        out = subprocess.run(["xfconf-query", "-c", "xsettings", "-p", "/Net/ThemeName"],
                             capture_output=True, timeout=3).stdout.decode().strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return ("dark" if "dark" in out.lower() else "light") if out else None


def _gtk_scheme() -> str | None:
    """Anything else using GTK settings - Cinnamon, MATE, Budgie, plain window managers."""
    path = _config_home() / "gtk-3.0" / "settings.ini"
    if _ini_value(path, "gtk-application-prefer-dark-theme") in ("1", "true", "yes"):
        return "dark"
    name = _ini_value(path, "gtk-theme-name") or ""
    return ("dark" if "dark" in name.lower() else "light") if name else None


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
    if os.name == "nt" or sys.platform == "darwin":
        return "dark"               # Qt answers for these; the probes below are Linux desktops'
    for probe in (_portal_scheme, _cosmic_scheme, _kde_scheme, _xfce_scheme, _gnome_scheme, _gtk_scheme):
        found = probe()
        if found:
            return found
    return "dark"


def palette(choice: str = "auto") -> dict:
    scheme = choice if choice in ("dark", "light") else system_scheme()
    return dict(PORCELAIN if scheme == "light" else GRAPHITE)


def qt_palette(p: dict, QPalette, QColor):
    """App-wide colours so anything the stylesheet misses (popup frames, scroll areas) still fits."""
    role = QPalette.ColorRole
    pal = QPalette()
    for r, key in ((role.Window, "bg"), (role.Base, "field"), (role.AlternateBase, "card"), (role.Button, "card"),
                   (role.ToolTipBase, "card"), (role.WindowText, "text"), (role.Text, "text"),
                   (role.ButtonText, "text"), (role.ToolTipText, "text"), (role.PlaceholderText, "muted"),
                   (role.Highlight, "accent"), (role.HighlightedText, "on_accent")):
        pal.setColor(r, QColor(p[key]))
    pal.setColor(QPalette.ColorGroup.Disabled, role.Text, QColor(p["muted"]))
    pal.setColor(QPalette.ColorGroup.Disabled, role.ButtonText, QColor(p["muted"]))
    return pal


def stylesheet(p: dict, font: str = "") -> str:
    tick = check_image(p)
    tick_rule = f"image: url({tick});" if tick else ""
    return font_rule(font) + f"""
QWidget {{ color: {p['text2']}; font-size: 13px; }}
QMainWindow, QDialog {{ background: {p['bg']}; }}
QMenuBar {{ background: {p['bg']}; color: {p['text2']}; padding: 2px 6px; border-bottom: 1px solid {p['hair']}; }}
QMenuBar::item {{ padding: 5px 10px; border-radius: 6px; }}
QMenuBar::item:selected {{ background: {p['sel']}; }}
QMenu {{ background: {p['card']}; border: 1px solid {p['hair']}; border-radius: 10px; padding: 5px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p['sel']}; color: {p['text']}; }}
QMenu::item:disabled {{ color: {p['muted']}; }}
QMenu::separator {{ height: 1px; background: {p['hair']}; margin: 5px 8px; }}
QSplitter::handle {{ background: {p['hair']}; }}
QStatusBar {{ color: {p['muted']}; border-top: 1px solid {p['hair']}; }}
#Sidebar {{ background: {p['side']}; border-right: 1px solid {p['hair']}; }}
#SidebarList {{ background: transparent; border: none; outline: 0; }}
#SidebarList::item {{ border-radius: 10px; margin: 1px 8px; }}
#SidebarList::item:selected {{ background: {p['sel']}; }}
#SidebarList::item:hover:!selected {{ background: {p['card_hover']}; }}
QLineEdit, QPlainTextEdit, QDoubleSpinBox, QComboBox, QDateTimeEdit {{
    background: {p['field']}; border: 1px solid {p['hair']}; border-radius: 8px; padding: 6px 10px;
    color: {p['text']}; selection-background-color: {p['muted']}; selection-color: {p['bg']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QDoubleSpinBox:focus, QComboBox:focus, QDateTimeEdit:focus {{
    border-color: {p['muted']}; }}
QLineEdit#Search {{ background: {p['fill']}; border-color: transparent; border-radius: 9px; padding: 6px 12px; }}
QLineEdit#Search:focus {{ border-color: {p['muted']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {p['card']}; border: 1px solid {p['hair']}; border-radius: 8px;
    selection-background-color: {p['sel']}; selection-color: {p['text']}; padding: 4px; }}
QPushButton {{ background: {p['fill']}; border: none; border-radius: 8px; padding: 7px 14px;
    color: {p['text']}; font-weight: 500; }}
QPushButton:hover {{ background: {p['fill_hover']}; }}
QPushButton:pressed {{ background: {p['sel']}; }}
QPushButton:disabled {{ color: {p['muted']}; }}
QPushButton::menu-indicator {{ width: 0; height: 0; image: none; }}
QPushButton#Primary {{ background: {p['accent']}; color: {p['on_accent']}; font-weight: 600; }}
QPushButton#Primary:hover {{ background: {p['accent_soft']}; }}
QPushButton#Primary:disabled {{ background: {p['fill']}; color: {p['muted']}; }}
QPushButton#Flat {{ background: transparent; color: {p['muted']}; padding: 5px 8px; }}
QPushButton#Flat:hover {{ background: {p['fill']}; color: {p['text']}; }}
QPushButton#More {{ padding: 7px 11px; font-weight: 700; letter-spacing: 1px; }}
#Segmented {{ background: {p['fill']}; border-radius: 9px; }}
QPushButton#Seg {{ background: transparent; color: {p['muted']}; border-radius: 7px; padding: 5px 13px; }}
QPushButton#Seg:hover {{ color: {p['text']}; }}
QPushButton#Seg:checked {{ background: {p['card'] if p['scheme'] == 'light' else p['muted']};
    color: {p['text'] if p['scheme'] == 'light' else p['bg']}; font-weight: 600; }}
QProgressBar {{ background: {p['track']}; border: none; border-radius: 2px; max-height: 4px; min-height: 4px; }}
QProgressBar::chunk {{ background: {p['accent']}; border-radius: 2px; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 3px; }}
QScrollBar::handle:vertical {{ background: {p['fill_hover']}; border-radius: 3px; min-height: 36px; }}
QScrollBar::handle:vertical:hover {{ background: {p['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
#AchList {{ background: {p['card']}; border: 1px solid {p['hair']}; border-radius: 14px; outline: 0;
    padding: 2px 0; }}
#AchList::item, #AchList::item:selected, #AchList::item:hover {{ background: transparent; }}
#Muted, #Hint {{ color: {p['muted']}; }}
#Hint {{ font-size: 12px; }}
#Tiles {{ background: {p['card']}; border: 1px solid {p['hair']}; border-radius: 14px; }}
#TileRule {{ background: {p['hair']}; }}
#StatLabel {{ color: {p['muted']}; font-size: 11px; font-weight: 500; }}
#StatValue {{ color: {p['text']}; font-size: 18px; font-weight: 600; }}
#GameTitle {{ color: {p['text']}; font-size: 28px; font-weight: 700; }}
#GameSub {{ color: {p['muted']}; font-size: 13px; }}
#SectionLabel {{ color: {p['muted']}; font-size: 10px; font-weight: 600; letter-spacing: 0.8px; }}
#AchRow {{ background: transparent; border: none; border-bottom: 1px solid {p['hair']}; }}
#AchRow[last="true"] {{ border-bottom: none; }}
#AchName {{ color: {p['muted']}; font-size: 14px; font-weight: 600; }}
#AchRow[unlocked="true"] #AchName {{ color: {p['text']}; }}
#AchDesc {{ color: {p['muted']}; font-size: 12px; }}
#AchDate {{ color: {p['muted']}; font-size: 11.5px; }}
#AchPct {{ color: {p['muted']}; font-size: 11.5px; }}
#AchPct[rare="true"] {{ color: {p['text']}; background: {p['fill']}; border-radius: 9px; padding: 2px 8px;
    font-weight: 600; }}
#Empty {{ color: {p['muted']}; font-size: 15px; }}
#Running {{ color: {p['live']}; font-size: 12px; font-weight: 600; }}
#Tag {{ color: {p['muted']}; background: {p['fill']}; border-radius: 5px; padding: 1px 6px; font-size: 10px;
    font-weight: 600; }}
#Tag[kind="rare"] {{ color: {p['on_accent']}; background: {p['accent']}; }}
#ReviewRow {{ background: {p['card']}; border: 1px solid {p['hair']}; border-radius: 12px; }}
#ReviewRow[checked="true"] {{ border-color: {p['muted']}; }}
#Found {{ color: {p['live']}; font-size: 11.5px; }}
#SaveStatus {{ color: {p['muted']}; font-size: 11.5px; }}
QTabWidget::pane {{ border: 1px solid {p['hair']}; border-radius: 14px; top: -1px; background: {p['card']}; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {p['muted']}; padding: 6px 14px; margin: 0 2px 8px 0;
    border: none; border-radius: 8px; font-weight: 500; }}
QTabBar::tab:selected {{ background: {p['fill']}; color: {p['text']}; }}
QTabBar::tab:hover:!selected {{ color: {p['text2']}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; background: {p['field']};
    border: 1px solid {p['muted']}; }}
QCheckBox::indicator:checked {{ background: {p['accent']}; border-color: {p['accent']}; {tick_rule} }}
QToolTip {{ background: {p['card']}; color: {p['text']}; border: 1px solid {p['hair']}; border-radius: 6px;
    padding: 5px; }}
"""
