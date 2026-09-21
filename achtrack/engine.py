"""Background engine: watches achievement files, tracks playtime, fetches Steam metadata.

It has no Qt dependency; the GUI runs it on a worker thread and receives events via a Listener.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import sqlite3
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import backup, descriptions, export, matcher, psn, savefiles, share, steam_api, sync
from .config import BACKUP_DIR, CACHE_DIR, EXPORT_DIR, SHARE_DIR, Config
from .db import PSN, SAVE, SCREEN, STEAM, Database
from .emulators import parse_file
from .playtime import ProcessMonitor
from .scanner import detect_steam_id, find_local_schema, find_state_files

log = logging.getLogger(__name__)

META_RETRY = 600            # seconds between failed metadata attempts for the same game
META_MAX_AGE = 7 * 86400    # refresh names/percentages weekly
RARE_PCT = 10.0             # Steam marks achievements under 10% as rare
SAVE_SCAN_EVERY = 30.0      # seconds between looks for newly written save files while a game runs
SNAPSHOT_FILES = 40         # save-looking files snapshotted when a game starts
SNAPSHOT_DIR = CACHE_DIR / "saves"


class Listener:
    def on_unlock(self, event: dict) -> None: ...
    def on_game_started(self, appid: int, name: str, header: str) -> None: ...
    def on_game_stopped(self, appid: int, seconds: int) -> None: ...
    def on_changed(self) -> None: ...
    def on_error(self, message: str) -> None: ...
    def on_described(self, appid: int, status: dict) -> None: ...
    def on_shared(self, status: dict) -> None: ...


@dataclass
class _Running:
    session: int
    started: float
    last: float
    seconds: float = 0.0
    pending: float = 0.0
    misses: int = 0
    prefix: str | None = None
    game_dir: str | None = None
    next_save_scan: float = 0.0
    snapshots: dict | None = None   # path -> flattened save as it was when the game started


def game_title(row, appid: int) -> str:
    return (row["name"] if row is not None and row["name"] else None) or f"App {appid}"


def needs_review(db: Database, appid: int) -> bool:
    """Whether to ask after a session: only for games whose crack has never recorded an achievement."""
    game = db.game(appid)
    if game is None or game["review"] == 0:
        return False
    if db.suggestions(appid):
        return True
    locked = db.q("""SELECT COUNT(*) FROM achievements a WHERE a.appid = ? AND NOT EXISTS
                     (SELECT 1 FROM unlocks u WHERE u.appid = a.appid AND u.name = a.name)""", (appid,))[0][0]
    if not locked:
        return False
    return game["review"] == 1 or db.emulator_unlock_count(appid) == 0


def build_unlock_event(db: Database, appid: int, name: str, download: bool = True) -> dict:
    """Everything a popup needs about one unlocked achievement."""
    game = db.game(appid)
    ach = db.achievement(appid, name)
    icon_ref = ach["icon"] if ach is not None and ach["icon"] else ""
    if download:
        icon = steam_api.ensure_icon(appid, icon_ref, timeout=5) if icon_ref else None
    else:
        icon = steam_api.cached_icon(appid, icon_ref)
    pct = ach["global_pct"] if ach is not None else None
    total = db.q("SELECT COUNT(*) FROM achievements WHERE appid = ?", (appid,))[0][0]
    return {
        "appid": appid,
        "game": game_title(game, appid),
        "name": name,
        "title": (ach["display_name"] if ach is not None else None) or name,
        "description": (ach["description"] if ach is not None else None) or "",
        "icon": icon,
        "pct": pct,
        "rare": pct is not None and pct < RARE_PCT,
        "unlocked": len(db.unlocked_names(appid)),
        "total": total,
    }


class Engine:
    def __init__(self, cfg: Config, db: Database | None = None, listener: Listener | None = None,
                 requests: queue.Queue | None = None):
        self.cfg = cfg
        self.db = db or Database()
        self.listener = listener or Listener()
        self.requests = requests or queue.Queue()
        self.started_at = time.time()
        self.sources = {r["path"]: dict(r) for r in self.db.sources()}
        self.monitor = ProcessMonitor() if ProcessMonitor.available() else None
        self.running: dict = {}
        self.meta_queue: deque = deque()
        self._meta_attempts: dict = {}
        self._force_scan = False
        self._force_steam = False
        self._steam_id: str | None = None
        self._steam_thread: threading.Thread | None = None
        self._psn_thread: threading.Thread | None = None
        self._describe_queue: deque = deque()
        self._describe_thread: threading.Thread | None = None
        self._share_thread: threading.Thread | None = None
        self._export_thread: threading.Thread | None = None
        self._next_beat = 0.0
        self._force_psn = False
        self._next_backup = time.time() + 300      # first automatic backup shortly after startup
        self._next_sync = 0.0
        self._icons = ThreadPoolExecutor(max_workers=4, thread_name_prefix="achtrack-icons")
        self.saves = {}
        for r in self.db.save_files():
            if savefiles.is_candidate(r["path"]):
                self.saves[(r["path"], r["appid"])] = dict(r)
            else:   # registered by an older version that mistook engine caches for saves
                self.db.x("DELETE FROM save_files WHERE path = ? AND appid = ?", (r["path"], r["appid"]))
        self._models: dict = {}
        self._last_error = ("", 0.0)

    # main loop -----------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        self.db.close_dangling_sessions()
        self.queue_stale_meta()
        next_scan = next_poll = next_proc = next_steam = next_psn = 0.0
        while not stop.is_set():
            now = time.monotonic()
            try:
                self._drain_requests()
                if now >= next_scan or self._force_scan:
                    self._force_scan = False
                    self.discover()
                    next_scan = now + max(10.0, self.cfg.rescan_interval)
                if now >= next_poll:
                    self.poll()
                    self.poll_saves()
                    next_poll = now + max(0.5, self.cfg.poll_interval)
                if self.monitor and now >= next_proc:
                    self.track_processes()
                    next_proc = now + max(1.0, self.cfg.process_interval)
                for appid, run in list(self.running.items()):
                    if self.cfg.save_detection and time.time() >= run.next_save_scan:
                        run.next_save_scan = time.time() + SAVE_SCAN_EVERY
                        self.discover_saves(appid, run)
                if now >= next_steam or self._force_steam:
                    self._force_steam = False
                    self._start_steam_sync()
                    next_steam = now + max(120.0, self.cfg.steam_sync_interval)
                if now >= next_psn or self._force_psn:
                    self._force_psn = False
                    self._start_psn_sync()
                    next_psn = now + max(300.0, self.cfg.psn_sync_interval)
                if self.meta_queue:
                    self.ensure_meta(self.meta_queue.popleft(), force=True)
                if self._describe_queue:
                    self._start_describe()
                if self.cfg.backup_daily and time.time() >= self._next_backup:
                    self._next_backup = time.time() + 86400
                    self.run_backup()
                if time.time() >= self._next_beat:
                    self._next_beat = time.time() + 10
                    seen = getattr(self.monitor, "seen", {}) if self.monitor else {}
                    self.db.set_meta("heartbeat", {
                        "time": int(time.time()),
                        "running": sorted(self.running),
                        "misses": {str(a): r.misses for a, r in self.running.items()},
                        # what the engine itself is matching - its cache can hold a detection a
                        # fresh scan would no longer make
                        "matches": {str(a): seen.get(a, []) for a in self.running},
                    })
                if self.cfg.sync_dir.strip() and time.time() >= self._next_sync:
                    self._next_sync = time.time() + max(60.0, self.cfg.sync_interval)
                    self.run_device_sync()
            except Exception:
                log.exception("engine tick failed")
            stop.wait(0.25)
        self.shutdown()

    def shutdown(self) -> None:
        now = time.time()
        for appid, run in list(self.running.items()):
            self._flush_playtime(appid, run, now)
            self.db.update_session(run.session, int(run.seconds), int(now))
        self.running.clear()
        self._icons.shutdown(wait=False)

    def _drain_requests(self) -> None:
        while True:
            try:
                req = self.requests.get_nowait()
            except queue.Empty:
                return
            kind = req[0]
            if kind == "rescan":
                self._force_scan = True
                if self.monitor:
                    self.monitor.reset()
            elif kind == "refresh" and self.db.game(req[1]) is not None:
                self._meta_attempts.pop(req[1], None)
                self.meta_queue.appendleft(req[1])
            elif kind == "refresh_all":
                self.meta_queue.extend(r["appid"] for r in self.db.games(include_hidden=True))
            elif kind == "steam_sync":
                self._steam_id = None
                self._force_steam = True
            elif kind == "psn_sync":
                self._force_psn = True
            elif kind == "backup":
                self.run_backup()
            elif kind == "sync":
                self._next_sync = 0.0
            elif kind == "export":
                self._start_export(deploy=len(req) > 1 and bool(req[1]))
            elif kind == "describe" and self.db.game(req[1]) is not None:
                self._describe_queue.append((req[1], req[2] if len(req) > 2 else "",
                                             req[3] if len(req) > 3 else "", True, True, True))
            elif kind == "not_playing" and req[1] in self.running:
                self.end_session(req[1])
            elif kind == "share":
                self._start_share(req[1] if len(req) > 1 else "", bool(req[2]) if len(req) > 2 else False)
            elif kind == "icons" and self.db.game(req[1]) is not None:
                self._prefetch_game_icons(req[1])

    def _error(self, message: str) -> None:
        log.warning(message)
        last, when = self._last_error
        if message != last or time.time() - when > 300:
            self._last_error = (message, time.time())
            self.listener.on_error(message)

    # achievement files ---------------------------------------------------

    def discover(self) -> None:
        game_dirs = [r["game_dir"] for r in self.db.q("SELECT game_dir FROM games WHERE game_dir IS NOT NULL AND game_dir != ''")]
        for sf in find_state_files(self.cfg.scan_dirs, game_dirs):
            if sf.path not in self.sources:
                log.info("found %s achievements for %s: %s", sf.emulator, sf.appid, sf.path)
                self.sources[sf.path] = {"path": sf.path, "appid": sf.appid, "emulator": sf.emulator, "mtime": 0.0, "size": -1}
                self._process(sf.path)

    def poll(self) -> None:
        for path, src in list(self.sources.items()):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime != src["mtime"] or st.st_size != src["size"]:
                self._process(path, st)

    def _process(self, path: str, st: os.stat_result | None = None) -> None:
        src = self.sources[path]
        appid = src["appid"]
        try:
            st = st or os.stat(path)
            states = parse_file(Path(path))
        except (OSError, ValueError, UnicodeError) as e:
            # Usually the game is mid-write; the next poll retries because mtime isn't recorded.
            log.debug("could not read %s: %s", path, e)
            return

        # Anything written before we started was earned while we weren't watching: import quietly.
        silent = st.st_mtime < self.started_at
        if self.db.ensure_game(appid):
            self.meta_queue.append(appid)
        if not silent:
            self.ensure_meta(appid)

        canonical = {n.lower(): n for n in self.db.achievement_names(appid)}
        known = self.db.unlocked_names(appid)
        new = []
        with self.db.tx():
            for state in states.values():
                name = canonical.get(state.name.lower(), state.name)
                if state.max:
                    self.db.set_progress(appid, name, state.cur, state.max)
                if state.unlocked and name not in known:
                    ts = state.unlocked_at or int(time.time())
                    if not silent:
                        ts = min(ts, int(time.time()))
                    if self.db.add_unlock(appid, name, ts, src["emulator"]):
                        new.append(name)
                        known.add(name)
            self.db.upsert_source(path, appid, src["emulator"], st.st_mtime, st.st_size)
        src["mtime"], src["size"] = st.st_mtime, st.st_size

        self.listener.on_changed()
        if not silent:
            for name in new:
                log.info("unlocked %s / %s", appid, name)
                self.listener.on_unlock(self.unlock_event(appid, name))

    def unlock_event(self, appid: int, name: str) -> dict:
        return build_unlock_event(self.db, appid, name)

    # Steam metadata ------------------------------------------------------

    def queue_stale_meta(self) -> None:
        cutoff = time.time() - META_MAX_AGE
        for row in self.db.games(include_hidden=True):
            if (not row["schema_fetched_at"] or row["schema_fetched_at"] < cutoff or not row["name"]
                    or not row["genre"]):
                self.meta_queue.append(row["appid"])

    def ensure_meta(self, appid: int, force: bool = False) -> None:
        game = self.db.game(appid)
        if game is None or game["psn_id"]:      # PlayStation games get their data from PSN
            return
        if not force and game["schema_fetched_at"] and game["name"] and game["genre"]:
            return
        if not force and time.time() - self._meta_attempts.get(appid, 0) < META_RETRY:
            return
        self._meta_attempts[appid] = time.time()

        name, header, genre = game["name"], game["header_url"], game["genre"]
        if not name or not header or not genre:
            # The store API is rate limited, so only ask it for what we don't already know.
            try:
                store_name, store_header, store_genre = steam_api.fetch_app_info(appid)
                name, header, genre = store_name or name, store_header or header, store_genre or genre
            except steam_api.SteamError as e:
                log.info("store lookup for %s failed: %s", appid, e)

        achievements, answered = [], False
        key = self.cfg.steam_api_key.strip()
        if key:
            try:
                schema_name, achievements = steam_api.fetch_schema(appid, key, self.cfg.language or "english")
                name = name or schema_name
                answered = True
            except steam_api.SteamError as e:
                self._error(f"Couldn't load achievements for {name or appid}: {e}")
        if not achievements and game["game_dir"]:
            local = find_local_schema(game["game_dir"])
            if local:
                try:
                    achievements = steam_api.load_local_schema(local, self.cfg.language or "english")
                    answered = True
                except (OSError, ValueError) as e:
                    log.info("bad local schema %s: %s", local, e)

        try:
            pct = steam_api.fetch_global_pct(appid)
        except steam_api.SteamError:
            pct = {}

        if achievements:
            steam_api.fill_hidden_descriptions(appid, achievements, self.cfg.language or "english")
            self.db.set_schema(appid, achievements, pct)
            if not self._steam_only(appid):
                self._prefetch_game_icons(appid)
                if self.db.blank_descriptions(appid):
                    self._describe_queue.append((appid, "", "", False, False, False))
        elif pct:
            self.db.set_global_pct(appid, pct)

        values = {"name": name, "header_url": header, "genre": genre}
        if answered:
            values["schema_fetched_at"] = int(time.time())
        self.db.update_game(appid, **{k: v for k, v in values.items() if v})
        if header:
            self._prefetch_icons(appid, [header])
        self.listener.on_changed()

    def _steam_only(self, appid: int) -> bool:
        # A big Steam library would mean tens of thousands of icons; those load when the game is opened.
        game = self.db.game(appid)
        return bool(game["steam_owned"]) and not game["playtime"] and not self.db.sources_for(appid)

    def _prefetch_game_icons(self, appid: int) -> None:
        rows = self.db.q("SELECT icon, icon_gray FROM achievements WHERE appid = ?", (appid,))
        refs = [r["icon"] for r in rows] + [r["icon_gray"] for r in rows]
        if any(ref and not steam_api.cached_icon(appid, ref) for ref in refs):
            self._prefetch_icons(appid, refs)

    def _prefetch_icons(self, appid: int, refs: list) -> None:
        refs = [r for r in refs if r]

        def work():
            for ref in refs:
                steam_api.ensure_icon(appid, ref)
            self.listener.on_changed()

        try:
            self._icons.submit(work)
        except RuntimeError:
            pass  # shutting down

    # save files ----------------------------------------------------------

    def _prefix_from_sources(self, appid: int) -> str | None:
        for path in self.db.sources_for(appid):
            idx = path.find("/drive_c/")
            if idx > 0:
                return path[:idx]
        return None

    def _is_shared(self, path: str) -> bool:
        # The registry is shared by everything in the prefix: only read it while the game runs.
        return os.path.basename(path).lower() == "user.reg"

    def _own_registry_keys(self, appid: int, flat: dict) -> dict:
        """Registry keys that belong to this game: Unity writes them under Software\\<Company>\\<Game>."""
        game = self.db.game(appid)
        name_words = {w for w in matcher.tokens(game["name"] or "") if len(w) > 2 and not w.isdigit()} if game else set()
        if not name_words:
            return {}
        own = {}
        for key, value in flat.items():
            section = set(matcher.tokens(" ".join(key.split(".")[1:3]), is_key=True))
            if len(section & name_words) >= max(1, len(name_words) // 2):
                own[key] = value
        return own

    def _snapshot_path(self, path: str, appid: int):
        return SNAPSHOT_DIR / f"{appid}-{hashlib.sha1(path.encode()).hexdigest()[:16]}.json"

    def _read_snapshot(self, path: str, appid: int) -> dict | None:
        try:
            return json.loads(self._snapshot_path(path, appid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_snapshot(self, path: str, appid: int, flat: dict) -> None:
        dest = self._snapshot_path(path, appid)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        tmp.write_text(json.dumps(flat), encoding="utf-8")
        tmp.replace(dest)

    def _snapshot_candidates(self, appid: int, run: _Running) -> dict:
        """Remember save-looking files as they are at game start, so the first save of the
        session can be compared against them instead of only being read as a starting point."""
        snaps: dict = {}
        deadline = time.monotonic() + 5.0
        try:
            files = savefiles.candidate_files(run.prefix, run.game_dir)
        except OSError:
            return snaps
        files = [f for f in files if savefiles.looks_like_save(f) and (f, appid) not in self.saves
                 and f not in self.sources]
        files.sort(key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0, reverse=True)
        for path in files[:SNAPSHOT_FILES]:
            if time.monotonic() > deadline:
                break
            try:
                snaps[path] = savefiles.load_save(path)[1]
            except (savefiles.Unreadable, OSError, ValueError, UnicodeError):
                continue
        return snaps

    def discover_saves(self, appid: int, run: _Running) -> None:
        """Registers files the game wrote during this session as its saves."""
        try:
            files = savefiles.candidate_files(run.prefix, run.game_dir)
        except OSError:
            return
        for path in savefiles.changed_since(files, run.started - 2):
            if (path, appid) in self.saves or path in self.sources:
                continue
            if not savefiles.is_candidate(path):
                continue
            old = (run.snapshots or {}).get(path)
            try:
                fmt, flat = savefiles.load_save(path)
                if self._is_shared(path):
                    flat = self._own_registry_keys(appid, flat)
                    if not flat:
                        continue
            except savefiles.Unreadable as e:
                if savefiles.looks_like_save(path):
                    st = os.stat(path)
                    log.info("save for %s is unreadable (%s): %s", appid, e, path)
                    self.db.upsert_save_file(path, appid, None, False, str(e), st.st_mtime, st.st_size)
                    self.saves[(path, appid)] = {"path": path, "appid": appid, "readable": 0,
                                                 "mtime": st.st_mtime, "size": st.st_size}
                    self.listener.on_changed()
                continue
            except (OSError, ValueError, UnicodeError):
                continue
            log.info("found %s save for %s: %s", fmt, appid, path)
            self._handle_save(path, appid, fmt, flat, old)

    def poll_saves(self) -> None:
        if not self.cfg.save_detection:
            return
        for (path, appid), info in list(self.saves.items()):
            if not info.get("readable"):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime == info["mtime"] and st.st_size == info["size"]:
                continue
            try:
                fmt, flat = savefiles.load_save(path)
            except savefiles.Unreadable:
                continue
            except (OSError, ValueError, UnicodeError):
                continue    # probably mid-write; retry next poll
            if self._is_shared(path):
                flat = self._own_registry_keys(appid, flat)
            match = appid in self.running or not self._is_shared(path)
            self._handle_save(path, appid, fmt, flat, self._read_snapshot(path, appid), match=match)

    def _handle_save(self, path: str, appid: int, fmt: str, flat: dict, old: dict | None, match: bool = True) -> None:
        st = os.stat(path)
        if match:
            self._apply_save_matches(appid, old, flat, first_time=(path, appid) not in self.saves)
        self._write_snapshot(path, appid, flat)
        self.db.upsert_save_file(path, appid, fmt, True, None, st.st_mtime, st.st_size)
        self.saves[(path, appid)] = {"path": path, "appid": appid, "readable": 1, "format": fmt,
                                     "mtime": st.st_mtime, "size": st.st_size}

    def _achievement_models(self, appid: int) -> list:
        rows = self.db.achievements(appid)
        key = (len(rows), tuple(sorted((r["name"], r.get("description") or "") for r in rows)).__hash__())
        cached = self._models.get(appid)
        if cached is None or cached[0] != key:
            cached = self._models[appid] = (key, matcher.build_models(rows))
        return cached[1]

    def _apply_save_matches(self, appid: int, old: dict | None, new: dict, first_time: bool = False) -> None:
        models = self._achievement_models(appid)
        if not models:
            return
        rejected = self.db.rejections(appid)
        best = matcher.best_per_achievement(matcher.evaluate(models, old, new, rejected))
        if old is not None and first_time:
            # A newly found save may already show things earned before AchTrack watched it: suggest those.
            for name, m in matcher.best_per_achievement(matcher.evaluate(models, None, new, rejected)).items():
                if name not in best and m.satisfied:
                    m.confidence = "medium"
                    best[name] = m
        unlocked = self.db.unlocked_names(appid)
        suggested = self.db.suggestions(appid)
        baseline = old is None
        fresh = []
        for name, m in best.items():
            if name in unlocked:
                continue
            if m.progress and m.confidence == "high":
                self.db.set_progress(appid, name, m.progress[0], m.progress[1])
            if not m.satisfied:
                continue
            # Only a clear change seen during play unlocks by itself; anything else is a suggestion.
            if not baseline and m.confidence == "high":
                if self.db.add_unlock(appid, name, int(time.time()), SAVE, m.reason):
                    fresh.append(name)
            elif name not in suggested:
                self.db.add_suggestion(appid, name, m.reason)
        self.listener.on_changed()
        for name in fresh:
            log.info("unlocked %s / %s from save (%s)", appid, name, best[name].reason)
            self.listener.on_unlock(self.unlock_event(appid, name))

    # Steam library -------------------------------------------------------

    def end_session(self, appid: int) -> None:
        """You said you're not playing: close the session and ignore whatever is still matching.

        A game that hangs on exit leaves a process behind that still looks exactly like the game -
        because it is the game - so the only way to know it's over is that you say so.
        """
        run = self.running.pop(appid, None)
        if run is None:
            return
        now = time.time()
        self._flush_playtime(appid, run, now)
        self.db.update_session(run.session, int(run.seconds), int(now))
        if self.monitor:
            self.monitor.ignore_matching(appid)
        log.info("session for %s ended by hand after %ds", appid, run.seconds)
        self.listener.on_game_stopped(appid, int(run.seconds))
        self.listener.on_changed()

    def _start_export(self, deploy: bool = False) -> None:
        """The phone page goes over the network - and, with a sync command, through rclone. Neither
        belongs on the loop that watches your games."""
        if self._export_thread is not None and self._export_thread.is_alive():
            return

        def work():
            try:
                self.build_export(deploy, db=Database(self.db.path))
            except Exception:
                log.exception("phone page crashed")

        self._export_thread = threading.Thread(target=work, name="achtrack-export", daemon=True)
        self._export_thread.start()

    def _start_share(self, name: str, publish: bool) -> None:
        """Building the page reads the library and may upload it, so it gets its own thread too."""
        if self._share_thread is not None and self._share_thread.is_alive():
            return

        def work():
            try:
                status = self.build_share(name, publish, db=Database(self.db.path))
            except Exception:
                log.exception("share page crashed")
                status = {"time": int(time.time()), "page": "", "url": "",
                          "error": "building the page failed - see the log"}
            self.listener.on_shared(status)

        self._share_thread = threading.Thread(target=work, name="achtrack-share", daemon=True)
        self._share_thread.start()

    def _start_describe(self) -> None:
        """Hidden-description lookups go over the network, so they get their own thread - the main
        loop has to stay free to notice that a game has stopped."""
        if self._describe_thread is not None and self._describe_thread.is_alive():
            return

        def work():
            db = Database(self.db.path)
            while self._describe_queue:
                appid, trophy_list, page_url, force, announce, find_page = self._describe_queue.popleft()
                try:
                    status = self.describe_hidden(appid, force=force, trophy_list=trophy_list,
                                                  page_url=page_url, find_page=find_page, db=db)
                except Exception:
                    log.exception("description lookup crashed")
                    status = {"filled": 0, "blanks": 0, "source": "", "title": "", "searched": False,
                              "error": "the lookup failed - see the log"}
                if announce:
                    self.listener.on_described(appid, status)

        self._describe_thread = threading.Thread(target=work, name="achtrack-describe", daemon=True)
        self._describe_thread.start()

    def _start_steam_sync(self) -> None:
        # The first sync of a big library takes a while; keep watching achievement files meanwhile.
        if self._steam_thread is not None and self._steam_thread.is_alive():
            return

        def work():
            try:
                self.steam_sync(Database(self.db.path))
            except Exception:
                log.exception("Steam sync crashed")

        self._steam_thread = threading.Thread(target=work, name="achtrack-steam", daemon=True)
        self._steam_thread.start()

    def steam_sync(self, db: Database, report=None) -> dict:
        """Copies owned Steam games, their playtime and Steam-earned achievements into the library.

        The outcome is stored as meta "steam_sync" so Settings can show it; report(line) gets
        one line per game for the command line."""
        status = {"time": int(time.time()), "ok": False, "error": "", "games": 0, "achievements": 0,
                  "new_achievements": 0, "steam_id": ""}

        def finish(error: str = "") -> dict:
            status["error"] = error
            status["ok"] = not error
            db.set_meta("steam_sync", status)
            if error:
                self._error(f"Steam sync: {error}")
            self.listener.on_changed()
            return status

        key = self.cfg.steam_api_key.strip()
        if not self.cfg.steam_sync:
            return finish("turned off in Settings")
        if not key:
            return finish("no Steam Web API key in Settings")
        try:
            if self._steam_id is None:
                self._steam_id = (steam_api.parse_steam_id(self.cfg.steam_id, key) if self.cfg.steam_id.strip()
                                  else detect_steam_id()) or ""
            status["steam_id"] = self._steam_id
            if not self._steam_id:
                return finish("couldn't find your Steam account - enter your Steam profile URL in Settings")
            owned = steam_api.fetch_owned_games(key, self._steam_id)
        except steam_api.SteamError as e:
            return finish(str(e))

        for entry in owned:
            appid = entry.get("appid")
            seconds = int(entry.get("playtime_forever") or 0) * 60
            last = int(entry.get("rtime_last_played") or 0) or None
            game = db.game(appid) if appid else None
            if not appid or (game is None and not seconds):
                continue  # owned but never played: keep the library about what you actually played
            if game is None:
                db.ensure_game(appid)
                db.update_game(appid, name=entry.get("name") or None,
                               header_url=steam_api.HEADER_URL.format(appid=appid))
                self.meta_queue.append(appid)
                game = db.game(appid)
            status["games"] += 1
            db.update_game(appid, steam_owned=1, steam_playtime=seconds, steam_last_played=last)
            # Fetch when playtime moved, or until we've successfully read this game once.
            synced = db.get_meta(f"steam_ach:{appid}")
            if synced is not None and synced.get("playtime") == seconds and synced.get("last") == last:
                status["achievements"] += synced.get("count", 0)
                continue
            try:
                earned = steam_api.fetch_player_achievements(key, self._steam_id, appid, self.cfg.language or "english")
            except steam_api.SteamError as e:
                if e.status in (401, 403):
                    return finish("Steam won't share your achievements: set your Steam profile's Game details to "
                                  "Public (Steam → Profile → Edit Profile → Privacy Settings), then Sync again")
                if report:
                    report(f"  {entry.get('name') or appid}: couldn't read achievements ({e})")
                continue
            added = 0
            with db.tx():
                for api_name, unlocked_at in earned:
                    if db.add_unlock(appid, api_name, unlocked_at or int(time.time()), STEAM):
                        added += 1
            db.set_meta(f"steam_ach:{appid}", {"playtime": seconds, "last": last, "count": len(earned)})
            status["achievements"] += len(earned)
            status["new_achievements"] += added
            if report:
                report(f"  {entry.get('name') or appid}: {seconds / 3600:.1f} h, {len(earned)} achievements"
                       + (f" ({added} new)" if added else ""))
        if status["new_achievements"]:
            log.info("imported %d Steam achievements", status["new_achievements"])
        return finish()

    # backups and the phone page -------------------------------------------

    def run_backup(self) -> dict:
        try:
            result = backup.run_backup(self.db, Path(self.cfg.backup_dir or BACKUP_DIR), self.cfg.backup_keep)
        except (OSError, sqlite3.Error) as e:
            self._error(f"Backup failed: {e}")
            return {"error": str(e)}
        log.info("backup written to %s", result["dir"])
        if self.cfg.export_auto:
            result["export"] = self.build_export()
        self.db.set_meta("backup", {**result, "error": ""})
        self._run_sync_command(result["dir"])
        self.listener.on_changed()
        return result

    def run_device_sync(self) -> dict:
        """Exchanges libraries with your other machines through the shared folder."""
        folder = self.cfg.sync_dir.strip()
        if folder and self.cfg.sync_pull_command.strip():
            self._run_command(self.cfg.sync_pull_command, folder, "Fetching the shared folder")
        status = sync.run_sync(self.db, self.cfg.sync_dir)
        if folder and not status["error"] and self.cfg.sync_push_command.strip():
            self._run_command(self.cfg.sync_push_command, folder, "Uploading to the shared folder")
        if status["error"]:
            self._error(f"Device sync failed: {status['error']}")
        elif status["unlocks"] or status["games"] or status["removed"] or status["updated"]:
            log.info("device sync: %s", status)
            self.listener.on_changed()
        return status

    def build_share(self, name: str = "", publish: bool = False, out_dir: str = "",
                    db: Database | None = None) -> dict:
        """Writes the page you send to friends, and publishes it when asked."""
        db = db or self.db
        folder = Path(out_dir).expanduser() if out_dir else SHARE_DIR
        status = {"time": int(time.time()), "page": "", "url": "", "error": ""}
        try:
            status["page"] = str(share.write_page(db, folder, name or self.cfg.share_name))
        except (OSError, ValueError) as e:
            status["error"] = str(e)
            self._error(f"Building the share page failed: {e}")
            return status
        if publish:
            token = self.cfg.vercel_token.strip()
            if not token:
                status["error"] = "no Vercel token in Settings, so there's nothing to publish to"
                return status
            from .deploy import DeployError, publish as send
            try:
                status["url"] = send(folder, token, self.cfg.share_project or "achtrack-stats",
                                     self.cfg.vercel_team or None)
                log.info("share page published to %s", status["url"])
            except DeployError as e:
                status["error"] = str(e)
                self._error(f"Publishing the share page failed: {e}")
        db.set_meta("share", status)
        self.listener.on_changed()
        return status

    def build_export(self, deploy: bool = False, db: Database | None = None) -> str:
        """Writes the phone page; publishes it too when a Vercel token is set."""
        db = db or self.db
        folder = Path(self.cfg.export_dir or EXPORT_DIR)
        try:
            page = export.write_page(db, folder, self.cfg.export_passphrase)
        except (OSError, ValueError) as e:
            self._error(f"Phone page failed: {e}")
            return ""
        status = {"time": int(time.time()), "page": str(page), "url": "", "error": ""}
        if self.cfg.publish_command.strip():
            ok = self._run_command(self.cfg.publish_command, str(folder), "Publishing the phone page")
            status["url"] = "" if not ok else status["url"]
            status["error"] = "" if ok else "see the log"
        elif deploy or (self.cfg.vercel_token.strip() and self.cfg.export_auto):
            from .deploy import DeployError, publish
            try:
                status["url"] = publish(folder, self.cfg.vercel_token, self.cfg.vercel_project,
                                        self.cfg.vercel_team or None)
                log.info("phone page published to %s", status["url"])
            except DeployError as e:
                status["error"] = str(e)
                self._error(f"Publishing the phone page failed: {e}")
        db.set_meta("export", status)
        self._run_sync_command(str(folder))
        self.listener.on_changed()
        return status["url"] or str(page)

    def _run_sync_command(self, folder: str) -> None:
        """Optional command after a backup/export, e.g. rclone copy {dir} gdrive:AchTrack."""
        if self.cfg.sync_command.strip():
            self._run_command(self.cfg.sync_command, folder, "Sync command")

    def _run_command(self, template: str, folder: str, what: str) -> bool:
        try:
            proc = subprocess.run(template.replace("{dir}", folder), shell=True, capture_output=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as e:
            self._error(f"{what} failed: {e}")
            return False
        if proc.returncode:
            self._error(f"{what} failed: {proc.stderr.decode('utf-8', 'replace')[:200]}")
            return False
        log.info("%s ok: %s", what, proc.stdout.decode('utf-8', 'replace')[:200].strip())
        return True

    # PlayStation ---------------------------------------------------------

    def _start_psn_sync(self) -> None:
        if self._psn_thread is not None and self._psn_thread.is_alive():
            return

        def work():
            try:
                self.psn_sync(Database(self.db.path))
            except Exception:
                log.exception("PlayStation sync crashed")

        self._psn_thread = threading.Thread(target=work, name="achtrack-psn", daemon=True)
        self._psn_thread.start()

    def _known_trophy_lists(self, appid: int, name: str, db: Database | None = None) -> list:
        """Trophy lists worth trying for a PC game: the same game in your PlayStation library,
        and anything you've linked by hand."""
        db = db or self.db
        game = db.game(appid)
        known = [game["trophy_src"]] if game["trophy_src"] and game["trophy_src"] != "-" else []
        wanted = psn.normalize_name(name)
        for row in db.q("SELECT name, psn_id FROM games WHERE psn_id IS NOT NULL AND psn_id != ''"):
            if psn.normalize_name(row["name"] or "") == wanted and row["psn_id"] not in known:
                known.append(row["psn_id"])
        return known

    def describe_hidden(self, appid: int, note=None, force: bool = False, trophy_list: str = "",
                        page_url: str = "", find_page: bool = False,
                        db: Database | None = None) -> dict:
        """Describes a game's hidden achievements, which Steam blanks out for good.

        Either from the game's PlayStation trophy list, or - `page_url` - from a page you found
        yourself that lists the achievements. Both are only believed once what they say about the
        achievements already readable matches; see descriptions.py.
        """
        status = {"filled": 0, "blanks": 0, "source": "", "title": "", "error": "", "searched": False}
        db = db or self.db          # a background thread brings its own connection
        game = db.game(appid)
        if game is None:
            return {**status, "error": "no such game"}
        if appid < 0:
            return {**status, "error": "PlayStation games already carry their descriptions"}
        status["blanks"] = db.blank_descriptions(appid)
        if not status["blanks"]:
            return status
        if not force and not self.cfg.psn_describe_hidden:
            return {**status, "error": "turned off in Settings"}
        if page_url:
            return self._describe_from_page(appid, page_url, status, note, db)
        npsso = self.cfg.psn_npsso.strip()
        if not npsso:
            return {**status, "error": "no PlayStation login in Settings"}
        name = game["name"] or ""
        if not name:
            return {**status, "error": "this game has no name yet"}
        linked = descriptions.trophy_id(trophy_list)
        if trophy_list and not linked:
            return {**status, "error": f"{trophy_list!r} has no trophy list id in it (looks like NPWR12345_00)"}
        known = game["trophy_src"] or ""
        if known == "-" and not (force or linked or find_page):
            return {**status, "error": "no matching trophy list (searched before)"}
        if known == "-" and find_page and not linked:
            return self._describe_from_page(appid, name, status, note, db)

        achievements = db.achievements(appid)
        try:
            tokens = psn.valid_token(db.get_meta("psn_tokens"), npsso)
            db.set_meta("psn_tokens", tokens)
            token = tokens["access_token"]
            if linked:
                found, source, title = descriptions.lookup(token, name, achievements, note,
                                                           known=[linked], search=False)
            elif known and known != "-" and not force:
                comm_id, _, service = known.partition("/")
                trophies = psn.trophy_definitions(token, comm_id, service)
                found, agreement, matched = descriptions.match(achievements, trophies)
                source, title = (known, name) if found else ("", "")
                if note:
                    note(f"{comm_id}: {matched} names in common ({agreement:.0%}), "
                         f"{len(found)} descriptions to fill")
            else:
                status["searched"] = True
                found, source, title = descriptions.lookup(
                    token, name, achievements, note, known=self._known_trophy_lists(appid, name, db))
        except psn.PSNError as e:
            return {**status, "error": str(e)}

        if not found and find_page and not linked:
            if note:
                note("PlayStation has no trophy list for this account - looking for a page instead")
            db.update_game(appid, trophy_src="-")
            return self._describe_from_page(appid, name, status, note, db)
        if found:
            status["filled"] = db.learn_descriptions(appid, found, source)
        if found or not linked:
            db.update_game(appid, trophy_src=source or "-")
        status["source"], status["title"] = source, title
        self.listener.on_changed()
        return status

    def _describe_from_page(self, appid: int, where: str, status: dict, note=None,
                            db: Database | None = None) -> dict:
        """Takes the descriptions off a page. `where` is an address, or a name to look one up by."""
        db = db or self.db
        achievements = db.achievements(appid)
        if re.match(r"https?://", where.strip(), re.I):
            pages = [(where.strip(), "")]
        else:
            pages = descriptions.find_pages(where)
            if note:
                note(f"looked for a page about {where!r}: "
                     + (", ".join(title for _url, title in pages) if pages else "found nothing"))
            if not pages:
                return {**status, "error": f"couldn't find a page listing {where}'s achievements"}

        last = "nothing matched"
        for url, title in pages:
            try:
                page = descriptions.fetch_page(url)
            except OSError as e:
                last = str(e)
                if note:
                    note(f"  {url}: {last}")
                continue
            found, confidence, why = descriptions.from_page(page, achievements)
            if note:
                note(f"  {title or url} - {why}")
            if not found:
                last = why
                continue
            status["filled"] = db.learn_descriptions(appid, found, url)
            status["source"], status["title"] = url, title or f"{confidence:.0%} sure"
            self.listener.on_changed()
            return status
        return {**status, "error": last}

    def forget_descriptions(self, appid: int) -> int:
        """Drops what a lookup filled in, so a game goes back to Steam's own blanks."""
        names = [r["name"] for r in self.db.q("SELECT name FROM learned_descriptions WHERE appid = ?", (appid,))]
        for name in names:
            self.db.x("UPDATE achievements SET description = '' WHERE appid = ? AND name = ?", (appid, name))
        self.db.x("DELETE FROM learned_descriptions WHERE appid = ?", (appid,))
        self.db.update_game(appid, trophy_src="")
        self.listener.on_changed()
        return len(names)

    def psn_sync(self, db: Database, report=None) -> dict:
        """Copies your PlayStation games, play time and trophies into the library."""
        status = {"time": int(time.time()), "ok": False, "error": "", "games": 0, "trophies": 0, "new_trophies": 0}

        def finish(error: str = "") -> dict:
            status["error"] = error
            status["ok"] = not error
            db.set_meta("psn_sync", status)
            if error:
                self._error(f"PlayStation sync: {error}")
            self.listener.on_changed()
            return status

        npsso = self.cfg.psn_npsso.strip()
        if not self.cfg.psn_sync:
            return finish("turned off in Settings")
        if not npsso:
            return finish("no PlayStation login in Settings")
        try:
            tokens = psn.valid_token(db.get_meta("psn_tokens"), npsso)
            db.set_meta("psn_tokens", tokens)
            token = tokens["access_token"]
            titles = psn.trophy_titles(token)
            played = {psn.normalize_name(t["name"]): t for t in psn.play_times(token)}
        except psn.PSNError as e:
            return finish(str(e))

        for title in titles:
            psn_id = title.get("npCommunicationId")
            if not psn_id:
                continue
            name = title.get("trophyTitleName") or psn_id
            platform = (title.get("trophyTitlePlatform") or "PS5").split(",")[0].lower()
            earned = title.get("earnedTrophies") or {}
            earned_count = sum(int(earned.get(k) or 0) for k in psn.TROPHY_KINDS)
            game = db.psn_game(psn_id)
            if game is None:
                appid = db.next_psn_appid()
                db.ensure_game(appid)
                db.update_game(appid, psn_id=psn_id, platform=platform, name=name,
                               header_url=title.get("trophyTitleIconUrl") or "")
                game = db.game(appid)
            appid = game["appid"]
            status["games"] += 1
            status["trophies"] += earned_count

            match = played.get(psn.normalize_name(name))
            if match:
                db.update_game(appid, psn_playtime=match["seconds"], psn_last_played=match["last_played"])
            db.update_game(appid, name=name, platform=platform,
                           header_url=game["header_url"] or title.get("trophyTitleIconUrl") or "")

            # Only re-read a game's trophies when its progress moved since the last sync.
            seen = db.get_meta(f"psn_title:{psn_id}")
            stamp = {"earned": earned_count, "updated": title.get("lastUpdatedDateTime"),
                     "version": title.get("trophySetVersion")}
            if seen == stamp:
                continue
            try:
                trophies = psn.title_trophies(token, psn_id, title.get("npServiceName") or "trophy2")
            except psn.PSNError as e:
                if report:
                    report(f"  {name}: {e}")
                continue
            schema = [{"name": f"trophy_{t['id']}", "display_name": t["name"], "description": t["detail"],
                       "icon": t["icon"], "icon_gray": t["icon"], "hidden": int(t["hidden"]),
                       "global_pct": t["rate"], "kind": t["kind"]} for t in trophies]
            if schema:
                db.set_schema(appid, schema, {})
                self._prefetch_icons(appid, [t["icon"] for t in trophies] + [game["header_url"]])
            added = 0
            with db.tx():
                for t in trophies:
                    if t["earned"] and db.add_unlock(appid, f"trophy_{t['id']}",
                                                     t["earned_at"] or int(time.time()), PSN):
                        added += 1
            db.update_game(appid, schema_fetched_at=int(time.time()))
            db.set_meta(f"psn_title:{psn_id}", stamp)
            status["new_trophies"] += added
            if report:
                hours = (match["seconds"] / 3600) if match else 0
                report(f"  {name} [{platform}]: {hours:.1f} h, {earned_count}/{len(trophies)} trophies"
                       + (f" ({added} new)" if added else ""))
        if status["new_trophies"]:
            log.info("imported %d PlayStation trophies", status["new_trophies"])
        return finish()

    # playtime ------------------------------------------------------------

    def track_processes(self) -> None:
        exe_map = {}
        for row in self.db.q("SELECT appid, exe_name FROM games WHERE exe_name IS NOT NULL AND exe_name != ''"):
            for exe in row["exe_name"].split(";"):
                if exe.strip():
                    exe_map[os.path.basename(exe.strip()).lower()] = row["appid"]
        found = self.monitor.scan(exe_map)
        now = time.time()
        changed = False

        for appid, game_dir in found.items():
            run = self.running.get(appid)
            if run is not None:
                run.misses = 0
                continue
            created = self.db.ensure_game(appid)
            game = self.db.game(appid)
            if game_dir and not game["game_dir"]:
                self.db.update_game(appid, game_dir=game_dir)
                self._force_scan = True  # look for local saves in the new folder
            if game["hidden"]:
                self.db.update_game(appid, hidden=0)
            run = self.running[appid] = _Running(self.db.start_session(appid, int(now)), now, now)
            run.prefix = self.monitor.prefixes.get(appid) or self._prefix_from_sources(appid)
            run.game_dir = game_dir or game["game_dir"]
            run.next_save_scan = now + SAVE_SCAN_EVERY
            if self.cfg.save_detection:
                run.snapshots = self._snapshot_candidates(appid, run)
            self.db.update_game(appid, last_played=int(now))
            self.ensure_meta(appid, force=created)
            game = self.db.game(appid)
            header = steam_api.cached_icon(appid, game["header_url"] or "") or ""
            log.info("game started: %s", appid)
            self.listener.on_game_started(appid, game_title(game, appid), header)
            changed = True

        for appid, run in list(self.running.items()):
            if appid in found:
                self._flush_playtime(appid, run, now)
                self.db.update_session(run.session, int(run.seconds))
                continue
            # Launchers hand off between processes; wait for two empty scans before ending.
            run.misses += 1
            if run.misses >= 2:
                if self.cfg.save_detection:
                    self.discover_saves(appid, run)   # games often write the final save on exit
                if self.screen is not None and self.screen.appid == appid:
                        self._stop_screen()
                self.db.update_session(run.session, int(run.seconds), int(now))
                del self.running[appid]
                log.info("game stopped: %s after %ds", appid, run.seconds)
                changed = True
                self.listener.on_game_stopped(appid, int(run.seconds))
                self.listener.on_changed()      # the library should stop saying "playing now" now
                if self.cfg.export_auto:
                    self._start_export()

        if changed or self.running:
            self.listener.on_changed()

    def _flush_playtime(self, appid: int, run: _Running, now: float) -> None:
        # Cap each step so a suspended machine doesn't count as playtime.
        delta = min(max(0.0, now - run.last), max(1.0, self.cfg.process_interval) * 3)
        run.last = now
        run.seconds += delta
        run.pending += delta
        whole = int(run.pending)
        if whole:
            run.pending -= whole
            self.db.add_playtime(appid, whole, int(now))
