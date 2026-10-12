from __future__ import annotations

import argparse
import logging
import os
import logging.handlers
import sys
import time
from pathlib import Path

from . import __version__
from .config import APP_ID, DATA_DIR


def setup_logging(verbose: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list = [logging.handlers.RotatingFileHandler(DATA_DIR / f"{APP_ID}.log", maxBytes=1_000_000, backupCount=2)]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_scan() -> int:
    from .config import Config
    from .db import Database
    from .emulators import parse_file
    from .playtime import ProcessMonitor
    from .scanner import find_prefixes, find_state_files
    from pathlib import Path

    cfg = Config.load()
    prefixes = find_prefixes(cfg.scan_dirs)
    print("Windows itself is the host here: emulator folders are read directly." if os.name == "nt"
          else f"Wine/Proton prefixes ({len(prefixes)}):")
    for p in prefixes:
        print(f"  {p}")
    db = Database()
    game_dirs = [r["game_dir"] for r in db.games(include_hidden=True) if r["game_dir"]]
    files = find_state_files(cfg.scan_dirs, game_dirs)
    print(f"\nAchievement files ({len(files)}):")
    for sf in files:
        try:
            states = parse_file(Path(sf.path))
            summary = f"{sum(s.unlocked for s in states.values())}/{len(states)} unlocked"
        except (OSError, ValueError) as e:
            summary = f"unreadable: {e}"
        print(f"  [{sf.emulator}] app {sf.appid}: {summary}\n      {sf.path}")
    beat = db.get_meta("heartbeat") or {}
    open_now = db.open_sessions()
    if beat:
        age = int(time.time()) - int(beat.get("time") or 0)
        state = "running normally" if age < 60 else f"NOT TICKING - last tick {age}s ago"
        print(f"\nEngine: {state}; it thinks these are playing: {beat.get('running') or 'nothing'}")
        for appid, procs in (beat.get("matches") or {}).items():
            misses = (beat.get("misses") or {}).get(appid, 0)
            print(f"  app {appid}: the engine is matching {len(procs)} process(es), "
                  f"{misses} scan(s) with none")
            for entry in procs:
                pid, pstate, what = entry[0], entry[1], entry[2]
                print(f"      pid {pid} [{pstate}] {what}")
                if len(entry) > 3 and entry[3]:
                    print(f"          because {entry[3]}")
    else:
        print("\nEngine: hasn't reported yet (start AchTrack, or it is an older version)")
    if open_now:
        print(f"Open sessions (what makes the library say \"playing now\"): {sorted(open_now)}")

    if ProcessMonitor.available():
        exe_map = {}
        for r in db.games(include_hidden=True):
            for exe in (r["exe_name"] or "").split(";"):
                if exe.strip():
                    exe_map[exe.strip().lower()] = r["appid"]
        monitor = ProcessMonitor()
        running = monitor.scan(exe_map)
        names = {r["appid"]: r["name"] for r in db.games(include_hidden=True)}
        print(f"\nRunning games ({len(running)}):")
        for appid, game_dir in running.items():
            print(f"  app {appid}  {names.get(appid) or ''}  {game_dir or ''}")
            for pid, state, what, why in monitor.seen.get(appid, []):
                print(f"      pid {pid} [{state}] {what}")
                if why:
                    print(f"          because {why}")
        if not running:
            print("  (nothing - if the app still says a game is playing, it is the app's own state,"
                  " not a process)")
        if os.name == "nt":
            _print_windows_candidates(monitor, set(running))
    return 0


def _print_windows_candidates(monitor, running: set) -> None:
    """Every program of yours that isn't part of Windows, and why it did or didn't count as a game.

    When a game isn't noticed, it is in this list - and the line says what was missing.
    """
    from . import winhost
    from .playtime import _is_steam_install
    from .scanner import appid_in_dir
    rows, seen = [], set()
    for (pid, _started), proc in monitor.cache.items():
        exe = proc.exe
        if proc.ignore or not exe or winhost.is_system_path(exe) or exe.lower() in seen:
            continue
        seen.add(exe.lower())
        if proc.detected and proc.detected[0] in running:
            continue
        low = exe.lower()
        if "\\windowsapps\\" in low or "\\microsoft\\" in low or "\\python" in low or low.endswith("\\achtrack.exe"):
            continue
        if _is_steam_install(exe):
            verdict = "installed through Steam - left alone, Steam records its achievements itself"
        else:
            folder, config = os.path.dirname(exe), None
            for _ in range(3):
                if appid_in_dir(folder):
                    config = folder
                    break
                folder = os.path.dirname(folder)
            libs = [m for m in monitor.src.mapped(pid) if m.lower().endswith(("steam_api64.dll", "steam_api.dll"))]
            if config:
                verdict = f"crack config found in {config}"
            elif libs:
                verdict = (f"uses {libs[0]}, but no steam_appid.txt / steam_settings / emulator .ini "
                           f"was found beside it")
            else:
                verdict = "no Steam library loaded and no crack config beside it"
        rows.append((exe, verdict))
    print(f"\nOther programs running ({len(rows)}):")
    for exe, verdict in sorted(rows)[:60]:
        print(f"  {exe}\n      {verdict}")
    if rows:
        print("\nIf your game is listed above, it can still be tracked: add it in AchTrack (Add a game) "
              "and set its executable name.")


def cmd_list() -> int:
    from .db import Database

    def fmt_hours(seconds: int) -> str:
        return f"{seconds / 3600:.1f} h" if seconds >= 3600 else f"{seconds // 60} min"

    rows = Database().games(include_hidden=True)
    if not rows:
        print("Library is empty.")
        return 0
    width = max(len(r["name"] or str(r["appid"])) for r in rows)
    for r in rows:
        ach = f"{r['unlocked']}/{r['total']}" if r["total"] else str(r["unlocked"])
        print(f"{(r['name'] or 'App ' + str(r['appid'])):<{width}}  {r['appid']:>8}  {fmt_hours(r['total_playtime']):>10}  {ach:>8} achievements")
    return 0


def cmd_backup(restore_from: str | None) -> int:
    from pathlib import Path
    from .backup import restore, run_backup
    from .config import BACKUP_DIR, Config, DB_FILE
    from .db import Database

    cfg = Config.load()
    if restore_from:
        target = restore(Path(restore_from), DB_FILE)
        print(f"Restored {restore_from} into {target} (the previous database is beside it as .db.replaced).")
        return 0
    result = run_backup(Database(), Path(cfg.backup_dir or BACKUP_DIR), cfg.backup_keep)
    print(f"Database snapshot: {result['database']}")
    print(f"JSON export:       {result['json']}")
    if result["pruned"]:
        print(f"Removed {result['pruned']} older backup file(s), keeping {cfg.backup_keep}.")
    return 0


def cmd_export(deploy_it: bool) -> int:
    from pathlib import Path
    from .config import EXPORT_DIR, Config
    from .db import Database
    from .export import write_page

    cfg = Config.load()
    folder = Path(cfg.export_dir or EXPORT_DIR)
    page = write_page(Database(), folder, cfg.export_passphrase)
    size = page.stat().st_size / 1024
    print(f"Phone page: {page} ({size:.0f} KB, "
          + ("encrypted with your passphrase)" if cfg.export_passphrase.strip() else "NOT encrypted - set a passphrase in Settings)"))
    if not deploy_it:
        return 0
    from .deploy import DeployError, publish
    try:
        url = publish(folder, cfg.vercel_token, cfg.vercel_project, cfg.vercel_team or None)
    except DeployError as e:
        print(f"Deploy failed: {e}")
        return 1
    print(f"Published: {url}")
    return 0


def cmd_device_sync() -> int:
    from .config import Config
    from .db import Database
    from .sync import run_sync

    cfg = Config.load()
    if not cfg.sync_dir.strip():
        print("No shared folder set (Settings → Devices).")
        return 1
    status = run_sync(Database(), cfg.sync_dir)
    if status["error"]:
        print(f"Device sync failed: {status['error']}")
        return 1
    print(f"Merged {status['devices']} other machine(s): {status['games']} new games, "
          f"{status['unlocks']} new unlocks, {status['removed']} removals, {status['updated']} games updated.")
    return 0


def cmd_devices() -> int:
    import time
    from .config import Config
    from .db import Database
    from .sync import device_id, device_name, devices

    cfg = Config.load()
    db = Database()
    print(f"This machine: {device_name(db)} ({device_id(db)})")
    if not cfg.sync_dir.strip():
        print("No shared folder set (Settings → Devices).")
        return 1
    rows = devices(db, cfg.sync_dir)
    if not rows:
        print(f"Nothing in {cfg.sync_dir} yet.")
        return 0
    print(f"\nIn {cfg.sync_dir}:")
    for row in rows:
        if row.get("error"):
            print(f"  {row['file']}  unreadable: {row['error']}")
            continue
        when = time.strftime("%d %b %H:%M", time.localtime(row["written"]))
        mark = "  <- this machine" if row["this_machine"] else ""
        print(f"  {row['name']:<20} {row['games']:>4} games, {row['unlocks']:>5} unlocks, written {when}"
              f", {row['size'] / 1024:.0f} KB{mark}")
    return 0


def cmd_psn_sync() -> int:
    from .config import Config
    from .db import Database
    from .engine import Engine, Listener

    cfg = Config.load()
    db = Database()
    status = Engine(cfg, db, Listener()).psn_sync(db, report=print)
    if status["error"]:
        print(f"PlayStation sync failed: {status['error']}")
        return 1
    print(f"{status['games']} PlayStation games, {status['trophies']} trophies earned "
          f"({status['new_trophies']} new).")
    return 0


def cmd_not_playing(target: str) -> int:
    """Tells the running app a game it thinks is playing has actually stopped."""
    from .app import send_to_running
    from .db import Database

    db = Database()
    matches = [r for r in db.q("SELECT appid, name FROM games")
               if target.strip().lower() in (r["name"] or "").lower() or str(r["appid"]) == target.strip()]
    open_now = db.open_sessions()
    matches = [r for r in matches if r["appid"] in open_now] or matches
    if len(matches) != 1:
        print(f"{'No' if not matches else 'More than one'} game matches {target!r}"
              + (": " + ", ".join(m["name"] or str(m["appid"]) for m in matches[:6]) if matches else ""))
        return 1
    if not send_to_running(f"not-playing:{matches[0]['appid']}"):
        print("AchTrack isn't running, so there's no session to end.")
        return 1
    print(f"Ended the session for {matches[0]['name'] or matches[0]['appid']}.")
    return 0


def cmd_heroic() -> int:
    """Adds games Heroic has installed to the library, with Steam's achievement list."""
    from .config import Config
    from .db import Database
    from .engine import Engine, Listener
    from .scanner import heroic_installed

    found = heroic_installed()
    print(f"Heroic has {len(found)} game(s) installed:")
    for game in found:
        print(f"  [{game['store']}] {game['title']}  {game['folder'] or ''}")
    if not found:
        print("  (nothing - install a game in Heroic first, or it keeps its library elsewhere)")
        return 0
    status = Engine(Config.load(), Database(), Listener()).import_heroic(note=lambda t: print(f"  {t}"))
    print(f"\n{status['added']} added, {status['linked']} linked to what was already there, "
          f"{status['known']} already complete.")
    if status["unmatched"]:
        print("No Steam achievement list for these, so there is nothing to show for them:")
        for title in status["unmatched"]:
            print(f"  {title}")
    return 0


def _one_game(db, target: str):
    wanted = target.strip().lower()
    found = [r for r in db.q("SELECT appid, name FROM games WHERE appid > 0")
             if wanted in (r["name"] or "").lower() or str(r["appid"]) == wanted]
    exact = [r for r in found if (r["name"] or "").lower() == wanted or str(r["appid"]) == wanted]
    found = exact or found
    if len(found) != 1:
        print(f"{'No' if not found else 'More than one'} game matches {target!r}"
              + (": " + ", ".join(r["name"] or str(r["appid"]) for r in found[:6]) if found else ""))
        return None
    return found[0]


def cmd_saves_backup(target: str | None) -> int:
    """Packs save files into the folder your devices share."""
    from .config import Config
    from .db import Database
    from .engine import Engine, Listener

    db = Database()
    engine = Engine(Config.load(), db, Listener())
    appid = None
    if target:
        game = _one_game(db, target)
        if game is None:
            return 1
        appid = game["appid"]
    status = engine.saves_job(db, "backup", appid)
    names = {r["appid"]: r["name"] for r in db.q("SELECT appid, name FROM games")}
    for done in status["done"]:
        print(f"{names.get(done['appid']) or done['appid']}: {done['files']} files, "
              f"{done['bytes'] / 1024:.0f} KB -> {done['path']}")
    if status["error"]:
        print(status["error"])
        return 1
    if not status["done"]:
        print("No save files known on this machine yet.")
    return 0


def cmd_saves_restore(target: str, pick: int, listing: bool) -> int:
    """Puts a backup of a game's saves in place on this machine."""
    import datetime
    from .app import send_to_running
    from .config import BACKUP_DIR, Config
    from .db import Database
    from .engine import Engine, Listener
    from . import savesync

    db = Database()
    cfg = Config.load()
    game = _one_game(db, target)
    if game is None:
        return 1
    found = [b for b in savesync.backups(cfg, BACKUP_DIR, game["appid"]) if not b["pending"]]
    if not found:
        print(f"No backups of {game['name']} to restore.")
        return 1
    for number, entry in enumerate(found, 1):
        when = datetime.datetime.fromtimestamp(entry["created"]).strftime("%Y-%m-%d %H:%M")
        note = f" ({entry['note']})" if entry["note"] else ""
        print(f"  {number}. {entry['device_name'] or entry['device']}  {when}  {entry['files']} files{note}")
    if listing:
        return 0
    if not 1 <= pick <= len(found):
        print(f"There is no backup number {pick}.")
        return 1
    if game["appid"] in db.open_sessions():
        print("The game is running - quit it first, or it will overwrite what is restored.")
        return 1
    status = Engine(cfg, db, Listener()).saves_job(db, "restore", game["appid"], found[pick - 1]["path"])
    if status["error"]:
        print(f"Not restored: {status['error']}")
        return 1
    done = status["done"][0]
    print(f"Restored {done['files']} files from backup {pick}."
          + (" Your previous saves were backed up first." if done["safety"] else ""))
    send_to_running("rescan")
    return 0


def cmd_reader_kit(target: str, out: str) -> int:
    """Makes the brief for writing a save reader for one game."""
    from .config import BACKUP_DIR, DATA_DIR, Config
    from .db import Database
    from . import readerkit

    db = Database()
    game = _one_game(db, target)
    if game is None:
        return 1
    try:
        kit = readerkit.build(db, Config.load(), BACKUP_DIR, game["appid"], Path(out).expanduser() if out else DATA_DIR / "reader-kits")
    except ValueError as e:
        print(e)
        return 1
    print(f"Reader kit for {game['name']}: {kit['files']} save files"
          + (f", plus {kit['history']} older sets to compare" if kit["history"] else ""))
    print(f"  folder: {kit['folder']}\n  zip:    {kit['zip']}")
    print("\nGive the zip to an AI assistant with: \"Read INSTRUCTIONS.md in the attached kit and do what "
          "it asks. Reply with the finished Python file.\"")
    print(f"Save its answer as {kit['reader_file']}, then:  achtrack reader-install {kit['reader_file']}")
    return 0


def _try_reader(db, reader, appid: int | None) -> int:
    """Runs a reader over the saves AchTrack knows, and says what it made of them."""
    from . import readers, savefiles

    rows = db.save_files(appid) if appid is not None else db.save_files()
    decoded = 0
    for row in rows:
        path = row["path"]
        if os.path.basename(path).lower() == "user.reg" or not os.path.isfile(path):
            continue
        data = Path(path).read_bytes()
        if not reader.can_read(os.path.basename(path), data):
            continue
        try:
            values = reader.read(os.path.basename(path), data)
            flat = savefiles.flatten(values)
        except readers.ReaderProblem as e:
            print(f"  could not read {os.path.basename(path)}: {e}")
            continue
        decoded += 1
        print(f"  {os.path.basename(path)}: {len(flat):,} values, for example:")
        for key in list(flat)[:6]:
            print(f"      {key} = {str(flat[key])[:60]}")
        try:
            rulings = reader.rules(values)
        except readers.ReaderProblem as e:
            print(f"      its achievements() failed: {e}")
            continue
        if rulings is None:
            continue
        game_of = row["appid"]
        titles = {a["name"].lower(): a["display_name"] or a["name"] for a in db.achievements(game_of)}
        earned = [n for n, v in rulings.items() if v is True or (isinstance(v, tuple) and v[1] > 0 and v[0] >= v[1])]
        unknown = [n for n in rulings if n.lower() not in titles]
        print(f"      it rules on {len(rulings)} achievements; earned in this file: "
              + (", ".join(titles.get(n.lower(), n) for n in earned) or "none"))
        if unknown:
            print(f"      WARNING: these are not achievements of this game, so they will be ignored: {', '.join(unknown[:8])}")
    return decoded


def cmd_reader_test(file: str, target: str | None) -> int:
    """Checks a save reader and tries it, without installing it."""
    from .db import Database
    from . import readers

    db = Database()
    appid = None
    if target:
        game = _one_game(db, target)
        if game is None:
            return 1
        appid = game["appid"]
    try:
        reader = readers.Reader(Path(file).expanduser())
    except (readers.ReaderProblem, OSError, UnicodeError) as e:
        print(f"This reader is not acceptable:\n  {str(e).replace('; ', chr(10) + '  ')}")
        return 1
    print(f"The reader passes the checks. It calls itself \"{reader.name}\".")
    if not reader.has_rules:
        print("It has no achievements() function, so it can only suggest achievements, never unlock them.")
    decoded = _try_reader(db, reader, appid)
    print(f"It decoded {decoded} save file(s)." if decoded else
          "It didn't recognise any save file AchTrack knows about - can_read() returned False for all of them.")
    return 0 if decoded else 1


def cmd_reader_install(file: str) -> int:
    """Checks a save reader, installs it and re-reads the saves it understands."""
    from .app import send_to_running
    from .db import Database
    from . import readers

    bundled = readers.BUILTIN_DIR / f"{Path(file).stem}.py"
    if not Path(file).expanduser().exists() and bundled.is_file():
        file = str(bundled)                    # one that comes with AchTrack, asked for by name
    try:
        dest = readers.install(file)
    except readers.ReaderProblem as e:
        print(f"Not installed - this reader is not acceptable:\n  {str(e).replace(chr(10), chr(10) + '  ')}")
        print("Give that message to whoever wrote it and ask for a corrected file.")
        return 1
    reader = readers.Reader(dest)
    print(f"Installed \"{reader.name}\" as {dest}")
    decoded = _try_reader(Database(), reader, None)
    if not decoded:
        print("It is installed, but it didn't recognise any save AchTrack knows about yet.")
    if send_to_running("readers"):
        print("AchTrack is re-reading those saves now. Anything they already show appears in the game's review.")
    else:
        print("Start AchTrack and it will use the reader.")
    return 0


def cmd_readers() -> int:
    from . import readers

    found = readers.installed()
    print(f"Installed save readers ({readers.READERS_DIR}):")
    for reader in found:
        print(f"  {reader.name}  ({reader.path.name})")
    if not found:
        print("  none")
    offered = readers.available()
    if offered:
        print("Come with AchTrack, not installed:")
        for reader in offered:
            print(f"  {reader.name}  ->  achtrack reader-install {reader.path.stem}")
    return 0


def cmd_share(name: str, publish: bool, out: str) -> int:
    """Builds the page you send to friends."""
    from .config import Config
    from .db import Database
    from .engine import Engine, Listener

    cfg = Config.load()
    engine = Engine(cfg, Database(), Listener())
    status = engine.build_share(name, publish, out)
    if status["page"]:
        size = Path(status["page"]).stat().st_size / 1024
        print(f"Page: {status['page']} ({size:.0f} KB) - send it as a file, it opens on its own.")
    if status["url"]:
        print(f"Link: {status['url']}")
    if status["error"]:
        print(status["error"])
        return 1
    if not publish:
        print("Add --publish to put it online and get a link.")
    return 0


def cmd_describe(target: str | None, force: bool, trophy_list: str, page: str, forget: bool) -> int:
    """Fills in hidden achievement descriptions, which Steam never reveals."""
    from .config import Config
    from .db import Database
    from .engine import Engine, Listener

    cfg = Config.load()
    db = Database()
    engine = Engine(cfg, db, Listener())
    every = db.q("SELECT appid, name FROM games WHERE appid > 0 ORDER BY name")
    games = every if forget else [r for r in every if db.blank_descriptions(r["appid"])]
    if target:
        wanted = target.strip().lower()
        games = [g for g in games if wanted in (g["name"] or "").lower() or str(g["appid"]) == wanted]
        if not games:
            print(f"No game with blank hidden descriptions matches {target!r}.")
            return 1
    if not games:
        print("Every achievement already has a description.")
        return 0

    if (trophy_list or page or forget) and len(games) > 1:
        print(f"That matches {len(games)} games - name one of them: "
              + ", ".join((g["name"] or str(g["appid"]))[:30] for g in games[:6]))
        return 1
    if forget:
        if not target:
            print("Name the game to forget descriptions for.")
            return 1
        for game in games:
            print(f"{game['name']}: forgot {engine.forget_descriptions(game['appid'])} looked-up descriptions")
        return 0

    total = 0
    for game in games:
        blanks = db.blank_descriptions(game["appid"])
        print(f"\n{game['name'] or game['appid']} - {blanks} without a description")
        status = engine.describe_hidden(game["appid"], note=lambda t: print(f"  {t}"), force=force,
                                        trophy_list=trophy_list, page_url=page, find_page=True)
        if status["error"]:
            print(f"  {status['error']}")
        elif status["filled"]:
            print(f"  filled {status['filled']} from \"{status['title']}\" ({status['source']})")
            total += status["filled"]
        else:
            print("  nothing to fill")
    print(f"\n{total} description{'s' if total != 1 else ''} filled in.")
    return 0


def cmd_saves(appid: int | None, file: str | None, fields: bool) -> int:
    """Shows which save files AchTrack reads and what it matches in them."""
    from .db import Database
    from .matcher import best_per_achievement, build_models, evaluate
    from .savefiles import Unreadable, load_save

    db = Database()
    if file:
        if not appid:
            print("Pass --appid with --file so the save can be matched against that game's achievements.")
            return 1
        targets = [(file, appid)]
    else:
        rows = db.save_files(appid) if appid else db.save_files()
        targets = [(r["path"], r["appid"]) for r in rows]
        if not targets:
            print("No save files found yet. AchTrack registers saves the game writes while you play,")
            print("so play for a bit (and save) with AchTrack running, then try again.")
            return 0
    for path, game_id in targets:
        game = db.game(game_id)
        print(f"\n== {(game['name'] if game is not None else None) or game_id} ({game_id})\n   {path}")
        try:
            fmt, flat = load_save(path)
        except Unreadable as e:
            print(f"   unreadable: {e}")
            continue
        except OSError as e:
            print(f"   can't open: {e}")
            continue
        print(f"   format: {fmt}, {len(flat)} fields")
        if fields:
            for key, value in list(flat.items())[:400]:
                print(f"     {key} = {value!r}"[:160])
        models = build_models(db.achievements(game_id))
        if not models:
            print("   no achievement list for this game yet (needs the Steam API key)")
            continue
        best = best_per_achievement(evaluate(models, None, flat, db.rejections(game_id)))
        if not best:
            print("   no field in this save matches an achievement description")
        names = {r["name"]: r.get("display_name") or r["name"] for r in db.achievements(game_id)}
        for m in sorted(best.values(), key=lambda m: (-m.score, m.name)):
            state = "EARNED " if m.satisfied else "not yet"
            prog = f"  {m.progress[0]}/{m.progress[1]}" if m.progress else ""
            print(f"   {state} {m.confidence:6} {m.score:.2f}  {names.get(m.name, m.name)}  <-  {m.reason}{prog}"[:200])
    return 0


def cmd_sync_steam() -> int:
    from .config import Config
    from .db import Database
    from .engine import Engine, Listener

    cfg = Config.load()
    db = Database()
    engine = Engine(cfg, db, Listener())
    status = engine.steam_sync(db, report=print)
    if status["error"]:
        print(f"Steam sync failed: {status['error']}")
        return 1
    print(f"Steam account {status['steam_id']}: {status['games']} played games, "
          f"{status['achievements']} achievements ({status['new_achievements']} new).")
    return 0


def main(argv=None) -> int:
    # A Windows console may not be able to print every character in a game's name; say "?" for
    # those rather than stopping. (A windowed app has no console at all, hence the None check.)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog=APP_ID, description="Steam achievements, popups and playtime for games on Steam emulators.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--background", action="store_true", help="start hidden in the tray (for autostart)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log to the terminal too")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("scan", help="show detected prefixes, achievement files and running games")
    sub.add_parser("list", help="print the library with playtime and achievement counts")
    sub.add_parser("sync-steam", help="copy your Steam games, playtime and achievements into the library now")
    sub.add_parser("sync-psn", help="copy your PlayStation games, play time and trophies into the library now")
    sub.add_parser("sync-devices", help="merge libraries with your other machines through the shared folder")
    sub.add_parser("devices", help="list the machines sharing the sync folder")
    backup = sub.add_parser("backup", help="save a database snapshot and a JSON export")
    backup.add_argument("--restore", metavar="FILE", help="put a snapshot back instead")
    export = sub.add_parser("export", help="build the phone page from your library")
    export.add_argument("--deploy", action="store_true", help="also publish it to Vercel")
    kit = sub.add_parser("reader-kit", help="make the brief for writing a save reader for a game")
    kit.add_argument("game", help="appid or part of the name")
    kit.add_argument("--out", default="", metavar="DIR", help="where to put it")
    rtest = sub.add_parser("reader-test", help="check a save reader and try it, without installing")
    rtest.add_argument("file")
    rtest.add_argument("game", nargs="?", help="only try it on this game's saves")
    rinstall = sub.add_parser("reader-install", help="check a save reader and install it")
    rinstall.add_argument("file")
    sub.add_parser("readers", help="list installed save readers")
    sub.add_parser("update-check", help="say whether updating from inside the app can work here, and why not")
    sbackup = sub.add_parser("saves-backup", help="pack a game's save files into the shared folder")
    sbackup.add_argument("game", nargs="?", help="appid or part of the name; default every game")
    srestore = sub.add_parser("saves-restore", help="put a backup of a game's saves in place here")
    srestore.add_argument("game", help="appid or part of the name")
    srestore.add_argument("--pick", type=int, default=1, help="which backup, by its number (default the newest)")
    srestore.add_argument("--list", action="store_true", help="only show the backups there are")
    sub.add_parser("heroic", help="add Heroic's installed games, using Steam's achievement lists")
    sharing = sub.add_parser("share", help="build the statistics page you send to friends")
    sharing.add_argument("--name", default="", help="the name shown on the page")
    sharing.add_argument("--publish", action="store_true", help="put it online and print the link")
    sharing.add_argument("--out", default="", metavar="DIR", help="write it somewhere else")
    describe = sub.add_parser("describe", help="fill in hidden achievement descriptions from PlayStation trophy lists")
    describe.add_argument("game", nargs="?", help="appid or part of a name; default every game with blanks")
    describe.add_argument("--force", action="store_true", help="search again for games that found nothing before")
    describe.add_argument("--trophy-list", default="", metavar="NPWR12345_00",
                          help="use this PlayStation trophy list for the game (an id or a link containing one)")
    describe.add_argument("--from-page", default="", metavar="URL|NAME", dest="page",
                          help="read the descriptions off a page listing this game's achievements - "
                               "an address, or a name to look one up by")
    describe.add_argument("--forget", action="store_true",
                          help="drop the descriptions a lookup filled in for this game")
    saves = sub.add_parser("saves", help="show the save files AchTrack reads and what it matches in them")
    saves.add_argument("appid", nargs="?", type=int, help="only this game")
    saves.add_argument("--file", help="test any save file (with --appid)")
    saves.add_argument("--appid", dest="appid_opt", type=int, help="game to match --file against")
    saves.add_argument("--fields", action="store_true", help="also print the fields read from each save")
    test = sub.add_parser("test-popup", help="show a sample achievement popup")
    test.add_argument("--rare", action="store_true", help="show the rare (gold) variant")
    playing = sub.add_parser("not-playing", help="end a stuck \"playing now\" session")
    playing.add_argument("game", help="appid or part of the name")
    sub.add_parser("quit", help="stop the running instance")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    if args.command == "update-check":
        from . import updater
        folder = updater.source_dir()
        print(f"AchTrack {__version__}")
        print(f"Installed from: {folder or 'not found'}")
        try:
            print(f"  recorded in {updater.SOURCE_NOTE}: {updater.SOURCE_NOTE.read_bytes()[:300]!r}")
        except OSError as e:
            print(f"  no record at {updater.SOURCE_NOTE} ({e.__class__.__name__})")
        print(f"git: {updater.git_exe() or 'not found'}")
        if folder:
            print(f"  a git clone: {'yes' if (folder / '.git').exists() else 'no'}")
        print(f"Verdict: {updater.problem(folder) or 'updating from inside the app can work'}")
        return 0
    if args.command == "scan":
        return cmd_scan()
    if args.command == "list":
        return cmd_list()
    if args.command == "sync-steam":
        return cmd_sync_steam()
    if args.command == "sync-psn":
        return cmd_psn_sync()
    if args.command == "sync-devices":
        return cmd_device_sync()
    if args.command == "devices":
        return cmd_devices()
    if args.command == "backup":
        return cmd_backup(args.restore)
    if args.command == "export":
        return cmd_export(args.deploy)
    if args.command == "reader-kit":
        return cmd_reader_kit(args.game, args.out)
    if args.command == "reader-test":
        return cmd_reader_test(args.file, args.game)
    if args.command == "reader-install":
        return cmd_reader_install(args.file)
    if args.command == "readers":
        return cmd_readers()
    if args.command == "saves-backup":
        return cmd_saves_backup(args.game)
    if args.command == "saves-restore":
        return cmd_saves_restore(args.game, args.pick, args.list)
    if args.command == "heroic":
        return cmd_heroic()
    if args.command == "share":
        return cmd_share(args.name, args.publish, args.out)
    if args.command == "describe":
        return cmd_describe(args.game, args.force, args.trophy_list, args.page, args.forget)
    if args.command == "saves":
        return cmd_saves(args.appid or args.appid_opt, args.file, args.fields)

    from .app import run_gui, send_to_running, show_test_popup, make_app
    if args.command == "test-popup":
        return show_test_popup(args.rare)
    if args.command == "not-playing":
        return cmd_not_playing(args.game)
    if args.command == "quit":
        make_app()
        return 0 if send_to_running("quit") else 1
    return run_gui(background=args.background)


if __name__ == "__main__":
    sys.exit(main())
