"""Official Steam achievement data: names, descriptions, icons and global unlock percentages."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from .config import ICON_DIR
from .emulators import read_text

UA = f"AchTrack/{__version__} (+linux)"
CDN = "https://cdn.cloudflare.steamstatic.com"
ICON_URL = CDN + "/steamcommunity/public/images/apps/{appid}/{file}"
HEADER_URL = CDN + "/steam/apps/{appid}/header.jpg"


class SteamError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _get(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and "api.steampowered.com" in url:
            raise SteamError("Steam refused the request - check the Web API key in Settings, and that your "
                             "Steam profile's Game details are Public", e.code) from e
        raise SteamError(f"HTTP {e.code} for {url.split('?')[0]}", e.code) from e
    except (urllib.error.URLError, OSError) as e:
        raise SteamError(f"network error: {e}") from e


def _json(url: str, timeout: float = 20):
    try:
        return json.loads(_get(url, timeout))
    except ValueError as e:
        raise SteamError("Steam returned invalid JSON") from e


def fetch_schema(appid: int, key: str, language: str = "english") -> tuple:
    """Returns (game_name, [achievement dicts]) from ISteamUserStats/GetSchemaForGame."""
    query = urllib.parse.urlencode({"key": key, "appid": appid, "l": language})
    data = _json(f"https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/?{query}")
    game = data.get("game") or {}
    raw = ((game.get("availableGameStats") or {}).get("achievements")) or []
    achievements = [
        {
            "name": a["name"],
            "display_name": a.get("displayName") or a["name"],
            "description": a.get("description") or "",
            "icon": a.get("icon") or "",
            "icon_gray": a.get("icongray") or "",
            "hidden": int(a.get("hidden") or 0),
        }
        for a in raw if a.get("name")
    ]
    name = game.get("gameName") or ""
    if name.startswith("ValveTestApp") or name.startswith("UntitledApp"):
        name = ""
    return name, achievements


def fetch_global_pct(appid: int) -> dict:
    data = _json(
        f"https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v0002/?gameid={appid}&format=json"
    )
    out = {}
    for a in (data.get("achievementpercentages") or {}).get("achievements") or []:
        try:
            out[a["name"]] = float(a["percent"])
        except (KeyError, TypeError, ValueError):
            pass
    return out


def fetch_app_info(appid: int) -> tuple:
    """(name, header image url) from the store. Delisted games may return nothing."""
    data = _json(f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic")
    entry = data.get(str(appid)) or {}
    if not entry.get("success"):
        return "", HEADER_URL.format(appid=appid)
    info = entry.get("data") or {}
    return info.get("name") or "", info.get("header_image") or HEADER_URL.format(appid=appid)


STEAMID64_BASE = 76561197960265728


def parse_steam_id(text: str, key: str = "") -> str | None:
    """SteamID64 from an id, a profile URL, or a custom (vanity) profile URL/name."""
    text = text.strip()
    if not text:
        return None
    m = re.search(r"(7656119\d{10})", text)
    if m:
        return m.group(1)
    m = re.search(r"steamcommunity\.com/id/([^/?#]+)", text)
    vanity = m.group(1) if m else (text if re.fullmatch(r"[\w-]{2,32}", text) else None)
    if not vanity or not key:
        return None
    query = urllib.parse.urlencode({"key": key, "vanityurl": vanity})
    resp = _json(f"https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/?{query}").get("response") or {}
    return resp.get("steamid") if resp.get("success") == 1 else None


def fetch_owned_games(key: str, steam_id: str) -> list:
    query = urllib.parse.urlencode({"key": key, "steamid": steam_id, "include_appinfo": 1,
                                    "include_played_free_games": 1, "format": "json"})
    resp = _json(f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/?{query}").get("response") or {}
    if "games" not in resp:
        if resp.get("game_count") == 0:
            return []
        raise SteamError("Steam didn't return your games - set your Steam profile's Game details to Public "
                         "(Steam → Profile → Edit Profile → Privacy Settings)")
    return resp["games"]


def fetch_player_achievements(key: str, steam_id: str, appid: int, language: str = "english") -> list:
    """[(api_name, unlock_time)] for achievements the player has on Steam."""
    query = urllib.parse.urlencode({"key": key, "steamid": steam_id, "appid": appid, "l": language})
    try:
        data = _json(f"https://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v1/?{query}")
    except SteamError as e:
        if e.status == 400:  # "Requested app has no stats"
            return []
        raise
    stats = data.get("playerstats") or {}
    return [(a["apiname"], int(a.get("unlocktime") or 0))
            for a in stats.get("achievements") or [] if a.get("achieved") and a.get("apiname")]


def fetch_community_achievements(appid: int, language: str = "english") -> list:
    """(icon file, title, description) from the public global-achievements page.

    That page usually shows descriptions the Web API hides for hidden achievements.
    """
    page = _get(f"https://steamcommunity.com/stats/{appid}/achievements/?l={urllib.parse.quote(language)}")
    text = page.decode("utf-8", errors="replace")
    out = []
    for chunk in text.split('class="achieveRow')[1:]:
        img = re.search(r'<img[^>]+src="([^"]+)"', chunk)
        title = re.search(r"<h3>(.*?)</h3>", chunk, re.S)
        desc = re.search(r"<h5>(.*?)</h5>", chunk, re.S)
        if not title:
            continue
        out.append((
            os.path.basename(urllib.parse.urlparse(img.group(1)).path) if img else "",
            html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip(),
            html.unescape(re.sub(r"<[^>]+>", "", desc.group(1))).strip() if desc else "",
        ))
    return out


def fill_hidden_descriptions(appid: int, achievements: list, language: str = "english") -> int:
    """Fills blank descriptions of hidden achievements in place. Returns how many were filled."""
    missing = [a for a in achievements if a.get("hidden") and not a.get("description")]
    if not missing:
        return 0
    try:
        rows = fetch_community_achievements(appid, language)
    except SteamError:
        return 0
    by_icon = {icon: desc for icon, _title, desc in rows if icon and desc}
    by_title = {title.lower(): desc for _icon, title, desc in rows if desc}
    filled = 0
    for a in missing:
        icon = os.path.basename(urllib.parse.urlparse(resolve_icon(appid, a.get("icon") or "")).path)
        desc = by_icon.get(icon) or by_title.get((a.get("display_name") or "").lower())
        if desc:
            a["description"] = desc
            filled += 1
    return filled


def _localized(value, language: str) -> str:
    if isinstance(value, dict):
        return value.get(language) or value.get("english") or next(iter(value.values()), "") or ""
    return str(value or "")


def load_local_schema(path: Path, language: str = "english") -> list:
    """Goldberg's generated steam_settings/achievements.json."""
    data = json.loads(read_text(path))
    if not isinstance(data, list):
        return []
    base = path.parent
    out = []
    for a in data:
        if not isinstance(a, dict) or not a.get("name"):
            continue

        def icon(value):
            value = str(value or "")
            if not value or value.startswith("http"):
                return value
            local = base / value
            return str(local) if local.is_file() else value

        out.append({
            "name": a["name"],
            "display_name": _localized(a.get("displayName") or a.get("display_name"), language) or a["name"],
            "description": _localized(a.get("description"), language),
            "icon": icon(a.get("icon")),
            "icon_gray": icon(a.get("icon_gray") or a.get("icongray")),
            "hidden": int(str(a.get("hidden") or 0).strip() in ("1", "true", "True")),
        })
    return out


def resolve_icon(appid: int, value: str) -> str:
    """Absolute URL or local path for an icon reference (full URL, CDN hash, or file)."""
    if not value:
        return ""
    if value.startswith(("http://", "https://")) or os.path.isabs(value):
        return value
    if re.fullmatch(r"[0-9a-f]{40}(\.jpg)?", value):
        return ICON_URL.format(appid=appid, file=value if value.endswith(".jpg") else value + ".jpg")
    return value


def cache_path(appid: int, url: str) -> Path:
    digest = hashlib.sha1(url.encode()).hexdigest()[:20]
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".jpg"
    return ICON_DIR / str(appid) / f"{digest}{ext}"


def cached_icon(appid: int, value: str) -> str | None:
    """Local path for an icon if we already have it."""
    ref = resolve_icon(appid, value)
    if not ref:
        return None
    if os.path.isabs(ref):
        return ref if os.path.isfile(ref) else None
    path = cache_path(appid, ref)
    return str(path) if path.is_file() else None


def ensure_icon(appid: int, value: str, timeout: float = 15) -> str | None:
    existing = cached_icon(appid, value)
    if existing:
        return existing
    ref = resolve_icon(appid, value)
    if not ref.startswith("http"):
        return None
    dest = cache_path(appid, ref)
    try:
        data = _get(ref, timeout)
    except SteamError:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return str(dest)
