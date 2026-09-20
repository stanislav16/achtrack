"""Backups: a consistent copy of the database plus a plain JSON export of everything in it.

The JSON matters as much as the database copy - it stays readable without AchTrack, so the record
of what you unlocked and when survives the app itself.
"""
from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .db import Database

TABLES = ("games", "achievements", "unlocks", "progress", "sessions", "sources", "save_files", "save_suggestions")


def snapshot(db: Database, dest_dir: Path) -> Path:
    """A safe copy of the live database (sqlite's own backup, so it works while the app runs)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"achtrack-{datetime.now():%Y-%m-%d_%H%M%S}.db"
    target = sqlite3.connect(str(dest))
    try:
        db.conn.backup(target)
    finally:
        target.close()
    return dest


def export_json(db: Database, dest_dir: Path, compress: bool = True) -> Path:
    """Everything in the database as one JSON file (gzipped by default)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    data = {"app": "achtrack", "version": 1, "exported": int(time.time()),
            **{table: [dict(r) for r in db.q(f"SELECT * FROM {table}")] for table in TABLES}}
    raw = json.dumps(data, indent=1).encode("utf-8")
    dest = dest_dir / f"achtrack-{datetime.now():%Y-%m-%d_%H%M%S}.json"
    if compress:
        dest = dest.with_suffix(".json.gz")
        dest.write_bytes(gzip.compress(raw))
    else:
        dest.write_bytes(raw)
    return dest


def prune(dest_dir: Path, keep: int) -> int:
    """Keeps the newest `keep` of each kind, deletes the rest. Returns how many went."""
    removed = 0
    for pattern in ("achtrack-*.db", "achtrack-*.json", "achtrack-*.json.gz"):
        files = sorted(dest_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[max(1, keep):]:
            old.unlink(missing_ok=True)
            removed += 1
    return removed


def run_backup(db: Database, dest_dir: Path, keep: int = 10) -> dict:
    dest_dir = Path(dest_dir).expanduser()
    result = {"time": int(time.time()), "dir": str(dest_dir)}
    result["database"] = str(snapshot(db, dest_dir))
    result["json"] = str(export_json(db, dest_dir))
    result["pruned"] = prune(dest_dir, keep)
    return result


def restore(backup: Path, db_path: Path) -> Path:
    """Puts a snapshot back, keeping the current database next to it as .replaced."""
    backup, db_path = Path(backup).expanduser(), Path(db_path)
    with sqlite3.connect(str(backup)) as check:      # refuse anything that isn't our database
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "games" not in tables or "unlocks" not in tables:
        raise ValueError(f"{backup} doesn't look like an AchTrack backup")
    if db_path.exists():
        shutil.copy2(db_path, db_path.with_suffix(".db.replaced"))
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    shutil.copy2(backup, db_path)
    return db_path
