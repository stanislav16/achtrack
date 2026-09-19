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
        print(f"{(r['name'] or 'App ' + str(r['appid'])):<{width}}  {r['appid']:>8}  {fmt_hours(r['playtime']):>10}  {ach:>8} achievements")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog=APP_ID, description="Steam achievements, popups and playtime for games on Steam emulators.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--background", action="store_true", help="start hidden in the tray (for autostart)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log to the terminal too")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("scan", help="show detected prefixes, achievement files and running games")
    sub.add_parser("list", help="print the library with playtime and achievement counts")
    test = sub.add_parser("test-popup", help="show a sample achievement popup")
    test.add_argument("--rare", action="store_true", help="show the rare (gold) variant")
    sub.add_parser("quit", help="stop the running instance")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    if args.command == "scan":
        return cmd_scan()
    if args.command == "list":
        return cmd_list()

    from .app import run_gui, send_to_running, show_test_popup, make_app
    if args.command == "test-popup":
        return show_test_popup(args.rare)
    if args.command == "quit":
        make_app()
        return 0 if send_to_running("quit") else 1
    return run_gui(background=args.background)


if __name__ == "__main__":
    sys.exit(main())
