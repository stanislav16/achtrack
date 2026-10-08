"""Save readers you add yourself.

AchTrack decodes the common save formats on its own. A game with its own format needs a reader:
one small Python file that turns the save's bytes into named values. Once the values have names,
the usual matching against achievement descriptions takes over - a reader only decodes.

A reader is written outside AchTrack (by a person or a chatbot, from the kit `readerkit` makes)
and dropped into the readers folder. It is somebody else's code, so it is held to a narrow shape:

    NAME = "Hades II"
    def can_read(filename: str, data: bytes) -> bool: ...
    def read(filename: str, data: bytes) -> dict: ...
    def achievements(values: dict) -> dict: ...      # optional: {"ApiName": True, "Other": (37, 50)}

`achievements` is how a reader says outright which value proves which achievement. Without it
AchTrack falls back to comparing names with descriptions, which is only safe for a small save - on
a dump of a whole game's state it finds coincidences - so a reader without rules never unlocks
anything by itself; it can only suggest.

and before it is ever run it is checked: only a short list of standard modules may be imported,
and nothing that touches files, the network, processes or the interpreter may be used. It then
runs with a cut-down set of builtins and a time limit. That is a seat belt, not a vault - read a
reader before installing it if you didn't get it from somewhere you trust.
"""
from __future__ import annotations

import ast
import builtins
import logging
import os
import threading
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger(__name__)

READERS_DIR = DATA_DIR / "readers"
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin_readers"     # offered, never used unasked
TIME_LIMIT = 8.0
ALLOWED_MODULES = {
    "struct", "zlib", "gzip", "bz2", "lzma", "json", "re", "io", "math", "base64", "binascii",
    "collections", "itertools", "functools", "datetime", "hashlib", "typing", "dataclasses", "enum",
    "string", "codecs", "array", "operator", "__future__",
}
FORBIDDEN_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "memoryview", "exit", "quit", "help",
}
SAFE_BUILTINS = (
    "abs all any ascii bin bool bytearray bytes callable chr dict divmod enumerate filter float format "
    "frozenset hash hex int isinstance issubclass iter len list map max min next object oct ord pow print "
    "range repr reversed round set slice sorted str sum tuple type zip "
    "ArithmeticError AssertionError AttributeError EOFError Exception IndexError KeyError LookupError "
    "NotImplementedError OverflowError RuntimeError StopIteration TypeError UnicodeDecodeError ValueError "
    "ZeroDivisionError True False None __build_class__ staticmethod classmethod property super"
).split()


class ReaderProblem(Exception):
    """A reader that can't be accepted; the message says why in plain words."""


def check(source: str) -> list:
    """Everything wrong with a reader's source - an empty list means it may be installed."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"it isn't valid Python: line {e.lineno}: {e.msg}"]
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_MODULES:
                    problems.append(f"line {node.lineno}: imports '{alias.name}', which a reader may not use")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED_MODULES:
                problems.append(f"line {node.lineno}: imports from '{node.module}', which a reader may not use")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"line {node.lineno}: uses '{node.id}', which a reader may not use")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__") \
                and node.attr != "__init__":
            problems.append(f"line {node.lineno}: reaches into '{node.attr}', which a reader may not do")
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for needed in ("can_read", "read"):
        if needed not in defined:
            problems.append(f"it has no function called {needed}(filename, data)")
    return problems


def _limited_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name.split(".")[0] not in ALLOWED_MODULES:
        raise ImportError(f"a reader may not import {name}")
    return builtins.__import__(name, globals, locals, fromlist, level)


class Reader:
    def __init__(self, path: Path):
        self.path = Path(path)
        source = self.path.read_text(encoding="utf-8")
        problems = check(source)
        if problems:
            raise ReaderProblem("; ".join(problems))
        space = {"__builtins__": {name: getattr(builtins, name) for name in SAFE_BUILTINS if hasattr(builtins, name)},
                 "__name__": f"achtrack_reader_{self.path.stem}"}
        space["__builtins__"]["__import__"] = _limited_import
        try:
            exec(compile(source, str(self.path), "exec"), space)        # checked above
        except Exception as e:
            raise ReaderProblem(f"it failed while loading: {type(e).__name__}: {e}") from e
        self.name = str(space.get("NAME") or self.path.stem)
        self.builtin = False
        self._can_read, self._read = space["can_read"], space["read"]
        self._rules = space.get("achievements") if callable(space.get("achievements")) else None

    def _timed(self, func, *args):
        box: dict = {}

        def work():
            try:
                box["value"] = func(*args)
            except BaseException as e:      # a reader's mistake must not take the app with it
                box["error"] = e

        thread = threading.Thread(target=work, daemon=True, name=f"reader-{self.path.stem}")
        thread.start()
        thread.join(TIME_LIMIT)
        if thread.is_alive():
            raise ReaderProblem(f"it took longer than {TIME_LIMIT:.0f} seconds")
        if "error" in box:
            raise ReaderProblem(f"{type(box['error']).__name__}: {box['error']}")
        return box.get("value")

    def can_read(self, filename: str, data: bytes) -> bool:
        try:
            return bool(self._timed(self._can_read, filename, data))
        except ReaderProblem:
            return False

    def read(self, filename: str, data: bytes) -> dict:
        value = self._timed(self._read, filename, data)
        if not isinstance(value, dict) or not value:
            raise ReaderProblem("read() must return a dict of the save's values, and it returned "
                                + ("an empty one" if isinstance(value, dict) else type(value).__name__))
        return value


    @property
    def has_rules(self) -> bool:
        return self._rules is not None

    def rules(self, values: dict) -> dict | None:
        """{api name: True/False, or (current, target)} as the reader states it - None if it has no rules."""
        if self._rules is None:
            return None
        stated = self._timed(self._rules, values)
        if not isinstance(stated, dict):
            raise ReaderProblem("achievements() must return a dict")
        out = {}
        for name, value in stated.items():
            if isinstance(value, bool):
                out[str(name)] = value
            elif isinstance(value, (tuple, list)) and len(value) == 2 \
                    and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in value):
                out[str(name)] = (value[0], value[1])
            else:
                raise ReaderProblem(f"achievements() gave {name!r} the value {value!r}; it must be True, False "
                                    f"or a (current, target) pair of numbers")
        return out


_loaded: dict = {"stamp": None, "readers": []}


def _files(folder: Path) -> list:
    try:
        return sorted(p for p in Path(folder).glob("*.py") if not p.name.startswith("_"))
    except OSError:
        return []


def installed(folder: Path | None = None) -> list:
    """The readers you have installed - the only ones ever used. Reloaded when a file changes."""
    files = _files(Path(folder or READERS_DIR))
    try:
        stamp = tuple((str(p), p.stat().st_mtime, p.stat().st_size) for p in files)
    except OSError:
        return _loaded["readers"]
    if stamp != _loaded["stamp"]:
        readers = []
        for path in files:
            try:
                readers.append(Reader(path))
            except (ReaderProblem, OSError, UnicodeError) as e:
                log.warning("save reader %s was not loaded: %s", path.name, e)
        _loaded.update(stamp=stamp, readers=readers)
    return _loaded["readers"]


def available(folder: Path | None = None) -> list:
    """Readers that come with AchTrack and that you have not installed. They do nothing until you do."""
    taken = {p.name for p in _files(Path(folder or READERS_DIR))}
    out = []
    for path in _files(BUILTIN_DIR):
        if path.name in taken:
            continue
        try:
            reader = Reader(path)
            reader.builtin = True
            out.append(reader)
        except (ReaderProblem, OSError, UnicodeError) as e:
            log.warning("bundled save reader %s is broken: %s", path.name, e)
    return out


def remove(reader) -> None:
    """Takes out a reader you installed."""
    try:
        Path(reader.path).unlink()
    except OSError as e:
        raise ReaderProblem(f"couldn't remove it: {e}") from e


def try_read(path: str, data: bytes) -> tuple | None:
    """(reader name, what it decoded, its achievement rulings or None) from the first installed
    reader that understands this file."""
    filename = os.path.basename(path)
    for reader in installed():
        if not reader.can_read(filename, data):
            continue
        try:
            values = reader.read(filename, data)
            return reader.name, values, reader.rules(values)
        except ReaderProblem as e:
            log.info("reader %s could not read %s: %s", reader.name, filename, e)
    return None


def install(source_file: str, folder: Path | None = None) -> Path:
    """Checks a reader and copies it into the readers folder. Raises ReaderProblem if it won't do."""
    src = Path(source_file).expanduser()
    try:
        text = src.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        raise ReaderProblem(f"can't read {src}: {e}") from e
    problems = check(text)
    if problems:
        raise ReaderProblem("\n".join(problems))
    Reader(src)                                  # loads it once, so a crash on import is caught here
    folder = Path(folder or READERS_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in src.stem).strip("_") or "reader"
    dest = folder / f"{name}.py"
    dest.write_text(text, encoding="utf-8")
    return dest
