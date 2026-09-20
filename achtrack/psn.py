"""PlayStation Network: your own trophies and play time.

Sony publishes no API for this, so AchTrack talks to the endpoints the PlayStation mobile app uses,
authenticated with an NPSSO token you copy from a signed-in browser (the way community trophy
trackers do it). Everything here is read-only, and the token stays in your local config.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

AUTH = "https://ca.account.sony.com/api/authz/v3/oauth"
API = "https://m.np.playstation.com/api"
CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
CLIENT_AUTH = "Basic MDk1MTUxNTktNzIzNy00MzcwLTliNDAtMzgwNmU2N2MwODkxOnVjUGprYTV0bnRCMktxc1A="
REDIRECT = "com.scee.psxandroid.scecompcall://redirect"
SCOPE = "psn:mobile.v2.core psn:clientapp"
UA = f"AchTrack/{__version__}"
NPSSO_HELP = ("Sign in at playstation.com, then open https://ca.account.sony.com/api/v1/ssocookie "
              "and copy the 64-character npsso value")
GAME_CATEGORIES = "ps4_game,ps5_native_game"
TROPHY_KINDS = ("bronze", "silver", "gold", "platinum")


class PSNError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _decode(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError as e:
        raise PSNError("PlayStation returned something unreadable") from e


def _call(url: str, headers: dict, data: bytes | None = None, timeout: float = 25):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _decode(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:400].decode("utf-8", "replace")
        if e.code in (401, 403):
            raise PSNError(f"PlayStation refused the request ({e.code}) - your login may have expired") from e
        raise PSNError(f"PlayStation error {e.code}: {body}") from e
    except (urllib.error.URLError, OSError) as e:
        raise PSNError(f"network error: {e}") from e


# --- login -------------------------------------------------------------------------------------

def exchange_npsso(npsso: str) -> dict:
    """Turns the NPSSO token from your browser into access/refresh tokens."""
    npsso = npsso.strip().strip('"')
    if not re.fullmatch(r"[A-Za-z0-9_.-]{40,120}", npsso):
        raise PSNError(f"that doesn't look like an NPSSO token. {NPSSO_HELP}")
    query = urllib.parse.urlencode({"access_type": "offline", "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
                                    "response_type": "code", "scope": SCOPE})
    req = urllib.request.Request(f"{AUTH}/authorize?{query}", headers={"User-Agent": UA, "Cookie": f"npsso={npsso}"})
    opener = urllib.request.build_opener(_NoRedirect)
    location = ""
    try:
        with opener.open(req, timeout=25) as resp:
            location = resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location", "")
    except (urllib.error.URLError, OSError) as e:
        raise PSNError(f"network error: {e}") from e
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [""])[0]
    if not code:
        raise PSNError(f"PlayStation didn't accept that NPSSO token - it may have expired. {NPSSO_HELP}")
    return _token_request({"code": code, "redirect_uri": REDIRECT, "grant_type": "authorization_code",
                           "token_format": "jwt"})


def refresh_tokens(refresh_token: str) -> dict:
    return _token_request({"refresh_token": refresh_token, "grant_type": "refresh_token", "scope": SCOPE,
                           "token_format": "jwt"})


def _token_request(fields: dict) -> dict:
    data = _call(f"{AUTH}/token", {"Authorization": CLIENT_AUTH,
                                   "Content-Type": "application/x-www-form-urlencoded"},
                 urllib.parse.urlencode(fields).encode())
    if not data.get("access_token"):
        raise PSNError("PlayStation didn't return a login token")
    now = int(time.time())
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": now + int(data.get("expires_in", 3600)) - 60,
        "refresh_expires_at": now + int(data.get("refresh_token_expires_in", 60 * 86400)) - 3600,
    }


def valid_token(tokens: dict | None, npsso: str) -> dict:
    """Returns usable tokens, refreshing or re-logging in as needed."""
    now = int(time.time())
    if tokens and tokens.get("access_token") and tokens.get("expires_at", 0) > now:
        return tokens
    if tokens and tokens.get("refresh_token") and tokens.get("refresh_expires_at", 0) > now:
        try:
            return refresh_tokens(tokens["refresh_token"])
        except PSNError:
            pass
    return exchange_npsso(npsso)


def _get(path: str, token: str, params: dict | None = None):
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    return _call(f"{API}/{path}{query}", {"Authorization": f"Bearer {token}"})


# --- trophies and play time ---------------------------------------------------------------------

def trophy_titles(token: str, limit: int = 800) -> list:
    """Every game on the account that has trophies, with how many are earned."""
    out, offset = [], 0
    while offset < limit:
        page = _get("trophy/v1/users/me/trophyTitles", token, {"limit": 100, "offset": offset})
        titles = page.get("trophyTitles") or []
        out += titles
        offset += 100
        if len(titles) < 100 or offset >= int(page.get("totalItemCount") or 0):
            break
    return out


def title_trophies(token: str, np_comm_id: str, service: str) -> list:
    """Definitions (name, description, icon, type, hidden) merged with what you earned and rarity."""
    params = {"npServiceName": service}
    defs = _get(f"trophy/v1/npCommunicationIds/{np_comm_id}/trophyGroups/all/trophies", token, params)
    earned = _get(f"trophy/v1/users/me/npCommunicationIds/{np_comm_id}/trophyGroups/all/trophies", token, params)
    by_id = {t.get("trophyId"): t for t in earned.get("trophies") or []}
    out = []
    for t in defs.get("trophies") or []:
        mine = by_id.get(t.get("trophyId")) or {}
        rate = mine.get("trophyEarnedRate")
        out.append({
            "id": t.get("trophyId"),
            "name": t.get("trophyName") or "",
            "detail": t.get("trophyDetail") or "",
            "icon": t.get("trophyIconUrl") or "",
            "kind": (t.get("trophyType") or "").lower(),
            "hidden": bool(t.get("trophyHidden")),
            "earned": bool(mine.get("earned")),
            "earned_at": _timestamp(mine.get("earnedDateTime")),
            "rate": float(rate) if rate not in (None, "") else None,
        })
    return out


def play_times(token: str, limit: int = 500) -> list:
    """Play time per game from PlayStation's own game list."""
    out, offset = [], 0
    while offset < limit:
        page = _get("gamelist/v2/users/me/titles", token,
                    {"categories": GAME_CATEGORIES, "limit": 200, "offset": offset})
        titles = page.get("titles") or []
        for t in titles:
            out.append({
                "title_id": t.get("titleId") or "",
                "name": t.get("localizedName") or t.get("name") or "",
                "image": t.get("localizedImageUrl") or t.get("imageUrl") or "",
                "seconds": parse_duration(t.get("playDuration") or ""),
                "last_played": _timestamp(t.get("lastPlayedDateTime")),
                "category": t.get("category") or "",
            })
        offset += 200
        if len(titles) < 200 or offset >= int(page.get("totalItemCount") or 0):
            break
    return out


DURATION_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?")


def parse_duration(value: str) -> int:
    """ISO-8601 duration ("PT12H34M56S") to seconds."""
    m = DURATION_RE.fullmatch(value.strip()) if value else None
    if not m:
        return 0
    days, hours, minutes, seconds = (float(g or 0) for g in m.groups())
    return int(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def _timestamp(value: str | None) -> int | None:
    """PlayStation's "2026-08-18T14:57:31Z" to a unix timestamp."""
    if not value:
        return None
    try:
        from datetime import datetime
        return int(datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z").timestamp())
    except (ValueError, TypeError):
        return None


def normalize_name(name: str) -> str:
    """For matching a trophy set to a game in the play-time list."""
    name = re.sub(r"[™®©]", "", name or "").lower()
    name = re.sub(r"\b(ps4|ps5|playstation ?[45]|standard|digital|deluxe|definitive|remastered|edition)\b", " ", name)
    return re.sub(r"[^a-z0-9]+", "", name)
