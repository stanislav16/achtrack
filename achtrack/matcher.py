"""Matches save-file values to achievement descriptions with plain word matching (no AI, no network).

An achievement description is reduced to weighted words ("Collect 50 moonstones" -> collect,
moonston + target 50), proper names ("Soul of Cinder") and whether its number is a count or a
chapter/level. Each save field name is reduced the same way ("StonesCollected" -> ston, collect).
A field matches when it covers most of an achievement's distinctive words; rare words (names,
items) weigh more than words shared by many achievements ("enemy", "defeat").
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "with", "and", "or", "your", "you", "yours", "all", "any",
    "each", "every", "time", "times", "game", "from", "by", "as", "is", "be", "it", "its", "that", "this", "into",
    "without", "while", "during", "using", "use", "least", "more", "than", "most", "once", "first", "have", "has",
    "get", "got", "make", "take", "total", "over", "up", "out", "after", "before", "within", "single", "same",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "different", "unique",
    "achievement", "achievements", "trophy", "mode", "difficulty", "new", "or", "more",
}

_RAW_SYNONYMS = {
    "kill": "defeat", "slay": "defeat", "slain": "defeat", "beat": "defeat", "defeat": "defeat", "destroy": "defeat",
    "vanquish": "defeat", "killed": "defeat", "kills": "defeat", "death": "defeat",
    "collect": "collect", "obtain": "collect", "gather": "collect", "acquire": "collect", "pick": "collect",
    "find": "collect", "found": "collect", "loot": "collect", "discover": "collect", "own": "collect",
    "complete": "complete", "finish": "complete", "clear": "complete", "done": "complete", "completion": "complete",
    "cleared": "complete", "finished": "complete", "beaten": "complete",
    "reach": "reach", "attain": "reach", "unlock": "unlock", "unlocked": "unlock", "open": "open",
    "craft": "craft", "build": "craft", "forge": "craft", "upgrade": "upgrade", "win": "win", "won": "win",
    "victory": "win", "enemies": "enemy", "bosses": "boss", "lvl": "level", "lv": "level", "ch": "chapter",
}
ORDINAL_WORDS = {"chapter", "act", "level", "stage", "episode", "mission", "part", "rank", "tier", "floor", "wave",
                 "area", "zone", "day", "season", "round", "world", "circle", "phase", "quest"}
PROGRESS_WORDS = {"complete", "highest", "max", "best", "unlock", "cleared", "reached"}
NOISE_WORDS = {
    "timer", "playtime", "second", "sec", "date", "pos", "position", "loc", "location", "rot", "rotation", "camera",
    "cam", "seed", "random", "rng", "version", "ver", "guid", "hash", "checksum", "setting", "option", "volume",
    "sensitivity", "resolution", "scale", "velocity", "speed", "offset", "anim", "frame", "tick", "fov",
    "brightness", "gamma", "language", "audio", "graphic", "quality", "timestamp", "utc", "epoch", "session",
    "slot", "screenshot", "thumbnail", "texture", "mesh", "transform", "input", "keybind", "binding", "color",
}
# Words that say what kind of thing, not which one: a field needn't contain them to match.
GENERIC_WORDS = {"enemy", "boss", "item", "foe", "opponent", "monster", "creature", "target", "character", "npc",
                 "thing", "object", "people", "person", "unit", "mob"}
# Fields describing limits, prices or spending rather than progress ("MoonstonesRequired = 50").
NEGATIVE_WORDS = {"max", "cap", "limit", "required", "requirement", "need", "goal", "target", "cost", "price",
                  "sold", "sell", "spend", "spent", "lost", "lose", "consume", "discard", "drop", "capacity",
                  "default", "min", "threshold"}
PROGRESS_OVERRIDE = {"reach", "complete", "unlock", "highest", "best", "achiev", "record"}
IRREGULAR = {"caught": "catch", "ate": "eat", "bought": "buy", "fought": "fight", "brought": "bring",
             "taught": "teach", "thrown": "throw", "drove": "drive", "rode": "ride", "ridden": "ride",
             "spoke": "speak", "stole": "steal", "stolen": "steal", "built": "build", "fell": "fall",
             "fallen": "fall", "flew": "fly", "flown": "fly", "swam": "swim", "sang": "sing", "ran": "run",
             "hit": "hit", "made": "make", "met": "meet", "saved": "save", "died": "die", "dead": "die"}
KEY_PREFIXES = {"b", "m", "f", "i", "n", "s", "p", "k", "e", "is", "has", "was"}

HIGH = 0.8      # share of an achievement's weighted words a field must cover for an automatic unlock
MEDIUM = 0.6    # ...for a suggestion in the review


def stem(word: str) -> str:
    w = word.lower()
    if w in _RAW_SYNONYMS or len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("sses"):
        w = w[:-2]
    elif w.endswith(("ches", "shes", "xes", "zes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    if w.endswith("ied") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("ed") and len(w) > 4:
        w = w[:-2]
    elif w.endswith("ing") and len(w) > 5:
        w = w[:-3]
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "lsz":
        w = w[:-1]
    return w


SYNONYMS = {**{stem(k): v for k, v in _RAW_SYNONYMS.items()}, **_RAW_SYNONYMS}
ACTIONS = set(_RAW_SYNONYMS.values()) - {"enemy", "boss", "level", "chapter"}
PLAYER_WORDS = {"player", "character", "hero", "pc", "protagonist", "profile"}


def canon(word: str) -> str:
    word = IRREGULAR.get(word.lower(), word)
    s = stem(word)
    return SYNONYMS.get(word.lower(), SYNONYMS.get(s, s))


# Compare word lists in the same normalised form the tokens use ("required" -> "requir").
ORDINAL_WORDS = {canon(w) for w in ORDINAL_WORDS}
PROGRESS_WORDS = {canon(w) for w in PROGRESS_WORDS}
NOISE_WORDS = {canon(w) for w in NOISE_WORDS}
GENERIC_WORDS = {canon(w) for w in GENERIC_WORDS}
NEGATIVE_WORDS = {canon(w) for w in NEGATIVE_WORDS}
PROGRESS_OVERRIDE = {canon(w) for w in PROGRESS_OVERRIDE}
PLAYER_WORDS = {canon(w) for w in PLAYER_WORDS}


def split_words(text: str) -> list:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)
    return re.findall(r"[A-Za-z]+|\d+", text)


def tokens(text: str, is_key: bool = False) -> list:
    out = []
    for w in split_words(text):
        if w.isdigit():
            out.append(str(int(w)))
            continue
        low = w.lower()
        if low in STOPWORDS or (is_key and low in KEY_PREFIXES):
            continue
        out.append(canon(low))
    return out


_KEY_CACHE: dict = {}


def key_tokens(key: str) -> tuple:
    """(word set, number set) for a flattened save path, ignoring list indexes."""
    clean = re.sub(r"\[\d+\]", "", key).replace(".__len__", " count").replace("__class__", "")
    hit = _KEY_CACHE.get(clean)
    if hit is None:
        toks = tokens(clean, is_key=True)
        hit = ({t for t in toks if not t.isdigit()}, {t for t in toks if t.isdigit()})
        if len(_KEY_CACHE) > 100_000:
            _KEY_CACHE.clear()
        _KEY_CACHE[clean] = hit
    return hit


# --- achievements --------------------------------------------------------------------------------

@dataclass
class AchModel:
    name: str
    words: dict = field(default_factory=dict)      # token -> weight
    phrases: list = field(default_factory=list)    # sets of tokens that must appear together
    number: int | None = None
    ordinal: bool = False
    action: str | None = None


PHRASE_RE = re.compile(r"[A-Z][\w'’-]*(?:\s+(?:of|the|de|du|von|van|and|in)\s+[A-Z][\w'’-]*|\s+[A-Z][\w'’-]*)*")


def _model(name: str, text: str) -> AchModel:
    model = AchModel(name)
    words = tokens(text)
    model.action = next((w for w in words if w in ACTIONS), None)
    m = re.search(r"(\d[\d,]*)", text)
    if m:
        model.number = int(m.group(1).replace(",", ""))
        before = text[:m.start()].split()[-2:]
        model.ordinal = any(canon(w.strip(".,:;")) in ORDINAL_WORDS for w in before)
    for match in PHRASE_RE.finditer(text):
        phrase = match.group(0)
        toks = {t for t in tokens(phrase) if t not in ACTIONS and not t.isdigit()}
        # A capitalised first word alone is just the start of the sentence.
        if not toks or (match.start() == 0 and len(phrase.split()) == 1):
            continue
        model.phrases.append(toks)
    model.words = {w: 1.0 for w in words if not w.isdigit()}
    return model


def build_models(achievements: list) -> list:
    """achievements: rows with name, display_name, description."""
    models = []
    for a in achievements:
        text = (a.get("description") or "").strip() or (a.get("display_name") or "")
        if text:
            models.append(_model(a["name"], text))
    df: dict = {}
    for m in models:
        for w in m.words:
            df[w] = df.get(w, 0) + 1
    n = len(models)
    for m in models:
        for w in m.words:
            weight = math.log((n + 1) / (df[w] + 0.5)) + 0.1
            if w in ACTIONS or w in GENERIC_WORDS:
                weight *= 0.4
            m.words[w] = weight
    return models


def index_models(models: list) -> dict:
    index: dict = {}
    for m in models:
        for w in m.words:
            index.setdefault(w, []).append(m)
    return index


# --- matching -------------------------------------------------------------------------------------

@dataclass
class Match:
    name: str
    key: str
    value: object
    kind: str               # "counter" | "flag" | "ordinal"
    satisfied: bool
    score: float
    confidence: str         # "high" | "medium"
    progress: tuple | None = None

    @property
    def reason(self) -> str:
        return f"{self.key} = {self.value}"


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "none", "no", "locked")
    return bool(value)


def _numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def evaluate(models: list, old: dict | None, new: dict, rejected: set = frozenset()) -> list:
    """Matches for fields that changed between two saves (or all fields when old is None)."""
    index = index_models(models)
    if not index:
        return []
    matches: list = []
    per_key: dict = {}
    for key, value in new.items():
        if old is not None and old.get(key) == value:
            continue
        kset, knums = key_tokens(key)
        if kset & NOISE_WORDS or (kset & NEGATIVE_WORDS and not kset & PROGRESS_OVERRIDE):
            continue
        vset = set()
        if isinstance(value, str) and 0 < len(value) < 120:
            vset = {t for t in tokens(value, is_key=True) if not t.isdigit()}
        hay = kset | vset
        candidates = {id(m): m for t in hay for m in index.get(t, ())}
        for m in candidates.values():
            if (m.name, key) in rejected:
                continue
            score = _coverage(m, hay)
            if kset & PLAYER_WORDS:
                score += 0.1   # "Player.Level" beats "Sword.Level" for "Reach level 20"
            phrase_ok = not m.phrases or any(p <= hay for p in m.phrases)
            if not phrase_ok:
                score *= 0.6
            if score < MEDIUM:
                continue
            result = _judge(m, key, value, kset, knums, score)
            if result:
                matches.append(result)
                per_key.setdefault(key, []).append(result)

    # One flag field that fits several achievements equally is too ambiguous to act on alone.
    for key, found in per_key.items():
        flags = [f for f in found if f.kind == "flag" and f.confidence == "high"]
        if len(flags) > 1:
            for f in flags:
                f.confidence = "medium"
    # Likewise one achievement fitting several fields equally well ("Player.Level" and "Sword.Level").
    per_ach: dict = {}
    for match in matches:
        if match.confidence == "high":
            per_ach.setdefault(match.name, []).append(match)
    for found in per_ach.values():
        top = max(f.score for f in found)
        leaders = [f for f in found if f.score >= top - 0.05]
        # Several fields are fine when they agree (a boss flag and the defeated-bosses list);
        # when they disagree it's unclear which one is the real one.
        disagree = len({f.satisfied for f in leaders}) > 1
        for f in found:
            if disagree or f.score < top - 0.05:
                f.confidence = "medium"
    return matches


def _coverage(m: AchModel, hay: set) -> float:
    """Share of the achievement's distinctive words present in the field; "kind" words
    (actions like defeat/collect, generic nouns like enemy/boss) only count when nothing else does."""
    core = {t: w for t, w in m.words.items() if t not in ACTIONS and t not in GENERIC_WORDS}
    words = core or m.words
    total = sum(words.values()) or 1.0
    return sum(w for t, w in words.items() if t in hay) / total


def _judge(m: AchModel, key: str, value, kset: set, knums: set, score: float) -> Match | None:
    confident = score >= HIGH
    if m.number is not None and m.ordinal:
        if knums:
            # "Chapter5Cleared" style: the field names one specific chapter.
            if str(m.number) not in knums or not (isinstance(value, bool) or _numeric(value) or isinstance(value, str)):
                return None
            return Match(m.name, key, value, "flag", _truthy(value), score, "high" if confident else "medium")
        if not _numeric(value) or value < 0 or value > 100_000:
            return None
        # "CurrentChapter = 6" means chapter 5 is done; "HighestChapterCleared = 5" means the same.
        need = m.number if (kset & PROGRESS_WORDS or m.action != "complete") else m.number + 1
        return Match(m.name, key, value, "ordinal", value >= need, score, "high" if confident else "medium")

    if m.number is not None:
        if not _numeric(value) or value < 0 or value > 10_000_000:
            return None
        if knums and str(m.number) not in knums:
            return None
        return Match(m.name, key, value, "counter", value >= m.number, score,
                     "high" if confident else "medium", progress=(min(value, m.number), m.number))

    if isinstance(value, bool):
        return Match(m.name, key, value, "flag", value, score, "high" if confident else "medium")
    if _numeric(value):
        if value < 0 or value > 10_000_000:
            return None
        return Match(m.name, key, value, "flag", value >= 1, score, "high" if confident else "medium")
    if isinstance(value, str):
        return Match(m.name, key, value, "flag", _truthy(value), score, "high" if confident else "medium")
    return None


def best_per_achievement(matches: list) -> dict:
    """Strongest match per achievement: a confident field wins even when it says "not yet"."""
    best: dict = {}
    for match in matches:
        current = best.get(match.name)
        rank = (match.confidence == "high", match.satisfied, match.score)
        if current is None or rank > (current.confidence == "high", current.satisfied, current.score):
            best[match.name] = match
    return best
