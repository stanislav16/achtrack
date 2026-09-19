"""Background engine: watches achievement files, tracks playtime, fetches Steam metadata.

It has no Qt dependency; the GUI runs it on a worker thread and receives events via a Listener.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import matcher, savefiles, steam_api
from .config import CACHE_DIR, Config
from .db import SAVE, STEAM, Database
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
        next_scan = next_poll = next_proc = next_steam = 0.0
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
                if self.meta_queue:
                    self.ensure_meta(self.meta_queue.popleft(), force=True)
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
            if not row["schema_fetched_at"] or row["schema_fetched_at"] < cutoff or not row["name"]:
                self.meta_queue.append(row["appid"])

    def ensure_meta(self, appid: int, force: bool = False) -> None:
        game = self.db.game(appid)
        if game is None:
            return
        if not force and game["schema_fetched_at"] and game["name"]:
            return
        if not force and time.time() - self._meta_attempts.get(appid, 0) < META_RETRY:
            return
        self._meta_attempts[appid] = time.time()

        name, header = game["name"], game["header_url"]
        if not name or not header:
            # The store API is rate limited, so only ask it for what we don't already know.
            try:
                store_name, store_header = steam_api.fetch_app_info(appid)
                name, header = store_name or name, store_header or header
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
        elif pct:
            self.db.set_global_pct(appid, pct)

        values = {"name": name, "header_url": header}
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
                self.db.update_session(run.session, int(run.seconds), int(now))
                del self.running[appid]
                log.info("game stopped: %s after %ds", appid, run.seconds)
                self.listener.on_game_stopped(appid, int(run.seconds))
                changed = True

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
