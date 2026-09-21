"""Publishes the phone page to Vercel with their REST API (no Vercel CLI needed).

The page is one self-contained file, so a deployment is a single small upload. With a passphrase set
the contents are encrypted, which is what makes a public URL acceptable.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__

API = "https://api.vercel.com"
UA = f"AchTrack/{__version__}"


class DeployError(Exception):
    pass


def best_url(result: dict, project: str) -> str:
    """The address to hand out.

    A deployment answers with several: the project's own domain, one per team slug, and a hashed
    one for this exact build. Only the project domain is public - Vercel's deployment protection
    asks the others for a login, which is no use in a link you send a friend.
    """
    aliases = [a for a in (result.get("alias") or []) if isinstance(a, str) and a]
    canonical = f"{project}.vercel.app"
    if canonical in aliases:
        return canonical
    plain = [a for a in aliases if not re.search(r"-[a-z0-9]{8,}(-|\.)", a)]
    return min(plain or aliases, key=len) if aliases else (result.get("url") or "")


def scope(team: str | None) -> str:
    """Vercel wants ?teamId= for an id and ?slug= for a team's short name; personal accounts neither."""
    team = (team or "").strip()
    if not team:
        return ""
    key = "teamId" if team.startswith("team_") else "slug"
    return f"?{key}={urllib.parse.quote(team)}"


def _call(url: str, token: str, payload: dict | None = None, raw: bytes | None = None, extra: dict | None = None):
    headers = {"Authorization": f"Bearer {token}", "User-Agent": UA, **(extra or {})}
    data = raw
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except ValueError:
            pass
        if e.code in (401, 403):
            raise DeployError(f"Vercel refused the token ({e.code}): {detail}") from e
        raise DeployError(f"Vercel error {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise DeployError(f"network error: {e}") from e


def deploy(folder: Path, token: str, project: str = "achtrack", team: str | None = None) -> str:
    """Uploads every file in `folder` as a production deployment. Returns the URL."""
    token = token.strip()
    if not token:
        raise DeployError("no Vercel token - add one in Settings")
    folder = Path(folder).expanduser()
    files = [p for p in sorted(folder.rglob("*")) if p.is_file()]
    if not files:
        raise DeployError(f"nothing to deploy in {folder}")
    query = scope(team)

    entries = []
    for path in files:
        blob = path.read_bytes()
        digest = hashlib.sha1(blob).hexdigest()
        # Files are uploaded once by hash, then referenced by the deployment.
        _call(f"{API}/v2/files{query}", token, raw=blob,
              extra={"Content-Type": "application/octet-stream", "x-vercel-digest": digest,
                     "Content-Length": str(len(blob))})
        entries.append({"file": str(path.relative_to(folder)), "sha": digest, "size": len(blob)})

    result = _call(f"{API}/v13/deployments{query}", token, {
        "name": project, "files": entries, "target": "production",
        "projectSettings": {"framework": None, "buildCommand": None, "outputDirectory": None,
                            "installCommand": None, "devCommand": None},
    })
    url = best_url(result, project)
    if not url:
        raise DeployError(f"Vercel didn't return a URL: {json.dumps(result)[:200]}")
    return url if url.startswith("http") else f"https://{url}"


def inline_deploy(page: Path, token: str, project: str = "achtrack", team: str | None = None) -> str:
    """Fallback: send the file's contents with the deployment instead of uploading it first."""
    query = scope(team)
    body = page.read_bytes()
    result = _call(f"{API}/v13/deployments{query}", token, {
        "name": project, "target": "production",
        "files": [{"file": page.name, "data": base64.b64encode(body).decode(), "encoding": "base64"}],
        "projectSettings": {"framework": None},
    })
    url = best_url(result, project)
    return url if url.startswith("http") else f"https://{url}"


def publish(folder: Path, token: str, project: str = "achtrack", team: str | None = None) -> str:
    try:
        return deploy(folder, token, project, team)
    except DeployError as first:
        page = Path(folder).expanduser() / "index.html"
        if not page.is_file():
            raise
        try:
            return inline_deploy(page, token, project, team)
        except DeployError:
            raise first
