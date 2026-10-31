"""What the home page shows: where the library stands, and what happened lately."""
from __future__ import annotations

from . import share
from .db import Database

ALMOST_FROM = 0.5       # a game counts as "almost there" from this far along
MIN_ACHIEVEMENTS = 5    # one trophy of two isn't a game you're close to finishing


def collect(db: Database, cards: int = 6, rows: int = 7) -> dict:
    games = db.games(include_hidden=False)

    def game(row) -> dict:
        return {"appid": row["appid"], "name": row["name"] or f"App {row['appid']}", "art": row["header_url"] or "",
                "last": row["last_any"] or 0, "unlocked": min(row["unlocked"], row["total"] or row["unlocked"]),
                "total": row["total"]}

    continuing = [game(r) for r in games if r["last_any"]][:cards]
    close = [r for r in games if r["total"] >= MIN_ACHIEVEMENTS and r["unlocked"] < r["total"]
             and r["unlocked"] / r["total"] >= ALMOST_FROM]
    close.sort(key=lambda r: (r["total"] - r["unlocked"], -r["unlocked"] / r["total"]))

    earned = """SELECT a.appid, a.name, a.display_name, a.icon, a.global_pct, a.kind, u.unlocked_at, g.name AS game
                FROM unlocks u
                JOIN achievements a ON a.appid = u.appid AND a.name = u.name
                JOIN games g ON g.appid = a.appid
                WHERE g.hidden = 0"""

    def trophies(order: str, limit: int) -> list:
        return [{"appid": r["appid"], "name": r["display_name"] or r["name"], "icon": r["icon"] or "",
                 "pct": r["global_pct"], "game": r["game"] or "", "at": r["unlocked_at"] or 0, "kind": r["kind"] or ""}
                for r in db.q(f"{earned} AND {order} LIMIT {int(limit)}")]

    return {
        "stats": share.collect(db)["stats"],
        "continue": continuing,
        "recent": trophies("u.unlocked_at > 0 ORDER BY u.unlocked_at DESC", rows),
        "almost": [game(r) for r in close[:5]],
        "rare": trophies("a.global_pct IS NOT NULL ORDER BY a.global_pct ASC", 5),
    }
