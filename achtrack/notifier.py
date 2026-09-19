"""Steam-style achievement popups."""
from __future__ import annotations

import math
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import (QEasingCurve, QObject, QParallelAnimationGroup, QPoint, QPointF, QPropertyAnimation,
                            QRectF, Qt, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QWidget

from . import sound
from .config import APP_ICON, APP_NAME, Config

CARD_W, CARD_H = 372, 102
SHADOW = 16
RADIUS = 6
ICON = 64
MAX_VISIBLE = 3
EDGE_MARGIN = 14
SPACING = 8

BLUE = QColor("#66c0f4")
GOLD = QColor("#f0c75e")


@dataclass
class ToastData:
    kicker: str
    title: str
    body: str = ""
    icon: str | None = None
    footer: str = ""
    footer_right: str = ""
    rare: bool = False
    square_crop: bool = False
    sound: str | None = "unlock"
    sound_file: str = ""


def _font(px: float, weight=QFont.Weight.Normal, spacing: float = 0.0) -> QFont:
    f = QFont()
    f.setFamilies(["Motiva Sans", "Inter", "Noto Sans", "Cantarell", "Ubuntu", "DejaVu Sans", "Arial"])
    f.setPixelSize(int(px))
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def wrap_text(text: str, fm: QFontMetrics, width: int, max_lines: int) -> list:
    words = text.split()
    lines, i = [], 0
    while i < len(words) and len(lines) < max_lines:
        line = words[i]
        i += 1
        while i < len(words) and fm.horizontalAdvance(line + " " + words[i]) <= width:
            line += " " + words[i]
            i += 1
        lines.append(line)
    if i < len(words) and lines:
        lines[-1] = lines[-1] + " " + " ".join(words[i:])
    return [fm.elidedText(line, Qt.TextElideMode.ElideRight, width) for line in lines]


def trophy_pixmap(size: int, color: QColor = GOLD) -> QPixmap:
    """Fallback artwork when an achievement has no icon."""
    pm = QPixmap(size, size)
    pm.fill(QColor("#3b4452"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 64
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    cup = QPainterPath()
    cup.moveTo(20 * s, 14 * s)
    cup.lineTo(44 * s, 14 * s)
    cup.cubicTo(44 * s, 32 * s, 38 * s, 38 * s, 32 * s, 38 * s)
    cup.cubicTo(26 * s, 38 * s, 20 * s, 32 * s, 20 * s, 14 * s)
    p.drawPath(cup)
    p.drawRect(QRectF(29 * s, 37 * s, 6 * s, 8 * s))
    p.drawRoundedRect(QRectF(22 * s, 45 * s, 20 * s, 6 * s), 2 * s, 2 * s)
    pen = QPen(color, 3 * s)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(12 * s, 16 * s, 12 * s, 14 * s), 90 * 16, 180 * 16)
    p.drawArc(QRectF(40 * s, 16 * s, 12 * s, 14 * s), 270 * 16, 180 * 16)
    p.end()
    return pm


def _sparkle(p: QPainter, center: QPointF, size: float, color: QColor) -> None:
    path = QPainterPath()
    path.moveTo(center.x(), center.y() - size)
    for dx, dy in ((0.22, -0.22), (1, 0), (0.22, 0.22), (0, 1), (-0.22, 0.22), (-1, 0), (-0.22, -0.22), (0, -1)):
        path.lineTo(center.x() + dx * size, center.y() + dy * size)
    p.fillPath(path, color)


class Toast(QWidget):
    finished = Signal(object)

    def __init__(self, data: ToastData, seconds: float):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.X11BypassWindowManagerHint
                 | Qt.WindowType.NoDropShadowWindowHint)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_X11DoNotAcceptFocus)
        self.setFixedSize(CARD_W + 2 * SHADOW, CARD_H + 2 * SHADOW)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.data = data
        self.seconds = max(1.5, seconds)
        self.phase = 0.0
        self.closing = False
        self.icon = self._load_icon()
        self._anims: list = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

    def _load_icon(self) -> QPixmap:
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(self.data.icon) if self.data.icon else QPixmap()
        if pm.isNull():
            pm = trophy_pixmap(ICON * 2, GOLD if self.data.rare else BLUE)
        elif self.data.square_crop:
            side = min(pm.width(), pm.height())
            pm = pm.copy((pm.width() - side) // 2, (pm.height() - side) // 2, side, side)
        pm = pm.scaled(int(ICON * dpr), int(ICON * dpr), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                       Qt.TransformationMode.SmoothTransformation)
        pm.setDevicePixelRatio(dpr)
        return pm

    # animation -----------------------------------------------------------

    @staticmethod
    def _can_fade() -> bool:
        # Only the X11 backend supports window opacity; elsewhere Qt warns on every frame.
        return QGuiApplication.platformName() == "xcb"

    def present(self, start: QPoint, end: QPoint) -> None:
        self.move(start)
        if self._can_fade():
            self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        group = QParallelAnimationGroup(self)
        slide = QPropertyAnimation(self, b"pos")
        slide.setDuration(420)
        slide.setStartValue(start)
        slide.setEndValue(end)
        slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade = QPropertyAnimation(self, b"windowOpacity")
        fade.setDuration(260)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        group.addAnimation(slide)
        if self._can_fade():
            group.addAnimation(fade)
        group.start()
        self._anims.append(group)
        if self.data.rare:
            glow = QVariantAnimation(self)
            glow.setStartValue(0.0)
            glow.setEndValue(float(self.seconds))
            glow.setDuration(int(self.seconds * 1000))
            glow.valueChanged.connect(self._tick)
            glow.start()
            self._anims.append(glow)
        self._timer.start(int(self.seconds * 1000))

    def _tick(self, value) -> None:
        self.phase = float(value)
        self.update()

    def move_to(self, end: QPoint) -> None:
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(260)
        anim.setEndValue(end)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.start()
        self._anims.append(anim)

    def dismiss(self) -> None:
        if self.closing:
            return
        self.closing = True
        self._timer.stop()
        if not self._can_fade():
            self._finish()
            return
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(320)
        fade.setStartValue(self.windowOpacity())
        fade.setEndValue(0.0)
        fade.finished.connect(self._finish)
        fade.start()
        self._anims.append(fade)

    def _finish(self) -> None:
        self.hide()
        self.finished.emit(self)

    def mousePressEvent(self, event) -> None:
        self.dismiss()

    def enterEvent(self, event) -> None:
        if not self.closing:
            self._timer.stop()

    def leaveEvent(self, event) -> None:
        if not self.closing:
            self._timer.start(2000)

    # painting ------------------------------------------------------------

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
                         | QPainter.RenderHint.TextAntialiasing)
        rare = self.data.rare
        card = QRectF(SHADOW, SHADOW, CARD_W, CARD_H)

        p.setPen(Qt.PenStyle.NoPen)
        for i in range(SHADOW, 0, -2):
            alpha = int(46 * (1 - i / SHADOW) ** 2) + 2
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawRoundedRect(card.adjusted(-i, -i + 4, i, i + 4), RADIUS + i, RADIUS + i)

        body = QPainterPath()
        body.addRoundedRect(card, RADIUS, RADIUS)
        bg = QLinearGradient(card.topLeft(), card.bottomRight())
        if rare:
            bg.setColorAt(0.0, QColor("#3b3324"))
            bg.setColorAt(0.55, QColor("#252019"))
            bg.setColorAt(1.0, QColor("#1b1812"))
        else:
            bg.setColorAt(0.0, QColor("#2d3441"))
            bg.setColorAt(0.55, QColor("#222833"))
            bg.setColorAt(1.0, QColor("#1a1e26"))
        p.fillPath(body, bg)

        # Rare achievements get Steam's golden sweep.
        if rare and 0.25 <= self.phase <= 1.65:
            t = (self.phase - 0.25) / 1.4
            cx = card.left() - 120 + (card.width() + 240) * t
            sweep = QLinearGradient(cx - 70, card.top(), cx + 70, card.bottom())
            sweep.setColorAt(0.0, QColor(255, 220, 140, 0))
            sweep.setColorAt(0.5, QColor(255, 225, 150, 60))
            sweep.setColorAt(1.0, QColor(255, 220, 140, 0))
            p.save()
            p.setClipPath(body)
            p.fillRect(card, sweep)
            p.restore()

        border = QPen()
        border.setWidthF(1.2)
        if rare:
            edge = QLinearGradient(card.topLeft(), card.topRight())
            edge.setColorAt(0.0, QColor("#f7dc8b"))
            edge.setColorAt(0.5, QColor("#a8792a"))
            edge.setColorAt(1.0, QColor("#f7dc8b"))
            border.setBrush(edge)
        else:
            border.setColor(QColor(255, 255, 255, 30))
        p.strokePath(body, border)
        p.setPen(QPen(QColor(255, 255, 255, 22 if not rare else 40), 1))
        p.drawLine(QPointF(card.left() + RADIUS, card.top() + 1.2), QPointF(card.right() - RADIUS, card.top() + 1.2))

        icon_rect = QRectF(card.left() + 16, card.top() + (CARD_H - ICON) / 2, ICON, ICON)
        if rare:
            glow = QRadialGradient(icon_rect.center(), ICON * 0.95)
            pulse = 0.75 + 0.25 * math.sin(self.phase * 3.2)
            glow.setColorAt(0.0, QColor(255, 196, 80, int(120 * pulse)))
            glow.setColorAt(1.0, QColor(255, 196, 80, 0))
            p.fillRect(icon_rect.adjusted(-22, -22, 22, 22), glow)
        clip = QPainterPath()
        clip.addRoundedRect(icon_rect, 3, 3)
        p.save()
        p.setClipPath(clip)
        p.drawPixmap(icon_rect.topLeft(), self.icon)
        p.restore()
        p.setPen(QPen(GOLD if rare else QColor(0, 0, 0, 110), 1.4 if rare else 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(icon_rect, 3, 3)
        if rare:
            for i, (dx, dy, size) in enumerate(((-6, 4, 5), (ICON + 5, 10, 4), (ICON - 2, ICON + 4, 6), (8, ICON + 6, 3.5))):
                alpha = max(0.0, math.sin(self.phase * 4.0 + i * 1.7))
                _sparkle(p, QPointF(icon_rect.left() + dx, icon_rect.top() + dy), size,
                         QColor(255, 236, 170, int(230 * alpha)))

        x = icon_rect.right() + 14
        width = int(card.right() - 14 - x)
        top = card.top() + 12

        kicker = _font(10.5, QFont.Weight.Bold, 0.9)
        p.setFont(kicker)
        right_w = 0
        if self.data.footer_right:
            right_w = QFontMetrics(kicker).horizontalAdvance(self.data.footer_right) + 8
            p.setPen(GOLD if rare else QColor("#8b98a5"))
            p.drawText(QRectF(x, top, width, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       self.data.footer_right)
        p.setPen(GOLD if rare else BLUE)
        p.drawText(QRectF(x, top, width - right_w, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(kicker).elidedText(self.data.kicker, Qt.TextElideMode.ElideRight, width - right_w))

        title = _font(15.5, QFont.Weight.DemiBold)
        p.setFont(title)
        p.setPen(QColor("#ffffff"))
        p.drawText(QRectF(x, top + 16, width, 21), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetrics(title).elidedText(self.data.title, Qt.TextElideMode.ElideRight, width))

        desc = _font(12.5)
        p.setFont(desc)
        p.setPen(QColor("#b4bcc6"))
        for i, line in enumerate(wrap_text(self.data.body, QFontMetrics(desc), width, 2)):
            p.drawText(QRectF(x, top + 38 + i * 16, width, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)

        if self.data.footer:
            foot = _font(10.5, QFont.Weight.Medium)
            p.setFont(foot)
            p.setPen(QColor("#77818c"))
            p.drawText(QRectF(x, card.bottom() - 20, width, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(foot).elidedText(self.data.footer, Qt.TextElideMode.ElideRight, width))
        p.end()


class ToastManager(QObject):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.queue: deque = deque()
        self.active: list = []

    def show(self, data: ToastData) -> None:
        self.queue.append(data)
        self._pump()

    def _pump(self) -> None:
        while self.queue and len(self.active) < MAX_VISIBLE:
            data = self.queue.popleft()
            toast = Toast(data, self.cfg.popup_seconds)
            toast.finished.connect(self._done)
            self.active.append(toast)
            end = self._slot(len(self.active) - 1)
            offset = 46 if self.cfg.popup_corner.startswith("bottom") else -46
            toast.present(end + QPoint(0, offset), end)
            if data.sound and self.cfg.sound:
                sound.play(data.sound, data.sound_file)

    def _slot(self, index: int) -> QPoint:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        corner = self.cfg.popup_corner
        step = CARD_H + SPACING
        if corner.endswith("right"):
            x = geo.x() + geo.width() - EDGE_MARGIN - CARD_W - SHADOW
        else:
            x = geo.x() + EDGE_MARGIN - SHADOW
        if corner.startswith("bottom"):
            y = geo.y() + geo.height() - EDGE_MARGIN - CARD_H - SHADOW - index * step
        else:
            y = geo.y() + EDGE_MARGIN - SHADOW + index * step
        return QPoint(x, y)

    def _done(self, toast: Toast) -> None:
        if toast in self.active:
            self.active.remove(toast)
        toast.deleteLater()
        for i, other in enumerate(self.active):
            other.move_to(self._slot(i))
        self._pump()


def system_notify(title: str, body: str, icon: str | None, tray=None) -> None:
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "-a", APP_NAME, "-i", icon or str(APP_ICON), title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return
        except OSError:
            pass
    if tray is not None:
        tray.showMessage(title, body)


class Notifier(QObject):
    def __init__(self, cfg: Config, tray=None):
        super().__init__()
        self.cfg = cfg
        self.tray = tray
        self.toasts = ToastManager(cfg)

    def achievement(self, ev: dict) -> None:
        style = self.cfg.popup_style
        rare = bool(ev.get("rare"))
        pct = ev.get("pct")
        footer = ev.get("game") or ""
        if ev.get("total"):
            footer += f"  ·  {ev['unlocked']} of {ev['total']}"
        data = ToastData(
            kicker="Rare achievement unlocked".upper() if rare else "Achievement unlocked".upper(),
            title=ev.get("title") or ev.get("name") or "Achievement",
            body=ev.get("description") or "",
            icon=ev.get("icon"),
            footer=footer,
            footer_right=f"{pct:.1f}%" if pct is not None else "",
            rare=rare,
            sound="rare" if rare else "unlock",
            sound_file=self.cfg.rare_sound_file if rare else self.cfg.sound_file,
        )
        self._deliver(data, f"Achievement unlocked: {data.title}", data.body)

    def game_started(self, appid: int, name: str, header: str) -> None:
        if not self.cfg.session_toast:
            return
        data = ToastData(kicker="NOW PLAYING", title=name, body="Achievements and playtime are being tracked",
                         icon=header or None, square_crop=True, sound="session")
        self._deliver(data, f"Now playing: {name}", data.body)

    def test(self, rare: bool = False) -> None:
        self.achievement({
            "game": "AchTrack", "title": "Legend of the Tracker" if rare else "Welcome Aboard",
            "description": "Unlock an achievement that fewer than one in ten players have." if rare
            else "Popups are working. Go earn some achievements!",
            "icon": None, "pct": 3.7 if rare else 58.2, "rare": rare, "unlocked": 1, "total": 1,
        })

    def _deliver(self, data: ToastData, title: str, body: str) -> None:
        style = self.cfg.popup_style
        if style in ("steam", "both"):
            self.toasts.show(data)
        if style in ("system", "both"):
            system_notify(title, body, data.icon, self.tray)
            if style == "system" and self.cfg.sound and data.sound:
                sound.play(data.sound, data.sound_file)
