"""Merging libraries between your machines through a shared folder.

Each device writes one small file (`achtrack-sync-<device>.json.gz`) holding its own view of the
library, and reads the files the other devices left. Nothing overwrites wholesale:

  * unlocks are a union, keeping the earliest unlock time, unless the other device removed one
    later than this one added it (removals are remembered as tombstones)
  * play time takes the larger value per source, so nothing is double counted
  * what you edited (hidden, review setting, executable, game folder) follows whichever device
    edited it last
  * achievement lists come from whichever device fetched them more recently

Point every device at the same synced folder (Dropbox, Drive, Nextcloud, Syncthing, rclone...).
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import socket
import time
import uuid
from pathlib import Path

from .db import Database

log = logging.getLogger(__name__)

PREFIX = "achtrack-sync-"
VERSION = 1
GAME_COPY = ("name", "header_url", "genre", "platform", "psn_id", "steam_owned", "trophy_src")
PLAYTIME_MAX = ("playtime", "steam_playtime", "psn_playtime", "last_played", "steam_last_played", "psn_last_played")
EDITED = ("hidden", "review", "exe_name", "game_dir")


def device_id(db: Database) -> str:
    stored = db.get_meta("device")
    if stored and stored.get("id"):
        return stored["id"]
    new = {"id": uuid.uuid4().hex[:12], "name": socket.gethostname()}
    db.set_meta("device", new)
    return new["id"]


def device_name(db: Database) -> str:
    return (db.get_meta("device") or {}).get("name") or socket.gethostname()


def game_key(row) -> str:
    """Stable across devices: Steam AppID, or the PlayStation trophy set."""
    if row["psn_id"]:
        return f"psn:{row['psn_id']}"
    return f"steam:{row['appid']}"


def state(db: Database) -> dict:
    """This device's whole library, in the shape the other devices read."""
    games = []
    for row in db.games(include_hidden=True):
        appid = row["appid"]
        games.append({
            "key": game_key(row),
            **{field: row[field] for field in GAME_COPY + PLAYTIME_MAX + EDITED},
            "edited_at": row["edited_at"] or 0,
            "schema_fetched_at": row["schema_fetched_at"] or 0,
            "achievements": [[a["name"], a["display_name"], a["description"], a["icon"], a["icon_gray"],
                              a["hidden"], a["global_pct"], a["sort_order"], a["kind"]]
                             for a in db.q("SELECT * FROM achievements WHERE appid = ? ORDER BY sort_order", (appid,))],
            "unlocks": [[u["name"], u["unlocked_at"], u["source"], u["note"], u["added_at"] or u["unlocked_at"]]
                        for u in db.q("SELECT * FROM unlocks WHERE appid = ?", (appid,))],
            "deletions": [[d["name"], d["deleted_at"]] for d in db.deletions(appid)],
        })
    return {"app": "achtrack", "version": VERSION, "device": device_id(db), "name": device_name(db),
            "written": int(time.time()), "games": games}


def write_state(db: Database, folder: Path) -> Path:
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{PREFIX}{device_id(db)}.json.gz"
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(gzip.compress(json.dumps(state(db), separators=(",", ":")).encode("utf-8"), 6))
    tmp.replace(dest)
    return dest


def read_states(folder: Path, skip_device: str) -> list:
    out = []
    for path in sorted(Path(folder).expanduser().glob(f"{PREFIX}*.json.gz")):
        try:
            data = json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError) as e:
            log.info("ignoring %s: %s", path.name, e)
            continue
        if data.get("app") == "achtrack" and data.get("device") != skip_device:
            data["_path"] = str(path)
            out.append(data)
    return out


def _local_index(db: Database) -> dict:
    return {game_key(row): row for row in db.games(include_hidden=True)}


def merge(db: Database, remote: dict) -> dict:
    """Folds one other device's state into this database. Returns what changed."""
    counts = {"games": 0, "unlocks": 0, "removed": 0, "updated": 0, "schemas": 0}
    index = _local_index(db)
    for game in remote.get("games") or []:
        key = game.get("key") or ""
        local = index.get(key)
        if local is None:
            appid = int(key.split(":")[1]) if key.startswith("steam:") else db.next_psn_appid()
            db.ensure_game(appid)
            db.update_game(appid, psn_id=game.get("psn_id"), platform=game.get("platform") or "pc")
            local = db.game(appid)
            index[key] = local
            counts["games"] += 1
        appid = local["appid"]

        values = {}
        for field in GAME_COPY:
            if game.get(field) and not local[field]:
                values[field] = game[field]
        for field in PLAYTIME_MAX:
            mine, theirs = local[field] or 0, game.get(field) or 0
            if theirs > mine:
                values[field] = theirs
        # Your own edits follow whichever device made them last.
        if (game.get("edited_at") or 0) > (local["edited_at"] or 0):
            for field in EDITED:
                if game.get(field) != local[field]:
                    values[field] = game.get(field)
            values["edited_at"] = game["edited_at"]
        if values:
            db.update_game(appid, **values)
            counts["updated"] += 1

        remote_schema = game.get("achievements") or []
        have = db.q("SELECT COUNT(*) FROM achievements WHERE appid = ?", (appid,))[0][0]
        if remote_schema and (not have or (game.get("schema_fetched_at") or 0) > (local["schema_fetched_at"] or 0)):
            db.set_schema(appid, [{"name": a[0], "display_name": a[1], "description": a[2], "icon": a[3],
                                   "icon_gray": a[4], "hidden": a[5], "global_pct": a[6], "kind": a[8]}
                                  for a in remote_schema], {})
            db.update_game(appid, schema_fetched_at=game.get("schema_fetched_at") or int(time.time()))
            counts["schemas"] += 1

        mine_unlocks = {u["name"]: u for u in db.q("SELECT * FROM unlocks WHERE appid = ?", (appid,))}
        my_deletions = {d["name"]: d["deleted_at"] for d in db.deletions(appid)}
        their_deletions = dict(game.get("deletions") or [])

        for name, unlocked_at, source, note, added_at in game.get("unlocks") or []:
            gone = my_deletions.get(name)
            if gone and gone >= (added_at or unlocked_at):
                continue                                  # removed here after they added it
            existing = mine_unlocks.get(name)
            if existing is None:
                if db.add_unlock(appid, name, unlocked_at, source, note, added_at):
                    counts["unlocks"] += 1
            elif unlocked_at and unlocked_at < (existing["unlocked_at"] or 0):
                db.x("UPDATE unlocks SET unlocked_at = ? WHERE appid = ? AND name = ?", (unlocked_at, appid, name))

        for name, deleted_at in their_deletions.items():
            existing = mine_unlocks.get(name)
            if existing is not None and deleted_at >= (existing["added_at"] or existing["unlocked_at"] or 0):
                db.remove_unlock(appid, name)
                counts["removed"] += 1
            elif existing is None:
                db.x("INSERT OR REPLACE INTO unlock_deletions VALUES(?,?,?)", (appid, name, deleted_at))
    return counts


def devices(db: Database, folder: str) -> list:
    """What every machine has left in the shared folder, newest first."""
    mine = device_id(db)
    out = []
    for path in sorted(Path(folder).expanduser().glob(f"{PREFIX}*.json.gz")):
        try:
            data = json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError) as e:
            out.append({"file": path.name, "error": str(e)})
            continue
        games = data.get("games") or []
        out.append({"file": path.name, "device": data.get("device", "?"), "name": data.get("name", "?"),
                    "written": data.get("written", 0), "games": len(games),
                    "unlocks": sum(len(g.get("unlocks") or []) for g in games),
                    "this_machine": data.get("device") == mine,
                    "size": path.stat().st_size})
    return sorted(out, key=lambda d: -d.get("written", 0))


def run_sync(db: Database, folder: str) -> dict:
    """Writes this device's file and merges everyone else's."""
    status = {"time": int(time.time()), "ok": False, "error": "", "devices": 0,
              "games": 0, "unlocks": 0, "removed": 0, "updated": 0, "schemas": 0}
    if not folder.strip():
        status["error"] = "no sync folder set"
        return status
    path = Path(folder).expanduser()
    try:
        for remote in read_states(path, device_id(db)):
            counts = merge(db, remote)
            status["devices"] += 1
            for field, value in counts.items():
                status[field] += value
            log.info("merged %s from %s: %s", remote.get("name", "?"), os.path.basename(remote["_path"]), counts)
        write_state(db, path)
    except (OSError, ValueError) as e:
        status["error"] = str(e)
        return status
    status["ok"] = True
    db.set_meta("sync", status)
    return status
