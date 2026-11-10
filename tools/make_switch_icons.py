"""Draws the achievement icons for AchTrack's own Switch goals (Mario Kart 8 Deluxe, Mario Kart World, Donkey Kong
Bananza and Ocarina of Time).

Each icon is a line glyph for what the goal is about, in AchTrack's own look: one even line weight, near-white on a
graphite tile, like the app's trophy. How hard the goal is (bronze, silver, gold, platinum) shows only as a faint
wash behind the glyph. The app rounds the corners itself. They are AchTrack's own drawings, not the games' artwork. Run from the repository root:

    python tools/make_switch_icons.py

It writes achtrack/assets/switch/<title key>/<goal>.png (256 px). Needs Pillow.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from achtrack import switch  # noqa: E402

S = 1024                      # drawn large, then scaled down for smooth edges
OUT = 256
W = 46                        # line weight, the same for every glyph (the app's own trophy: 3 px in 64)
INK = (245, 245, 247, 255)    # theme "text"
DIM = (245, 245, 247, 120)
CLEAR = (0, 0, 0, 0)
TOP, BOTTOM = (46, 46, 50), (28, 28, 30)   # graphite, like the app's cards
GLOW = {   # a faint wash behind the glyph, the only hint of how hard the goal is
    "b": (205, 140, 95, 34), "s": (200, 206, 216, 30), "g": (232, 192, 100, 42), "p": (186, 196, 236, 46),
}

# goal -> (glyph, tier)
GOALS = {
    # Mario Kart World
    "race_1": ("flag", "b"), "race_25": ("flag", "s"), "race_100": ("flag", "g"), "race_500": ("flag", "p"),
    "win_1": ("crown", "b"), "win_10": ("crown", "s"), "win_50": ("crown", "g"), "win_200": ("crown", "p"),
    "podium_25": ("podium", "s"), "comeback": ("rocket", "g"),
    "overtake_500": ("chevrons", "s"), "overtake_5000": ("chevrons", "g"),
    "cup_1": ("cup", "b"), "cup_25": ("cup", "s"), "gold_1": ("cup1", "g"), "gold_8": ("cup1", "p"), "perfect": ("stars3", "g"),
    "course_5": ("pin", "b"), "course_15": ("pin", "s"), "course_30": ("globe", "p"),
    "driver_5": ("wheel", "b"), "driver_20": ("wheel", "g"),
    "vehicle_5": ("kart", "b"), "vehicle_20": ("kart", "g"),
    "coin_20": ("coin", "b"), "coin_1000": ("coins", "s"), "coin_10000": ("coins", "g"),
    "time_trial_1": ("watch", "b"), "time_trial_25": ("watch", "g"),
    "free_roam": ("road", "b"), "mission_1": ("pswitch", "b"), "mission_25": ("pswitch", "s"),
    "clear_1": ("pswitch_check", "b"), "clear_25": ("pswitch_check", "s"), "clear_100": ("pswitch_check", "g"),
    "clear_all": ("pswitch_check", "p"),
    "panel_1": ("panel", "b"), "panel_25": ("panel", "s"), "panel_75": ("panel", "g"), "panel_all": ("panel", "p"),
    "medal_1": ("medal", "b"), "medal_25": ("medal", "s"), "medal_100": ("medal", "g"), "medal_all": ("medal", "p"),
    # Mario Kart 8 Deluxe
    "race_50": ("flag", "s"), "race_250": ("flag", "g"), "race_1000": ("flag", "p"),
    "trophy_1": ("cup", "b"), "trophy_12": ("cup", "s"), "trophy_30": ("cup", "g"), "trophy_60": ("cup", "p"),
    "ending": ("film", "s"), "gold_mario": ("crown", "g"),
    "ghost_150": ("ghost", "s"), "ghost_150_all": ("ghost", "g"), "ghost_200": ("ghost_bolt", "g"),
    "ghost_200_all": ("ghost_bolt", "p"),
    "parts_30": ("gear", "s"), "parts_50": ("gear", "g"), "mii_suit": ("shirt", "s"),
    "ultra_100": ("spark", "g"), "drift_5000": ("drift", "s"), "trick_1000": ("star", "s"),
    "lakitu_50": ("cloud", "b"), "fly_30": ("wing", "s"), "swim_30": ("wave", "s"),
    "battle_1": ("balloon", "b"), "battle_all": ("balloon", "g"),
    # Donkey Kong Bananza
    "dk_banana_1": ("banana", "b"), "dk_banana_50": ("banana", "s"), "dk_banana_150": ("bananas", "s"),
    "dk_banana_300": ("bananas", "g"), "dk_banana_500": ("bananas", "g"), "dk_banana_all": ("bananas", "p"),
    "dk_fossil_1": ("bone", "b"), "dk_fossil_50": ("bone", "s"), "dk_fossil_200": ("bone", "s"), "dk_fossil_400": ("bone", "g"),
    "dk_fossil_all": ("bone", "p"),
    "dk_chest_1": ("chest", "b"), "dk_chest_25": ("chest", "s"), "dk_chest_all": ("chest", "g"),
    "dk_skill_1": ("fist", "b"), "dk_skill_15": ("fist", "s"), "dk_skill_all": ("fist", "g"),
    "dk_layer_3": ("layers", "b"), "dk_layer_9": ("layers", "s"), "dk_layer_all": ("layers", "g"), "dk_core": ("core", "g"),
    "dk_clear": ("film", "g"), "dk_island": ("palm", "s"), "dk_emerald_rush": ("emerald", "p"),
    # The Legend of Zelda: Ocarina of Time
    "oot_sword": ("sword", "b"), "oot_emerald": ("emerald", "s"), "oot_ruby": ("emerald", "s"),
    "oot_sapphire": ("emerald", "s"), "oot_ocarina": ("ocarina", "s"), "oot_master_sword": ("sword", "g"),
    "oot_medal_light": ("medallion", "s"), "oot_medal_forest": ("medallion", "s"), "oot_medal_fire": ("medallion", "s"),
    "oot_medal_water": ("medallion", "g"), "oot_medal_shadow": ("medallion", "g"), "oot_medal_spirit": ("medallion", "g"),
    "oot_ganon": ("triforce", "p"),
    "oot_song_1": ("note", "b"), "oot_warp_songs": ("notes", "s"), "oot_song_all": ("notes", "g"),
    "oot_scarecrow": ("note", "s"),
    "oot_skulltula_1": ("skull", "b"), "oot_skulltula_10": ("skull", "s"), "oot_skulltula_50": ("skull", "g"),
    "oot_skulltula_100": ("skull", "p"),
    "oot_heart_piece": ("heart_piece", "b"), "oot_hearts_10": ("heart", "s"), "oot_hearts_20": ("heart", "p"),
    "oot_big_poe_1": ("lantern", "b"), "oot_big_poe_all": ("lantern", "g"), "oot_fairies": ("fairy", "g"),
    "oot_bottles": ("bottle", "g"), "oot_mask_truth": ("mask", "g"), "oot_epona": ("horseshoe", "s"),
    "oot_gerudo_card": ("card", "s"), "oot_biggoron": ("sword", "p"), "oot_mirror_shield": ("shield", "g"),
    "oot_longshot": ("hookshot", "s"), "oot_arrows": ("arrow", "g"), "oot_gauntlets": ("glove", "g"),
    "oot_scale": ("scale", "s"), "oot_outfits": ("shirt", "g"), "oot_wallet": ("purse", "s"),
    "oot_chest_25": ("chest", "s"), "oot_chest_100": ("chest", "g"), "oot_places_15": ("pin", "s"),
    "oot_places_30": ("globe", "g"), "oot_hall": ("pillars", "s"), "oot_kinds_25": ("book", "s"),
    "oot_kinds_50": ("book", "g"), "oot_defeated_500": ("swords", "g"),
}


def font(size: int, bold: bool = True):
    for name in ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc", "DejaVuSans-Bold.ttf"):
        try:
            f = ImageFont.truetype(name, size)
            try:
                f.set_variation_by_name("Semibold" if bold else "Regular")
            except Exception:
                pass
            return f
        except OSError:
            continue
    return ImageFont.load_default()


# --- drawing helpers: every stroke W wide with round ends and joins ------------------------------------------------

def dot(d, x, y, r, fill=INK):
    d.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def line(d, pts, w=W, fill=INK, closed=False):
    pts = [tuple(p) for p in pts]
    if closed:
        pts = pts + [pts[0], pts[1]]
    d.line(pts, fill=fill, width=w, joint="curve")
    if not closed:
        for x, y in (pts[0], pts[-1]):
            dot(d, x, y, w / 2, fill)
    for x, y in pts:   # PIL's curve joints leave notches on sharp turns: a round cap on every vertex closes them
        dot(d, x, y, w / 2 - 1, fill)


def ring(d, cx, cy, r, w=W, fill=INK):   # a circle whose line is centred on radius r
    d.ellipse((cx - r - w / 2, cy - r - w / 2, cx + r + w / 2, cy + r + w / 2), outline=fill, width=w)


def box(d, x0, y0, x1, y1, radius, w=W, fill=INK):   # a rounded rectangle, line centred on its edge
    h = w / 2
    d.rounded_rectangle((x0 - h, y0 - h, x1 + h, y1 + h), radius=radius + h, outline=fill, width=w)


def erase_box(d, x0, y0, x1, y1, radius):   # clear the inside of a shape that sits in front of another
    h = W / 2
    d.rounded_rectangle((x0 - h, y0 - h, x1 + h, y1 + h), radius=radius + h, fill=CLEAR)


def erase_circle(d, cx, cy, r):
    r += W / 2
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=CLEAR)


def arc(d, cx, cy, r, a0, a1, w=W, fill=INK, ry=None):   # degrees, clockwise from 3 o'clock, round ends
    ry = r if ry is None else ry
    pts = [(cx + r * math.cos(math.radians(a)), cy + ry * math.sin(math.radians(a)))
           for a in [a0 + (a1 - a0) * i / 64 for i in range(65)]]
    line(d, pts, w, fill)


def outline_of(d, mask: Image.Image, fill=INK, w=W):
    """The edge of a filled shape (an "L" mask), drawn W wide just inside it."""
    inner = mask.filter(ImageFilter.GaussianBlur(w / 2.37)).point(lambda v: 255 if v >= 254 else 0)   # eats w deep
    edge = ImageChops.subtract(mask, inner)
    d.bitmap((0, 0), edge, fill=fill)


def mask():
    m = Image.new("L", (S, S), 0)
    return m, ImageDraw.Draw(m)


def text(d, xy, s: str, size: int, fill=INK):
    f = font(size)
    b = d.textbbox((0, 0), s, font=f)
    d.text((xy[0] - (b[0] + b[2]) / 2, xy[1] - (b[1] + b[3]) / 2), s, font=f, fill=fill)


def star_pts(cx, cy, r, inner=0.48, points=5, turn=0.0):
    return [(cx + (r if i % 2 == 0 else r * inner) * math.sin(i * math.pi / points + turn),
             cy - (r if i % 2 == 0 else r * inner) * math.cos(i * math.pi / points + turn)) for i in range(points * 2)]


def star(d, cx, cy, r, w=W, fill=INK, inner=0.48, points=5):
    line(d, star_pts(cx, cy, r, inner, points), w, fill, closed=True)


def bezier(p0, p1, p2, p3, n=32):
    out = []
    for i in range(n + 1):
        t = i / n; u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1]))
    return out


# --- glyphs, on a 1024 grid, kept inside about 250..774 --------------------------------------------------------------

def g_flag(d):   # a chequered flag on a pole
    line(d, [(330, 790), (330, 250)])
    x0, y0, x1, y1 = 330, 260, 750, 540
    line(d, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], closed=True)
    cw, ch = (x1 - x0) / 4, (y1 - y0) / 3
    for r in range(3):
        for c in range(4):
            if (r + c) % 2 == 0:
                d.rectangle((x0 + c * cw, y0 + r * ch, x0 + (c + 1) * cw, y0 + (r + 1) * ch), fill=INK)


def g_crown(d):
    line(d, [(290, 700), (262, 380), (402, 520), (512, 320), (622, 520), (762, 380), (734, 700)], closed=True)
    line(d, [(300, 790), (724, 790)])


def _cup(d):
    bowl = [(352, 270)] + bezier((352, 270), (352, 470), (420, 560), (512, 560))[1:] + \
        bezier((512, 560), (604, 560), (672, 470), (672, 270))[1:]
    line(d, bowl, closed=True)
    arc(d, 352, 360, 90, 90, 270, ry=70)
    arc(d, 672, 360, 90, -90, 90, ry=70)
    line(d, [(512, 560), (512, 680)])
    line(d, [(400, 770), (412, 690), (612, 690), (624, 770)], closed=True)


def g_cup(d):
    _cup(d)


def g_cup1(d):
    _cup(d)
    text(d, (512, 400), "1", 190)


def g_podium(d):
    line(d, [(250, 790), (250, 580), (412, 580), (412, 450), (612, 450), (612, 640), (774, 640), (774, 790)], closed=True)
    star(d, 512, 290, 100, w=int(W * 0.85))


def g_rocket(d):   # a comeback: the line climbs from the back
    line(d, [(262, 720), (420, 560), (540, 650), (742, 380)])
    line(d, [(612, 370), (750, 370), (750, 508)])


def g_chevrons(d):
    for y, f in ((320, INK), (500, INK), (680, DIM)):
        line(d, [(320, y + 110), (512, y - 50), (704, y + 110)], fill=f)


def g_stars3(d):
    star(d, 512, 360, 170)
    star(d, 312, 660, 110, w=int(W * 0.85))
    star(d, 712, 660, 110, w=int(W * 0.85))


def g_pin(d):
    cx, cy, r, tip = 512, 440, 220, (512, 800)
    dist = tip[1] - cy
    a = math.degrees(math.acos(r / dist))          # where the sides leave the circle
    pts = [tip] + [(cx + r * math.cos(math.radians(t)), cy + r * math.sin(math.radians(t)))
                   for t in [90 - a + (-(180 + 2 * (90 - a))) * i / 64 for i in range(65)]]
    line(d, pts, closed=True)
    ring(d, cx, cy, 70)


def g_globe(d):
    ring(d, 512, 512, 262)
    d.ellipse((512 - 120 - W / 2, 250 - W / 2, 512 + 120 + W / 2, 774 + W / 2), outline=INK, width=W)
    line(d, [(250, 512), (774, 512)])
    arc(d, 512, 512, 230, 200, 340, w=int(W * 0.8), ry=150)
    arc(d, 512, 512, 230, 20, 160, w=int(W * 0.8), ry=150)


def g_wheel(d):   # a steering wheel
    ring(d, 512, 512, 262)
    ring(d, 512, 512, 62)
    for a in (180, 0, 90):
        r = math.radians(a)
        line(d, [(512 + 62 * math.cos(r), 512 + 62 * math.sin(r)), (512 + 262 * math.cos(r), 512 + 262 * math.sin(r))])


def g_kart(d):
    line(d, [(232, 610), (252, 520), (420, 520), (470, 410), (600, 410), (650, 520), (792, 540), (792, 610)], closed=True)
    line(d, [(600, 410), (640, 330)])
    for x in (360, 676):
        erase_circle(d, x, 640, 82)
        ring(d, x, 640, 82)
        dot(d, x, 640, 22)


def g_coin(d):
    ring(d, 512, 512, 262)
    box(d, 476, 372, 548, 652, 36)


def g_coins(d):   # a stack of three, seen a little from above
    for i, cy in enumerate((690, 560, 430)):
        rx, ry, thick = 230, 80, 60
        d.rectangle((512 - rx - W / 2, cy, 512 + rx + W / 2, cy + thick), fill=CLEAR)
        d.ellipse((512 - rx - W / 2, cy + thick - ry - W / 2, 512 + rx + W / 2, cy + thick + ry + W / 2), fill=CLEAR)
        d.ellipse((512 - rx - W / 2, cy - ry - W / 2, 512 + rx + W / 2, cy + ry + W / 2), fill=CLEAR)
        arc(d, 512, cy + thick, rx, 0, 180, ry=ry)
        line(d, [(512 - rx, cy), (512 - rx, cy + thick)])
        line(d, [(512 + rx, cy), (512 + rx, cy + thick)])
        d.ellipse((512 - rx - W / 2, cy - ry - W / 2, 512 + rx + W / 2, cy + ry + W / 2), outline=INK, width=W)
    box(d, 492, 404, 532, 456, 18, w=int(W * 0.7))


def g_watch(d):   # a stopwatch
    ring(d, 512, 560, 232)
    line(d, [(512, 328), (512, 260)])
    line(d, [(452, 250), (572, 250)])
    line(d, [(690, 370), (726, 334)])
    line(d, [(512, 560), (512, 430)])
    dot(d, 512, 560, 34)


def g_road(d):   # off the map: an open road
    line(d, [(436, 250), (262, 790)])
    line(d, [(588, 250), (762, 790)])
    for y0, y1 in ((300, 370), (460, 560), (650, 780)):
        line(d, [(512, y0), (512, y1)], w=int(W * 0.8))


def _pswitch(d):
    box(d, 336, 330, 688, 660, 130)
    erase_box(d, 266, 640, 758, 770, 34)
    box(d, 266, 640, 758, 770, 34)
    text(d, (512, 488), "P", 240)


def g_pswitch(d):
    _pswitch(d)


def g_pswitch_check(d):
    _pswitch(d)
    erase_circle(d, 724, 300, 96)
    ring(d, 724, 300, 96)
    line(d, [(682, 302), (714, 336), (770, 266)], w=int(W * 0.85))


def g_panel(d):   # a ? Panel
    box(d, 262, 262, 762, 762, 64)
    for x, y in ((322, 322), (702, 322), (322, 702), (702, 702)):
        dot(d, x, y, 20)
    text(d, (512, 520), "?", 330)


def g_medal(d):
    line(d, [(392, 250), (472, 470)])
    line(d, [(632, 250), (552, 470)])
    erase_circle(d, 512, 640, 168)
    ring(d, 512, 640, 168)
    star(d, 512, 646, 82, w=int(W * 0.8))


def g_film(d):   # the ending
    box(d, 252, 310, 772, 714, 44)
    for y in (380, 644):
        for x in range(312, 732, 84):
            d.rounded_rectangle((x, y - 18, x + 40, y + 18), radius=10, fill=INK)
    line(d, [(462, 440), (462, 584), (584, 512)], w=int(W * 0.85), closed=True)


def _ghost(d):
    top = [(512 + 212 * math.cos(math.radians(a)), 470 + 212 * math.sin(math.radians(a))) for a in range(180, 361, 6)]
    hem = [(724, 770), (653, 710), (582, 770), (512, 710), (442, 770), (371, 710), (300, 770)]
    line(d, top + hem, closed=True)
    for x in (442, 582):
        d.ellipse((x - 30, 430, x + 30, 510), fill=INK)


def g_ghost(d):
    _ghost(d)


def g_ghost_bolt(d):
    _ghost(d)
    pts = [(760, 170), (668, 340), (736, 340), (690, 480), (820, 290), (750, 290), (806, 170)]
    d.polygon(pts, fill=CLEAR)
    m, md = mask(); md.polygon(pts, fill=255)
    d.bitmap((0, 0), m.filter(ImageFilter.MaxFilter(25)), fill=CLEAR)
    line(d, pts, w=int(W * 0.75), closed=True)


def g_gear(d):
    pts = []
    for i in range(8):
        base = i * 45
        for a, r in ((-14, 220), (-9, 268), (9, 268), (14, 220)):
            t = math.radians(base + a)
            pts.append((512 + r * math.cos(t), 512 + r * math.sin(t)))
    line(d, pts, closed=True)
    ring(d, 512, 512, 84)


def g_shirt(d):
    line(d, [(400, 260), (300, 290), (200, 420), (300, 480), (346, 432), (346, 780), (678, 780), (678, 432),
             (724, 480), (824, 420), (724, 290), (624, 260), (590, 320), (434, 320)], closed=True)


def g_spark(d):   # an ultra mini-turbo: a four-pointed spark
    line(d, star_pts(492, 532, 280, inner=0.22, points=4), closed=True)
    line(d, star_pts(746, 278, 86, inner=0.25, points=4), w=int(W * 0.75), closed=True)


def g_drift(d):   # a long slide round a bend
    end = 410
    arc(d, 512, 512, 250, 150, end)
    t = math.radians(end)
    ex, ey = 512 + 250 * math.cos(t), 512 + 250 * math.sin(t)
    tx, ty = -math.sin(t), math.cos(t)                 # the way the arc runs at its end
    head = []
    for side in (-1, 1):
        a = math.atan2(ty, tx) + math.pi + side * math.radians(40)
        head.append((ex + 120 * math.cos(a), ey + 120 * math.sin(a)))
    line(d, [head[0], (ex, ey), head[1]])
    arc(d, 512, 512, 140, 170, 330, w=int(W * 0.8), fill=DIM)


def g_star(d):
    star(d, 512, 540, 300)


def g_cloud(d):   # Lakitu's cloud
    m, md = mask()
    for x, y, r in ((380, 560, 140), (530, 470, 180), (670, 570, 130)):
        md.ellipse((x - r, y - r, x + r, y + r), fill=255)
    md.rounded_rectangle((240, 560, 800, 700), radius=70, fill=255)
    outline_of(d, m)
    for x in (470, 590):
        d.ellipse((x - 22, 520, x + 22, 584), fill=INK)


def g_wing(d):   # a glider
    arc(d, 512, 470, 280, 195, 345, ry=170)
    arc(d, 512, 470, 280, 200, 340, ry=100, w=int(W * 0.7))
    for x in (250, 512, 774):
        line(d, [(x, 430 if x != 512 else 330), (512, 740)], w=int(W * 0.6))
    box(d, 476, 730, 548, 790, 16)


def g_wave(d):
    for y, f in ((420, INK), (600, DIM)):
        line(d, [(x, y + 56 * math.sin((x - 250) / 84)) for x in range(250, 780, 8)], fill=f)


def g_balloon(d):
    d.ellipse((512 - 170 - W / 2, 220 - W / 2, 512 + 170 + W / 2, 610 + W / 2), outline=INK, width=W)
    line(d, [(484, 650), (540, 650), (512, 614)], w=int(W * 0.8), closed=True)
    line(d, bezier((512, 660), (460, 720), (570, 760), (512, 820)), w=int(W * 0.6))


def _banana_pts(cx, cy, scale, turn):
    """A banana's outline: the lower arc of one circle and the flatter arc of a bigger one above it, meeting in a point
    on the left and cut off blunt on the right, where a short stem carries on the curve. Tilted by `turn` degrees and
    centred on (cx, cy). Returns the outline and the stem."""
    R, c = 1.749, -1.199      # the inner circle: through the left tip (165 degrees on the outer one) and (0, 0.55)
    outer = [(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in [165 - 125 * i / 40 for i in range(41)]]
    inner = [(R * math.cos(math.radians(a)), c + R * math.sin(math.radians(a)))
             for a in [67.8 + 55.7 * i / 30 for i in range(30)]]
    end = ((outer[-1][0] + inner[0][0]) / 2, (outer[-1][1] + inner[0][1]) / 2)
    along = (math.sin(math.radians(40)), -math.cos(math.radians(40)))      # the outer curve's way at the cut
    stem = [end, (end[0] + 0.24 * along[0], end[1] + 0.24 * along[1])]
    t = math.radians(turn)

    def turned(pts):
        return [(300 * scale * (x * math.cos(t) - y * math.sin(t)), 300 * scale * (x * math.sin(t) + y * math.cos(t)))
                for x, y in pts]
    body, stem = turned(outer + inner), turned(stem)
    xs, ys = [x for x, _ in body + stem], [y for _, y in body + stem]
    dx, dy = cx - (min(xs) + max(xs)) / 2, cy - (min(ys) + max(ys)) / 2     # centred on (cx, cy)
    return [(x + dx, y + dy) for x, y in body], [(x + dx, y + dy) for x, y in stem]


def _banana(d, cx, cy, scale=1.0, turn=-18, fill=INK):
    body, stem = _banana_pts(cx, cy, scale, turn)
    d.polygon(body, fill=CLEAR)                       # hide whatever is behind it
    line(d, body, w=W * 2, fill=CLEAR, closed=True)
    line(d, stem, w=W * 2, fill=CLEAR)
    w = int(W * min(1.0, 0.25 + scale))
    line(d, body, w=w, fill=fill, closed=True)
    line(d, stem, w=int(w * 0.8), fill=fill)


def g_banana(d):
    _banana(d, 512, 512, 1.15, -22)


def g_bananas(d):   # a bunch: one banana in front of another
    _banana(d, 560, 430, 0.95, -30, DIM)
    _banana(d, 490, 580, 1.0, -12)


def g_bone(d):   # a fossil
    m, md = mask()
    md.rounded_rectangle((300, 462, 724, 562), radius=40, fill=255)
    for x in (300, 724):
        for y in (436, 588):
            md.ellipse((x - 82, y - 82, x + 82, y + 82), fill=255)
    m = m.rotate(-30, resample=Image.BICUBIC, center=(512, 512))
    outline_of(d, m)


def g_chest(d):
    box(d, 252, 300, 772, 560, 110)
    erase_box(d, 252, 480, 772, 760, 30)
    box(d, 252, 480, 772, 760, 30)
    erase_box(d, 464, 430, 560, 580, 22)
    box(d, 464, 430, 560, 580, 22, w=int(W * 0.85))


def g_fist(d):   # a skill learned: level up
    ring(d, 512, 512, 262)
    line(d, [(512, 660), (512, 372)])
    line(d, [(400, 484), (512, 372), (624, 484)])


def g_layers(d):   # the layers of the world
    line(d, [(512, 270), (780, 410), (512, 550), (244, 410)], closed=True)
    line(d, [(244, 520), (512, 660), (780, 520)])
    line(d, [(244, 630), (512, 770), (780, 630)], fill=DIM)


def g_core(d):   # the planet's core
    ring(d, 512, 512, 262, fill=DIM)
    ring(d, 512, 512, 160)
    dot(d, 512, 512, 66)


def g_palm(d):   # DK Island
    arc(d, 512, 800, 290, 200, 340, ry=120)
    line(d, bezier((520, 720), (500, 600), (520, 470), (560, 360)))
    top = (560, 350)
    for a, length in ((-175, 250), (-130, 220), (-55, 220), (-8, 250)):
        t = math.radians(a)
        pts = [(top[0] + length * k * math.cos(t), top[1] + length * k * math.sin(t) + 150 * k * k)
               for k in [i / 12 for i in range(13)]]
        line(d, pts, w=int(W * 0.9))


def g_emerald(d):
    pts = [(372, 280), (652, 280), (782, 430), (512, 782), (242, 430)]
    line(d, pts, closed=True)
    fw = int(W * 0.6)
    line(d, [(242, 430), (782, 430)], w=fw)
    line(d, [(372, 280), (432, 430), (512, 280), (592, 430), (652, 280)], w=fw)
    line(d, [(432, 430), (512, 782), (592, 430)], w=fw)


# Ocarina of Time

def _blade(d, x0, y0, x1, y1, guard=110, w=W):   # a sword from the pommel (x0, y0) to the tip (x1, y1)
    dx, dy = x1 - x0, y1 - y0
    n = math.hypot(dx, dy); ux, uy = dx / n, dy / n
    gx, gy = x0 + ux * n * 0.24, y0 + uy * n * 0.24
    line(d, [(gx, gy), (x1, y1)], w=w)
    line(d, [(gx - uy * guard, gy + ux * guard), (gx + uy * guard, gy - ux * guard)], w=w)
    line(d, [(x0, y0), (gx, gy)], w=w)
    dot(d, x0, y0, w * 0.9)


def g_sword(d):
    _blade(d, 300, 724, 740, 284)


def g_swords(d):   # two crossed swords
    _blade(d, 300, 740, 724, 290, guard=90)
    _blade(d, 724, 740, 300, 290, guard=90)


def g_ocarina(d):
    m, md = mask()
    md.ellipse((232, 352, 712, 692), fill=255)
    md.rounded_rectangle((660, 462, 820, 562), radius=46, fill=255)
    outline_of(d, m)
    for x, y in ((360, 480), (460, 440), (560, 480), (460, 580), (360, 590)):
        dot(d, x, y, 28)


def g_medallion(d):
    ring(d, 512, 512, 250)
    ring(d, 512, 512, 150, w=int(W * 0.7))
    for a in range(0, 360, 60):
        t = math.radians(a - 90)
        line(d, [(512 + 150 * math.cos(t), 512 + 150 * math.sin(t)), (512 + 250 * math.cos(t), 512 + 250 * math.sin(t))],
             w=int(W * 0.6))


def g_triforce(d):
    h = 440 * math.sqrt(3) / 2
    top, left, right = (512, 512 - h * 0.62), (292, 512 + h * 0.38), (732, 512 + h * 0.38)
    mid = lambda a, b: ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    line(d, [top, right, left], closed=True)
    line(d, [mid(top, left), mid(top, right), mid(left, right)], closed=True)


def _note(d, x, y, stem=300):
    d.ellipse((x - 74, y - 54, x + 74, y + 54), fill=INK)
    line(d, [(x + 58, y - 10), (x + 58, y - stem)])
    return x + 58, y - stem


def g_note(d):
    tx, ty = _note(d, 460, 690)
    line(d, [(tx, ty), (tx + 120, ty + 110)])


def g_notes(d):
    a = _note(d, 360, 700)
    b = _note(d, 640, 640)
    line(d, [a, b])
    line(d, [(a[0], a[1] + 80), (b[0], b[1] + 80)])


def g_skull(d):   # a Gold Skulltula Token
    m, md = mask()
    md.ellipse((302, 262, 722, 642), fill=255)
    md.rounded_rectangle((392, 560, 632, 760), radius=40, fill=255)
    outline_of(d, m)
    for x in (430, 594):
        d.ellipse((x - 52, 440, x + 52, 540), fill=INK)
    for x in (462, 562):
        line(d, [(x, 660), (x, 730)], w=int(W * 0.6))


def _heart(md, cx=512, cy=520, r=150):
    md.ellipse((cx - 2 * r, cy - r * 1.4, cx, cy + r * 0.6), fill=255)
    md.ellipse((cx, cy - r * 1.4, cx + 2 * r, cy + r * 0.6), fill=255)
    md.polygon([(cx - 2 * r + 12, cy - r * 0.2), (cx, cy + r * 1.75), (cx + 2 * r - 12, cy - r * 0.2)], fill=255)


def g_heart(d):
    m, md = mask(); _heart(md); outline_of(d, m)


def g_heart_piece(d):
    m, md = mask(); _heart(md); outline_of(d, m, fill=DIM)
    q, qd = mask(); _heart(qd)
    qd.rectangle((0, 0, 512, 1024), fill=0); qd.rectangle((0, 470, 1024, 1024), fill=0)
    d.bitmap((0, 0), q, fill=INK)


def g_lantern(d):   # a Poe's lantern
    line(d, [(512, 250), (512, 320)])
    arc(d, 512, 320, 60, 180, 360)
    box(d, 392, 380, 632, 700, 60)
    line(d, [(372, 380), (652, 380)]); line(d, [(372, 700), (652, 700)])
    m, md = mask()
    md.polygon([(512, 450), (566, 560), (512, 640), (458, 560)], fill=255)
    d.bitmap((0, 0), m, fill=INK)


def g_fairy(d):   # a fairy: a body and two pairs of wings
    line(d, [(512, 400), (512, 700)])
    dot(d, 512, 350, 58)
    for sx in (-1, 1):
        for cy, rx, ry, turn in ((440, 150, 90, -30), (610, 110, 70, 30)):
            m, md = mask()
            md.ellipse((512 - rx, cy - ry, 512 + rx, cy + ry), fill=255)
            m = m.rotate(sx * turn, resample=Image.BICUBIC, center=(512, cy))
            m = m.transform(m.size, Image.AFFINE, (1, 0, -sx * (rx + 75), 0, 1, 0), resample=Image.BICUBIC)
            outline_of(d, m, w=int(W * 0.8))


def g_bottle(d):
    m, md = mask()
    md.rounded_rectangle((440, 250, 584, 330), radius=20, fill=255)
    md.rectangle((462, 320, 562, 440), fill=255)
    md.rounded_rectangle((332, 420, 692, 780), radius=150, fill=255)
    outline_of(d, m)
    line(d, [(380, 600), (644, 600)], w=int(W * 0.6), fill=DIM)


def g_mask(d):
    m, md = mask()
    md.ellipse((282, 262, 742, 782), fill=255)
    outline_of(d, m)
    for x in (420, 604):
        d.ellipse((x - 60, 430, x + 60, 510), fill=INK)
    arc(d, 512, 600, 110, 20, 160)


def g_horseshoe(d):
    arc(d, 512, 470, 210, 160, 380)
    for a in (160, 20):
        t = math.radians(a)
        x, y = 512 + 210 * math.cos(t), 470 + 210 * math.sin(t)
        line(d, [(x, y), (x + (-30 if a > 90 else 30), y + 230)])
    for a in (200, 250, 290, 340):
        t = math.radians(a)
        dot(d, 512 + 210 * math.cos(t), 470 + 210 * math.sin(t), 18, fill=CLEAR)


def g_card(d):
    box(d, 252, 330, 772, 694, 50)
    ring(d, 400, 500, 62, w=int(W * 0.7))
    line(d, [(520, 470), (700, 470)], w=int(W * 0.6))
    line(d, [(520, 560), (660, 560)], w=int(W * 0.6))
    line(d, [(320, 630), (700, 630)], w=int(W * 0.6), fill=DIM)


def g_shield(d):
    pts = [(512, 260), (742, 330)] + bezier((742, 330), (742, 560), (640, 700), (512, 780))[1:] + \
        bezier((512, 780), (384, 700), (282, 560), (282, 330))[1:]
    line(d, pts, closed=True)
    line(d, [(512, 360), (512, 680)], w=int(W * 0.6), fill=DIM)
    line(d, [(372, 470), (652, 470)], w=int(W * 0.6), fill=DIM)


def g_hookshot(d):
    line(d, [(300, 724), (520, 504)])
    for i in range(3):
        x, y = 560 + i * 60, 464 - i * 60
        ring(d, x, y, 22, w=int(W * 0.55))
    line(d, [(700, 324), (760, 264)])
    line(d, [(690, 254), (760, 264), (770, 334)])


def g_arrow(d):
    line(d, [(290, 734), (712, 312)])
    line(d, [(612, 300), (734, 290), (724, 412)])
    for k in (0, 60):
        line(d, [(330 + k, 694 - k), (290 + k, 600 - k)], w=int(W * 0.7))
        line(d, [(330 + k, 694 - k), (424 + k, 734 - k)], w=int(W * 0.7))


def g_glove(d):
    m, md = mask()
    md.rounded_rectangle((352, 420, 652, 700), radius=80, fill=255)
    for i, x in enumerate((360, 432, 504, 576)):
        md.rounded_rectangle((x, 270 + (40 if i in (0, 3) else 0), x + 64, 500), radius=32, fill=255)
    md.rounded_rectangle((600, 460, 720, 540), radius=40, fill=255)
    md.rectangle((382, 690, 622, 780), fill=255)
    outline_of(d, m)
    line(d, [(382, 690), (622, 690)], w=int(W * 0.6))


def g_scale(d):   # the Golden Scale: a fish scale
    pts = [(512, 260)] + bezier((512, 260), (760, 420), (720, 700), (512, 780))[1:] + \
        bezier((512, 780), (304, 700), (264, 420), (512, 260))[1:]
    line(d, pts, closed=True)
    for y in (480, 600):
        arc(d, 512, y - 120, 140, 40, 140, w=int(W * 0.6), fill=DIM)


def g_purse(d):   # a wallet
    m, md = mask()
    md.ellipse((282, 400, 742, 780), fill=255)
    md.polygon([(412, 420), (380, 300), (512, 360), (644, 300), (612, 420)], fill=255)
    outline_of(d, m)
    line(d, [(400, 430), (624, 430)], w=int(W * 0.7))
    m2, md2 = mask(); md2.polygon([(512, 520), (570, 620), (454, 620)], fill=255)
    d.bitmap((0, 0), m2, fill=INK)


def g_pillars(d):   # the Hall of Fallen Foes
    line(d, [(252, 360), (512, 250), (772, 360)], closed=True)
    for x in (330, 450, 574, 694):
        line(d, [(x, 420), (x, 680)])
    line(d, [(252, 760), (772, 760)])


def g_book(d):   # a field guide to monsters
    line(d, [(512, 330), (512, 740)])
    for sx in (-1, 1):
        line(d, [(512, 330)] + bezier((512, 330), (512 + sx * 90, 280), (512 + sx * 200, 280), (512 + sx * 260, 300))[1:] +
             [(512 + sx * 260, 700)] + bezier((512 + sx * 260, 700), (512 + sx * 200, 680), (512 + sx * 90, 690), (512, 740))[1:])


def tile(tier: str) -> Image.Image:
    img = Image.new("RGBA", (S, S))
    px = img.load()
    for y in range(S):
        t = y / (S - 1)
        c = tuple(round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)) + (255,)
        for x in range(S):
            px[x, y] = c
    glow = Image.new("RGBA", (S, S), CLEAR)
    ImageDraw.Draw(glow).ellipse((150, 120, 874, 844), fill=GLOW[tier])
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(150)))
    return img


def draw(glyph: str, tier: str) -> Image.Image:
    img = tile(tier)
    layer = Image.new("RGBA", (S, S), CLEAR)
    globals()["g_" + glyph](ImageDraw.Draw(layer))
    img.alpha_composite(layer)
    return img.resize((OUT, OUT), Image.LANCZOS)


def main() -> None:
    for title in switch.TITLES:
        folder = ROOT / "achtrack" / "assets" / "switch" / title.key
        folder.mkdir(parents=True, exist_ok=True)
        for goal in title.goals:
            glyph, tier = GOALS[goal.name]
            draw(glyph, tier).save(folder / f"{goal.name}.png", optimize=True)
        print(title.name, len(title.goals), "icons in", folder.relative_to(ROOT))


if __name__ == "__main__":
    main()
