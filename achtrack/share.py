"""Builds the page you send to friends: one self-contained HTML file of your statistics.

Unlike the phone page, this is meant to be seen by someone else, so it carries a summary rather
than your library: totals, your rarest finds, what you've played most and what you unlocked
recently. No achievement list, no notes, no account ids, and nothing about where an unlock came
from. It is one file, so it works either way - send it as an attachment, or publish it and send
the link.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .db import Database

RARE = 8         # rarest finds to show
RECENT = 10      # most recent unlocks to show
MOST_PLAYED = 6


def collect(db: Database, name: str = "") -> dict:
    games, unlocked, total, seconds, perfect, platinums = [], 0, 0, 0, 0, 0
    for row in db.games(include_hidden=False):
        if not row["total"] and not row["total_playtime"]:
            continue                     # nothing to say about it
        games.append({
            "n": row["name"] or f"App {row['appid']}", "p": row["platform"] or "pc",
            "t": row["total_playtime"], "l": row["last_any"] or 0, "g": row["genre"] or "",
            "h": row["header_url"] or "", "u": row["unlocked"], "c": row["total"],
        })
        unlocked += row["unlocked"]
        total += row["total"]
        seconds += row["total_playtime"]
        perfect += 1 if row["total"] and row["unlocked"] >= row["total"] else 0

    earned = """SELECT a.display_name, a.icon, a.global_pct, a.kind, u.unlocked_at, g.name AS game
                FROM unlocks u
                JOIN achievements a ON a.appid = u.appid AND a.name = u.name
                JOIN games g ON g.appid = a.appid
                WHERE coalesce(g.hidden, 0) = 0"""
    platinums = db.q(f"SELECT COUNT(*) FROM ({earned}) WHERE kind = 'platinum'")[0][0]

    def trophies(order: str, limit: int) -> list:
        return [[r["display_name"] or "", r["icon"] or "", r["global_pct"], r["game"] or "",
                 r["unlocked_at"], r["kind"] or ""]
                for r in db.q(f"{earned} AND {order} LIMIT {limit}")]

    return {
        "name": name.strip(),
        "generated": int(time.time()),
        "stats": {"games": len(games), "unlocked": unlocked, "total": total, "seconds": seconds,
                  "perfect": perfect, "platinums": platinums},
        "games": sorted(games, key=lambda g: -g["t"]),
        "rare": trophies("a.global_pct IS NOT NULL ORDER BY a.global_pct ASC", RARE),
        "recent": trophies("u.unlocked_at > 0 ORDER BY u.unlocked_at DESC", RECENT),
    }


def build_page(db: Database, name: str = "") -> str:
    data = collect(db, name)
    title = f"{data['name']} · achievements" if data["name"] else "Achievements"
    return (PAGE.replace("__TITLE__", json.dumps(title)[1:-1])
                .replace("__MOST_PLAYED__", str(MOST_PLAYED))
                .replace("__DATA__", json.dumps(data, separators=(",", ":"))))


def write_page(db: Database, out_dir: Path, name: str = "") -> Path:
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    page = out_dir / "index.html"
    page.write_text(build_page(db, name), encoding="utf-8")
    (out_dir / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    return page


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#111112" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#F5F5F7" media="(prefers-color-scheme: light)">
<title>__TITLE__</title>
<style>
:root {
  --bg:#111112; --card:#1C1C1E; --hair:#2B2B2E; --text:#F5F5F7; --text2:#D1D1D6;
  --muted:#8E8E93; --accent:#F5F5F7; --track:#2C2C2E; --shadow:0 1px 2px rgba(0,0,0,.4);
}
@media (prefers-color-scheme: light) {
  :root { --bg:#F5F5F7; --card:#FFFFFF; --hair:#DCDCE0; --text:#1D1D1F; --text2:#3A3A3C;
          --muted:#86868B; --accent:#1D1D1F; --track:#E2E2E6; --shadow:0 1px 2px rgba(0,0,0,.06); }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text2); font:15px/1.45 -apple-system, BlinkMacSystemFont,
  "Segoe UI", Roboto, "Noto Sans", sans-serif; -webkit-font-smoothing:antialiased; }
.wrap { max-width:880px; margin:0 auto; padding:48px 20px 64px; }
h1 { font-size:34px; line-height:1.1; letter-spacing:-.025em; margin:0; color:var(--text); font-weight:700; }
h2 { font-size:20px; letter-spacing:-.01em; color:var(--text); margin:44px 0 12px; font-weight:650; }
.sub { color:var(--muted); margin:8px 0 0; font-size:14px; }
.hero { display:flex; align-items:center; gap:20px; }
.hero .grow { flex:1; min-width:0; }
.ring { flex:none; width:84px; height:84px; border-radius:50%; display:grid; place-items:center;
        background:conic-gradient(var(--accent) calc(var(--p) * 1%), var(--track) 0); }
.ring b { width:70px; height:70px; border-radius:50%; background:var(--bg); display:flex; align-items:baseline;
          justify-content:center; line-height:70px;
          color:var(--text); font-size:19px; font-weight:650; font-variant-numeric:tabular-nums; }
.ring small { font-size:12px; color:var(--muted); font-weight:500; margin-left:1px; }
/* One card, split by hairlines: the 1px gap shows the hairline colour behind the tiles. */
.tiles { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:1px; margin-top:28px; background:var(--hair);
         border:1px solid var(--hair); border-radius:18px; overflow:hidden; box-shadow:var(--shadow); }
@media (min-width:640px) { .tiles { grid-template-columns:repeat(var(--n,4),minmax(0,1fr)); } }
.tile { background:var(--card); padding:16px 18px; }
@media (max-width:639px) { .tile:first-child:nth-last-child(odd) { grid-column:span 2; } }
.tile .v { font-size:24px; letter-spacing:-.02em; color:var(--text); font-weight:650; font-variant-numeric:tabular-nums; }
.tile .k { font-size:12.5px; color:var(--muted); margin-top:2px; }
/* minmax(0,…): a grid child refuses to shrink below its own text otherwise, and a long game
   name then pushes the row off the side of a phone screen. */
.list, .games { display:grid; grid-template-columns:minmax(0,1fr); background:var(--card); border:1px solid var(--hair);
        border-radius:18px; overflow:hidden; box-shadow:var(--shadow); }
.item { display:flex; min-width:0; gap:14px; align-items:center; padding:12px 16px; position:relative; }
.item + .item::before { content:""; position:absolute; top:0; right:0; left:var(--inset,74px); height:1px;
        background:var(--hair); }
.games .item, #played .item { --inset:124px; }
.item img, .item .art { flex:none; background:var(--track); object-fit:cover; }
.ico { width:44px; height:44px; border-radius:11px; }
.art { width:94px; height:44px; border-radius:9px; }
.grow { flex:1; min-width:0; }
.t { color:var(--text); font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.d { font-size:13px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-top:1px; }
.pill { flex:none; font-size:12px; font-weight:600; font-variant-numeric:tabular-nums; color:var(--text);
        background:var(--track); border-radius:999px; padding:3px 10px; }
.pill.plain { color:var(--muted); background:none; font-weight:400; padding-right:0; }
.bar { height:4px; background:var(--track); border-radius:999px; overflow:hidden; margin-top:7px; max-width:260px; }
.bar i { display:block; height:100%; background:var(--accent); border-radius:999px; }
.sorts { display:inline-flex; gap:2px; padding:2px; margin:0 0 12px; background:var(--track); border-radius:10px;
         max-width:100%; overflow-x:auto; }
.sorts button { font:inherit; font-size:13px; color:var(--muted); background:none; border:none; border-radius:8px;
  padding:5px 13px; cursor:pointer; white-space:nowrap; }
.sorts button[aria-pressed="true"] { color:var(--text); background:var(--card); font-weight:600; box-shadow:var(--shadow); }
footer { margin-top:48px; color:var(--muted); font-size:13px; text-align:center; }
@media (max-width:560px) { .wrap { padding:28px 16px 48px; } h1 { font-size:27px; } h2 { font-size:18px; margin-top:34px; }
  .ring { width:68px; height:68px; } .ring b { width:56px; height:56px; line-height:56px; font-size:16px; }
  .art { width:74px; height:35px; } .games .item, #played .item { --inset:104px; } .item { padding:11px 14px; } }
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div class="grow"><h1 id="who"></h1><p class="sub" id="when"></p></div>
    <div class="ring" id="ring"><b></b></div>
  </div>
  <div class="tiles" id="tiles"></div>

  <section id="rare-wrap" hidden><h2>Rarest finds</h2><div class="list" id="rare"></div></section>
  <section id="played-wrap" hidden><h2>Most played</h2><div class="list" id="played"></div></section>
  <section id="recent-wrap" hidden><h2>Recently unlocked</h2><div class="list" id="recent"></div></section>

  <section id="all-wrap" hidden>
    <h2>Every game</h2>
    <div class="sorts">
      <button data-by="t" aria-pressed="true">Time played</button>
      <button data-by="pct">Completion</button>
      <button data-by="c">Achievements</button>
      <button data-by="n">Name</button>
    </div>
    <div class="games" id="all"></div>
  </section>

  <footer>Tracked with AchTrack</footer>
</div>
<script>
const BLANK = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";
function noimg(el) { el.onerror = null; el.src = BLANK; }
const D = __DATA__;
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const hours = s => s >= 3600 ? Math.round(s / 3600).toLocaleString() + " h" : Math.round(s / 60) + " min";
const when = t => !t ? "" : new Date(t * 1000).toLocaleDateString(undefined, {year:"numeric", month:"short", day:"numeric"});
const pctOf = g => g.c ? g.u / g.c : 0;
const PLATFORM = {pc:"PC", ps5:"PS5", ps4:"PS4", ps3:"PS3"};

document.title = D.name ? D.name + " · achievements" : "Achievements";
document.getElementById("who").textContent = D.name ? D.name + "'s achievements" : "Achievements";
document.getElementById("when").textContent = "As of " + when(D.generated);

const s = D.stats, done = s.total ? Math.round(s.unlocked / s.total * 100) : 0;
const ring = document.getElementById("ring");
ring.style.setProperty("--p", done);
ring.firstChild.innerHTML = done + "<small>%</small>";
const tiles = [
  [s.unlocked.toLocaleString(), "of " + s.total.toLocaleString() + " unlocked"],
  [hours(s.seconds), "Time played"],
  [s.games.toLocaleString(), s.games === 1 ? "Game tracked" : "Games tracked"],
  [s.perfect.toLocaleString(), s.perfect === 1 ? "Game completed" : "Games completed"],
].concat(s.platinums ? [[s.platinums.toLocaleString(), s.platinums === 1 ? "Platinum" : "Platinums"]] : [])
;
document.getElementById("tiles").style.setProperty("--n", tiles.length > 4 ? 5 : 4);
document.getElementById("tiles").innerHTML =
  tiles.map(([v, k]) => `<div class="tile"><div class="v">${v}</div><div class="k">${esc(k)}</div></div>`).join("");

function trophyRow([title, icon, pct, game, at, kind], tail) {
  return `<div class="item">
    <img class="ico" loading="lazy" onerror="noimg(this)" src="${esc(icon)}" alt="">
    <div class="grow"><div class="t">${esc(title)}${kind === "platinum" ? " · Platinum" : ""}</div>
    <div class="d">${esc(game)}</div></div>
    <div class="pill${tail.plain ? " plain" : ""}">${esc(tail.text)}</div></div>`;
}
function fill(id, rows) {
  if (!rows.length) return;
  document.getElementById(id + "-wrap").hidden = false;
  document.getElementById(id).innerHTML = rows.join("");
}
fill("rare", D.rare.map(t => trophyRow(t, {text: t[2] + "%"})));
fill("recent", D.recent.map(t => trophyRow(t, {text: when(t[4]), plain: true})));

function gameRow(g, note) {
  const pct = Math.round(pctOf(g) * 100);
  return `<div class="item">
    ${g.h ? `<img class="art" loading="lazy" onerror="noimg(this)" src="${esc(g.h)}" alt="">` : `<div class="art"></div>`}
    <div class="grow"><div class="t">${esc(g.n)}</div>
      <div class="d">${esc(note(g))}</div>
      ${g.c ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}</div>
    <div class="pill${g.c && g.u >= g.c ? "" : " plain"}">${g.c ? g.u + " / " + g.c : "—"}</div></div>`;
}
const platform = g => PLATFORM[g.p] || (g.p || "").toUpperCase();
fill("played", D.games.filter(g => g.t > 0).slice(0, __MOST_PLAYED__)
  .map(g => gameRow(g, x => [hours(x.t), platform(x), x.g.split(",")[0].trim()].filter(Boolean).join(" · "))));

const all = document.getElementById("all");
if (D.games.length) {
  document.getElementById("all-wrap").hidden = false;
  const note = g => [g.t ? hours(g.t) : "", platform(g), g.l ? "last played " + when(g.l) : ""].filter(Boolean).join(" · ");
  const orders = {
    t: (a, b) => b.t - a.t,
    pct: (a, b) => pctOf(b) - pctOf(a) || b.u - a.u,
    c: (a, b) => b.u - a.u || b.c - a.c,
    n: (a, b) => a.n.localeCompare(b.n),
  };
  const draw = by => { all.innerHTML = D.games.slice().sort(orders[by]).map(g => gameRow(g, note)).join(""); };
  document.querySelectorAll(".sorts button").forEach(b => b.onclick = () => {
    document.querySelectorAll(".sorts button").forEach(o => o.setAttribute("aria-pressed", o === b));
    draw(b.dataset.by);
  });
  draw("t");
}
</script>
</body>
</html>
"""
