"""Filling in hidden achievement descriptions from the PlayStation trophy list.

Steam blanks out the description of a hidden achievement and never shows it, not even on the
public achievement page. PlayStation spells every trophy out. For a game that exists on both -
most of them - the two lists are the same list under different names, so the trophy list can
describe Steam's blanks.

Nothing here trusts the match blindly: a candidate list is only used once its trophy names line
up with the achievement names we can already see.
"""
from __future__ import annotations

import logging
import re

from . import psn

log = logging.getLogger(__name__)

MIN_VISIBLE = 4        # fewer than this and there's nothing to check a candidate against
MIN_AGREEMENT = 0.6    # share of visible achievements the trophy list must also have
MAX_TITLES = 3         # store entries to try per game
MAX_SETS = 2           # trophy lists to try per store entry


def key(name: str) -> str:
    """Achievement and trophy names match on their letters, not their punctuation."""
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"[™®©]", "", name or "").lower())


def _details(trophies: list) -> dict:
    """Trophy name -> description, minus any name that appears twice with different text."""
    out: dict = {}
    for t in trophies:
        k = key(t.get("name") or "")
        detail = (t.get("detail") or "").strip()
        if not k or not detail:
            continue
        if k in out and out[k] != detail:
            out[k] = ""            # ambiguous, don't use it
        else:
            out.setdefault(k, detail)
    return {k: v for k, v in out.items() if v}


def match(achievements: list, trophies: list) -> tuple:
    """(descriptions to fill in, agreement, visible achievements the trophy list also has)."""
    details = _details(trophies)
    visible = [a for a in achievements if (a["description"] or "").strip()]
    matched = [a for a in visible if key(a["display_name"] or a["name"]) in details]
    agreement = len(matched) / len(visible) if visible else 0.0
    if len(visible) < MIN_VISIBLE or agreement < MIN_AGREEMENT:
        return {}, agreement, len(matched)
    filled = {}
    for a in achievements:
        if (a["description"] or "").strip():
            continue
        detail = details.get(key(a["display_name"] or a["name"]))
        if detail:
            filled[a["name"]] = detail
    return filled, agreement, len(matched)


TROPHY_ID_RE = re.compile(r"\bNPWR\d{5}_\d{2}\b", re.I)


def trophy_id(text: str) -> str:
    """The NPWR id in a pasted id, link or note - "" if there isn't one."""
    match = TROPHY_ID_RE.search(text or "")
    return match.group(0).upper() if match else ""


def lookup(token: str, game_name: str, achievements: list, note=None, known: list | None = None,
           search: bool = True) -> tuple:
    """Finds the trophy list for a game and returns (descriptions, "NPWR…/trophy2", list name).

    `known` is tried first: trophy lists we already have a reason to believe in, such as the same
    game in your own PlayStation library. Otherwise the store is searched, which only turns up a
    trophy list for games your account has played - Sony publishes no lookup for the rest.

    `note` gets a line per step, so the CLI can show its working.
    """
    def say(text: str) -> None:
        log.info("%s: %s", game_name, text)
        if note:
            note(text)

    blanks = sum(1 for a in achievements if not (a["description"] or "").strip())
    if not blanks:
        return {}, "", ""
    best = ({}, 0.0, "", "")
    for comm_id in known or []:
        comm_id, _, service = comm_id.partition("/")
        try:
            trophies = psn.trophy_definitions(token, comm_id, service)
        except psn.PSNError as e:
            say(f"  {comm_id}: {e}")
            continue
        filled, agreement, matched = match(achievements, trophies)
        say(f"  {comm_id} - {len(trophies)} trophies, {matched} names in common ({agreement:.0%}), "
            f"{len(filled)} descriptions to fill")
        if filled and agreement > best[1]:
            best = (filled, agreement, f"{comm_id}/{service or 'trophy2'}", game_name)
    if best[1] >= 0.95 or not search:
        if not best[0]:
            say("that trophy list doesn't match this game" if known else "no trophy list matched this game")
        return best[0], best[2], best[3]

    title_ids = psn.search_titles(token, game_name, MAX_TITLES)
    say(f"PlayStation store search: {', '.join(title_ids) if title_ids else 'no results'}")
    for title_id in title_ids:
        try:
            sets = psn.trophy_sets(token, title_id)
        except psn.PSNError as e:
            say(f"  {title_id}: {e}")
            continue
        if not sets:
            say(f"  {title_id}: no trophy list")
        for tset in sets[:MAX_SETS]:
            try:
                trophies = psn.trophy_definitions(token, tset["id"], tset["service"])
            except psn.PSNError as e:
                say(f"  {tset['id']}: {e}")
                continue
            filled, agreement, matched = match(achievements, trophies)
            say(f"  {tset['id']} \"{tset['name']}\" - {len(trophies)} trophies, "
                f"{matched} names in common ({agreement:.0%}), {len(filled)} descriptions to fill")
            if filled and agreement > best[1]:
                best = (filled, agreement, f"{tset['id']}/{tset['service']}", tset["name"])
            if agreement >= 0.95:
                break
        if best[1] >= 0.95:
            break
    if not best[0]:
        say("no trophy list matched this game")
    return best[0], best[2], best[3]
