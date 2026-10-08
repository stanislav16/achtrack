"""Builds a "reader kit": everything someone needs to write a save reader for one game.

When a game's crack records nothing and its save is in a format AchTrack can't decode, the missing
piece is a reader (see readers.py). Writing one is a job for a person or a chatbot, away from
AchTrack - so the kit is a folder, and a zip of it, that explains the whole task from nothing:
what AchTrack is, exactly what file to write and what it may contain, the game's achievements,
copies of the save files (and older copies, to compare), a first look at their bytes, a template
and a way to test. It assumes the reader has never heard of AchTrack.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

from . import __version__, savesync
from .readers import ALLOWED_MODULES, READERS_DIR

MAX_COPY = 24 * 1024 * 1024
SIGNATURES = [
    (b"SGB1", "Supergiant Games save (Hades, Hades II): a short header, then LZ4-compressed Lua data"),
    (b"GVAS", "Unreal Engine save (GVAS)"),
    (b"\x1f\x8b", "gzip stream"),
    (b"x\x9c", "zlib stream"), (b"x\x01", "zlib stream"), (b"x\xda", "zlib stream"),
    (b"\x04\"M\x18", "LZ4 frame"),
    (b"(\xb5/\xfd", "Zstandard stream"),
    (b"PK\x03\x04", "zip archive"),
    (b"SQLite format 3", "SQLite database"),
    (b"UnityFS", "Unity asset bundle"),
    (b"BZh", "bzip2 stream"),
    (b"\xfd7zXZ", "xz stream"),
    (b"{", "probably JSON text"), (b"<?xml", "XML text"), (b"<", "possibly XML text"),
]


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "game").lower()).strip("_") or "game"


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data[:1_000_000]:
        counts[b] += 1
    total = min(len(data), 1_000_000)
    return -sum(c / total * math.log2(c / total) for c in counts if c)


def hexdump(data: bytes, limit: int = 128) -> str:
    lines = []
    for offset in range(0, min(len(data), limit), 16):
        chunk = data[offset:offset + 16]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:06x}  {chunk.hex(' '):<47}  {text}")
    return "\n".join(lines)


def describe(path: Path) -> str:
    data = path.read_bytes()
    out = [f"### {path.name}", f"size: {len(data):,} bytes"]
    known = [what for magic, what in SIGNATURES if data.startswith(magic)]
    out.append("starts like: " + (known[0] if known else "nothing AchTrack recognises"))
    bits = entropy(data)
    kind = ("very high - the bulk is compressed or encrypted" if bits > 7.5 else
            "high - likely compressed or packed binary" if bits > 6.5 else
            "moderate - structured binary" if bits > 4.5 else "low - mostly text or sparse data")
    out.append(f"randomness: {bits:.2f} bits per byte ({kind})")
    out.append("first bytes:\n```\n" + hexdump(data) + "\n```")
    strings = [s.decode("ascii") for s in re.findall(rb"[\x20-\x7e]{5,60}", data[:2_000_000])]
    if strings:
        shown = list(dict.fromkeys(strings))[:40]
        out.append(f"readable text inside ({len(strings):,} runs; the first {len(shown)} distinct): " + " | ".join(shown))
    else:
        out.append("readable text inside: none - so any text is compressed or encrypted")
    return "\n".join(out)


TEMPLATE = '''"""Save reader for {game}. Written from an AchTrack reader kit."""
import struct
import zlib

NAME = "{game}"


def can_read(filename: str, data: bytes) -> bool:
    """True only for this game's save files. Check the file's first bytes, not just its name."""
    return data[:4] == b"XXXX"          # TODO: the real signature


def read(filename: str, data: bytes) -> dict:
    """Decode the save into named values. Raise ValueError if the bytes are not what you expect."""
    if not can_read(filename, data):
        raise ValueError("not a {game} save")
    values = {{}}
    # TODO: decode. Keep the game's own names for things. Numbers as int/float, flags as bool,
    # text as str, nested dicts and lists are fine.
    return values


def achievements(values: dict) -> dict:
    """Which achievements this save proves, by their api_name from achievements.json.

    True / False, or (current, target) for a counter. Include an achievement ONLY when one specific
    value in the save settles it. Leave every other achievement out of the dict.
    """
    state = values.get("GameState", {{}})
    return {{
        # "ACH_BEAT_FIRST_BOSS": state.get("EnemyKills", {{}}).get("FirstBoss", 0) >= 1,
        # "ACH_KILL_500": (state.get("TotalKills", 0), 500),
    }}
'''

TESTER = r'''"""Tries a reader on the save files in this kit. Usage:  python3 test_reader.py my_reader.py

Needs only Python 3. It does NOT enforce AchTrack's rules (allowed imports and so on) - AchTrack
checks those when the reader is installed - it only shows what the reader makes of each file.
"""
import importlib.util, json, sys
from pathlib import Path

def flatten(obj, prefix="", out=None):
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, (list, tuple)):
        out[f"{prefix}.__len__"] = len(obj)
        for i, v in enumerate(obj[:200]):
            flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj
    return out

if len(sys.argv) < 2:
    sys.exit(__doc__)
spec = importlib.util.spec_from_file_location("reader", sys.argv[1])
reader = importlib.util.module_from_spec(spec); spec.loader.exec_module(reader)
here = Path(__file__).parent
files = sorted(p for p in (here / "saves").rglob("*") if p.is_file())
understood = 0
for path in files:
    data = path.read_bytes()
    rel = path.relative_to(here)
    if not reader.can_read(path.name, data):
        print(f"-- {rel}: can_read() said no"); continue
    try:
        values = reader.read(path.name, data)
        flat = flatten(values)
    except Exception as e:
        print(f"!! {rel}: read() failed: {type(e).__name__}: {e}"); continue
    understood += 1
    print(f"OK {rel}: {len(flat)} values")
    for key in list(flat)[:12]:
        print(f"      {key} = {str(flat[key])[:70]}")
    if hasattr(reader, "achievements"):
        rulings = reader.achievements(values)
        known = {a["api_name"].lower() for a in json.loads((here / "achievements.json").read_text())}
        print("      achievements():", {k: v for k, v in rulings.items()})
        wrong = [k for k in rulings if k.lower() not in known]
        if wrong:
            print("      !! not api_names from achievements.json:", wrong)
    else:
        print("      (no achievements() function: this reader can only suggest, never unlock)")
print(f"\n{understood} of {len(files)} files decoded.")
'''


def _instructions(game: str, appid: int, file_name: str, achievements: list, reports: list, history: list,
                  skipped: list) -> str:
    listed = "\n".join(f"- `{a['api_name']}` - **{a['title']}**"
                       + (f" - {a['description']}" if a["description"] else " - (no description published)")
                       for a in achievements)
    modules = ", ".join(sorted(m for m in ALLOWED_MODULES if not m.startswith("_")))
    history_text = ("`saves/history/` holds older copies of the same files, one folder per moment in time "
                    f"({', '.join(history)}). Comparing a file across them shows which bytes change as the "
                    "game is played - the most useful clue there is." if history else
                    "There are no older copies yet. If you need to compare, ask the person to play a little, "
                    "then make a new kit: AchTrack keeps a copy of the saves each time a session ends.")
    return f"""# Save reader kit: {game}

This folder is a complete brief for writing a small program called a **save reader**. It is written
so that someone who has never heard of AchTrack - a person or an AI assistant - can do the job from
this folder alone.

## If you are the person who owns this kit

1. Give this whole folder (or the zip of it) to an AI assistant, and say:
   *"Read INSTRUCTIONS.md in the attached kit and do what it asks. Reply with the finished Python file."*
2. Save the Python file it gives you as `{file_name}`.
3. Install it: in AchTrack open **Library → Save readers…**, click **Install from a file…** and
   pick the file - or drop the file onto that window.
4. AchTrack checks the file, tries it on your saves and tells you what it found. If it says the
   reader is not acceptable, copy that message back to the assistant and ask it to fix the file.

There is a command for each step too, if you prefer a terminal: `achtrack reader-install {file_name}`,
and `achtrack reader-test {file_name} "{game}"` to try a reader without installing it.

## If you are writing the reader - read all of this

### The situation
AchTrack is a desktop app that shows achievements for PC games. For this game it has the list of
achievements (below) but nothing tells it when one is earned, so it wants to work that out from the
game's save file. It cannot decode this game's save format. Your job has two parts:

1. **Decode** the save file's bytes into named values.
2. **State the rules**: for each achievement you can be certain about, which value proves it.

### What to deliver
Exactly one Python 3 file, `{file_name}`, containing:

```python
NAME = "{game}"

def can_read(filename: str, data: bytes) -> bool:
    # True only for this game's save files. Decide from the bytes (a signature), not the name alone.

def read(filename: str, data: bytes) -> dict:
    # The save's contents as a dict. Raise ValueError if the bytes are not what you expect.

def achievements(values: dict) -> dict:
    # values is what read() returned. Return {{api_name: True/False}} or {{api_name: (current, target)}}.
```

`data` is the whole file. `read` returns a dict whose values are `str`, `int`, `float`, `bool`,
`None`, or nested dicts and lists of those. `reader_template.py` is a starting point.

`achievements` uses the `api_name` of each achievement from `achievements.json` as the key. The
value is `True` or `False`, or a pair `(current, target)` for something counted, such as
`(137, 500)` kills. **Leave out every achievement you are not certain about** - AchTrack then
simply doesn't judge it, and the player can still tick it by hand.

### Why the rules matter
AchTrack acts on `achievements()` directly: when a value goes from `False` to `True` between two
saves, it unlocks that achievement and shows the player a popup. So a wrong rule gives the player
an achievement they did not earn. Being certain means you can point at one value whose meaning is
plain from its own name (`EnemyKills.FinalBoss`, `WeaponsUnlocked.Axe`, `ClearedRunsCache`) and
that matches the achievement's description without a leap. A word appearing somewhere in the save
is not evidence: the name of a region also appears in music tracks, dialogue and room lists.

Where there are older copies of the save, use them to confirm: a value that was 0 before the
player did something and 1 after is strong evidence. Say in a comment what each rule relies on.

Without an `achievements` function the reader still helps a little - AchTrack can suggest
achievements for the player to confirm - but it will never unlock one by itself.

### Rules for the file - AchTrack refuses a reader that breaks them
- Only these standard-library modules may be imported: {modules}.
  Nothing else: no `os`, `sys`, `pathlib`, `subprocess`, `socket`, no third-party packages. If the
  format needs something that isn't in the list (LZ4 or Zstandard decompression, for example),
  implement it in the file in plain Python.
- Do not use `open`, `exec`, `eval`, `compile`, `__import__`, `getattr`, `setattr`, `globals`,
  `locals`, `vars`, `input`, or attributes whose names start and end with two underscores.
- No reading or writing files, no network, no starting programs. Each function is a pure function
  of what it is given.
- It must finish within a few seconds on a file of this size.

### What makes the decoding good
- **Keep the game's own names.** If the save says `EnemyKills` or `WeaponsUnlocked`, use those
  as keys. Do not rename things to what you guess they mean; give anything you cannot name a
  neutral name such as `HeaderValue4`.
- **Keep the structure.** Nested dicts and lists are fine.
- **Use real types.** Counts as numbers, flags as booleans, not everything as text.
- **Check what can be checked.** If the format has a checksum, verify it and raise `ValueError`
  when it fails: the game may be half way through writing the file.
- **Be honest about failure.** If the file is encrypted and the key is not in the file, or you
  cannot work the format out, say so plainly instead of delivering a reader that returns guesses.

### What is in this kit
- `saves/` - the game's save files as they are now. {history_text}
- `file_report.md` - a first look at each file: size, signature, how random the bytes are, a hex
  dump of the start and any readable text found inside. A summary is below.
- `achievements.json` - the game's achievements. The same list is below.
- `reader_template.py` - the shape of the file to deliver.
- `test_reader.py` - run `python3 test_reader.py {file_name}` to see what your reader makes of
  every file in `saves/`. Use it before you deliver.
{("- Not copied because they are too large: " + ", ".join(skipped)) if skipped else ""}

### The save files, at a glance
{chr(10).join(reports)}

### The achievements ({len(achievements)})
`api_name` is the key to use in `achievements()`.

{listed}

---
Kit made by AchTrack {__version__} for Steam app {appid} on {time.strftime('%Y-%m-%d')}.
"""


def build(db, cfg, backup_dir: Path, appid: int, out_root: Path) -> dict:
    """Writes the kit for one game. Returns {"folder", "zip", "files", "history"}."""
    game = db.game(appid)
    if game is None:
        raise ValueError("no such game")
    name = game["name"] or f"App {appid}"
    layout = savesync.find_layout(db, appid)
    found = savesync.collect(db, appid, layout)
    if not found:
        raise ValueError("AchTrack hasn't seen this game write a save on this machine yet - play it and "
                         "save once, then make the kit")
    folder = Path(out_root) / f"{slug(name)}_reader_kit"
    if folder.exists():
        shutil.rmtree(folder)
    (folder / "saves").mkdir(parents=True)
    copied, skipped, used = [], [], set()
    for real, _portable in found:
        base = os.path.basename(real)
        if os.path.getsize(real) > MAX_COPY:
            skipped.append(base)
            continue
        target = base if base not in used else f"{len(used)}_{base}"
        used.add(target)
        shutil.copy2(real, folder / "saves" / target)
        copied.append(folder / "saves" / target)

    history = []
    for entry in [b for b in savesync.backups(cfg, backup_dir, appid) if not b["pending"]][:6]:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(entry["created"]))
        try:
            with zipfile.ZipFile(entry["path"]) as archive:
                members = [m for m in archive.namelist() if m.startswith("files/") and not m.endswith("/")]
                for member in members:
                    dest = folder / "saves" / "history" / stamp / os.path.basename(member)
                    if archive.getinfo(member).file_size <= MAX_COPY:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(archive.read(member))
        except (OSError, zipfile.BadZipFile, KeyError):
            continue
        if (folder / "saves" / "history" / stamp).is_dir():
            history.append(stamp)

    achievements = [{"api_name": a["name"], "title": a["display_name"] or a["name"],
                     "description": a.get("description") or "", "hidden": bool(a.get("hidden")),
                     "already_unlocked": bool(a.get("unlocked_at"))} for a in db.achievements(appid)]
    reports = [describe(p) for p in copied]
    file_name = f"{slug(name)}_reader.py"
    (folder / "achievements.json").write_text(json.dumps(achievements, indent=1, ensure_ascii=False), encoding="utf-8")
    (folder / "file_report.md").write_text(f"# Save files of {name}\n\n" + "\n\n".join(reports) + "\n", encoding="utf-8")
    (folder / "reader_template.py").write_text(TEMPLATE.format(game=name.replace('"', "'")), encoding="utf-8")
    (folder / "test_reader.py").write_text(TESTER, encoding="utf-8")
    (folder / "INSTRUCTIONS.md").write_text(
        _instructions(name, appid, file_name, achievements, reports, history, skipped), encoding="utf-8")

    archive_path = folder.with_suffix(".zip")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, f"{folder.name}/{path.relative_to(folder)}")
    return {"folder": str(folder), "zip": str(archive_path), "files": len(copied), "history": len(history),
            "reader_file": file_name, "readers_dir": str(READERS_DIR)}
