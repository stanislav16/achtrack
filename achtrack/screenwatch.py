"""Watches the screen while a game runs and reads its text locally (tesseract OCR) to spot
achievements whose progress never reaches a readable save file.

Only a few small areas of the screen are read, stacked into one image and passed to a single
tesseract call every couple of seconds at low CPU priority. What is seen:
  * direct text:   "STAGGERED" -> "Stagger an enemy.", "CHAPTER 6" -> "Complete Chapter 5."
  * named targets: "Soul of Cinder" on screen starts an encounter; a victory banner ("... DESTROYED",
                   "ENEMY FELLED") with no death screen in between means it was defeated
  * learned text:  when you confirm an achievement, the text seen just before it is remembered,
                   including the game's own victory banner, and used from then on
"""
from __future__ import annotations

import difflib
import io
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

from . import matcher
from .config import CACHE_DIR, DATA_DIR

log = logging.getLogger(__name__)

LOG_DIR = CACHE_DIR / "screen"
PROFILE_DIR = DATA_DIR / "screen"

# (name, left, top, right, bottom) as fractions of the screen
REGIONS = [
    ("top", 0.12, 0.02, 0.88, 0.24),
    ("center", 0.12, 0.34, 0.88, 0.64),
    ("bottom", 0.08, 0.74, 0.92, 0.97),
    ("left", 0.00, 0.12, 0.28, 0.58),
    ("right", 0.72, 0.12, 1.00, 0.58),
]
OCR_WIDTH = 1100            # regions are scaled to this width before OCR
MIN_CONFIDENCE = 55         # tesseract word confidence (0-100)
CONFIRM_WINDOW = 45.0       # a direct hit must be seen twice within this many seconds
ENCOUNTER_TIMEOUT = 120.0   # victory text must come within this long after the target was last seen

DEFAULT_VICTORY = ["defeated", "destroyed", "felled", "enemy felled", "great enemy felled", "victory", "vanquished",
                   "slain", "conquered", "you win", "battle won", "boss defeated", "target eliminated", "cleared",
                   "mission complete", "quest complete", "stage clear"]
DEFAULT_DEATH = ["you died", "game over", "retry", "try again", "continue?", "you are dead", "wasted",
                 "mission failed", "load last save", "you have been defeated", "defeat", "died"]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z' ]+", " ", text.lower())).strip()


def available() -> tuple:
    """(ok, reason)."""
    if not shutil.which("tesseract"):
        return False, "tesseract isn't installed (sudo apt install tesseract-ocr)"
    try:
        import mss  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False, "the mss / Pillow Python packages are missing - run install.sh again"
    if not os.environ.get("DISPLAY"):
        return False, "no X11 display to capture (screen watching needs an X11 or XWayland session)"
    return True, ""


# --- capture + OCR ------------------------------------------------------------------------------------

class ScreenReader:
    def __init__(self, monitor: int = 1):
        import mss
        self._mss = mss.mss()
        monitors = self._mss.monitors
        self.monitor = monitors[monitor] if 0 < monitor < len(monitors) else monitors[1 if len(monitors) > 1 else 0]

    def close(self) -> None:
        try:
            self._mss.close()
        except Exception:
            pass

    def capture(self):
        """Stacked grayscale image of all regions, plus the y offset where each region starts."""
        from PIL import Image, ImageOps
        m = self.monitor
        parts, offsets, y = [], [], 0
        brightness = []
        for name, l, t, r, b in REGIONS:
            box = {"left": m["left"] + int(m["width"] * l), "top": m["top"] + int(m["height"] * t),
                   "width": max(1, int(m["width"] * (r - l))), "height": max(1, int(m["height"] * (b - t)))}
            shot = self._mss.grab(box)
            img = Image.frombytes("RGB", shot.size, shot.rgb).convert("L")
            brightness.append(sum(img.resize((16, 16)).getdata()) / 256)
            scale = min(1.0, OCR_WIDTH / img.width)
            if scale < 1.0:
                img = img.resize((int(img.width * scale), int(img.height * scale)), Image.BILINEAR)
            img = ImageOps.autocontrast(img)
            parts.append(img)
            offsets.append((name, y, y + img.height))
            y += img.height + 24
        canvas = Image.new("L", (max(p.width for p in parts), y), 0)
        pos = 0
        for img in parts:
            canvas.paste(img, (0, pos))
            pos += img.height + 24
        return canvas, offsets, max(brightness)

    @staticmethod
    def ocr(image) -> list:
        """[(y, text)] lines, using tesseract's per-word confidence."""
        buf = io.BytesIO()
        image.save(buf, format="PNG", compress_level=1)
        proc = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "11", "-l", "eng", "tsv"],
                              input=buf.getvalue(), capture_output=True, timeout=20,
                              preexec_fn=lambda: os.nice(10))
        lines: dict = {}
        for row in proc.stdout.decode("utf-8", "replace").splitlines()[1:]:
            cols = row.split("\t")
            if len(cols) < 12 or not cols[11].strip():
                continue
            try:
                conf = float(cols[10])
            except ValueError:
                continue
            if conf < MIN_CONFIDENCE:
                continue
            key = (cols[2], cols[3], cols[4])
            top = int(cols[7])
            entry = lines.setdefault(key, [top, []])
            entry[1].append(cols[11])
        return [(top, " ".join(words)) for top, words in lines.values()]

    def read(self) -> tuple:
        """([(region, text)], seconds taken, brightest region brightness)."""
        start = time.monotonic()
        image, offsets, brightness = self.capture()
        out = []
        for y, text in self.ocr(image):
            region = next((name for name, a, b in offsets if a <= y < b), "?")
            text = text.strip()
            if len(normalize(text)) >= 3:
                out.append((region, text))
        return out, time.monotonic() - start, brightness


# --- per-game learned profile ----------------------------------------------------------------------------

def load_profile(appid: int) -> dict:
    try:
        return json.loads((PROFILE_DIR / f"{appid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"victory": [], "death": [], "triggers": {}, "target_region": None}


def save_profile(appid: int, profile: dict) -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    (PROFILE_DIR / f"{appid}.json").write_text(json.dumps(profile, indent=1), encoding="utf-8")


def read_log(appid: int) -> list:
    """[(time, region, text)] from the last sessions' screen log."""
    entries = []
    for path in (LOG_DIR / f"{appid}.log.1", LOG_DIR / f"{appid}.log"):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t", 2)
                    if len(parts) == 3:
                        entries.append((float(parts[0]), parts[1], parts[2]))
        except (OSError, ValueError):
            continue
    return entries


def learn(appid: int, model: matcher.AchModel, confirmed_at: float, entries: list | None = None) -> dict:
    """After you confirm an achievement, remember what the screen showed around it."""
    profile = load_profile(appid)
    entries = entries if entries is not None else read_log(appid)
    window = [e for e in entries if confirmed_at - 3 * 3600 <= e[0] <= confirmed_at]
    names = set().union(*model.phrases) if model.phrases else set()
    core = {t for t in model.words if t not in matcher.ACTIONS and t not in matcher.GENERIC_WORDS} or set(model.words)
    hits = [e for e in window if (set(matcher.tokens(e[2])) & (names or core))]
    if not hits:
        return profile
    defeat_named = model.action == "defeat" and bool(model.phrases)
    if not defeat_named:
        # For a boss the name is only the target (its health bar), never proof of the kill.
        triggers = profile.setdefault("triggers", {}).setdefault(model.name, [])
        for _t, _region, text in hits[-3:]:
            norm = normalize(text)
            if norm and norm not in triggers:
                triggers.append(norm)
    if defeat_named:
        regions = [r for _t, r, _x in hits]
        profile["target_region"] = max(set(regions), key=regions.count)
        # Short text that appeared shortly after the target was last seen is this game's victory banner.
        last = hits[-1][0]
        after = [e for e in window if last < e[0] <= last + 60 and e[1] in ("top", "center")]
        for _t, _r, text in after:
            norm = normalize(text)
            words = norm.split()
            if 1 <= len(words) <= 6 and not (set(matcher.tokens(text)) & names):
                if norm not in profile.setdefault("victory", []):
                    profile["victory"].append(norm)
                break
    save_profile(appid, profile)
    return profile


# --- matching screen text to achievements ------------------------------------------------------------

@dataclass
class ScreenHit:
    name: str
    satisfied: bool
    confidence: str
    reason: str


@dataclass
class _Encounter:
    first: float
    last: float
    sightings: int = 1
    died: bool = False


@dataclass
class ScreenMatcher:
    models: list
    profile: dict = field(default_factory=dict)
    rejected: set = field(default_factory=set)

    def __post_init__(self):
        self.index = matcher.index_models(self.models)
        self.pending: dict = {}          # achievement -> time first seen (direct hits wait for a 2nd sighting)
        self.encounters: dict = {}       # achievement -> _Encounter
        self.done: set = set()
        self.victory = [normalize(v) for v in DEFAULT_VICTORY] + [normalize(v) for v in self.profile.get("victory", [])]
        self.learned_victory = {normalize(v) for v in self.profile.get("victory", [])}
        self.death = [normalize(d) for d in DEFAULT_DEATH] + [normalize(d) for d in self.profile.get("death", [])]
        self.triggers = {name: [normalize(t) for t in texts] for name, texts in self.profile.get("triggers", {}).items()}

    def _blocked(self, name: str, text: str) -> bool:
        return (name, f"screen:{normalize(text)}") in self.rejected

    @staticmethod
    def _similar(a: str, b: str) -> bool:
        return a == b or (len(a) > 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.82)

    def _is_death(self, norm: str) -> bool:
        return any(d == norm or (len(d) > 4 and d in norm) for d in self.death)

    def _victory_kind(self, norm: str) -> str | None:
        for v in self.learned_victory:
            if self._similar(v, norm):
                return "learned"
        words = set(norm.split())
        for v in self.victory:
            if (" " in v and v in norm) or (" " not in v and v in words):
                return "default"
        return None

    def feed(self, lines: list, now: float | None = None) -> list:
        """lines: [(region, text)] from one screen read. Returns new ScreenHits."""
        now = time.time() if now is None else now
        hits: list = []
        norms = [(region, text, normalize(text)) for region, text in lines]

        if any(self._is_death(n) for _r, _t, n in norms):
            for enc in self.encounters.values():
                enc.died = True

        for region, text, norm in norms:
            toks = matcher.tokens(text)
            words = {t for t in toks if not t.isdigit()}
            numbers = [int(t) for t in toks if t.isdigit()]

            # Learned trigger text for a specific achievement.
            for name, texts in self.triggers.items():
                if name not in self.done and any(self._similar(t, norm) for t in texts) and not self._blocked(name, text):
                    hits.append(self._hit(name, True, "high", f'Seen on screen: "{text}"'))

            candidates = {id(m): m for t in words for m in self.index.get(t, ())}
            for m in candidates.values():
                if m.name in self.done or self._blocked(m.name, text):
                    continue
                score = matcher._coverage(m, words)
                named = bool(m.phrases) and any(p <= words for p in m.phrases)
                if m.phrases and not named:
                    score *= 0.6
                # A quest log / objective line repeats the description itself ("Defeat Iudex Gundyr").
                objective = m.action is not None and m.action in words and bool(toks) and toks[0] == m.action
                if objective:
                    continue
                if m.action == "defeat" and named:
                    enc = self.encounters.get(m.name)
                    if enc is None or enc.died or now - enc.last > ENCOUNTER_TIMEOUT:
                        self.encounters[m.name] = _Encounter(now, now)
                    else:
                        enc.last = now
                        enc.sightings += 1
                    continue
                if score < matcher.HIGH:
                    continue
                hit = self._direct(m, text, numbers, now)
                if hit:
                    hits.append(hit)

        # A named target seen repeatedly, then a victory banner with no death in between.
        victory = [(self._victory_kind(n), t) for _r, t, n in norms]
        victory = [(k, t) for k, t in victory if k]
        if victory:
            kind, text = victory[0]
            for name, enc in list(self.encounters.items()):
                if name in self.done or enc.died or enc.sightings < 2 or now - enc.last > ENCOUNTER_TIMEOUT:
                    continue
                confidence = "high" if kind == "learned" else "medium"
                hits.append(self._hit(name, True, confidence, f'Seen on screen: target, then "{text}"'))
                del self.encounters[name]
        return hits

    def _direct(self, m: matcher.AchModel, text: str, numbers: list, now: float) -> ScreenHit | None:
        if m.ordinal and "level" in m.words:
            # Levels are everywhere (enemy nameplates, gear): only a bare "Level 20" or a level-up message counts.
            norm = normalize(text)
            if not (re.fullmatch(r"(level|lv|lvl) ?\d+", norm) or "level up" in norm):
                return None
        if m.number is not None:
            if not numbers:
                return None
            value = numbers[0]
            if m.ordinal:
                need = m.number if m.action != "complete" else m.number + 1
                satisfied = value >= need or (m.action == "complete" and value == m.number and "complete" in normalize(text))
            else:
                satisfied = value >= m.number
            if not satisfied:
                return None
        first = self.pending.get(m.name)
        if first is None or now - first > CONFIRM_WINDOW:
            self.pending[m.name] = now
            return None
        del self.pending[m.name]
        # "Lv 20" is as likely an enemy's label as your own level: ask instead of unlocking.
        confidence = "medium" if m.ordinal and "level" in m.words else "high"
        return self._hit(m.name, True, confidence, f'Seen on screen: "{text}"')

    def _hit(self, name: str, satisfied: bool, confidence: str, reason: str) -> ScreenHit:
        self.done.add(name)
        return ScreenHit(name, satisfied, confidence, reason)


# --- background thread ------------------------------------------------------------------------------------

class ScreenWatcher(threading.Thread):
    """Reads the screen every `interval` seconds and puts ScreenHits on `events`."""

    def __init__(self, appid: int, screen_matcher: ScreenMatcher, interval: float = 2.5, monitor: int = 1):
        super().__init__(name="achtrack-screen", daemon=True)
        self.appid = appid
        self.matcher = screen_matcher
        self.interval = max(1.0, interval)
        self.monitor = monitor
        self.events: queue.Queue = queue.Queue()
        self.status = "starting"
        self.last_ocr_seconds = 0.0
        self._stop = threading.Event()
        self._dark = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            reader = ScreenReader(self.monitor)
        except Exception as e:
            self.status = f"can't capture the screen: {e}"
            log.warning(self.status)
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{self.appid}.log"
        if path.exists() and path.stat().st_size > 4_000_000:
            path.replace(LOG_DIR / f"{self.appid}.log.1")
        self.status = "watching"
        try:
            with open(path, "a", encoding="utf-8") as logf:
                while not self._stop.wait(self.interval):
                    try:
                        lines, took, brightness = reader.read()
                    except (subprocess.SubprocessError, OSError) as e:
                        self.status = f"OCR failed: {e}"
                        continue
                    except Exception as e:  # capture backend errors (e.g. display gone)
                        self.status = f"capture failed: {e}"
                        self._stop.wait(10)
                        continue
                    self.last_ocr_seconds = took
                    # A black capture every time means the session doesn't let X11 capture see the game.
                    self._dark = self._dark + 1 if brightness < 2 else 0
                    if self._dark >= 10:
                        self.status = "the captured screen is black - on Wayland, log in with an X11 session"
                    elif self.status != "watching":
                        self.status = "watching"
                    now = time.time()
                    for region, text in lines:
                        logf.write(f"{now:.1f}\t{region}\t{text}\n")
                    logf.flush()
                    for hit in self.matcher.feed(lines, now):
                        self.events.put(hit)
        finally:
            reader.close()
            self.status = "stopped"
