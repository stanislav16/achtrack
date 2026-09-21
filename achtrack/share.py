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
<meta name="theme-color" content="#0E0E10">
<title>__TITLE__</title>
<style>
:root {
  --bg:#0E0E10; --card:#17171A; --hair:#2A2A2E; --text:#F5F5F7; --text2:#D6D6DA;
  --muted:#8A8A8F; --accent:#E0913C; --track:#26262A; --shadow:0 1px 2px rgba(0,0,0,.4);
}
@media (prefers-color-scheme: light) {
  :root { --bg:#F6F5F3; --card:#FFFFFF; --hair:#DEDCD7; --text:#1C1C1E; --text2:#3A3A3C;
          --muted:#86847F; --accent:#B0702C; --track:#E4E2DD; --shadow:0 1px 2px rgba(0,0,0,.06); }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text2); font:15px/1.45 -apple-system, BlinkMacSystemFont,
  "Segoe UI", Roboto, "Noto Sans", sans-serif; -webkit-font-smoothing:antialiased; }
.wrap { max-width:940px; margin:0 auto; padding:40px 20px 64px; }
h1 { font-size:30px; line-height:1.15; letter-spacing:-.02em; margin:0; color:var(--text); font-weight:650; }
h2 { font-size:13px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted);
     margin:40px 0 14px; font-weight:600; }
.sub { color:var(--muted); margin:8px 0 0; font-size:14px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-top:28px; }
@media (min-width:640px) { .tiles { grid-template-columns:repeat(3,1fr); } }
.tile { background:var(--card); border:1px solid var(--hair); border-radius:16px; padding:16px 18px;
        box-shadow:var(--shadow); }
.tile .v { font-size:27px; letter-spacing:-.02em; color:var(--text); font-weight:650; font-variant-numeric:tabular-nums; }
.tile .k { font-size:13px; color:var(--muted); margin-top:3px; }
.tile .v small { font-size:15px; color:var(--muted); font-weight:500; letter-spacing:0; }
.list { display:grid; gap:8px; }
.item { display:flex; gap:13px; align-items:center; background:var(--card); border:1px solid var(--hair);
        border-radius:14px; padding:11px 13px; box-shadow:var(--shadow); }
.item img { flex:none; background:var(--track); object-fit:cover; }
.ico { width:44px; height:44px; border-radius:11px; }
.art { width:92px; height:43px; border-radius:9px; }
.grow { flex:1; min-width:0; }
.t { color:var(--text); font-weight:560; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.d { font-size:13px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-top:1px; }
.pill { flex:none; font-size:12px; font-variant-numeric:tabular-nums; color:var(--accent);
        border:1px solid color-mix(in srgb, var(--accent) 35%, transparent); border-radius:999px; padding:3px 9px; }
.pill.plain { color:var(--muted); border-color:var(--hair); }
.bar { height:4px; background:var(--track); border-radius:999px; overflow:hidden; margin-top:7px; max-width:260px; }
.bar i { display:block; height:100%; background:var(--accent); border-radius:999px; }
.games { display:grid; gap:8px; }
.sorts { display:flex; gap:6px; flex-wrap:wrap; margin:-4px 0 14px; }
.sorts button { font:inherit; font-size:13px; color:var(--muted); background:none; border:1px solid var(--hair);
  border-radius:999px; padding:5px 13px; cursor:pointer; }
.sorts button[aria-pressed="true"] { color:var(--text); border-color:color-mix(in srgb, var(--accent) 50%, transparent); }
footer { margin-top:48px; padding-top:18px; border-top:1px solid var(--hair); color:var(--muted); font-size:13px; }
@media (max-width:560px) { .wrap { padding:26px 16px 48px; } h1 { font-size:25px; } .art { width:74px; height:35px; } }
</style>
</head>
<body>
<div class="wrap">
  <h1 id="who"></h1>
  <p class="sub" id="when"></p>
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
document.getElementById("tiles").innerHTML = [
  [s.unlocked.toLocaleString(), "of " + s.total.toLocaleString() + " unlocked"],
  [done + "<small>%</small>", "Overall completion"],
  [hours(s.seconds), "Time played"],
  [s.games.toLocaleString(), s.games === 1 ? "Game tracked" : "Games tracked"],
  [s.perfect.toLocaleString(), s.perfect === 1 ? "Game completed" : "Games completed"],
].concat(s.platinums ? [[s.platinums.toLocaleString(), s.platinums === 1 ? "Platinum" : "Platinums"]] : [])
 .map(([v, k]) => `<div class="tile"><div class="v">${v}</div><div class="k">${esc(k)}</div></div>`).join("");

function trophyRow([title, icon, pct, game, at, kind], tail) {
  return `<div class="item">
    <img class="ico" loading="lazy" src="${esc(icon)}" alt="">
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
    ${g.h ? `<img class="art" loading="lazy" src="${esc(g.h)}" alt="">` : `<div class="art"></div>`}
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
