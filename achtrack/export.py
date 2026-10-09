"""Builds the phone page: one self-contained HTML file holding your whole library.

The data is gzipped and, with a passphrase set, encrypted with AES-GCM before it goes into the page,
so the file can sit on a public URL and still be unreadable without the passphrase. The browser does
the decryption; nothing is sent anywhere.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import time
from pathlib import Path

from .db import Database

PBKDF2_ROUNDS = 250_000


def collect(db: Database, include_locked: bool = True) -> dict:
    """The library in a compact shape: short keys, achievements as arrays."""
    games = []
    for row in db.games(include_hidden=False):
        achievements = []
        for a in db.achievements(row["appid"]):
            if not include_locked and not a.get("unlocked_at"):
                continue
            achievements.append([
                a.get("display_name") or a["name"],
                a.get("description") or "",
                a.get("icon") or "",
                a.get("global_pct"),
                a.get("unlocked_at"),
                a.get("source") or "",
                1 if a.get("hidden") else 0,
                a.get("kind") or "",
            ])
        achievements.sort(key=lambda x: (x[4] is None, -(x[4] or 0), -(x[3] or 0)))
        games.append({
            "i": row["appid"], "n": row["name"] or f"App {row['appid']}", "p": (row["platform"] or "pc"),
            "t": row["total_playtime"], "l": row["last_any"] or 0, "g": row["genre"] or "",
            "h": row["header_url"] or "", "u": row["unlocked"], "c": row["total"], "a": achievements,
        })
    return {"generated": int(time.time()), "games": games}


def encrypt(data: bytes, passphrase: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import hashlib
    salt, nonce = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, PBKDF2_ROUNDS, 32)
    return {"enc": 1, "salt": base64.b64encode(salt).decode(), "iv": base64.b64encode(nonce).decode(),
            "rounds": PBKDF2_ROUNDS, "data": base64.b64encode(AESGCM(key).encrypt(nonce, data, None)).decode()}


def build_page(db: Database, passphrase: str = "") -> str:
    payload = gzip.compress(json.dumps(collect(db), separators=(",", ":")).encode("utf-8"), 9)
    if passphrase.strip():
        blob = encrypt(payload, passphrase.strip())
    else:
        blob = {"enc": 0, "data": base64.b64encode(payload).decode()}
    return PAGE.replace("__PAYLOAD__", json.dumps(blob))


def write_page(db: Database, out_dir: Path, passphrase: str = "") -> Path:
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    page = out_dir / "index.html"
    page.write_text(build_page(db, passphrase), encoding="utf-8")
    (out_dir / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    return page


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0F0F0F">
<title>AchTrack</title>
<style>
:root {
  --bg:#111112; --side:#18181A; --card:#1C1C1E; --hair:#2B2B2E; --text:#F5F5F7; --text2:#D1D1D6;
  --muted:#8E8E93; --accent:#F5F5F7; --track:#2C2C2E;
}
@media (prefers-color-scheme: light) {
  :root { --bg:#F5F5F7; --side:#ECECEE; --card:#FFFFFF; --hair:#DCDCE0; --text:#1D1D1F; --text2:#3A3A3C;
          --muted:#86868B; --accent:#1D1D1F; --track:#E2E2E6; }
}
* { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
body { margin:0; background:var(--bg); color:var(--text2); font:15px/1.4 -apple-system, BlinkMacSystemFont,
  "Segoe UI", Roboto, "Noto Sans", sans-serif; padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom); }
header { position:sticky; top:0; background:color-mix(in srgb, var(--bg) 88%, transparent); backdrop-filter:blur(14px);
  border-bottom:1px solid var(--hair); padding:10px 16px; z-index:5; }
h1 { font-size:17px; margin:0 0 8px; color:var(--text); font-weight:600; }
.row { display:flex; gap:8px; align-items:center; }
input, select, button { font:inherit; color:var(--text); background:var(--card); border:1px solid var(--hair);
  border-radius:10px; padding:9px 11px; }
input { flex:1; min-width:0; }
button { cursor:pointer; }
main { padding:12px 16px 40px; max-width:820px; margin:0 auto; }
.game { display:flex; gap:12px; align-items:center; padding:10px; border:1px solid var(--hair); border-radius:14px;
  background:var(--card); margin-bottom:8px; cursor:pointer; }
.game img { width:44px; height:44px; border-radius:11px; object-fit:cover; background:var(--track); flex:none; }
.game .n { color:var(--text); font-weight:500; }
.game .s { color:var(--muted); font-size:12.5px; margin-top:2px; }
.bar { height:4px; border-radius:2px; background:var(--track); overflow:hidden; margin-top:6px; }
.bar > i { display:block; height:100%; background:var(--accent); }
.back { background:none; border:none; color:var(--accent); padding:0 0 10px; font-size:14px; }
.hero { display:flex; gap:14px; align-items:center; margin-bottom:14px; }
.hero img { width:112px; height:52px; object-fit:cover; border-radius:10px; background:var(--track); }
.hero h2 { margin:0; font-size:20px; color:var(--text); }
.meta { color:var(--muted); font-size:13px; margin-top:4px; }
.ach { display:flex; gap:12px; padding:10px; border:1px solid var(--hair); border-radius:12px; background:var(--card);
  margin-bottom:6px; }
.ach img { width:46px; height:46px; border-radius:10px; object-fit:cover; background:var(--track); flex:none; }
.ach.locked img { filter:grayscale(1); opacity:.45; }
.ach .t { color:var(--text); font-weight:500; font-size:14px; }
.ach.locked .t { color:var(--text2); font-weight:400; }
.ach .d { color:var(--muted); font-size:12.5px; margin-top:2px; }
.ach .w { color:var(--muted); font-size:11.5px; margin-top:4px; }
.rare { color:var(--accent); }
.empty, .note { color:var(--muted); text-align:center; padding:28px 12px; }
form.gate { max-width:320px; margin:18vh auto; padding:0 16px; text-align:center; }
form.gate p { color:var(--muted); font-size:13px; }
form.gate input { width:100%; text-align:center; margin:10px 0; }
form.gate button { width:100%; background:var(--accent); border:none; color:var(--bg); font-weight:600; }
</style>
</head>
<body>
<div id="app"></div>
<script>
const PAYLOAD = __PAYLOAD__;
const app = document.getElementById("app");
const fmtH = s => s >= 3600 ? (s/3600).toFixed(1) + " h" : Math.round(s/60) + " min";
const fmtDate = t => t ? new Date(t*1000).toLocaleDateString(undefined, {day:"numeric", month:"short", year:"numeric"}) : "";
const PLAT = {pc:"PC", ps5:"PS5", ps4:"PS4", ps3:"PS3", psvita:"Vita"};
let DATA = null, sort = localStorage.getItem("sort") || "recent", query = "";

async function gunzip(buf) {
  const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
  return await new Response(stream).text();
}
const b64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));

async function decrypt(pass) {
  const enc = new TextEncoder();
  const base = await crypto.subtle.importKey("raw", enc.encode(pass), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    {name:"PBKDF2", salt:b64(PAYLOAD.salt), iterations:PAYLOAD.rounds, hash:"SHA-256"},
    base, {name:"AES-GCM", length:256}, false, ["decrypt"]);
  const plain = await crypto.subtle.decrypt({name:"AES-GCM", iv:b64(PAYLOAD.iv)}, key, b64(PAYLOAD.data));
  return JSON.parse(await gunzip(plain));
}

function gate(message) {
  app.innerHTML = `<form class="gate"><h1>AchTrack</h1><p>${message || "Enter your passphrase"}</p>
    <input type="password" autocomplete="current-password" autofocus><button>Unlock</button></form>`;
  app.querySelector("form").onsubmit = async e => {
    e.preventDefault();
    const pass = app.querySelector("input").value;
    try {
      DATA = await decrypt(pass);
      localStorage.setItem("pass", pass);
      library();
    } catch (err) { gate("That passphrase didn't work"); }
  };
}

function sorted(games) {
  const by = {
    recent: (a,b) => b.l - a.l, name: (a,b) => a.n.localeCompare(b.n), playtime: (a,b) => b.t - a.t,
    progress: (a,b) => (b.c ? b.u/b.c : -1) - (a.c ? a.u/a.c : -1), platform: (a,b) => a.p.localeCompare(b.p) || a.n.localeCompare(b.n),
  }[sort];
  const q = query.toLowerCase();
  return games.filter(g => !q || g.n.toLowerCase().includes(q) || (g.g||"").toLowerCase().includes(q)).sort(by);
}

function library() {
  const when = new Date(DATA.generated*1000).toLocaleString();
  app.innerHTML = `<header><h1>Library</h1><div class="row">
      <input id="q" placeholder="Search games" value="${query.replace(/"/g,"&quot;")}">
      <select id="s">
        <option value="recent">Recent</option><option value="name">Name</option>
        <option value="playtime">Play time</option><option value="progress">Completion</option>
        <option value="platform">Platform</option>
      </select></div></header><main id="list"></main>`;
  const sel = document.getElementById("s"); sel.value = sort;
  sel.onchange = () => { sort = sel.value; localStorage.setItem("sort", sort); draw(); };
  const input = document.getElementById("q");
  input.oninput = () => { query = input.value; draw(); };
  draw();
  function draw() {
    const list = document.getElementById("list");
    const games = sorted(DATA.games);
    list.innerHTML = games.map((g,i) => `<div class="game" data-i="${DATA.games.indexOf(g)}">
        <img loading="lazy" onerror="noimg(this)" src="${g.h}" alt="">
        <div style="flex:1;min-width:0">
          <div class="n">${esc(g.n)}</div>
          <div class="s">${g.p !== "pc" ? PLAT[g.p] + " · " : ""}${fmtH(g.t)}${g.c ? " · " + g.u + "/" + g.c : ""}</div>
          ${g.c ? `<div class="bar"><i style="width:${Math.round(g.u/g.c*100)}%"></i></div>` : ""}
        </div></div>`).join("") ||
      `<div class="empty">Nothing matches “${esc(query)}”.</div>`;
    list.innerHTML += `<div class="note">${games.length} games · updated ${when}</div>`;
    list.querySelectorAll(".game").forEach(el => el.onclick = () => detail(DATA.games[+el.dataset.i]));
  }
}

let achSort = localStorage.getItem("achSort") || "default";

function orderAchievements(list) {
  const pct = a => a[3] === null ? 999 : a[3];
  const copy = [...list];
  if (achSort === "rarest") copy.sort((a, b) => pct(a) - pct(b));
  else if (achSort === "common") copy.sort((a, b) => pct(b) - pct(a));
  else if (achSort === "name") copy.sort((a, b) => String(a[0]).localeCompare(String(b[0])));
  return copy;
}

function detail(g) {
  const done = orderAchievements(g.a.filter(a => a[4])), locked = orderAchievements(g.a.filter(a => !a[4]));
  app.innerHTML = `<main><button class="back">‹ Library</button>
    <div class="hero"><img onerror="noimg(this)" src="${g.h}" alt=""><div><h2>${esc(g.n)}</h2>
      <div class="meta">${g.p !== "pc" ? PLAT[g.p] + " · " : ""}${fmtH(g.t)}${g.l ? " · " + fmtDate(g.l) : ""}
      ${g.g ? "<br>" + esc(g.g) : ""}</div></div></div>
    ${g.c ? `<div class="bar" style="margin-bottom:14px"><i style="width:${Math.round(g.u/g.c*100)}%"></i></div>` : ""}
    <div class="row" style="margin-bottom:10px">
      <div class="meta" style="flex:1">${g.u} of ${g.c || g.a.length} unlocked</div>
      <select id="as">
        <option value="default">Unlocked first</option><option value="rarest">Rarest first</option>
        <option value="common">Most common</option><option value="name">Name</option>
      </select></div>
    ${[...done, ...locked].map(a => ach(a)).join("")}</main>`;
  const sel = document.getElementById("as");
  sel.value = achSort;
  sel.onchange = () => { achSort = sel.value; localStorage.setItem("achSort", achSort); detail(g); };
  app.querySelector(".back").onclick = library;
  window.scrollTo(0,0);
}

function ach(a) {
  const [title, desc, icon, pct, at, source, hidden, kind] = a;
  const rare = pct !== null && pct < 10;
  return `<div class="ach ${at ? "" : "locked"}">
    <img loading="lazy" onerror="noimg(this)" src="${icon}" alt="">
    <div style="flex:1;min-width:0">
      <div class="t">${esc(!at && hidden ? "Hidden achievement" : title)}${kind === "platinum" ? " · Platinum" : ""}</div>
      <div class="d">${esc(!at && hidden ? "Revealed once unlocked" : desc)}</div>
      <div class="w">${at ? "Unlocked " + fmtDate(at) : ""}${at && source ? " · " + esc(source) : ""}
        ${pct !== null ? `<span class="${rare ? "rare" : ""}">${at ? " · " : ""}${pct.toFixed(pct < 10 ? 1 : 0)}%</span>` : ""}</div>
    </div></div>`;
}

const BLANK = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";
function noimg(el) { el.onerror = null; el.src = BLANK; }
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

(async () => {
  if (!PAYLOAD.enc) { DATA = JSON.parse(await gunzip(b64(PAYLOAD.data))); return library(); }
  const saved = localStorage.getItem("pass");
  if (saved) { try { DATA = await decrypt(saved); return library(); } catch (e) {} }
  gate();
})();
</script>
</body>
</html>
"""
