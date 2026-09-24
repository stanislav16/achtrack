"""Epic Games achievements - reading them through the login Heroic already has.

Epic added achievements to its store, and Heroic launches Epic games through Legendary, which
keeps the account's tokens in a config file. So in principle nothing new has to be logged into:
read the token, ask Epic's achievement service what the account has earned, and treat it like the
Steam and PlayStation syncs.

None of these endpoints are documented by Epic, so this module starts as a probe: it reports what
it can find and what answers back, and says plainly when something isn't there.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)
UA = f"AchTrack/{__version__}"

HOME = Path.home()
# Heroic keeps Legendary's config inside its own folder; a separate Legendary install has its own.
USER_FILES = [
    HOME / ".config/heroic/legendaryConfig/legendary/user.json",
    HOME / ".var/app/com.heroicgameslauncher.hgl/config/heroic/legendaryConfig/legendary/user.json",
    HOME / ".config/legendary/user.json",
    HOME / ".var/app/com.heroicgameslauncher.hgl/config/legendary/user.json",
]
LIBRARY_FILES = [
    HOME / ".config/heroic/store_cache/legendary_library.json",
    HOME / ".var/app/com.heroicgameslauncher.hgl/config/heroic/store_cache/legendary_library.json",
    HOME / ".config/heroic/legendaryConfig/legendary/installed.json",
    HOME / ".var/app/com.heroicgameslauncher.hgl/config/heroic/legendaryConfig/legendary/installed.json",
]
ACHIEVEMENTS = "https://achievements-public-service-prod.ol.epicgames.com/achievements/v2"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def credentials() -> tuple:
    """(the login Heroic stored, where it came from) - ({}, "") when there isn't one."""
    for path in USER_FILES:
        data = _read_json(path)
        if isinstance(data, dict) and data.get("access_token"):
            return data, str(path)
    return {}, ""


def games() -> list:
    """Epic games Heroic knows about: [(app name, title, namespace)]."""
    for path in LIBRARY_FILES:
        data = _read_json(path)
        entries = data.get("library") if isinstance(data, dict) else data
        if isinstance(entries, dict):
            entries = list(entries.values())
        if not isinstance(entries, list):
            continue
        out = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("app_name") or entry.get("appName") or ""
            title = entry.get("title") or entry.get("app_title") or ""
            namespace = entry.get("namespace") or entry.get("metadata", {}).get("namespace", "") \
                if isinstance(entry.get("metadata"), dict) else entry.get("namespace", "")
            if name:
                out.append((name, title, namespace))
        if out:
            return out
    return []


def _get(url: str, token: str, timeout: float = 20) -> tuple:
    """(status, body) - never raises, so a probe can report every outcome."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()[:4000]
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400]
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e).encode()


def probe(note=print) -> dict:
    """Reports what's available, without changing anything."""
    found = {"login": "", "account": "", "expires": "", "games": 0, "answers": []}
    creds, where = credentials()
    if not creds:
        note("No Epic login found. Looked in:")
        for path in USER_FILES:
            note(f"  {path}")
        note("Sign in to Epic inside Heroic first - AchTrack reads the token it stores, "
             "it never asks for your password.")
        return found

    found["login"] = where
    found["account"] = creds.get("account_id") or ""
    found["expires"] = creds.get("expires_at") or ""
    note(f"Epic login found in {where}")
    note(f"  account {found['account'] or 'unknown'}, token expires {found['expires'] or 'unknown'}")
    if found["expires"]:
        note("  (if that is in the past, open Heroic once to refresh it)")

    library = games()
    found["games"] = len(library)
    note(f"Epic games Heroic knows about: {len(library)}")
    for name, title, namespace in library[:5]:
        note(f"  {title or name}  [{namespace or 'no namespace'}]")

    token, account = creds["access_token"], found["account"]
    tries = [("all achievements for the account", f"{ACHIEVEMENTS}/player/{account}/achievements")]
    if library:
        namespace = next((ns for _n, _t, ns in library if ns), "")
        if namespace:
            tries.append((f"achievements for one game ({namespace})",
                          f"{ACHIEVEMENTS}/player/{account}/sandbox/{namespace}/achievements"))
    for label, url in tries:
        status, body = _get(url, token)
        summary = body[:160].decode("utf-8", "replace").replace("\n", " ")
        note(f"{label}: HTTP {status} {summary}")
        found["answers"].append({"url": url, "status": status, "body": summary})
    return found
