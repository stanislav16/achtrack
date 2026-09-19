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
from collections import deque
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


WAYLAND_TOOLS = ("grim", "cosmic-screenshot", "gnome-screenshot", "spectacle")


def _is_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))


def backend_candidates() -> list:
    """Capture methods to try, best first. X11 capture sees nothing of other apps on Wayland."""
    x11 = ["x11"] if os.environ.get("DISPLAY") else []
    tools = [t for t in WAYLAND_TOOLS if shutil.which(t)]
    return tools + x11 if _is_wayland() else x11 + tools


def available() -> tuple:
    """(ok, reason)."""
    if not shutil.which("tesseract"):
        return False, "tesseract isn't installed (sudo apt install tesseract-ocr)"
    try:
        from PIL import Image  # noqa: F401
        if not _is_wayland():
            import mss  # noqa: F401
    except ImportError:
        return False, "the mss / Pillow Python packages are missing - run install.sh again"
    if not backend_candidates():
        return False, "no way to capture the screen on this desktop"
    return True, ""


def _brightness(img) -> float:
    from PIL import ImageStat
    return ImageStat.Stat(img.convert("L").resize((16, 16))).mean[0]


# --- capture + OCR ------------------------------------------------------------------------------------

class ScreenReader:
    def __init__(self, monitor: int = 1, backend: str | None = "auto"):
        self.monitor_index = monitor
        self._mss = None
        self._shots = LOG_DIR / "shots"
        self.backend = self._pick() if backend == "auto" else backend

    def _pick(self) -> str:
        """First method that returns a non-black picture (X11 capture on Wayland returns black)."""
        fallback = None
        for name in backend_candidates():
            try:
                img = self._grab_with(name)
            except Exception as e:
                log.info("screen capture with %s failed: %s", name, e)
                continue
            fallback = fallback or name
            if _brightness(img) >= 2:
                return name
        if fallback is None:
            raise RuntimeError("no screen capture method works here")
        return fallback

    def _grab_with(self, name: str):
        from PIL import Image
        if name == "x11":
            if self._mss is None:
                import mss
                self._mss = mss.mss()
            monitors = self._mss.monitors
            m = monitors[self.monitor_index] if 0 < self.monitor_index < len(monitors) else monitors[-1]
            shot = self._mss.grab(m)
            return Image.frombytes("RGB", shot.size, shot.rgb)
        if name == "grim":
            out = subprocess.run(["grim", "-t", "ppm", "-"], capture_output=True, timeout=10, check=True).stdout
            return Image.open(io.BytesIO(out)).convert("RGB")
        self._shots.mkdir(parents=True, exist_ok=True)
        target = self._shots / "shot.png"
        if name == "cosmic-screenshot":
            for old in self._shots.glob("*.png"):
                old.unlink(missing_ok=True)
            proc = subprocess.run(["cosmic-screenshot", "--interactive=false", "--modal=false", "--notify=false",
                                   f"--save-dir={self._shots}"], capture_output=True, timeout=15, check=True)
            printed = proc.stdout.decode("utf-8", "replace").strip().splitlines()
            path = printed[-1] if printed and os.path.isfile(printed[-1]) else next(iter(self._shots.glob("*.png")), None)
            if not path:
                raise RuntimeError("cosmic-screenshot saved nothing")
            target = path
        elif name == "gnome-screenshot":
            subprocess.run(["gnome-screenshot", "-f", str(target)], capture_output=True, timeout=15, check=True)
        elif name == "spectacle":
            subprocess.run(["spectacle", "-b", "-n", "-f", "-o", str(target)], capture_output=True, timeout=15, check=True)
        img = Image.open(target).convert("RGB")
        img.load()
        os.unlink(target)
        return img

    def close(self) -> None:
        try:
            if self._mss is not None:
                self._mss.close()
        except Exception:
            pass

    def grab(self):
        return self._grab_with(self.backend)

    @staticmethod
    def crops(full) -> list:
        """[(region name, grayscale crop)] for every watched area of a full-screen picture."""
        width, height = full.size
        gray = full.convert("L")
        return [(name, gray.crop((int(width * l), int(height * t), int(width * r), int(height * b))))
                for name, l, t, r, b in REGIONS]

    @staticmethod
    def _bright_text(img):
        """Black-on-white image of only the brightest pixels: game UI text is nearly always bright,
        and this strips the busy scene behind it, which is what trips tesseract up."""
        hist = img.histogram()
        total, seen, p93 = sum(hist), 0, 255
        for value, count in enumerate(hist):
            seen += count
            if seen >= total * 0.93:
                p93 = value
                break
        cut = min(235, max(150, p93 - 10))
        return img.point([0 if v >= cut else 255 for v in range(256)])

    @classmethod
    def stack(cls, crops: list):
        """One image holding every region twice - as captured and as isolated bright text - so a
        single tesseract call reads them all; plus where each region sits in it."""
        from PIL import Image, ImageOps
        parts, offsets, y = [], [], 0
        for name, img in crops:
            # Tesseract reads text best around 25-40 px high: enlarge the narrow side areas, never shrink.
            if img.width < 900:
                img = img.resize((int(img.width * 1.6), int(img.height * 1.6)), Image.BICUBIC)
            for variant in (ImageOps.autocontrast(img), cls._bright_text(img)):
                parts.append(variant)
                offsets.append((name, y, y + variant.height))
                y += variant.height + 24
        canvas = Image.new("L", (max(p.width for p in parts), y), 255)
        pos = 0
        for img in parts:
            canvas.paste(img, (0, pos))
            pos += img.height + 24
        return canvas, offsets

    def ocr_regions(self, crops: list) -> list:
        image, offsets = self.stack(crops)
        out, seen = [], set()
        for y, text in self.ocr(image):
            region = next((name for name, a, b in offsets if a <= y < b), "?")
            text = text.strip()
            key = (region, normalize(text))
            if len(key[1]) >= 3 and key not in seen:     # each region is read twice: keep one copy
                seen.add(key)
                out.append((region, text))
        return out

    def capture(self):
        """Stacked grayscale image of all regions, where each region starts, and the brightest region."""
        crops = self.crops(self.grab())
        image, offsets = self.stack(crops)
        return image, offsets, max(_brightness(img) for _n, img in crops)

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


def probe_backends(monitor: int = 1) -> list:
    """[(method, works, detail)] for every capture method available here."""
    reader = ScreenReader(monitor, backend=None)
    results = []
    try:
        for name in backend_candidates():
            start = time.monotonic()
            try:
                img = reader._grab_with(name)
            except Exception as e:
                results.append((name, False, f"failed: {str(e)[:120]}"))
                continue
            ok = _brightness(img) >= 2
            results.append((name, ok, f"{'works' if ok else 'black picture'}, {img.size[0]}x{img.size[1]}, "
                                      f"{time.monotonic() - start:.2f} s"))
    finally:
        reader.close()
    return results


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
                hit = self._direct(m, text, numbers, now, region)
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

    def _direct(self, m: matcher.AchModel, text: str, numbers: list, now: float, region: str = "") -> ScreenHit | None:
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
        # A one-word banner that is exactly the achievement's key word ("STAGGERED" for "Stagger an
        # enemy") is unambiguous; banners like that can be on screen for under a second.
        core = [t for t in m.words if t not in matcher.ACTIONS and t not in matcher.GENERIC_WORDS]
        banner = matcher.tokens(text)
        if m.number is None and region in ("top", "center") and len(core) == 1 and banner == core:
            return self._hit(m.name, True, "high", f'Seen on screen: "{text}"')
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

class FlashDetector:
    """Cheap per-frame check (a few ms) for text appearing: game banners are bright, sharp-edged
    shapes, so a jump in "bright edge" density against the region's recent average means new text."""

    THUMB_WIDTH = 240
    _BRIGHT = [255 if v > 185 else 0 for v in range(256)]

    def __init__(self):
        self.baseline: dict = {}

    def check(self, name: str, img) -> bool:
        from PIL import ImageChops, ImageFilter, ImageStat
        scale = self.THUMB_WIDTH / max(1, img.width)
        thumb = img.resize((self.THUMB_WIDTH, max(1, int(img.height * scale))))
        edges = thumb.filter(ImageFilter.FIND_EDGES)
        score = ImageStat.Stat(ImageChops.multiply(edges, thumb.point(self._BRIGHT))).mean[0]
        base = self.baseline.get(name)
        if base is None:
            self.baseline[name] = score
            return False
        fired = score > base * 1.6 + 1.5
        # Follow slow changes (new area, lighting) but not the flash itself.
        self.baseline[name] = base * 0.97 + score * 0.03 if fired else base * 0.85 + score * 0.15
        return fired


class ScreenWatcher(threading.Thread):
    """Captures the screen every ~0.5 s. OCR runs when the flash detector sees text appear, plus a
    full read every few seconds for text that stays up (boss names, counters). OCR time is capped
    so it never takes more than a fraction of one CPU core."""

    FULL_READ_EVERY = 3.0      # seconds between full reads of all regions
    OCR_BUDGET = 0.4           # max share of wall time spent in OCR (of one core)

    def __init__(self, appid: int, screen_matcher: "ScreenMatcher | None", interval: float = 0.5,
                 monitor: int = 1, on_read=None):
        super().__init__(name="achtrack-screen", daemon=True)
        self.appid = appid
        self.matcher = screen_matcher
        self.interval = max(0.2, interval)
        self.monitor = monitor
        self.on_read = on_read          # callback(kind, lines, ocr_seconds) - used by screen-test
        self.events: queue.Queue = queue.Queue()
        self.status = "starting"
        self.last_ocr_seconds = 0.0
        self.stats = {"captures": 0, "ocr_runs": 0, "flash_runs": 0, "grab_seconds": 0.0, "ocr_seconds": 0.0}
        self._stop = threading.Event()
        self._dark = 0
        self._ocr_log: deque = deque()

    def stop(self) -> None:
        self._stop.set()

    def _budget_left(self, now: float) -> bool:
        while self._ocr_log and self._ocr_log[0][0] < now - 10:
            self._ocr_log.popleft()
        return sum(d for _t, d in self._ocr_log) < 10 * self.OCR_BUDGET

    def run(self) -> None:
        try:
            reader = ScreenReader(self.monitor)
        except Exception as e:
            self.status = f"can't capture the screen: {e}"
            log.warning(self.status)
            return
        logf = None
        if self.appid:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / f"{self.appid}.log"
            if path.exists() and path.stat().st_size > 4_000_000:
                path.replace(LOG_DIR / f"{self.appid}.log.1")
            logf = open(path, "a", encoding="utf-8")
        self.status = "watching"
        log.info("screen capture method: %s", reader.backend)
        detector = FlashDetector()
        next_full = 0.0
        grab_avg = 0.0
        delay = 0.0
        try:
            while not self._stop.wait(delay):
                tick = time.monotonic()
                try:
                    full = reader.grab()
                except Exception as e:
                    self.status = f"capture failed: {e}"
                    delay = 10.0
                    continue
                grab = time.monotonic() - tick
                grab_avg = grab if not grab_avg else grab_avg * 0.8 + grab * 0.2
                self.stats["captures"] += 1
                self.stats["grab_seconds"] += grab
                crops = reader.crops(full)
                brightness = max(_brightness(img) for _n, img in crops)
                self._dark = self._dark + 1 if brightness < 2 else 0
                if self._dark >= 20:
                    self.status = f"the captured screen is black ({reader.backend} capture)"
                elif self.status != "watching":
                    self.status = "watching"

                flashing = [name for name, img in crops if detector.check(name, img)]
                if tick >= next_full:
                    targets, kind = [c for c in crops], "full"
                    next_full = tick + self.FULL_READ_EVERY
                elif flashing and self._budget_left(tick):
                    targets, kind = [c for c in crops if c[0] in flashing], "flash"
                else:
                    targets, kind = [], ""
                if targets:
                    start = time.monotonic()
                    try:
                        lines = reader.ocr_regions(targets)
                    except (subprocess.SubprocessError, OSError) as e:
                        self.status = f"OCR failed: {e}"
                        lines = []
                    took = time.monotonic() - start
                    self._ocr_log.append((tick, took))
                    self.last_ocr_seconds = took
                    self.stats["ocr_runs"] += 1
                    self.stats["flash_runs"] += kind == "flash"
                    self.stats["ocr_seconds"] += took
                    now = time.time()
                    if logf is not None:
                        for region, text in lines:
                            logf.write(f"{now:.1f}\t{region}\t{text}\n")
                        logf.flush()
                    if self.on_read:
                        self.on_read(kind, lines, took)
                    if self.matcher is not None:
                        for hit in self.matcher.feed(lines, now):
                            self.events.put(hit)
                # Capture no faster than the capture method itself can keep up with.
                period = max(self.interval, grab_avg * 1.3)
                delay = max(0.02, period - (time.monotonic() - tick))
        finally:
            reader.close()
            if logf is not None:
                logf.close()
            self.status = "stopped"
