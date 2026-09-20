"""Filling in hidden achievement descriptions from the PlayStation trophy list.

Steam blanks out the description of a hidden achievement and never shows it, not even on the
public achievement page. PlayStation spells every trophy out. For a game that exists on both -
most of them - the two lists are the same list under different names, so the trophy list can
describe Steam's blanks.

Nothing here trusts the match blindly: a candidate list is only used once its trophy names line
up with the achievement names we can already see.
"""
from __future__ import annotations

import difflib
import gzip
import html
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, psn

log = logging.getLogger(__name__)

MIN_VISIBLE = 4        # fewer than this and there's nothing to check a candidate against
MIN_AGREEMENT = 0.6    # share of visible achievements the trophy list must also have
MAX_TITLES = 3         # store entries to try per game
MAX_SETS = 2           # trophy lists to try per store entry

# Reading a page you point AchTrack at
UA = f"AchTrack/{__version__}"
MAX_PAGE = 4 * 1024 * 1024
LOOKAHEAD = 4          # text pieces after a name to consider its description
SAME_TEXT = 0.75       # how alike two descriptions of the same achievement must read
MIN_CONFIRMED = 0.6    # share of checkable descriptions that must agree
MIN_NAMES = 0.6        # or, failing that, share of the game's names the page must list
NOT_DESCRIPTIONS = {"bronze", "silver", "gold", "platinum", "common", "uncommon", "rare", "very rare",
                    "ultra rare", "hidden", "secret", "locked", "unlocked", "trophy", "achievement",
                    "dlc", "base game", "story", "missable", "online", "offline"}


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


# --- reading a page you found yourself ------------------------------------------------------------
# Sony only hands out a trophy list id for games your own account has played, and the trophy sites
# don't publish those ids. But they do publish the descriptions, so you can point AchTrack at the
# page instead. Nothing on the page is followed or executed - it is read as text, and a description
# is only kept once the page has proved it is listing this game.

def fetch_page(url: str, timeout: float = 25) -> str:
    """The text of a page, as text. Raises OSError with something readable if it can't be had."""
    if not re.match(r"https?://", url.strip(), re.I):
        raise OSError("that doesn't look like a web address")
    request = urllib.request.Request(url.strip(), headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en;q=0.9",
        "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_PAGE)
            if (response.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        blocked = " - the site is refusing to be read automatically" if e.code in (403, 429) else ""
        raise OSError(f"the page returned {e.code}{blocked}") from e
    except urllib.error.URLError as e:
        raise OSError(f"couldn't reach the page: {e.reason}") from e
    return raw.decode(charset, errors="replace")


def text_pieces(page: str) -> list:
    """Every run of visible text on the page, in order, with the markup taken out."""
    body = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>", " ", page)
    body = re.sub(r"(?s)<!--.*?-->", " ", body)
    out = []
    for piece in re.split(r"(?s)<[^>]*>", body):
        text = html.unescape(piece)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append(text)
    return out


def _is_description(text: str, names: set) -> bool:
    if not 3 <= len(text) <= 400 or key(text) in names:
        return False
    if text.lower().strip(" .") in NOT_DESCRIPTIONS:
        return False
    return bool(re.search(r"[a-z]{3}", text, re.I)) and not re.fullmatch(r"[\d\s.,%/()x+-]+", text)


def read_page(page: str, achievements: list) -> dict:
    """Pairs each achievement name found on the page with the text that follows it."""
    names = {key(a["display_name"] or a["name"]) for a in achievements}
    pieces = text_pieces(page)
    found: dict = {}
    for index, piece in enumerate(pieces):
        name = key(piece)
        if name not in names or name in found:
            continue
        for following in pieces[index + 1:index + 1 + LOOKAHEAD]:
            if _is_description(following, names):
                found[name] = following
                break
    return found


def _alike(one: str, two: str) -> float:
    return difflib.SequenceMatcher(None, key(one), key(two)).ratio()


def from_page(page: str, achievements: list) -> tuple:
    """(descriptions to fill in, how sure we are, why) for a page you pointed AchTrack at."""
    on_page = read_page(page, achievements)
    if not on_page:
        return {}, 0.0, "none of this game's achievements are named on that page"

    # Achievements we can already read are the control: if the page's text for those matches what
    # Steam says, the page is this game's list and its other descriptions can be trusted too.
    checkable = [a for a in achievements if (a["description"] or "").strip()
                 and key(a["display_name"] or a["name"]) in on_page]
    agreed = [a for a in checkable
              if _alike(a["description"], on_page[key(a["display_name"] or a["name"])]) >= SAME_TEXT]
    confirmed = len(agreed) / len(checkable) if checkable else 0.0
    listed = len(on_page) / len(achievements) if achievements else 0.0
    if len(checkable) >= MIN_VISIBLE:
        if confirmed < MIN_CONFIRMED:
            return {}, confirmed, (f"the page describes {len(checkable)} achievements differently from Steam "
                                   f"({len(agreed)} of {len(checkable)} agree) - it looks like another game "
                                   f"or another version")
        why = f"{len(agreed)} of {len(checkable)} known descriptions match"
    elif listed < MIN_NAMES:
        return {}, listed, (f"only {len(on_page)} of {len(achievements)} achievement names appear on that "
                            f"page, and too few known descriptions to check it against")
    else:
        confirmed = listed
        why = f"{len(on_page)} of {len(achievements)} achievement names appear on it"

    filled = {}
    for a in achievements:
        if (a["description"] or "").strip():
            continue
        text = on_page.get(key(a["display_name"] or a["name"]))
        if text:
            filled[a["name"]] = text
    return filled, confirmed, why


# Finding the page yourself is the tedious part, so AchTrack can look for one. This is only used
# when you ask it to - the automatic pass stays on PlayStation's own API.
PAGE_SEARCH = "https://psn100.net/game?search={}"
PAGE_LINK = re.compile(r'href="(/game/\d+-[^"]+)"[^>]*>\s*([^<]{2,80})')


def find_pages(name: str, limit: int = 4) -> list:
    """Pages that might list this game's trophies, closest name first: [(url, title)]."""
    site = PAGE_SEARCH.split("/game")[0]
    try:
        page = fetch_page(PAGE_SEARCH.format(urllib.parse.quote(name)))
    except OSError as e:
        log.info("page search for %s failed: %s", name, e)
        return []
    seen: dict = {}
    for href, label in PAGE_LINK.findall(page):
        label = re.sub(r"\s+", " ", html.unescape(label)).strip()
        if label and href not in seen:
            seen[href] = label
    ranked = sorted(seen.items(),
                    key=lambda kv: -difflib.SequenceMatcher(None, key(name), key(kv[1])).ratio())
    return [(site + href, label) for href, label in ranked[:limit]]
