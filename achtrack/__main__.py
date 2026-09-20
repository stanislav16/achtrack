from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys

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
    print(f"Wine/Proton prefixes ({len(prefixes)}):")
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
    if ProcessMonitor.available():
        exe_map = {}
        for r in db.games(include_hidden=True):
            for exe in (r["exe_name"] or "").split(";"):
                if exe.strip():
                    exe_map[exe.strip().lower()] = r["appid"]
        running = ProcessMonitor().scan(exe_map)
        print(f"\nRunning games ({len(running)}):")
        for appid, game_dir in running.items():
            print(f"  app {appid}  {game_dir or ''}")
    return 0


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


def cmd_screen_test(seconds: float) -> int:
    """Runs the screen watcher's loop without a game attached and prints what it reads."""
    import os
    import time
    from .config import Config
    from .screenwatch import ScreenWatcher, available, probe_backends

    ok, reason = available()
    if not ok:
        print(f"Screen watcher unavailable: {reason}")
        return 1
    cfg = Config.load()
    print(f"Session type: {os.environ.get('XDG_SESSION_TYPE', 'unknown')}")
    print("Capture methods on this desktop:")
    for name, _ok, detail in probe_backends(cfg.screen_monitor):
        print(f"   {name:18} {detail}")
    print(f"\nWatching for {seconds:.0f} s - switch to your game now.\n")

    def show(kind: str, lines: list, took: float) -> None:
        stamp = time.strftime("%H:%M:%S")
        print(f"-- {stamp} {kind} read, OCR {took:.2f} s" + ("" if lines else "  (no text)"))
        for region, text in lines:
            print(f"   [{region:6}] {text}")

    watcher = ScreenWatcher(0, None, cfg.screen_capture_interval, cfg.screen_monitor, on_read=show)
    watcher.start()
    time.sleep(seconds)
    watcher.stop()
    watcher.join(10)
    st = watcher.stats
    if not st["captures"]:
        print(f"No captures: {watcher.status}")
        return 1
    print(f"\n{st['captures']} captures in {seconds:.0f} s ({st['captures'] / seconds:.1f} per second, "
          f"{st['grab_seconds'] / st['captures'] * 1000:.0f} ms each)")
    print(f"{st['ocr_runs']} OCR runs ({st['flash_runs']} triggered by text appearing), "
          f"{st['ocr_seconds']:.1f} s of OCR = {st['ocr_seconds'] / seconds * 100:.0f}% of one CPU core")
    if "black" in watcher.status:
        print(f"Warning: {watcher.status}")
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
    backup = sub.add_parser("backup", help="save a database snapshot and a JSON export")
    backup.add_argument("--restore", metavar="FILE", help="put a snapshot back instead")
    export = sub.add_parser("export", help="build the phone page from your library")
    export.add_argument("--deploy", action="store_true", help="also publish it to Vercel")
    screen = sub.add_parser("screen-test", help="read the screen like the watcher does and print what it sees")
    screen.add_argument("--seconds", type=float, default=20, help="how long to keep reading (default 20)")
    saves = sub.add_parser("saves", help="show the save files AchTrack reads and what it matches in them")
    saves.add_argument("appid", nargs="?", type=int, help="only this game")
    saves.add_argument("--file", help="test any save file (with --appid)")
    saves.add_argument("--appid", dest="appid_opt", type=int, help="game to match --file against")
    saves.add_argument("--fields", action="store_true", help="also print the fields read from each save")
    test = sub.add_parser("test-popup", help="show a sample achievement popup")
    test.add_argument("--rare", action="store_true", help="show the rare (gold) variant")
    sub.add_parser("quit", help="stop the running instance")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
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
    if args.command == "backup":
        return cmd_backup(args.restore)
    if args.command == "export":
        return cmd_export(args.deploy)
    if args.command == "screen-test":
        return cmd_screen_test(args.seconds)
    if args.command == "saves":
        return cmd_saves(args.appid or args.appid_opt, args.file, args.fields)

    from .app import run_gui, send_to_running, show_test_popup, make_app
    if args.command == "test-popup":
        return show_test_popup(args.rare)
    if args.command == "quit":
        make_app()
        return 0 if send_to_running("quit") else 1
    return run_gui(background=args.background)


if __name__ == "__main__":
    sys.exit(main())
