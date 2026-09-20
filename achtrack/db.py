from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .config import DB_FILE

SCHEMA = """
CREATE TABLE IF NOT EXISTS games(
    appid INTEGER PRIMARY KEY,
    name TEXT,
    header_url TEXT,
    exe_name TEXT,
    game_dir TEXT,
    playtime INTEGER NOT NULL DEFAULT 0,
    last_played INTEGER,
    added_at INTEGER NOT NULL,
    schema_fetched_at INTEGER,
    hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS achievements(
    appid INTEGER NOT NULL,
    name TEXT NOT NULL,
    display_name TEXT,
    description TEXT,
    icon TEXT,
    icon_gray TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    global_pct REAL,
    sort_order INTEGER,
    PRIMARY KEY(appid, name)
);
CREATE TABLE IF NOT EXISTS unlocks(
    appid INTEGER NOT NULL,
    name TEXT NOT NULL,
    unlocked_at INTEGER NOT NULL,
    source TEXT,
    PRIMARY KEY(appid, name)
);
CREATE TABLE IF NOT EXISTS progress(
    appid INTEGER NOT NULL,
    name TEXT NOT NULL,
    cur REAL,
    max REAL,
    PRIMARY KEY(appid, name)
);
CREATE TABLE IF NOT EXISTS sources(
    path TEXT PRIMARY KEY,
    appid INTEGER NOT NULL,
    emulator TEXT NOT NULL,
    mtime REAL NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS save_files(
    path TEXT NOT NULL,
    appid INTEGER NOT NULL,
    format TEXT,
    readable INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    mtime REAL NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    found_at INTEGER NOT NULL,
    PRIMARY KEY(path, appid)
);
CREATE TABLE IF NOT EXISTS save_suggestions(
    appid INTEGER NOT NULL,
    name TEXT NOT NULL,
    reason TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(appid, name)
);
CREATE TABLE IF NOT EXISTS save_rejections(
    appid INTEGER NOT NULL,
    name TEXT NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY(appid, name, key)
);
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appid INTEGER NOT NULL,
    started INTEGER NOT NULL,
    ended INTEGER,
    seconds INTEGER NOT NULL DEFAULT 0
);
"""

# Columns added after the first release; created on startup for existing databases.
MIGRATIONS = {
    "games": {
        "steam_playtime": "INTEGER NOT NULL DEFAULT 0",
        "steam_last_played": "INTEGER",
        "steam_owned": "INTEGER NOT NULL DEFAULT 0",
        "review": "INTEGER",  # NULL = decide automatically, 0 = never ask, 1 = always ask
        "genre": "TEXT",      # from the Steam store, e.g. "Action, RPG"
    },
    "unlocks": {
        "note": "TEXT",       # why it unlocked, e.g. the save field that matched
    },
}

GAME_FIELDS = {"name", "header_url", "exe_name", "game_dir", "last_played", "schema_fetched_at", "hidden",
               "steam_playtime", "steam_last_played", "steam_owned", "review", "genre"}

# Unlock sources that don't come from an emulator file.
MANUAL = "Manual"
STEAM = "Steam"
SAVE = "Save"
SCREEN = "Screen"


class Database:
    """One instance per thread; SQLite in WAL mode handles the concurrency."""

    def __init__(self, path: Path = DB_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, columns in MIGRATIONS.items():
            have = {r[1] for r in self.q(f"PRAGMA table_info({table})")}
            for column, decl in columns.items():
                if column not in have:
                    try:
                        self.x(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                    except sqlite3.OperationalError as e:
                        if "duplicate column" not in str(e):  # another connection just added it
                            raise

    def q(self, sql: str, args=()) -> list:
        return self.conn.execute(sql, args).fetchall()

    def x(self, sql: str, args=()) -> sqlite3.Cursor:
        return self.conn.execute(sql, args)

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # games ---------------------------------------------------------------

    def ensure_game(self, appid: int) -> bool:
        cur = self.x("INSERT OR IGNORE INTO games(appid, added_at) VALUES(?, ?)", (appid, int(time.time())))
        return cur.rowcount == 1

    def update_game(self, appid: int, **values) -> None:
        values = {k: v for k, v in values.items() if k in GAME_FIELDS}
        if not values:
            return
        cols = ", ".join(f"{k} = ?" for k in values)
        self.x(f"UPDATE games SET {cols} WHERE appid = ?", (*values.values(), appid))

    def game(self, appid: int):
        rows = self.q("SELECT * FROM games WHERE appid = ?", (appid,))
        return rows[0] if rows else None

    def games(self, include_hidden: bool = False) -> list:
        return self.q(
            """
            SELECT g.*,
                   (SELECT COUNT(*) FROM achievements a WHERE a.appid = g.appid) AS total,
                   (SELECT COUNT(*) FROM unlocks u WHERE u.appid = g.appid) AS unlocked,
                   g.playtime + g.steam_playtime AS total_playtime,
                   MAX(COALESCE(g.last_played, 0), COALESCE(g.steam_last_played, 0)) AS last_any
            FROM games g
            WHERE ? OR g.hidden = 0
            ORDER BY last_any DESC, g.name COLLATE NOCASE
            """,
            (1 if include_hidden else 0,),
        )

    def add_playtime(self, appid: int, seconds: int, now: int) -> None:
        self.x("UPDATE games SET playtime = playtime + ?, last_played = ? WHERE appid = ?", (seconds, now, appid))

    # achievements --------------------------------------------------------

    def set_schema(self, appid: int, achievements: list, pct: dict) -> None:
        with self.tx():
            self.x("DELETE FROM achievements WHERE appid = ?", (appid,))
            for order, a in enumerate(achievements):
                self.x(
                    "INSERT OR REPLACE INTO achievements VALUES(?,?,?,?,?,?,?,?,?)",
                    (appid, a["name"], a.get("display_name"), a.get("description"), a.get("icon"),
                     a.get("icon_gray"), int(a.get("hidden") or 0), pct.get(a["name"]), order),
                )
                # Emulator files sometimes differ in case from the official API names.
                for table in ("unlocks", "progress"):
                    self.x(
                        f"UPDATE OR IGNORE {table} SET name = ? WHERE appid = ? AND name != ? AND lower(name) = lower(?)",
                        (a["name"], appid, a["name"], a["name"]),
                    )

    def set_global_pct(self, appid: int, pct: dict) -> None:
        with self.tx():
            for name, value in pct.items():
                self.x("UPDATE achievements SET global_pct = ? WHERE appid = ? AND name = ?", (value, appid, name))

    def achievement_names(self, appid: int) -> list:
        return [r[0] for r in self.q("SELECT name FROM achievements WHERE appid = ?", (appid,))]

    def achievement(self, appid: int, name: str):
        rows = self.q("SELECT * FROM achievements WHERE appid = ? AND name = ?", (appid, name))
        return rows[0] if rows else None

    def achievements(self, appid: int) -> list:
        """Schema rows joined with unlock/progress, plus unlocks the schema doesn't know about."""
        rows = [dict(r) for r in self.q(
            """
            SELECT a.*, u.unlocked_at, u.source, p.cur, p.max FROM achievements a
            LEFT JOIN unlocks u ON u.appid = a.appid AND u.name = a.name
            LEFT JOIN progress p ON p.appid = a.appid AND p.name = a.name
            WHERE a.appid = ? ORDER BY a.sort_order
            """,
            (appid,),
        )]
        for r in self.q(
            """
            SELECT u.name, u.unlocked_at, u.source FROM unlocks u WHERE u.appid = ?
            AND NOT EXISTS (SELECT 1 FROM achievements a WHERE a.appid = u.appid AND a.name = u.name)
            """,
            (appid,),
        ):
            rows.append({"appid": appid, "name": r["name"], "display_name": r["name"], "description": "",
                         "icon": None, "icon_gray": None, "hidden": 0, "global_pct": None,
                         "unlocked_at": r["unlocked_at"], "source": r["source"], "cur": None, "max": None})
        return rows

    def remove_unlock(self, appid: int, name: str, source: str) -> bool:
        cur = self.x("DELETE FROM unlocks WHERE appid = ? AND name = ? AND source = ?", (appid, name, source))
        return cur.rowcount == 1

    def emulator_unlock_count(self, appid: int) -> int:
        return self.q("SELECT COUNT(*) FROM unlocks WHERE appid = ? AND COALESCE(source, '') NOT IN (?, ?, ?, ?)",
                      (appid, MANUAL, STEAM, SAVE, SCREEN))[0][0]

    def unlock_note(self, appid: int, name: str) -> str | None:
        rows = self.q("SELECT note FROM unlocks WHERE appid = ? AND name = ?", (appid, name))
        return rows[0][0] if rows else None

    def get_meta(self, key: str):
        rows = self.q("SELECT value FROM meta WHERE key = ?", (key,))
        if not rows:
            return None
        try:
            return json.loads(rows[0][0])
        except (TypeError, ValueError):
            return None

    def set_meta(self, key: str, value) -> None:
        self.x("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, json.dumps(value)))

    # save files -----------------------------------------------------------

    def save_files(self, appid: int | None = None) -> list:
        if appid is None:
            return self.q("SELECT * FROM save_files")
        return self.q("SELECT * FROM save_files WHERE appid = ? ORDER BY readable DESC, mtime DESC", (appid,))

    def upsert_save_file(self, path: str, appid: int, fmt: str | None, readable: bool, reason: str | None,
                         mtime: float, size: int) -> None:
        self.x("""INSERT INTO save_files(path, appid, format, readable, reason, mtime, size, found_at)
                  VALUES(?,?,?,?,?,?,?,?)
                  ON CONFLICT(path, appid) DO UPDATE SET format = excluded.format, readable = excluded.readable,
                  reason = excluded.reason, mtime = excluded.mtime, size = excluded.size""",
               (path, appid, fmt, int(readable), reason, mtime, size, int(time.time())))

    def suggestions(self, appid: int) -> dict:
        return {r["name"]: r["reason"] for r in self.q("SELECT name, reason FROM save_suggestions WHERE appid = ?", (appid,))}

    def add_suggestion(self, appid: int, name: str, reason: str) -> bool:
        cur = self.x("INSERT OR IGNORE INTO save_suggestions VALUES(?,?,?,?)", (appid, name, reason, int(time.time())))
        return cur.rowcount == 1

    def drop_suggestion(self, appid: int, name: str) -> None:
        self.x("DELETE FROM save_suggestions WHERE appid = ? AND name = ?", (appid, name))

    def rejections(self, appid: int) -> set:
        return {(r["name"], r["key"]) for r in self.q("SELECT name, key FROM save_rejections WHERE appid = ?", (appid,))}

    def reject(self, appid: int, name: str, key: str) -> None:
        self.x("INSERT OR IGNORE INTO save_rejections VALUES(?,?,?)", (appid, name, key))

    def unlocked_names(self, appid: int) -> set:
        return {r[0] for r in self.q("SELECT name FROM unlocks WHERE appid = ?", (appid,))}

    def add_unlock(self, appid: int, name: str, ts: int, source: str, note: str | None = None) -> bool:
        cur = self.x("INSERT OR IGNORE INTO unlocks(appid, name, unlocked_at, source, note) VALUES(?,?,?,?,?)",
                     (appid, name, ts, source, note))
        if cur.rowcount == 1:
            self.x("DELETE FROM save_suggestions WHERE appid = ? AND name = ?", (appid, name))
        return cur.rowcount == 1

    def set_progress(self, appid: int, name: str, cur, maximum) -> None:
        self.x("INSERT OR REPLACE INTO progress VALUES(?,?,?,?)", (appid, name, cur, maximum))

    # sources -------------------------------------------------------------

    def sources(self) -> list:
        return self.q("SELECT * FROM sources")

    def sources_for(self, appid: int) -> list:
        return [r["path"] for r in self.q("SELECT path FROM sources WHERE appid = ?", (appid,))]

    def upsert_source(self, path: str, appid: int, emulator: str, mtime: float, size: int) -> None:
        self.x("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)", (path, appid, emulator, mtime, size))

    # sessions ------------------------------------------------------------

    def start_session(self, appid: int, now: int) -> int:
        return self.x("INSERT INTO sessions(appid, started) VALUES(?, ?)", (appid, now)).lastrowid

    def update_session(self, session_id: int, seconds: int, ended=None) -> None:
        self.x("UPDATE sessions SET seconds = ?, ended = ? WHERE id = ?", (seconds, ended, session_id))

    def close_dangling_sessions(self) -> None:
        self.x("UPDATE sessions SET ended = started + seconds WHERE ended IS NULL")
