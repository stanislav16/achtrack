"""Library window: games, playtime and Steam-style achievement lists."""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime

from PySide6.QtCore import QDateTime, QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (QAction, QActionGroup, QBrush, QColor, QDesktopServices, QFont, QGuiApplication, QIcon,
                           QImage, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPalette, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDialog,
                               QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSplitter, QStackedWidget,
                               QTabWidget, QVBoxLayout, QWidget)

from . import __version__, desktop, theme
from .config import APP_ICON, APP_NAME, BACKUP_DIR, POPUP_CORNERS, Config
from .db import MANUAL, PSN, SAVE, SCREEN, STEAM, Database
from .engine import build_unlock_event, needs_review
from .notifier import ToastData, trophy_pixmap
from .scanner import detect_appid_in_tree, detect_steam_id
from .steam_api import cached_icon

REVIEW_MIN_SECONDS = 120   # don't ask after a game was only opened for a moment
POPUP_BURST = 5            # manual unlocks beyond this get one summary popup instead

RARE_PCT = 10.0
LANGUAGES = ["english", "german", "french", "italian", "spanish", "latam", "portuguese", "brazilian", "russian",
             "polish", "ukrainian", "bulgarian", "czech", "hungarian", "romanian", "turkish", "greek", "dutch",
             "swedish", "danish", "norwegian", "finnish", "japanese", "koreana", "schinese", "tchinese", "thai",
             "vietnamese", "indonesian", "arabic"]

P = theme.palette()          # current palette; swapped when the system switches light/dark


def accent() -> QColor:
    return QColor(P["accent"])


# formatting --------------------------------------------------------------

def fmt_playtime(seconds: int) -> str:
    if seconds < 60:
        return "Never" if seconds <= 0 else "< 1 minute"
    if seconds < 3600:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''}"
    return f"{seconds / 3600:.1f} hours"


def fmt_short_playtime(seconds: int) -> str:
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds / 3600:.1f} h"


def fmt_day(ts: int | None) -> str:
    if not ts:
        return "Never"
    day = datetime.fromtimestamp(ts).date()
    today = datetime.now().date()
    delta = (today - day).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    return day.strftime("%-d %b %Y") if day.year != today.year else day.strftime("%-d %b")


def fmt_unlock(ts: int) -> str:
    dt = datetime.fromtimestamp(ts)
    return f"Unlocked {dt.strftime('%-d %b %Y')} @ {dt.strftime('%-I:%M%p').lower()}"


def load_pixmap(path: str | None, size: int, gray: bool = False) -> QPixmap:
    pm = QPixmap(path) if path else QPixmap()
    if pm.isNull():
        pm = trophy_pixmap(size * 2)
    if gray:
        img = pm.toImage().convertToFormat(QImage.Format.Format_Grayscale8)
        pm = QPixmap.fromImage(img)
    return pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)


def detection_key(reason: str) -> str:
    """The field or screen text a detected unlock came from, used to stop matching it again."""
    quoted = re.search(r'"(.+)"\s*$', reason or "")
    if (reason or "").startswith("Seen on screen") and quoted:
        text = re.sub(r"\s+", " ", re.sub(r"[^0-9a-z' ]+", " ", quoted.group(1).lower())).strip()
        return f"screen:{text}"
    return (reason or "").split(" = ")[0]


PLATFORM_LABEL = {"pc": "PC", "ps5": "PS5", "ps4": "PS4", "ps3": "PS3", "psvita": "Vita"}


def platform_name(row) -> str:
    return PLATFORM_LABEL.get((row["platform"] or "pc").lower(), (row["platform"] or "PC").upper())


def sync_text(status: dict | None, what: str, unit: str) -> str:
    if not status:
        return "Not synced yet."
    when = fmt_day(status.get("time"))
    if status.get("error"):
        return f"<span style='color:#e5735c'>Last sync failed ({when}): {status['error']}</span>"
    return (f"Last sync {when.lower() if when in ('Today', 'Yesterday') else when}: "
            f"{status.get('games', 0)} games, {status.get(what, 0)} {unit}.")


def device_sync_text(status: dict | None) -> str:
    if not status:
        return "Not syncing with other machines yet."
    if status.get("error"):
        return f"<span style='color:#e5735c'>Last sync failed: {status['error']}</span>"
    parts = [f"{status.get('devices', 0)} other machine(s)"]
    for field, label in (("games", "new games"), ("unlocks", "new unlocks"), ("removed", "removals")):
        if status.get(field):
            parts.append(f"{status[field]} {label}")
    return f"Last sync {fmt_day(status.get('time')).lower()}: " + ", ".join(parts) + "."


def backup_text(status: dict | None) -> str:
    if not status:
        return "No backup yet."
    if status.get("error"):
        return f"<span style='color:#e5735c'>Last backup failed: {status['error']}</span>"
    return f"Last backup {fmt_day(status.get('time')).lower()} in {status.get('dir', '')}"


def export_text(status: dict | None) -> str:
    if not status:
        return "The phone page hasn't been built yet."
    if status.get("error"):
        return f"<span style='color:#e5735c'>Publishing failed: {status['error']}</span>"
    if status.get("url"):
        return f"Published: <a href='{status['url']}' style='color:#66c0f4'>{status['url']}</a>"
    return f"Built: {status.get('page', '')} (add a Vercel token to publish it)"


def steam_sync_text(status: dict | None) -> str:
    if not status:
        return "Not synced yet."
    when = fmt_day(status.get("time"))
    if status.get("error"):
        return f"<span style='color:#e5735c'>Last sync failed ({when}): {status['error']}</span>"
    return (f"Last sync {when.lower() if when in ('Today', 'Yesterday') else when}: {status.get('games', 0)} played "
            f"games, {status.get('achievements', 0)} Steam achievements.")


def likelihood(a: dict, hours: float) -> float:
    """Rough chance the player has this: common ones come early, rare ones need hours in the game."""
    pct = a.get("global_pct")
    if pct is None:
        return -1.0
    return pct if pct >= 30 else pct * min(1.0, 0.3 + hours / 20)


def parse_appid(text: str) -> int | None:
    m = re.search(r"/app/(\d+)", text)
    digits = m.group(1) if m else next(iter(re.findall(r"\d+", text)), None)
    return int(digits) if digits and int(digits) > 0 else None


# widgets -----------------------------------------------------------------

class MenuButton(QPushButton):
    """A pop-up button: the current choice plus a menu. Qt's combo popups misbehave on Wayland,
    and menus don't."""

    def __init__(self, options: list, current, on_change, width: int = 0):
        super().__init__()
        self.options = options              # [(label, value)]
        self.on_change = on_change
        self.value = current if current in [v for _l, v in options] else options[0][1]
        self._menu = QMenu(self)
        group = QActionGroup(self._menu)
        self.actions_by_value = {}
        for label, value in options:
            action = self._menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(value == self.value)
            action.triggered.connect(lambda _=False, v=value: self._choose(v))
            group.addAction(action)
            self.actions_by_value[value] = action
        self.setMenu(self._menu)
        if width:
            self.setMinimumWidth(width)
        self._sync_text()

    def _sync_text(self) -> None:
        label = next(l for l, v in self.options if v == self.value)
        self.setText(f"{label}  ⌄")

    def _choose(self, value) -> None:
        self.value = value
        for v, action in self.actions_by_value.items():
            action.setChecked(v == value)
        self._sync_text()
        self.on_change(value)


def letter_pixmap(name: str, size: int) -> QPixmap:
    """Stand-in artwork: the game's initial on a plain tile."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(P["sel"]))
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.26, size * 0.26)
    font = p.font()
    font.setPixelSize(int(size * 0.5))
    font.setWeight(QFont.Weight.DemiBold)
    p.setFont(font)
    p.setPen(QColor(P["muted"]))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, (name or "?").strip()[:1].upper())
    p.end()
    return pm


def game_thumbnail(appid: int, row, size: int = 40) -> QPixmap:
    art = QPixmap(cached_icon(appid, row["header_url"] or "") or "")
    if art.isNull():
        return letter_pixmap(row["name"] or "", size)
    scaled = art.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    crop = scaled.copy((scaled.width() - size) // 2, (scaled.height() - size) // 2, size, size)
    return rounded(crop, int(size * 0.26))


class Dot(QWidget):
    """Small accent dot marking the game you're playing."""

    def __init__(self, size: int = 8):
        super().__init__()
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent())
        p.drawEllipse(self.rect())


class GameRow(QWidget):
    def __init__(self, row, running: bool, icon: bool = True):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10 if icon else 14, 8, 12, 8)
        outer.setSpacing(10)
        if icon:
            thumb = QLabel()
            thumb.setFixedSize(40, 40)
            thumb.setPixmap(game_thumbnail(row["appid"], row))
            outer.addWidget(thumb, 0, Qt.AlignmentFlag.AlignVCenter)
        lay = QVBoxLayout()
        lay.setSpacing(3)
        name = QLabel(row["name"] or f"App {row['appid']}")
        name.setStyleSheet(f"color: {P['text'] if running else P['text2']}; font-size: 13.5px; font-weight: 500;"
                           " background: transparent;")
        lay.addWidget(name)
        parts = []
        if (row["platform"] or "pc") != "pc":
            parts.append(platform_name(row))
        parts.append(fmt_short_playtime(row["total_playtime"]))
        if row["total"]:
            parts.append(f"{min(row['unlocked'], row['total'])}/{row['total']}")
        elif row["unlocked"]:
            parts.append(str(row["unlocked"]))
        sub = QLabel("     ".join(parts))
        sub.setStyleSheet(f"color: {P['muted']}; font-size: 11.5px; background: transparent;")
        lay.addWidget(sub)
        outer.addLayout(lay, 1)
        if running:
            outer.addWidget(Dot(), 0, Qt.AlignmentFlag.AlignVCenter)


class AchievementRow(QFrame):
    def __init__(self, appid: int, a: dict, reveal: bool = False):
        super().__init__()
        unlocked = bool(a.get("unlocked_at"))
        self.setObjectName("AchRow")
        self.setProperty("unlocked", unlocked)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 9, 16, 9)
        lay.setSpacing(14)

        icon = QLabel()
        icon.setFixedSize(64, 64)
        if unlocked:
            icon.setPixmap(load_pixmap(cached_icon(appid, a.get("icon") or ""), 64))
        else:
            gray_path = cached_icon(appid, a.get("icon_gray") or "")
            icon.setPixmap(load_pixmap(gray_path or cached_icon(appid, a.get("icon") or ""), 64, gray=not gray_path))
        pct = a.get("global_pct")
        if pct is not None and pct < RARE_PCT:
            icon.setStyleSheet(f"border: 1px solid {P['accent']}; border-radius: 12px;")
        lay.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        texts = QVBoxLayout()
        texts.setSpacing(3)
        hidden_locked = a.get("hidden") and not unlocked and not reveal
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        name = QLabel("Hidden achievement" if hidden_locked else (a.get("display_name") or a["name"]))
        name.setObjectName("AchName")
        title_row.addWidget(name)
        if a.get("kind") in ("platinum", "gold"):
            title_row.addWidget(_tag(a["kind"].upper(), "rare" if a["kind"] == "platinum" else "plain"))
        title_row.addStretch(1)
        texts.addLayout(title_row)
        desc_text = ("Details for this achievement will be revealed once unlocked" if hidden_locked
                     else a.get("description") or "")
        if desc_text:
            desc = QLabel(desc_text)
            desc.setObjectName("AchDesc")
            desc.setWordWrap(True)
            texts.addWidget(desc)
        if not unlocked and a.get("max"):
            prog = QHBoxLayout()
            bar = QProgressBar()
            bar.setTextVisible(False)
            bar.setRange(0, int(a["max"]))
            bar.setValue(int(min(a.get("cur") or 0, a["max"])))
            bar.setMaximumWidth(260)
            prog.addWidget(bar)
            lbl = QLabel(f"{int(a.get('cur') or 0):,} / {int(a['max']):,}")
            lbl.setObjectName("AchPct")
            prog.addWidget(lbl)
            prog.addStretch(1)
            texts.addLayout(prog)
        texts.addStretch(1)
        lay.addLayout(texts, 1)

        right = QVBoxLayout()
        right.setSpacing(4)
        if unlocked:
            source = a.get("source")
            tag = {STEAM: "  ·  Steam", PSN: "  ·  PlayStation", MANUAL: "  ·  Added by you",
                   SAVE: "  ·  Found in your save", SCREEN: "  ·  Seen on screen"}.get(source, "")
            date = QLabel(fmt_unlock(a["unlocked_at"]) + tag)
            date.setObjectName("AchDate")
            date.setAlignment(Qt.AlignmentFlag.AlignRight)
            right.addWidget(date)
        if pct is not None:
            label = QLabel(f"{pct:.0f}%" if pct >= 10 else f"{pct:.1f}%")
            label.setObjectName("AchPct")
            label.setProperty("rare", pct < RARE_PCT)
            label.setAlignment(Qt.AlignmentFlag.AlignRight)
            right.addWidget(label)
        right.addStretch(1)
        lay.addLayout(right)


class HiddenSummaryRow(QFrame):
    def __init__(self, count: int):
        super().__init__()
        self.setObjectName("AchRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 14, 16, 14)
        text = QLabel(f"<b style='color:#e7eef4'>+{count} hidden achievement{'s' if count != 1 else ''} remaining</b>"
                      "<br><span style='color:#7f8b98'>Details for each achievement will be revealed once unlocked</span>")
        lay.addWidget(text)


class Stat(QWidget):
    """One "LABEL      value" line, the way a settings row reads."""

    def __init__(self, label: str):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        cap = QLabel(label.upper())
        cap.setObjectName("StatLabel")
        cap.setFixedWidth(116)
        self.value = QLabel("-")
        self.value.setObjectName("StatValue")
        lay.addWidget(cap, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.value, 1, Qt.AlignmentFlag.AlignVCenter)


BANNER_W, BANNER_H = 300, 140
WASH_H = 300


def rounded(pm: QPixmap, radius: int) -> QPixmap:
    out = QPixmap(pm.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, pm.width(), pm.height()), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


def gaussian(pm: QPixmap, radius: float) -> QPixmap:
    """A real Gaussian blur - stretching a tiny thumbnail instead leaves visible blocks."""
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(pm)
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius)
    effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    out = QImage(pm.size(), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    scene.render(painter, QRectF(0, 0, pm.width(), pm.height()), QRectF(0, 0, pm.width(), pm.height()))
    painter.end()
    return QPixmap.fromImage(out)


def blurred_wash(pm: QPixmap, width: int, height: int) -> QPixmap:
    """The game's art, blurred to a soft colour field, fading out downwards - the page's tint."""
    # Blur at a workable size, then scale up smoothly: sharp enough to avoid banding, cheap enough
    # to redo whenever you switch games.
    stage = pm.scaled(480, 240, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    blurred = gaussian(stage, 42)
    wash = blurred.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.SmoothTransformation).toImage().convertToFormat(
        QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(wash)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    fade = QLinearGradient(0, 0, 0, height)
    fade.setColorAt(0.0, QColor(0, 0, 0, 90))
    fade.setColorAt(0.35, QColor(0, 0, 0, 48))
    fade.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(0, 0, width, height, QBrush(fade))
    p.end()
    return QPixmap.fromImage(wash)


class DetailView(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.win = window
        self.appid: int | None = None
        self._sig = None
        self._wash: QPixmap | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(20)
        self.banner = QLabel()
        self.banner.setFixedSize(BANNER_W, BANNER_H)
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.banner, 0, Qt.AlignmentFlag.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(6)
        self.title = QLabel()
        self.title.setObjectName("GameTitle")
        self.title.setWordWrap(True)
        info.addWidget(self.title)
        playing = QHBoxLayout()
        playing.setSpacing(7)
        self.running_dot = Dot(7)
        self.running = QLabel("Playing now")
        self.running.setObjectName("Running")
        playing.addWidget(self.running_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        playing.addWidget(self.running, 0, Qt.AlignmentFlag.AlignVCenter)
        playing.addStretch(1)
        info.addLayout(playing)
        info.addSpacing(10)
        stats = QVBoxLayout()
        stats.setSpacing(7)
        self.stat_time = Stat("Play time")
        self.stat_last = Stat("Last played")
        self.stat_ach = Stat("Unlocked")
        self.stat_genre = Stat("Genre")
        self.stat_platform = Stat("Platform")
        for s in (self.stat_time, self.stat_last, self.stat_ach, self.stat_genre, self.stat_platform):
            stats.addWidget(s)
        info.addLayout(stats)
        info.addSpacing(6)
        prog = QHBoxLayout()
        prog.setSpacing(12)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumWidth(460)
        prog.addWidget(self.progress)
        self.progress_label = QLabel()
        self.progress_label.setObjectName("Muted")
        prog.addWidget(self.progress_label)
        prog.addStretch(1)
        info.addLayout(prog)
        self.save_status = QLabel()
        self.save_status.setObjectName("SaveStatus")
        self.save_status.setWordWrap(True)
        info.addWidget(self.save_status)
        info.addStretch(1)
        header.addLayout(info, 1)
        root.addLayout(header)

        bar = QHBoxLayout()
        self.filter = MenuButton([("All achievements", 0), ("Unlocked", 1), ("Locked", 2)], 0,
                                 lambda _v: self.refresh(force=True))
        bar.addWidget(self.filter)
        self.ach_sort = MenuButton([("Unlocked first", "default"), ("Rarest first", "rarest"),
                                    ("Most common first", "common"), ("Recently unlocked", "recent"),
                                    ("Name (A-Z)", "name")],
                                   window.cfg.achievement_sort, self._ach_sort_changed)
        bar.addWidget(self.ach_sort)
        self.reveal = QCheckBox("Reveal hidden achievements")
        self.reveal.toggled.connect(lambda _: self.refresh(force=True))
        bar.addWidget(self.reveal)
        bar.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search achievements")
        self.search.setMaximumWidth(260)
        self.search.textChanged.connect(lambda _: self.refresh(force=True))
        bar.addWidget(self.search)
        review = QPushButton("Review achievements…")
        review.setToolTip("Tick achievements you've earned that weren't recorded")
        review.clicked.connect(lambda: self.appid and self.win.open_review(self.appid))
        bar.addWidget(review)
        refresh = QPushButton("Refresh from Steam")
        refresh.clicked.connect(lambda: self.appid and self.win.request_refresh(self.appid))
        bar.addWidget(refresh)
        root.addLayout(bar)

        self.notice = QLabel()
        self.notice.setObjectName("Hint")
        self.notice.setWordWrap(True)
        self.notice.setOpenExternalLinks(False)
        self.notice.linkActivated.connect(lambda _: self.win.open_settings())
        root.addWidget(self.notice)

        section = QLabel("ACHIEVEMENTS")
        section.setObjectName("SectionLabel")
        root.addWidget(section)

        self.list = QListWidget()
        self.list.setObjectName("AchList")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setSpacing(3)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._row_menu)
        root.addWidget(self.list, 1)

    def _ach_sort_changed(self, value: str) -> None:
        self.win.cfg.achievement_sort = value
        self.win.cfg.save()
        self.refresh(force=True)

    def paintEvent(self, event) -> None:
        if self._wash is not None:
            QPainter(self).drawPixmap(0, 0, self._wash)
        super().paintEvent(event)

    def show_game(self, appid: int | None) -> None:
        if appid != self.appid:
            self.appid = appid
            self._sig = None
            self.list.scrollToTop()
            if appid is not None:
                self.win.worker.request("icons", appid)
        self.refresh()

    def _save_status(self, db) -> str:
        if not self.win.cfg.save_detection:
            return ""
        files = db.save_files(self.appid)
        readable = [f for f in files if f["readable"]]
        pending = len(db.suggestions(self.appid))
        extra = f"  ·  {pending} possible achievement{'s' if pending != 1 else ''} to review" if pending else ""
        lines = []
        saves = [f for f in files if not f["path"].lower().endswith("user.reg")]
        readable_saves = [f for f in saves if f["readable"]]
        registry = any(f["readable"] for f in files if f["path"].lower().endswith("user.reg"))
        if readable_saves:
            formats = sorted({f["format"] for f in readable_saves if f["format"]})
            lines.append(f"💾 Save file readable ({', '.join(formats)}) - checked for progress each time "
                         f"the game saves{extra}")
        elif saves:
            lines.append("💾 Save file uses a custom format AchTrack can't read"
                         + (" (only the game's registry settings are readable)" if registry else "") + extra)
        elif registry:
            lines.append("💾 Only the game's registry settings are readable - no save file found yet" + extra)
        elif extra:
            lines.append(extra.strip(" ·"))
        return "\n".join(lines)

    def _row_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        a = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not a or self.appid is None:
            return
        appid, name = self.appid, a["name"]
        menu = QMenu(self)
        if not a.get("unlocked_at"):
            menu.addAction("Mark as unlocked", lambda: self.win.manual_unlock(appid, [name], int(time.time())))
            menu.addAction("Mark as unlocked on…", lambda: self.win.manual_unlock_on(appid, name))
        elif a.get("source") == MANUAL:
            menu.addAction("Change unlock date…", lambda: self.win.change_unlock_date(appid, name, a["unlocked_at"]))
            menu.addAction("Undo - mark as locked", lambda: self.win.remove_manual_unlock(appid, name))
        elif a.get("source") in (SAVE, SCREEN):
            note = self.win.db.unlock_note(appid, name) or ""
            source = a.get("source")
            label = note if source == SCREEN else f"Found in save: {note}"
            info = menu.addAction(label or "Detected automatically")
            info.setEnabled(False)
            menu.addAction("Not earned - undo and don't match this again",
                           lambda: self.win.reject_detected_unlock(appid, name, source))
        else:
            info = menu.addAction(f"Unlocked via {a.get('source') or 'emulator'}")
            info.setEnabled(False)
        menu.exec(self.list.mapToGlobal(pos))

    def refresh(self, force: bool = False) -> None:
        if self.appid is None:
            return
        db = self.win.db
        game = db.game(self.appid)
        if game is None:
            return
        rows = db.achievements(self.appid)
        schema_total = db.q("SELECT COUNT(*) FROM achievements WHERE appid = ?", (self.appid,))[0][0]
        unlocked = sum(1 for r in rows if r.get("unlocked_at"))
        total = max(schema_total, unlocked)

        self.title.setText(game["name"] or f"App {self.appid}")
        for widget in (self.running, self.running_dot):
            widget.setVisible(self.appid in self.win.running)
        total_time = game["total_playtime"] if "total_playtime" in game.keys() else (
            game["playtime"] + game["steam_playtime"] + game["psn_playtime"])
        self.stat_time.value.setText(fmt_playtime(total_time))
        sources = [(f"Steam: {fmt_playtime(game['steam_playtime'])}", game["steam_playtime"]),
                   (f"PlayStation: {fmt_playtime(game['psn_playtime'])}", game["psn_playtime"]),
                   (f"Tracked by {APP_NAME}: {fmt_playtime(game['playtime'])}", game["playtime"])]
        shown = [text for text, value in sources if value]
        self.stat_time.setToolTip("\n".join(shown) if len(shown) > 1 else "")
        self.stat_platform.value.setText(platform_name(game))
        self.stat_platform.setVisible((game["platform"] or "pc") != "pc")
        last = max(game["last_played"] or 0, game["steam_last_played"] or 0, game["psn_last_played"] or 0)
        self.stat_last.value.setText("Now" if self.appid in self.win.running else fmt_day(last))
        self.stat_ach.value.setText(f"{unlocked} / {total}" if total else "-")
        self.stat_genre.value.setText(game["genre"] or "")
        self.stat_genre.setVisible(bool(game["genre"]))
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(unlocked)
        self.progress_label.setText(f"{unlocked * 100 // total}%" if total else "")
        self.save_status.setText(self._save_status(db))
        banner_path = cached_icon(self.appid, game["header_url"] or "")
        art = QPixmap(banner_path) if banner_path else QPixmap()
        if not art.isNull():
            pm = art.scaled(self.banner.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
            crop = pm.copy((pm.width() - BANNER_W) // 2, (pm.height() - BANNER_H) // 2, BANNER_W, BANNER_H)
            self.banner.setPixmap(rounded(crop, 14))
            self._wash = blurred_wash(art, max(self.width(), 600), WASH_H)
        else:
            self.banner.setPixmap(QPixmap())
            self._wash = None
        self.update()

        sync = db.get_meta("steam_sync") or {}
        if game["steam_owned"] and sync.get("error"):
            self.notice.setText(f"<span style='color:#e5735c'>Steam sync problem:</span> {sync['error']} "
                                "<a href='#' style='color:#66c0f4'>Open Settings</a>")
            self.notice.show()
        elif not schema_total:
            if not self.win.cfg.steam_api_key:
                self.notice.setText("Achievement names and icons come from Steam. "
                                    "<a href='#' style='color:#66c0f4'>Add your free Steam Web API key in Settings</a> "
                                    "to see this game's achievements.")
            else:
                self.notice.setText("No achievement data from Steam yet for this game. "
                                    "It may have no achievements, or Steam couldn't be reached - try Refresh.")
            self.notice.show()
        elif (unlocked < total and game["playtime"] >= 1800 and db.sources_for(self.appid)
              and not db.emulator_unlock_count(self.appid)):
            self.notice.setText("This game's crack hasn't recorded any achievements yet, so they won't unlock by "
                                "themselves. Use <b>Review achievements…</b> or right-click an achievement to add "
                                "what you've earned.")
            self.notice.show()
        else:
            self.notice.hide()

        icons = tuple(bool(cached_icon(self.appid, r.get("icon") or "")) for r in rows)
        query = self.search.text().strip().lower()
        sig = (repr(rows), icons, self.filter.value, self.win.cfg.achievement_sort, self.reveal.isChecked(),
               bool(banner_path), query)
        if sig == self._sig and not force:
            return
        self._sig = sig
        scroll = self.list.verticalScrollBar().value()
        self.list.setUpdatesEnabled(False)
        self.list.clear()

        done = [r for r in rows if r.get("unlocked_at")]
        locked = [r for r in rows if not r.get("unlocked_at")]
        order = self.win.cfg.achievement_sort
        if order == "rarest":
            key = lambda r: (r.get("global_pct") is None, r.get("global_pct") or 0)          # noqa: E731
            done, locked = sorted(done, key=key), sorted(locked, key=key)
        elif order == "common":
            key = lambda r: (r.get("global_pct") is None, -(r.get("global_pct") or 0))       # noqa: E731
            done, locked = sorted(done, key=key), sorted(locked, key=key)
        elif order == "name":
            key = lambda r: (r.get("display_name") or r["name"]).lower()                     # noqa: E731
            done, locked = sorted(done, key=key), sorted(locked, key=key)
        else:
            done.sort(key=lambda r: -r["unlocked_at"])
            locked.sort(key=lambda r: -(r.get("global_pct") or 0))
        mode = self.filter.value
        if order == "recent":
            shown = sorted(done, key=lambda r: -r["unlocked_at"]) if mode != 2 else []
            shown += locked if mode != 1 else []
        else:
            shown = (done if mode != 2 else []) + (locked if mode != 1 else [])
        reveal = self.reveal.isChecked()
        hidden_left = 0
        for r in shown:
            secret = r.get("hidden") and not r.get("unlocked_at") and not reveal
            if query and query not in (f"{r.get('display_name') or r['name']} "
                                       f"{'' if secret else r.get('description') or ''}").lower():
                continue
            if secret and not query:
                hidden_left += 1
                continue
            self._add_row(AchievementRow(self.appid, r, reveal or bool(query)), r)
        if hidden_left:
            self._add_row(HiddenSummaryRow(hidden_left))
        self.list.setUpdatesEnabled(True)
        QTimer.singleShot(0, lambda: self.list.verticalScrollBar().setValue(scroll))

    def _add_row(self, widget: QWidget, data: dict | None = None) -> None:
        item = QListWidgetItem()
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setData(Qt.ItemDataRole.UserRole, data)
        widget.adjustSize()
        item.setSizeHint(QSize(0, max(widget.sizeHint().height(), 82)))
        self.list.addItem(item)
        self.list.setItemWidget(item, widget)


# achievement review ------------------------------------------------------

def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _tag(text: str, kind: str) -> QLabel:
    tag = QLabel(text)
    tag.setObjectName("Tag")
    tag.setProperty("kind", kind)
    return tag


class ReviewRow(QFrame):
    """One locked achievement with a checkbox; clicking anywhere on the row toggles it."""

    def __init__(self, appid: int, a: dict, found: str | None = None):
        super().__init__()
        self.name = a["name"]
        self.found = found
        self.search_text = f"{a.get('display_name') or ''} {a.get('description') or ''}".lower()
        self.setObjectName("ReviewRow")
        self.setProperty("checked", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 14, 8)
        lay.setSpacing(12)
        self.check = QCheckBox()
        self.check.toggled.connect(self._toggled)
        lay.addWidget(self.check)
        icon = QLabel()
        icon.setFixedSize(48, 48)
        gray = cached_icon(appid, a.get("icon_gray") or "")
        icon.setPixmap(load_pixmap(gray or cached_icon(appid, a.get("icon") or ""), 48, gray=not gray))
        lay.addWidget(icon)

        texts = QVBoxLayout()
        texts.setSpacing(3)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        name = QLabel(a.get("display_name") or a["name"])
        name.setObjectName("AchName")
        title_row.addWidget(name)
        pct = a.get("global_pct")
        if a.get("hidden"):
            title_row.addWidget(_tag("HIDDEN", "hidden"))
        if pct is not None and pct < RARE_PCT:
            title_row.addWidget(_tag("RARE", "rare"))
        title_row.addStretch(1)
        texts.addLayout(title_row)
        desc = QLabel(a.get("description") or ("Hidden achievement - Steam doesn't publish its description"
                                               if a.get("hidden") else ""))
        desc.setObjectName("AchDesc")
        desc.setWordWrap(True)
        texts.addWidget(desc)
        if found:
            hit = QLabel(found if found.startswith("Seen on screen") else f"Found in your save: {found}")
            hit.setObjectName("Found")
            hit.setWordWrap(True)
            texts.addWidget(hit)
        lay.addLayout(texts, 1)

        if pct is not None:
            label = QLabel(f"{pct:.1f}%")
            label.setObjectName("AchPct")
            label.setProperty("rare", pct < RARE_PCT)
            label.setToolTip("Share of Steam players who have this achievement")
            lay.addWidget(label)

    def _toggled(self, checked: bool) -> None:
        self.setProperty("checked", checked)
        _repolish(self)

    def mousePressEvent(self, event) -> None:
        self.check.toggle()


class ReviewDialog(QDialog):
    """'Did you earn any of these?' - for games whose crack doesn't record achievements."""

    def __init__(self, win: "MainWindow", appid: int, session_seconds: int = 0):
        super().__init__(win)
        self.win = win
        self.appid = appid
        db = win.db
        game = db.game(appid)
        name = game["name"] or f"App {appid}"
        total_seconds = game["playtime"] + game["steam_playtime"]
        self.setWindowTitle(f"Review achievements - {name}")
        self.resize(760, 760)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.setSpacing(10)

        title = QLabel("Did you earn any of these?")
        title.setStyleSheet("color: white; font-size: 22px; font-weight: 700;")
        lay.addWidget(title)
        parts = [name]
        if session_seconds:
            parts.append(f"played {fmt_playtime(session_seconds)} this session")
        parts.append(f"{fmt_playtime(total_seconds)} total")
        sub = QLabel("  ·  ".join(parts))
        sub.setObjectName("Muted")
        lay.addWidget(sub)
        self.suggested = db.suggestions(appid)
        hint = QLabel(("Ticked ones were spotted in your save file or on screen - untick any you didn't earn. "
                       if self.suggested
                       else "") +
                      "Tick the ones you've done. The rest is sorted by how likely you are to have each one, based on "
                      "how many Steam players have it and how long you've played. Hidden achievements are included.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search achievements")
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)

        self.list = QListWidget()
        self.list.setObjectName("AchList")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setSpacing(2)
        lay.addWidget(self.list, 1)
        hours = total_seconds / 3600
        locked = [a for a in db.achievements(appid) if not a.get("unlocked_at")]
        locked.sort(key=lambda a: (a["name"] not in self.suggested, -likelihood(a, hours)))
        self.rows = []
        for a in locked:
            row = ReviewRow(appid, a, self.suggested.get(a["name"]))
            row.check.setChecked(a["name"] in self.suggested)
            row.check.toggled.connect(self._update_count)
            item = QListWidgetItem()
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            row.adjustSize()
            item.setSizeHint(QSize(0, max(row.sizeHint().height(), 66)))
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            self.rows.append((item, row))

        bottom = QHBoxLayout()
        self.dont_ask = QCheckBox("Don't ask after sessions of this game")
        self.dont_ask.setChecked(game["review"] == 0)
        bottom.addWidget(self.dont_ask)
        bottom.addStretch(1)
        later = QPushButton("Not now")
        later.clicked.connect(self._close)
        bottom.addWidget(later)
        self.unlock = QPushButton()
        self.unlock.setObjectName("Primary")
        self.unlock.clicked.connect(self._unlock)
        bottom.addWidget(self.unlock)
        lay.addLayout(bottom)
        self._update_count()

    def _filter(self, text: str) -> None:
        query = text.strip().lower()
        for item, row in self.rows:
            item.setHidden(bool(query) and query not in row.search_text)

    def _checked(self) -> list:
        return [row.name for _item, row in self.rows if row.check.isChecked()]

    def _update_count(self, *_args) -> None:
        count = len(self._checked())
        self.unlock.setText(f"Unlock {count} achievement{'s' if count != 1 else ''}" if count else "Unlock")
        self.unlock.setEnabled(bool(count))

    def _save_preference(self) -> None:
        game = self.win.db.game(self.appid)
        if self.dont_ask.isChecked():
            self.win.db.update_game(self.appid, review=0)
        elif game["review"] == 0:
            self.win.db.update_game(self.appid, review=None)

    def _close(self) -> None:
        self._save_preference()
        self.reject()

    def _unlock(self) -> None:
        self._save_preference()
        checked = set(self._checked())
        db = self.win.db
        for _item, row in self.rows:
            if row.found and row.name not in checked:
                # Unticked a save suggestion: don't suggest it from that field again.
                db.reject(self.appid, row.name, detection_key(row.found))
                db.drop_suggestion(self.appid, row.name)
        self.win.manual_unlock(self.appid, [n for n in self._checked()], int(time.time()),
                               notes={r.name: f"Confirmed from save: {r.found}" for _i, r in self.rows if r.found})
        self.accept()


class UnlockDateDialog(QDialog):
    def __init__(self, parent, title: str, ts: int | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("When did you unlock it?"))
        self.when = QDateTimeEdit(QDateTime.fromSecsSinceEpoch(ts) if ts else QDateTime.currentDateTime())
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("d MMM yyyy  h:mm AP")
        self.when.setMaximumDateTime(QDateTime.currentDateTime())
        lay.addWidget(self.when)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def timestamp(self) -> int:
        return int(self.when.dateTime().toSecsSinceEpoch())


# dialogs -----------------------------------------------------------------

class GameDialog(QDialog):
    def __init__(self, parent, db: Database, appid: int | None = None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Edit game" if appid else "Add a game")
        self.setMinimumWidth(560)
        game = db.game(appid) if appid else None
        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self.appid = QLineEdit(str(appid) if appid else "")
        self.appid.setPlaceholderText("e.g. 367520 or https://store.steampowered.com/app/367520/")
        self.appid.setEnabled(appid is None)
        form.addRow("Steam AppID", self.appid)

        folder_row = QHBoxLayout()
        self.folder = QLineEdit(game["game_dir"] or "" if game else "")
        self.folder.setPlaceholderText("Folder containing the game (optional)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_folder)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        form.addRow("Game folder", folder_row)

        exe_row = QHBoxLayout()
        self.exe = QLineEdit(game["exe_name"] or "" if game else "")
        self.exe.setPlaceholderText("Game.exe  (separate several with ;)")
        browse_exe = QPushButton("Browse…")
        browse_exe.clicked.connect(self._pick_exe)
        exe_row.addWidget(self.exe, 1)
        exe_row.addWidget(browse_exe)
        form.addRow("Executable", exe_row)
        lay.addLayout(form)

        hint = QLabel("Games with a steam_appid.txt or crack config are detected automatically when launched. "
                      "Set the executable if playtime isn't being tracked. You can also launch any game with "
                      "<code>ACHTRACK_APPID=&lt;appid&gt;</code> in its environment.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("Primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.saved_appid: int | None = None

    def _pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Game folder", self.folder.text() or os.path.expanduser("~"))
        if path:
            self.folder.setText(path)
            self._autofill_appid(path)

    def _pick_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Game executable", self.folder.text() or os.path.expanduser("~"))
        if path:
            self.exe.setText(os.path.basename(path))
            if not self.folder.text():
                self.folder.setText(os.path.dirname(path))
                self._autofill_appid(os.path.dirname(path))

    def _autofill_appid(self, folder: str) -> None:
        if self.appid.isEnabled() and not self.appid.text().strip():
            found = detect_appid_in_tree(folder)
            if found:
                self.appid.setText(str(found))

    def _save(self) -> None:
        appid = parse_appid(self.appid.text())
        if not appid:
            QMessageBox.warning(self, APP_NAME, "Enter the game's Steam AppID (the number in its store URL).")
            return
        self.db.ensure_game(appid)
        self.db.update_game(appid, hidden=0)
        self.db.x("UPDATE games SET exe_name = ?, game_dir = ? WHERE appid = ?",
                  (self.exe.text().strip() or None, self.folder.text().strip() or None, appid))
        self.saved_appid = appid
        self.accept()


class SettingsDialog(QDialog):
    """Grouped into tabs: one screen's worth of settings per topic."""

    def __init__(self, parent: "MainWindow"):
        super().__init__(parent)
        self.win = parent
        cfg = parent.cfg
        self.setWindowTitle(f"{APP_NAME} settings")
        self.setMinimumSize(660, 520)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(12)
        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        # --- Accounts -------------------------------------------------------
        accounts = self._page(tabs, "Accounts")
        key_row = QHBoxLayout()
        self.key = QLineEdit(cfg.steam_api_key)
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("32-character key")
        get_key = QPushButton("Get a key")
        get_key.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://steamcommunity.com/dev/apikey")))
        key_row.addWidget(self.key, 1)
        key_row.addWidget(get_key)
        accounts.addRow("Steam Web API key", key_row)

        steam_row = QHBoxLayout()
        self.steam_id = QLineEdit(cfg.steam_id)
        detected = detect_steam_id()
        self.steam_id.setPlaceholderText(f"Detected: {detected}" if detected else "Profile URL or SteamID64")
        sync_now = QPushButton("Sync now")
        sync_now.clicked.connect(self._sync_now)
        steam_row.addWidget(self.steam_id, 1)
        steam_row.addWidget(sync_now)
        accounts.addRow("Steam account", steam_row)
        self.steam_sync = QCheckBox("Copy my Steam games, playtime and achievements into my library")
        self.steam_sync.setChecked(cfg.steam_sync)
        accounts.addRow("", self.steam_sync)
        self.sync_status = self._status(accounts, steam_sync_text(parent.db.get_meta("steam_sync")))

        psn_row = QHBoxLayout()
        self.psn = QLineEdit(cfg.psn_npsso)
        self.psn.setEchoMode(QLineEdit.EchoMode.Password)
        self.psn.setPlaceholderText("NPSSO token from your browser")
        psn_help = QPushButton("Get one")
        psn_help.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://ca.account.sony.com/api/v1/ssocookie")))
        psn_now = QPushButton("Sync now")
        psn_now.clicked.connect(self._psn_now)
        psn_row.addWidget(self.psn, 1)
        psn_row.addWidget(psn_help)
        psn_row.addWidget(psn_now)
        accounts.addRow("PlayStation login", psn_row)
        self.psn_status = self._status(accounts, sync_text(parent.db.get_meta("psn_sync"), "trophies", "trophies"))
        self.psn_describe = QCheckBox("Describe hidden achievements from PlayStation trophy lists")
        self.psn_describe.setChecked(cfg.psn_describe_hidden)
        self.psn_describe.setToolTip("Steam never reveals what a hidden achievement asks for. PlayStation "
                                     "spells every trophy out, so the same game's trophy list can fill in "
                                     "the blanks.")
        accounts.addRow("", self.psn_describe)

        self.language = QComboBox()
        self.language.setEditable(True)
        self.language.addItems(LANGUAGES)
        self.language.setCurrentText(cfg.language)
        accounts.addRow("Achievement language", self.language)

        # --- Popups ---------------------------------------------------------
        popups = self._page(tabs, "Popups")
        self.style = QComboBox()
        self.style.addItem("AchTrack popup", "steam")
        self.style.addItem("Desktop notification", "system")
        self.style.addItem("Both", "both")
        self.style.addItem("Off", "none")
        self.style.setCurrentIndex(max(0, self.style.findData(cfg.popup_style)))
        popups.addRow("Unlock popups", self.style)
        self.corner = QComboBox()
        for corner in POPUP_CORNERS:
            self.corner.addItem(corner.replace("-", " ").capitalize(), corner)
        self.corner.setCurrentIndex(max(0, self.corner.findData(cfg.popup_corner)))
        popups.addRow("Position", self.corner)
        self.seconds = QDoubleSpinBox()
        self.seconds.setRange(2, 20)
        self.seconds.setSingleStep(0.5)
        self.seconds.setSuffix(" s")
        self.seconds.setValue(cfg.popup_seconds)
        popups.addRow("Duration", self.seconds)
        self.sound = QCheckBox("Play a sound when an achievement unlocks")
        self.sound.setChecked(cfg.sound)
        popups.addRow("", self.sound)
        self.session = QCheckBox("Show a “Now playing” popup when a game starts")
        self.session.setChecked(cfg.session_toast)
        popups.addRow("", self.session)
        self.sound_file = self._file_row(cfg.sound_file, "Default chime")
        popups.addRow("Custom sound", self.sound_file[0])
        self.rare_sound_file = self._file_row(cfg.rare_sound_file, "Default rare chime")
        popups.addRow("Custom rare sound", self.rare_sound_file[0])
        tests = QHBoxLayout()
        test = QPushButton("Test popup")
        test.clicked.connect(lambda: self._test(False))
        test_rare = QPushButton("Test rare popup")
        test_rare.clicked.connect(lambda: self._test(True))
        tests.addWidget(test)
        tests.addWidget(test_rare)
        tests.addStretch(1)
        popups.addRow("", tests)

        # --- Detection ------------------------------------------------------
        detect = self._page(tabs, "Detection")
        if not sys.platform.startswith("linux"):
            self._status(detect, f"Playtime tracking and crack watching need Linux. On this machine "
                                 f"{APP_NAME} keeps your library, edits achievements, and syncs Steam, "
                                 f"PlayStation and your other machines.")
        self.review = QCheckBox("After playing a game whose crack doesn't record achievements, ask what I earned")
        self.review.setChecked(cfg.session_review)
        detect.addRow("", self.review)
        self.saves = QCheckBox("Detect achievements from games' save files")
        self.saves.setChecked(cfg.save_detection)
        detect.addRow("", self.saves)
        self.dirs = QPlainTextEdit("\n".join(cfg.scan_dirs))
        self.dirs.setPlaceholderText("One folder per line - Wine prefixes, or folders that contain them")
        self.dirs.setFixedHeight(90)
        detect.addRow("Extra folders", self.dirs)
        self._status(detect, "Prefixes in ~/.wine, ~/Games (Lutris/Heroic), Bottles and Steam's compatdata are "
                             "found automatically.")

        # --- Appearance -----------------------------------------------------
        look = self._page(tabs, "Appearance")
        self.theme = QComboBox()
        self.theme.addItem("Match the system", "auto")
        self.theme.addItem("Graphite (dark)", "dark")
        self.theme.addItem("Porcelain (light)", "light")
        self.theme.setCurrentIndex(max(0, self.theme.findData(cfg.theme)))
        look.addRow("Theme", self.theme)
        self.font = QLineEdit(cfg.font_family)
        self.font.setPlaceholderText("Your desktop's font (e.g. Inter, Fira Sans, Noto Sans)")
        look.addRow("Font", self.font)
        self.x11 = QCheckBox("Use XWayland on Wayland so popups stay on top (restart to apply)")
        self.x11.setChecked(cfg.force_x11)
        look.addRow("", self.x11)
        self.autostart = QCheckBox(f"Start {APP_NAME} in the background when I log in")
        self.autostart.setChecked(desktop.autostart_enabled())
        look.addRow("", self.autostart)

        # --- Backup & phone --------------------------------------------------
        backup = self._page(tabs, "Backup && phone")
        backup_row = QHBoxLayout()
        self.backup_dir = QLineEdit(cfg.backup_dir)
        self.backup_dir.setPlaceholderText(str(BACKUP_DIR))
        pick_backup = QPushButton("Browse…")
        pick_backup.clicked.connect(lambda: self._pick_dir(self.backup_dir))
        backup_now = QPushButton("Back up now")
        backup_now.clicked.connect(lambda: self.win.worker.request("backup"))
        backup_row.addWidget(self.backup_dir, 1)
        backup_row.addWidget(pick_backup)
        backup_row.addWidget(backup_now)
        backup.addRow("Backup folder", backup_row)
        self.backup_daily = QCheckBox("Back up daily (database snapshot + readable JSON export)")
        self.backup_daily.setChecked(cfg.backup_daily)
        backup.addRow("", self.backup_daily)
        self.backup_status = self._status(backup, backup_text(parent.db.get_meta("backup")))

        self.passphrase = QLineEdit(cfg.export_passphrase)
        self.passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase.setPlaceholderText("Passphrase that unlocks the page on your phone")
        backup.addRow("Phone page", self.passphrase)
        token_row = QHBoxLayout()
        self.vercel = QLineEdit(cfg.vercel_token)
        self.vercel.setEchoMode(QLineEdit.EchoMode.Password)
        self.vercel.setPlaceholderText("Vercel token (optional)")
        publish_now = QPushButton("Build && publish")
        publish_now.clicked.connect(self._publish_now)
        token_row.addWidget(self.vercel, 1)
        token_row.addWidget(publish_now)
        backup.addRow("Vercel token", token_row)
        self.vercel_team = QLineEdit(cfg.vercel_team)
        self.vercel_team.setPlaceholderText("Team id or slug (empty = your personal account)")
        backup.addRow("Vercel team", self.vercel_team)
        self.publish_command = QLineEdit(cfg.publish_command)
        self.publish_command.setPlaceholderText("Instead of Vercel, e.g. netlify deploy --dir {dir} --prod")
        backup.addRow("Publish command", self.publish_command)
        self.sync_command = QLineEdit(cfg.sync_command)
        self.sync_command.setPlaceholderText("After backups, e.g. rclone copy {dir} gdrive:AchTrack")
        backup.addRow("Sync command", self.sync_command)

        devices = self._page(tabs, "Devices")
        sync_row = QHBoxLayout()
        self.sync_dir = QLineEdit(cfg.sync_dir)
        self.sync_dir.setPlaceholderText("A folder both machines see (Dropbox, Drive, Syncthing…)")
        pick_sync = QPushButton("Browse…")
        pick_sync.clicked.connect(lambda: self._pick_dir(self.sync_dir))
        sync_now_btn = QPushButton("Sync now")
        sync_now_btn.clicked.connect(self._devices_now)
        sync_row.addWidget(self.sync_dir, 1)
        sync_row.addWidget(pick_sync)
        sync_row.addWidget(sync_now_btn)
        devices.addRow("Shared folder", sync_row)
        self.sync_pull = QLineEdit(cfg.sync_pull_command)
        self.sync_pull.setPlaceholderText("Optional, before merging, e.g. rclone copy icloud:AchTrack {dir}")
        devices.addRow("Fetch command", self.sync_pull)
        self.sync_push = QLineEdit(cfg.sync_push_command)
        self.sync_push.setPlaceholderText("Optional, after writing, e.g. rclone copy {dir} icloud:AchTrack")
        devices.addRow("Upload command", self.sync_push)
        self.device_status = self._status(devices, device_sync_text(parent.db.get_meta("sync")))
        self._status(devices, "Point every machine at the same folder. Unlocks from all of them are combined, "
                              "play time takes the highest per source, and anything you undo stays undone.")
        self.export_status = self._status(backup, export_text(parent.db.get_meta("export")), links=True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("Primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _page(tabs: QTabWidget, title: str) -> QFormLayout:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(18, 18, 18, 18)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        tabs.addTab(scroll, title)
        return form

    @staticmethod
    def _status(form: QFormLayout, text: str, links: bool = False) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Hint")
        label.setWordWrap(True)
        label.setOpenExternalLinks(links)
        form.addRow("", label)
        return label

    def _file_row(self, value: str, placeholder: str):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit(value)
        edit.setPlaceholderText(placeholder)
        pick = QPushButton("Browse…")
        pick.clicked.connect(lambda: self._pick_sound(edit))
        h.addWidget(edit, 1)
        h.addWidget(pick)
        return row, edit

    def _pick_dir(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "Folder", edit.text() or os.path.expanduser("~"))
        if path:
            edit.setText(path)

    def _pick_sound(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Sound file", os.path.expanduser("~"),
                                              "Audio (*.wav *.ogg *.oga *.mp3 *.flac)")
        if path:
            edit.setText(path)

    # --- actions ------------------------------------------------------------

    def _sync_now(self) -> None:
        self.win.cfg.steam_api_key = self.key.text().strip()
        self.win.cfg.steam_id = self.steam_id.text().strip()
        self.win.cfg.steam_sync = True
        self.steam_sync.setChecked(True)
        self.win.worker.request("steam_sync")
        self.sync_status.setText("Syncing… (a large library can take a minute)")
        QTimer.singleShot(4000, self._refresh_sync_status)

    def _refresh_sync_status(self) -> None:
        status = self.win.db.get_meta("steam_sync")
        self.sync_status.setText(steam_sync_text(status))
        if status is None or time.time() - status.get("time", 0) > 5:
            QTimer.singleShot(3000, self._refresh_sync_status)

    def _psn_now(self) -> None:
        self.win.cfg.psn_npsso = self.psn.text().strip()
        self.win.cfg.psn_describe_hidden = self.psn_describe.isChecked()
        self.win.cfg.psn_sync = True
        self.win.worker.request("psn_sync")
        self.psn_status.setText("Syncing your PlayStation library…")
        QTimer.singleShot(5000, lambda: self.psn_status.setText(
            sync_text(self.win.db.get_meta("psn_sync"), "trophies", "trophies")))

    def _devices_now(self) -> None:
        self.win.cfg.sync_dir = self.sync_dir.text().strip()
        self.win.cfg.sync_pull_command = self.sync_pull.text().strip()
        self.win.cfg.sync_push_command = self.sync_push.text().strip()
        self.win.worker.request("sync")
        self.device_status.setText("Syncing with your other machines…")
        QTimer.singleShot(4000, lambda: self.device_status.setText(device_sync_text(self.win.db.get_meta("sync"))))

    def _publish_now(self) -> None:
        cfg = self.win.cfg
        cfg.export_passphrase = self.passphrase.text()
        cfg.vercel_token = self.vercel.text().strip()
        cfg.vercel_team = self.vercel_team.text().strip()
        cfg.publish_command = self.publish_command.text().strip()
        self.win.worker.request("export", bool(cfg.vercel_token or cfg.publish_command))
        self.export_status.setText("Building the phone page…")
        QTimer.singleShot(6000, lambda: self.export_status.setText(export_text(self.win.db.get_meta("export"))))

    def _apply(self, cfg: Config) -> None:
        cfg.popup_style = self.style.currentData()
        cfg.popup_corner = self.corner.currentData()
        cfg.popup_seconds = float(self.seconds.value())
        cfg.sound = self.sound.isChecked()
        cfg.sound_file = self.sound_file[1].text().strip()
        cfg.rare_sound_file = self.rare_sound_file[1].text().strip()
        cfg.session_toast = self.session.isChecked()

    def _test(self, rare: bool) -> None:
        # Preview with the unsaved popup settings, then put the saved ones back.
        cfg = self.win.cfg
        saved = (cfg.popup_style, cfg.popup_corner, cfg.popup_seconds, cfg.sound, cfg.sound_file,
                 cfg.rare_sound_file, cfg.session_toast)
        self._apply(cfg)
        self.win.notifier.test(rare)
        (cfg.popup_style, cfg.popup_corner, cfg.popup_seconds, cfg.sound, cfg.sound_file,
         cfg.rare_sound_file, cfg.session_toast) = saved

    def _save(self) -> None:
        cfg = self.win.cfg
        old_key, old_lang, old_dirs = cfg.steam_api_key, cfg.language, list(cfg.scan_dirs)
        old_steam = (cfg.steam_id, cfg.steam_sync)
        self._apply(cfg)
        cfg.steam_api_key = self.key.text().strip()
        cfg.language = self.language.currentText().strip() or "english"
        cfg.force_x11 = self.x11.isChecked()
        cfg.steam_id = self.steam_id.text().strip()
        cfg.steam_sync = self.steam_sync.isChecked()
        cfg.psn_npsso = self.psn.text().strip()
        cfg.psn_describe_hidden = self.psn_describe.isChecked()
        cfg.session_review = self.review.isChecked()
        cfg.save_detection = self.saves.isChecked()
        cfg.theme = self.theme.currentData()
        cfg.font_family = self.font.text().strip()
        cfg.backup_dir = self.backup_dir.text().strip()
        cfg.backup_daily = self.backup_daily.isChecked()
        cfg.export_passphrase = self.passphrase.text()
        cfg.vercel_token = self.vercel.text().strip()
        cfg.vercel_team = self.vercel_team.text().strip()
        cfg.publish_command = self.publish_command.text().strip()
        cfg.sync_command = self.sync_command.text().strip()
        cfg.sync_dir = self.sync_dir.text().strip()
        cfg.sync_pull_command = self.sync_pull.text().strip()
        cfg.sync_push_command = self.sync_push.text().strip()
        cfg.scan_dirs = [line.strip() for line in self.dirs.toPlainText().splitlines() if line.strip()]
        cfg.save()
        try:
            desktop.set_autostart(self.autostart.isChecked())
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't update autostart: {e}")
        if cfg.steam_api_key != old_key or cfg.language != old_lang:
            self.win.worker.request("refresh_all")
        if cfg.scan_dirs != old_dirs:
            self.win.worker.request("rescan")
        if cfg.steam_sync and (cfg.steam_api_key != old_key or (cfg.steam_id, cfg.steam_sync) != old_steam):
            self.win.worker.request("steam_sync")
        self.win.apply_theme()
        self.win.schedule_reload()
        self.accept()


# main window -------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, worker, notifier, has_tray: bool):
        super().__init__()
        self.cfg = cfg
        self.worker = worker
        self.notifier = notifier
        self.has_tray = has_tray
        self.db = Database()
        self.running: set = set()
        self._list_sig = None
        self._dirty = True
        self._told_background = False
        self._quitting = False
        self.on_hidden_without_tray = None
        self._reviews: dict = {}
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(APP_ICON)))
        self.resize(1200, 780)
        self.apply_theme()
        # Follow the desktop switching between light and dark while the app is open.
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda _scheme: self.apply_theme())
        self._theme_timer = QTimer(self)
        self._theme_timer.setInterval(20000)
        self._theme_timer.timeout.connect(self._check_theme)
        self._theme_timer.start()

        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(400)
        self._reload_timer.timeout.connect(self.reload)

        splitter = QSplitter()
        splitter.setHandleWidth(1)
        side = QWidget()
        side.setObjectName("Sidebar")
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(0, 10, 0, 10)
        side_lay.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search games")
        self.search.textChanged.connect(lambda _: self.reload(force=True))
        search_wrap = QHBoxLayout()
        search_wrap.setContentsMargins(10, 0, 10, 0)
        search_wrap.addWidget(self.search)
        side_lay.addLayout(search_wrap)
        self.sort = MenuButton([("Recently played", "recent"), ("Name (A-Z)", "name"), ("Play time", "playtime"),
                                ("Completion", "progress"), ("Genre", "genre"), ("Platform", "platform")],
                               cfg.sort, self._sort_changed)
        self.platform = MenuButton([("All platforms", "all"), ("PC", "pc"), ("PlayStation", "psn")],
                                   cfg.platform_filter, self._platform_changed)
        sort_wrap = QHBoxLayout()
        sort_wrap.setContentsMargins(10, 0, 10, 0)
        sort_wrap.setSpacing(6)
        sort_wrap.addWidget(self.sort, 1)
        sort_wrap.addWidget(self.platform, 1)
        side_lay.addLayout(sort_wrap)
        self.games = QListWidget()
        self.games.setObjectName("SidebarList")
        self.games.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.games.currentItemChanged.connect(self._on_select)
        self.games.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.games.customContextMenuRequested.connect(self._context_menu)
        side_lay.addWidget(self.games, 1)
        add = QPushButton("＋  Add a game")
        add.clicked.connect(self.add_game)
        add_wrap = QHBoxLayout()
        add_wrap.setContentsMargins(10, 0, 10, 0)
        add_wrap.addWidget(add)
        side_lay.addLayout(add_wrap)
        side.setMinimumWidth(240)
        splitter.addWidget(side)

        self.stack = QStackedWidget()
        empty = QWidget()
        empty_lay = QVBoxLayout(empty)
        empty_lay.addStretch(1)
        icon = QLabel()
        icon.setPixmap(QIcon(str(APP_ICON)).pixmap(96, 96))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(icon)
        msg = QLabel("No games yet.\n\nLaunch a game that uses a Steam emulator (Goldberg, CODEX, RUNE, EMPRESS, "
                     "OnlineFix…) and it will show up here automatically,\nor add one by hand.")
        msg.setObjectName("Empty")
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        empty_lay.addWidget(msg)
        row = QHBoxLayout()
        row.addStretch(1)
        add2 = QPushButton("Add a game")
        add2.setObjectName("Primary")
        add2.clicked.connect(self.add_game)
        settings = QPushButton("Settings")
        settings.clicked.connect(self.open_settings)
        row.addWidget(add2)
        row.addWidget(settings)
        row.addStretch(1)
        empty_lay.addLayout(row)
        empty_lay.addStretch(2)
        self.stack.addWidget(empty)
        self.detail = DetailView(self)
        self.stack.addWidget(self.detail)
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 920])
        self.setCentralWidget(splitter)
        self._build_menu()

    def apply_theme(self) -> None:
        global P
        P.update(theme.palette(self.cfg.theme))
        app = QApplication.instance()
        if app is not None:
            app.setPalette(theme.qt_palette(P, QPalette, QColor))
        self.setStyleSheet(theme.stylesheet(P, self.cfg.font_family))
        self.notifier.palette_changed(P)
        self._list_sig = None
        if self.isVisible():
            self.reload(force=True)

    def _check_theme(self) -> None:
        if theme.palette(self.cfg.theme)["scheme"] != P["scheme"]:
            self.apply_theme()

    def _build_menu(self) -> None:
        bar = self.menuBar()
        lib = bar.addMenu("Library")
        lib.addAction("Add a game…", self.add_game)
        lib.addAction("Rescan for achievement files", lambda: self.worker.request("rescan"))
        lib.addAction("Refresh all achievement data", lambda: self.worker.request("refresh_all"))
        lib.addAction("Sync Steam library now", self._sync_steam)
        lib.addAction("Sync PlayStation now", self._sync_psn)
        lib.addSeparator()
        lib.addAction("Back up now", lambda: self.worker.request("backup"))
        lib.addAction("Sync with my other machines", lambda: self.worker.request("sync"))
        lib.addAction("Build phone page", lambda: self.worker.request("export", bool(self.cfg.vercel_token)))
        lib.addSeparator()
        icons_action = QAction("Show game icons", self, checkable=True)
        icons_action.setChecked(self.cfg.show_icons)
        icons_action.toggled.connect(self._icons_toggled)
        lib.addAction(icons_action)
        self.show_hidden = QAction("Show removed games", self, checkable=True)
        self.show_hidden.toggled.connect(lambda _: self.reload(force=True))
        lib.addAction(self.show_hidden)
        lib.addSeparator()
        lib.addAction("Settings…", self.open_settings)
        lib.addSeparator()
        # On macOS this becomes the app menu's Quit item, so Cmd-Q really quits.
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))     # Qt maps Ctrl to Cmd on macOS
        quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        quit_action.triggered.connect(self.quit_app)
        self.addAction(quit_action)
        lib.addAction(quit_action)
        helpm = bar.addMenu("Help")
        helpm.addAction("Test achievement popup", lambda: self.notifier.test(False))
        helpm.addAction("Test rare achievement popup", lambda: self.notifier.test(True))
        helpm.addSeparator()
        helpm.addAction(f"About {APP_NAME}", self._about)

    # data ------------------------------------------------------------------

    def schedule_reload(self) -> None:
        if self.isVisible():
            self._reload_timer.start()
        else:
            self._dirty = True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._dirty:
            self._dirty = False
            self.reload(force=True)

    def _sort_changed(self, value: str) -> None:
        self.cfg.sort = value
        self.cfg.save()
        self.reload(force=True)

    def _platform_changed(self, value: str) -> None:
        self.cfg.platform_filter = value
        self.cfg.save()
        self.reload(force=True)

    def _icons_toggled(self, checked: bool) -> None:
        self.cfg.show_icons = checked
        self.cfg.save()
        self.reload(force=True)

    @staticmethod
    def _sort_key(mode: str):
        if mode == "name":
            return lambda r: ((r["name"] or "").lower(), )
        if mode == "playtime":
            return lambda r: (-r["total_playtime"], (r["name"] or "").lower())
        if mode == "progress":
            return lambda r: (-(r["unlocked"] / r["total"] if r["total"] else -1), (r["name"] or "").lower())
        if mode == "genre":
            return lambda r: ((r["genre"] or "zzz").lower(), (r["name"] or "").lower())
        if mode == "platform":
            return lambda r: ((r["platform"] or "pc"), (r["name"] or "").lower())
        return lambda r: (-(r["last_any"] or 0), (r["name"] or "").lower())

    def current_appid(self) -> int | None:
        item = self.games.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def reload(self, force: bool = False) -> None:
        rows = self.db.games(include_hidden=self.show_hidden.isChecked())
        query = self.search.text().strip().lower()
        if query:
            rows = [r for r in rows if query in (r["name"] or "").lower() or query in (r["genre"] or "").lower()
                    or query in str(r["appid"])]
        chosen = self.cfg.platform_filter
        if chosen == "pc":
            rows = [r for r in rows if (r["platform"] or "pc") == "pc"]
        elif chosen == "psn":
            rows = [r for r in rows if (r["platform"] or "pc") != "pc"]
        rows = sorted(rows, key=self._sort_key(self.cfg.sort))
        sig = tuple((r["appid"], r["name"], self.cfg.sort, self.cfg.platform_filter, self.cfg.show_icons,
                     r["total_playtime"] // 60, r["unlocked"], r["total"], r["hidden"],
                     r["appid"] in self.running) for r in rows)
        if force or sig != self._list_sig:
            self._list_sig = sig
            current = self.current_appid()
            self.games.blockSignals(True)
            self.games.clear()
            select = None
            for r in rows:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, r["appid"])
                widget = GameRow(r, r["appid"] in self.running, self.cfg.show_icons)
                if r["hidden"]:
                    widget.setEnabled(False)
                item.setSizeHint(QSize(0, 56 if self.cfg.show_icons else 52))
                self.games.addItem(item)
                self.games.setItemWidget(item, widget)
                if r["appid"] == current:
                    select = item
            if select is None and self.games.count():
                select = self.games.item(0)
            if select is not None:
                self.games.setCurrentItem(select)
            self.games.blockSignals(False)
        has_games = bool(self.db.games(include_hidden=self.show_hidden.isChecked()))
        self.stack.setCurrentIndex(1 if has_games and self.games.count() else 0)
        self.detail.show_game(self.current_appid())

    def _on_select(self, current, _previous) -> None:
        if current is not None:
            self.detail.show_game(current.data(Qt.ItemDataRole.UserRole))

    # events from the engine ------------------------------------------------

    def game_started(self, appid: int, *_args) -> None:
        self.running.add(appid)
        self.schedule_reload()

    def game_stopped(self, appid: int, seconds: int) -> None:
        self.running.discard(appid)
        self.schedule_reload()
        game = self.db.game(appid)
        if (self.cfg.session_review and seconds >= REVIEW_MIN_SECONDS and game is not None
                and not game["psn_id"] and needs_review(self.db, appid)):
            QTimer.singleShot(1500, lambda: self.open_review(appid, seconds))

    # manual unlocks ---------------------------------------------------------

    def open_review(self, appid: int, session_seconds: int = 0) -> None:
        existing = self._reviews.get(appid)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        if not self.db.achievement_names(appid):
            QMessageBox.information(self, APP_NAME, "There's no achievement list for this game yet. Add your Steam "
                                                    "Web API key in Settings, or try Refresh from Steam.")
            return
        dialog = ReviewDialog(self, appid, session_seconds)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(lambda _code: self._reviews.pop(appid, None))
        self._reviews[appid] = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def manual_unlock(self, appid: int, names: list, ts: int, popup: bool = True, notes: dict | None = None) -> None:
        notes = notes or {}
        added = [n for n in names if self.db.add_unlock(appid, n, ts, MANUAL, notes.get(n))]
        if popup and added:
            for name in added[:POPUP_BURST]:
                self.notifier.achievement(build_unlock_event(self.db, appid, name, download=False))
            if len(added) > POPUP_BURST:
                game = self.db.game(appid)
                self.notifier.toasts.show(ToastData(
                    kicker="Achievements unlocked", title=f"+{len(added) - POPUP_BURST} more",
                    body=game["name"] or f"App {appid}", sound=None))
        self.reload(force=True)

    def manual_unlock_on(self, appid: int, name: str) -> None:
        dialog = UnlockDateDialog(self, "Mark as unlocked")
        if dialog.exec():
            self.manual_unlock(appid, [name], dialog.timestamp(), popup=False)

    def change_unlock_date(self, appid: int, name: str, ts: int) -> None:
        dialog = UnlockDateDialog(self, "Change unlock date", ts)
        if dialog.exec():
            self.db.x("UPDATE unlocks SET unlocked_at = ? WHERE appid = ? AND name = ? AND source = ?",
                      (dialog.timestamp(), appid, name, MANUAL))
            self.reload(force=True)

    def remove_manual_unlock(self, appid: int, name: str) -> None:
        self.db.remove_unlock(appid, name, MANUAL)
        self.reload(force=True)

    def reject_detected_unlock(self, appid: int, name: str, source: str) -> None:
        note = self.db.unlock_note(appid, name) or ""
        if note:
            self.db.reject(appid, name, detection_key(note))
        self.db.remove_unlock(appid, name, source)
        self.reload(force=True)

    def _sync_psn(self) -> None:
        if not self.cfg.psn_npsso:
            QMessageBox.information(self, APP_NAME, "Add your PlayStation login (NPSSO token) in Settings first.")
            return
        self.worker.request("psn_sync")
        self.statusBar().showMessage("Syncing your PlayStation library…", 5000)

    def _sync_steam(self) -> None:
        if not self.cfg.steam_api_key:
            QMessageBox.information(self, APP_NAME, "Add your Steam Web API key in Settings first.")
            return
        self.worker.request("steam_sync")
        self.statusBar().showMessage("Syncing your Steam library…", 5000)

    def request_refresh(self, appid: int) -> None:
        self.worker.request("refresh", appid)

    def describe_hidden(self, appid: int, trophy_list: str = "") -> None:
        """Looks a game's hidden achievements up in its PlayStation trophy list."""
        if not self.cfg.psn_npsso:
            QMessageBox.information(self, APP_NAME, "Add your PlayStation login (NPSSO token) in Settings "
                                                    "first - the descriptions come from its trophy lists.")
            return
        blanks = self.db.blank_descriptions(appid)
        self.statusBar().showMessage(f"Looking up {blanks} hidden achievement"
                                     f"{'s' if blanks != 1 else ''} on PlayStation…", 30000)
        self.worker.request("describe", appid, trophy_list)

    def described(self, appid: int, status: dict) -> None:
        """What came back from a trophy-list lookup."""
        game = self.db.game(appid)
        name = (game["name"] if game is not None else "") or f"App {appid}"
        if status["filled"]:
            self.statusBar().showMessage(f"Described {status['filled']} hidden achievement"
                                         f"{'s' if status['filled'] != 1 else ''} of {name} "
                                         f"from its PlayStation trophy list.", 15000)
            self.reload(force=True)
            return
        self.statusBar().clearMessage()
        if status["error"]:
            QMessageBox.information(self, APP_NAME, f"Couldn't describe {name}: {status['error']}")
            return
        # Sony only hands out a game's trophy list by its id, and only publishes that id for games
        # your own account has played - so for everything else, ask for the list.
        text, ok = QInputDialog.getText(
            self, APP_NAME,
            f"No trophy list of PlayStation's matched {name}.\n\n"
            f"If you can find its trophy list id (NPWR12345_00), paste it here and AchTrack will "
            f"take the descriptions from there:", text="")
        if ok and text.strip():
            self.describe_hidden(appid, text.strip())

    # actions ---------------------------------------------------------------

    def add_game(self) -> None:
        dialog = GameDialog(self, self.db)
        if dialog.exec() and dialog.saved_appid:
            self.worker.request("refresh", dialog.saved_appid)
            self.worker.request("rescan")
            self.reload(force=True)
            self._select(dialog.saved_appid)

    def edit_game(self, appid: int) -> None:
        dialog = GameDialog(self, self.db, appid)
        if dialog.exec():
            self.worker.request("rescan")
            self.reload(force=True)

    def _select(self, appid: int) -> None:
        for i in range(self.games.count()):
            if self.games.item(i).data(Qt.ItemDataRole.UserRole) == appid:
                self.games.setCurrentRow(i)
                return

    def _context_menu(self, pos) -> None:
        item = self.games.itemAt(pos)
        if item is None:
            return
        appid = item.data(Qt.ItemDataRole.UserRole)
        game = self.db.game(appid)
        menu = QMenu(self)
        if game["psn_id"]:
            menu.addAction("Sync PlayStation now", self._sync_psn)
            menu.addSeparator()
            if game["hidden"]:
                menu.addAction("Restore to library", lambda: self._set_hidden(appid, False))
            else:
                menu.addAction("Remove from library", lambda: self._set_hidden(appid, True))
            menu.exec(self.games.mapToGlobal(pos))
            return
        menu.addAction("Review achievements…", lambda: self.open_review(appid))
        after = menu.addMenu("After a session")
        group = QActionGroup(after)
        for label, value in (("Ask automatically (crack not recording)", None), ("Always ask what I earned", 1),
                             ("Never ask", 0)):
            action = after.addAction(label)
            action.setCheckable(True)
            action.setChecked(game["review"] == value)
            action.triggered.connect(lambda _=False, v=value: self.db.update_game(appid, review=v))
            group.addAction(action)
        menu.addSeparator()
        if self.db.blank_descriptions(appid):
            menu.addAction("Describe hidden achievements…", lambda: self.describe_hidden(appid))
        menu.addAction("Refresh achievement data", lambda: self.request_refresh(appid))
        menu.addAction("Edit game…", lambda: self.edit_game(appid))
        sources = self.db.sources_for(appid)
        if sources:
            menu.addAction("Open achievement file location",
                           lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(sources[0]))))
        if game["game_dir"]:
            menu.addAction("Open game folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(game["game_dir"])))
        menu.addAction("View on Steam store",
                       lambda: QDesktopServices.openUrl(QUrl(f"https://store.steampowered.com/app/{appid}/")))
        menu.addSeparator()
        if game["hidden"]:
            menu.addAction("Restore to library", lambda: self._set_hidden(appid, False))
        else:
            menu.addAction("Remove from library", lambda: self._set_hidden(appid, True))
        menu.exec(self.games.mapToGlobal(pos))

    def _set_hidden(self, appid: int, hidden: bool) -> None:
        self.db.update_game(appid, hidden=1 if hidden else 0)
        self.reload(force=True)

    def open_settings(self) -> None:
        SettingsDialog(self).exec()

    def _about(self) -> None:
        QMessageBox.about(self, f"About {APP_NAME}",
                          f"<h3>{APP_NAME} {__version__}</h3><p>Steam achievements, unlock popups and playtime "
                          "for games running on Steam emulators under Linux.</p>")

    def closeEvent(self, event) -> None:
        # Quitting closes this window too - only a plain close should keep the app running.
        if self._quitting or QApplication.instance().closingDown():
            event.accept()
            return
        # Tracking has to keep running while games are played, so closing only hides the library.
        event.ignore()
        self.hide()
        if not self.has_tray and not self._told_background and self.on_hidden_without_tray:
            self._told_background = True
            self.on_hidden_without_tray()

    def quit_app(self) -> None:
        self._quitting = True
        QApplication.instance().quit()

    def bring_up(self) -> None:
        self.show()
        self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        self.raise_()
        self.activateWindow()
