"""Parsers for the achievement files written by Steam emulators.

Every parser returns {api_name: AchState}. Formats handled:
  * Goldberg / GBE / EMPRESS json:  {"ACH": {"earned": true, "earned_time": 1690000000}}
  * CODEX / RUNE ini:               [ACH] Achieved=1 UnlockTime=1690000000
  * OnlineFix ini:                  [ACH] achieved=true timestamp=1690000000
  * SKIDROW / flat ini:             [SteamAchievements] ACH=1
  * 3DM / Reloaded ini:             [State] ACH=0101  [Time] ACH=<hex>
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

MIN_TS = 946684800      # 2000-01-01
MAX_TS = 4102444800     # 2100-01-01

EARNED_KEYS = ("earned", "achieved", "unlocked", "haveachieved", "state", "done")
TIME_KEYS = ("earned_time", "unlocktime", "unlock_time", "unlocked_time", "timestamp", "haveachievedtime", "time", "date")
CUR_KEYS = ("progress", "curprogress", "cur_progress", "current")
MAX_KEYS = ("max_progress", "maxprogress", "max")

FLAT_SECTIONS = {"steamachievements", "achievements", "unlocked", "achievementsunlocked"}
TIME_SECTIONS = {"time", "achievementsunlocktimes", "unlocktimes", "unlocktime"}


@dataclass
class AchState:
    name: str
    unlocked: bool
    unlocked_at: int | None = None
    cur: float | None = None
    max: float | None = None


def _clean(value) -> str:
    return str(value).strip().strip('"').strip()


def _ts(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(_clean(value))
    except ValueError:
        return None
    if n > 1e12:
        n /= 1000
    return int(n) if MIN_TS <= n <= MAX_TS else None


def _hex_ts(value) -> int | None:
    ts = _ts(value)
    if ts is not None:
        return ts
    s = _clean(value)
    try:
        raw = bytes.fromhex(s)
    except ValueError:
        return None
    for order in ("big", "little"):
        n = int.from_bytes(raw, order)
        if MIN_TS <= n <= MAX_TS:
            return n
    return None


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(_clean(value))
    except ValueError:
        return None


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None:
        return False
    s = _clean(value).lower()
    if s in ("true", "yes", "on"):
        return True
    if s in ("", "false", "no", "off", "null", "none"):
        return False
    for base in (10, 16):
        try:
            return int(s, base) != 0
        except ValueError:
            pass
    return False


def _pick(mapping: dict, keys: tuple):
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        if key in lowered:
            return lowered[key]
    return None


def read_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if len(data) > 3 and data[1] == 0 and data[3] == 0:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def parse_json(path: Path) -> dict:
    data = json.loads(read_text(path))
    if isinstance(data, dict):
        items = list(data.items())
    elif isinstance(data, list):
        items = [(_pick(d, ("name", "apiname", "id")), d) for d in data if isinstance(d, dict)]
    else:
        raise ValueError("unexpected achievement json layout")
    out = {}
    for name, entry in items:
        if not name:
            continue
        name = str(name)
        if isinstance(entry, dict):
            unlocked = _truthy(_pick(entry, EARNED_KEYS))
            ts = _ts(_pick(entry, TIME_KEYS))
            cur = _num(_pick(entry, CUR_KEYS))
            mx = _num(_pick(entry, MAX_KEYS))
        else:
            unlocked, ts, cur, mx = _truthy(entry), None, None, None
        out[name] = AchState(name, unlocked, ts if unlocked else None, cur, mx)
    return out


def parse_ini_sections(text: str) -> dict:
    """Very tolerant ini reader: keeps key case, merges duplicate sections, ignores junk lines."""
    sections: dict = {}
    current = sections.setdefault("", {})
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if line.startswith("[") and "]" in line:
            current = sections.setdefault(line[1:line.index("]")].strip(), {})
        elif "=" in line:
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip()
    return sections


def parse_ini(path: Path) -> dict:
    sections = parse_ini_sections(read_text(path))
    out: dict = {}
    times: dict = {}

    def get(name: str) -> AchState:
        return out.setdefault(name, AchState(name, False))

    for section, values in sections.items():
        low = section.lower()
        lowered = {k.lower(): v for k, v in values.items()}
        if not section and not values:
            continue
        if low == "state":
            for name, value in values.items():
                get(name).unlocked = _truthy(value)
        elif low in TIME_SECTIONS:
            for name, value in values.items():
                times[name] = _hex_ts(value)
        elif section and any(k in lowered for k in EARNED_KEYS):
            state = get(section)
            state.unlocked = _truthy(_pick(lowered, EARNED_KEYS))
            state.unlocked_at = _ts(_pick(lowered, TIME_KEYS))
            state.cur = _num(_pick(lowered, CUR_KEYS))
            state.max = _num(_pick(lowered, MAX_KEYS))
        elif low in FLAT_SECTIONS:
            for name, value in values.items():
                # CODEX keeps an index here ("00000=ACH_NAME", "Count=N") - not state.
                if name.isdigit() or name.lower() == "count":
                    continue
                get(name).unlocked = _truthy(value)

    for name, ts in times.items():
        if name in out and out[name].unlocked and out[name].unlocked_at is None:
            out[name].unlocked_at = ts
    for state in out.values():
        if not state.unlocked:
            state.unlocked_at = None
    return out


def parse_file(path: Path) -> dict:
    if path.suffix.lower() == ".json":
        return parse_json(path)
    return parse_ini(path)
