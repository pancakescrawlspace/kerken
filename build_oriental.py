#!/usr/bin/env python3
"""Build the map of the six Oriental Orthodox churches and their home countries (in Dutch).

Usage:
    python3 build_oriental.py            # writes oriental-orthodox-nl.png and oriental-orthodox-nl.pdf, and
                                         # oriental-orthodox-nl-kaart.png (only the map, for screens)
    python3 build_oriental.py --no-pdf   # PNG only
    python3 build_oriental.py --svg DIR  # also save the SVG

Data: data/oriental-orthodox.csv (one row per country) and
data/countries-region.geojson (Natural Earth 1:50m country outlines).

Each country is coloured by its church, and the fill style shows how large the
church is in that country:
  meerderheid  solid fill    the church is followed by a majority of the population
  grootste     hatched       largest religious community, but no majority
  minderheid   light tint    small minority
"""
import argparse
import datetime as dt
import json
import math
import subprocess
import sys
from pathlib import Path

import render
from build import MONTHS, read_csv, BuildError
from render import text, SERIF, SANS, INK, MUTED, BG, SEA, BORDER

ROOT = Path(__file__).resolve().parent
DATA_CSV = ROOT / "data" / "oriental-orthodox.csv"
COUNTRIES = ROOT / "data" / "countries-region.geojson"
OUT = ROOT / "oriental-orthodox-nl.png"
OUT_MAP = ROOT / "oriental-orthodox-nl-kaart.png"   # only the map, for screens
MAP_PNG_WIDTH = 3840                                  # px; 4K width, sharp on large and high-DPI screens

# Layout on a 2400 x 1700 grid, scaled by S (same approach as render.py).
S = 1.5
GW, GH = 2400, 1700
W, H = int(GW * S), int(GH * S)
MAP = (40, 190, 2320, 1000)              # map frame on the grid: x, y, w, h
EXTENT = ((8, 97), (-3, 47))             # lon range, lat range that must be visible
CENTER = (52.0, 24.0)                    # projection centre (lon, lat)
POSTER_WIDTH_MM = 1200
LAND = "#f1f0ec"
CATEGORIES = {
    "meerderheid": "Meerderheid van de bevolking",
    "grootste": "Grootste geloofsgemeenschap, maar geen meerderheid",
    "minderheid": "Kleine minderheid",
}
TEXT = {
    "title": "De Oriëntaals-Orthodoxe kerken en hun thuislanden",
    "subtitle": "Welk deel van de bevolking hoort bij de kerk: een meerderheid, de grootste geloofsgemeenschap "
                "of een kleine minderheid? · {updated}",
    "chart": "Aandeel van de bevolking dat tot de kerk behoort",
    "majority_line": "meerderheid",
    "legend": "Vulling van het land",
    "legend_note": "De kleur staat voor de kerk; zie het label bij elk land.",
    "notes_title": "Bronnen en toelichting",
    "notes": [
        "Armenië: volkstelling 2022. Ethiopië: volkstelling 2007 (islam 34%, protestanten 19%).",
        "Eritrea: geen volkstelling; schattingen lopen uiteen van ca. 40% (World Religion",
        "Database, 2025) tot ruim 50%. Of de kerk groter is dan de islam, is onzeker.",
        "Egypte: schattingen 5–10%; de kerk zelf noemt ca. 12%.",
        "Syrië: alle christenen samen nog ca. 2–3% (2022–2025). De Syrisch-Orthodoxe Kerk heeft",
        "ook veel leden in Irak, Turkije en India (de Jakobitische kerk in Kerala).",
        "India: ca. 2,5 miljoen leden, vooral in Kerala; alle christenen samen 2,3% (volkstelling 2011).",
    ],
}


# ------------------------------------------------------------------ projection
class EqualArea:
    """Lambert azimuthal equal-area projection, fitted to EXTENT inside the map frame."""

    def __init__(self):
        self.l0, self.p0 = map(math.radians, CENTER)
        (lo0, lo1), (la0, la1) = EXTENT
        edge = [(lo, la0) for lo in range(lo0, lo1 + 1)] + [(lo, la1) for lo in range(lo0, lo1 + 1)] + \
               [(lo0, la) for la in range(la0, la1 + 1)] + [(lo1, la) for la in range(la0, la1 + 1)]
        pts = [self.raw(lo, la) for lo, la in edge]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        x, y, w, h = (v * S for v in MAP)
        self.k = min(w / (max(xs) - min(xs)), h / (max(ys) - min(ys)))
        self.ox = x + w / 2 - self.k * (max(xs) + min(xs)) / 2
        self.oy = y + h / 2 + self.k * (max(ys) + min(ys)) / 2

    def raw(self, lon, lat):
        l, p = math.radians(lon) - self.l0, math.radians(lat)
        c = 1 + math.sin(self.p0) * math.sin(p) + math.cos(self.p0) * math.cos(p) * math.cos(l)
        k = math.sqrt(2 / c)
        return k * math.cos(p) * math.sin(l), k * (math.cos(self.p0) * math.sin(p) - math.sin(self.p0) * math.cos(p) * math.cos(l))

    def __call__(self, lon, lat):
        x, y = self.raw(lon, lat)
        return self.ox + self.k * x, self.oy - self.k * y


# ------------------------------------------------------------------ helpers
def tint(hex_color, amount):
    """Mix a colour with white; amount 0 = white, 1 = the colour itself."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    mix = lambda c: round(255 + (c - 255) * amount)
    return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"


def polygons(feature):
    return feature["geometry"]["coordinates"]


def path_d(feature, proj):
    d = []
    for poly in polygons(feature):
        for ring in poly:
            d.append("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in (proj(lo, la) for lo, la in ring)) + "Z")
    return "".join(d)


def ring_area_centroid(ring, proj):
    pts = [proj(lo, la) for lo, la in ring]
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        f = x0 * y1 - x1 * y0
        a += f; cx += (x0 + x1) * f; cy += (y0 + y1) * f
    if abs(a) < 1e-9:
        return 0, pts[0]
    return abs(a / 2), (cx / (3 * a), cy / (3 * a))


def main_centroid(feature, proj):
    return max((ring_area_centroid(poly[0], proj) for poly in polygons(feature)), key=lambda t: t[0])[1]


def inside(pt, feature, proj):
    x, y = pt
    for poly in polygons(feature):
        ring = [proj(lo, la) for lo, la in poly[0]]
        c = False
        for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
            if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
                c = not c
        if c:
            return True
    return False


def fill_for(row, pattern_id):
    c = row["color"]
    if row["category"] == "meerderheid":
        return c, render_stroke(c), 1.2
    if row["category"] == "grootste":
        return f"url(#{pattern_id})", render_stroke(c), 1.2
    return tint(c, 0.22), c, 2.2


def render_stroke(c):
    # a slightly darker edge in the church colour
    r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
    return f"#{int(r * .7):02x}{int(g * .7):02x}{int(b * .7):02x}"


def hatch(pid, color, size=12):
    s = size * S
    return (f'<pattern id="{pid}" patternUnits="userSpaceOnUse" width="{s:.1f}" height="{s:.1f}" '
            f'patternTransform="rotate(45)"><rect width="{s:.1f}" height="{s:.1f}" fill="{tint(color, 0.3)}"/>'
            f'<rect width="{s / 2:.1f}" height="{s:.1f}" fill="{color}"/></pattern>')


def nl_pct(v):
    return f"{v:g}".replace(".", ",") + "%"


# ------------------------------------------------------------------ drawing
def render_svg(rows, countries, updated):
    proj = EqualArea()
    by_iso = {f["properties"]["iso"]: f for f in countries["features"]}
    missing = [r["iso"] for r in rows if r["iso"] not in by_iso]
    if missing:
        raise BuildError(f"no outline for country code(s) {', '.join(missing)} in {COUNTRIES.name}")

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
           f'<rect width="{W}" height="{H}" fill="{BG}"/>']
    defs = ['<defs>', f'<clipPath id="frame"><rect x="{MAP[0] * S}" y="{MAP[1] * S}" width="{MAP[2] * S}" '
                      f'height="{MAP[3] * S}" rx="{14 * S}"/></clipPath>']
    defs += [hatch(f"h-{r['iso']}", r["color"]) for r in rows if r["category"] == "grootste"]
    defs.append(hatch("h-legend", MUTED))
    defs.append('</defs>')
    out += defs

    # Map: sea, all countries in neutral grey, then the six highlighted countries
    x, y, w, h = (v * S for v in MAP)
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{14 * S}" fill="{SEA}"/>')
    out.append('<g clip-path="url(#frame)">')
    out.append(f'<g fill="{LAND}" stroke="{BORDER}" stroke-width="{1.0 * S:.1f}" stroke-linejoin="round" fill-rule="evenodd">')
    for f in countries["features"]:
        out.append(f'<path d="{path_d(f, proj)}"/>')
    out.append("</g>")
    for r in rows:
        fill, stroke, sw = fill_for(r, f"h-{r['iso']}")
        out.append(f'<path d="{path_d(by_iso[r["iso"]], proj)}" fill="{fill}" stroke="{stroke}" '
                   f'stroke-width="{sw * S:.1f}" stroke-linejoin="round" fill-rule="evenodd"/>')

    # Labels: country, share, church; with a leader line when placed outside the country
    halo = f'stroke="{LAND}" stroke-width="{6 * S:.1f}" stroke-linejoin="round" paint-order="stroke"'
    for r in rows:
        f = by_iso[r["iso"]]
        lx, ly = proj(float(r["label_lon"]), float(r["label_lat"]))
        sizes = (26 * S, 40 * S, 19 * S)
        lines = (r["country"], r["share_text"], r["church"])
        width = max(len(t) * s * 0.56 for t, s in zip(lines, sizes))
        top, bottom = ly - 52 * S, ly + 44 * S
        if r.get("leader", "auto") != "no" and not inside((lx, ly), f, proj):
            cx, cy = main_centroid(f, proj)
            sx = min(max(cx, lx - width / 2 - 8 * S), lx + width / 2 + 8 * S)
            sy = min(max(cy, top - 8 * S), bottom + 8 * S)
            out.append(f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{cx:.1f}" y2="{cy:.1f}" stroke="{INK}" '
                       f'stroke-width="{1.6 * S:.1f}"/><circle cx="{cx:.1f}" cy="{cy:.1f}" r="{3.5 * S:.1f}" fill="{INK}"/>')
        out.append(text(lx, ly - 24 * S, r["country"], f"{sizes[0]:.0f}", INK, weight=700, anchor="middle", extra=halo))
        out.append(text(lx, ly + 16 * S, r["share_text"], f"{sizes[1]:.0f}", INK, weight=700,
                        anchor="middle", extra=halo))
        out.append(text(lx, ly + 42 * S, r["church"], f"{sizes[2]:.0f}", MUTED, weight=500, anchor="middle", extra=halo))
    out.append("</g>")

    # Everything below is laid out on the 2400 x 1700 grid
    g = [f'<g transform="scale({S})">']
    g.append(text(40, 92, TEXT["title"], 58, family=SERIF, weight=600))
    g.append(text(40, 140, TEXT["subtitle"].format(updated=updated), 24, MUTED))

    # Bar chart
    top = MAP[1] + MAP[3] + 60
    g.append(text(40, top, TEXT["chart"], 26, INK, weight=600))
    x0, x1 = 250, 1180
    sx = lambda v: x0 + (x1 - x0) * v / 100
    row_h, bar_h = 54, 28
    y = top + 34
    chart_top = y
    rows_sorted = sorted(rows, key=lambda r: -float(r["high"]))
    for r in rows_sorted:
        lo, hi = float(r["low"]), float(r["high"])
        cy = y + row_h / 2
        g.append(text(x0 - 18, cy + 8, r["country"], 22, INK, weight=600, anchor="end"))
        g.append(f'<rect x="{x0}" y="{cy - bar_h / 2}" width="{x1 - x0}" height="{bar_h}" fill="#e7e9ed"/>')
        if lo > 0:
            g.append(f'<rect x="{x0}" y="{cy - bar_h / 2}" width="{sx(lo) - x0:.1f}" height="{bar_h}" fill="{r["color"]}"/>')
        if hi > lo:
            g.append(f'<rect x="{sx(lo):.1f}" y="{cy - bar_h / 2}" width="{sx(hi) - sx(lo):.1f}" height="{bar_h}" '
                     f'fill="{tint(r["color"], 0.4)}"/>')
        g.append(text(max(sx(hi), x0) + 12, cy + 8, r["share_text"], 22, INK, weight=600))
        y += row_h
    chart_bottom = y
    xm = sx(50)
    g.append(f'<line x1="{xm}" y1="{chart_top - 6}" x2="{xm}" y2="{chart_bottom + 4}" stroke="{INK}" '
             f'stroke-width="2" stroke-dasharray="7 6"/>')
    for v in (0, 25, 50, 75, 100):
        g.append(text(sx(v), chart_bottom + 30, nl_pct(v), 18, MUTED, anchor="middle"))
    g.append(text(xm + 8, chart_top - 12, f"50%: {TEXT['majority_line']}", 18, INK, weight=600))

    # Legend for the fill styles
    lx = 1340
    g.append(text(lx, top, TEXT["legend"], 26, INK, weight=600))
    ly = top + 30
    swatches = {
        "meerderheid": f'fill="{MUTED}" stroke="{render_stroke(MUTED)}" stroke-width="1.5"',
        "grootste": f'fill="url(#h-legend-grid)" stroke="{render_stroke(MUTED)}" stroke-width="1.5"',
        "minderheid": f'fill="{tint(MUTED, 0.22)}" stroke="{MUTED}" stroke-width="2.2"',
    }
    for key, label in CATEGORIES.items():
        g.append(f'<rect x="{lx}" y="{ly}" width="56" height="34" rx="4" {swatches[key]}/>')
        g.append(text(lx + 72, ly + 25, label, 22, INK))
        ly += 48
    g.append(text(lx, ly + 18, TEXT["legend_note"], 19, MUTED))

    # Sources and notes
    ny = ly + 64
    g.append(text(lx, ny, TEXT["notes_title"], 19, INK, weight=600))
    for i, line in enumerate(TEXT["notes"]):
        g.append(text(lx, ny + 28 + 26 * i, line, 17, MUTED))
    g.append("</g>")
    # the legend hatch lives on the grid, so it needs its own (unscaled) pattern
    out.insert(len(defs), hatch("h-legend-grid", MUTED, size=12 / S))
    out += g
    out.append("</svg>")
    return "\n".join(out)


def map_only(svg):
    """The same drawing, cropped to the map frame (labels included, title/chart/legend left out)."""
    x, y, w, h = (v * S for v in MAP)
    corner = f'rx="{14 * S}"'
    if svg.count(corner) != 2:
        raise BuildError("could not find the map frame corners to square them off")
    svg = svg.replace(corner, 'rx="0"')
    root = f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
    return svg.replace(root, f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:g}" height="{h:g}" '
                             f'viewBox="{x:g} {y:g} {w:g} {h:g}">', 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-pdf", action="store_true", help="skip the PDF")
    ap.add_argument("--svg", metavar="DIR", help="also save the SVG in this folder")
    args = ap.parse_args()

    rows = read_csv(DATA_CSV)
    errors = []
    for i, r in enumerate(rows, start=2):
        if r.get("leader", "auto") not in ("auto", "no"):
            errors.append(f"oriental-orthodox.csv line {i}: leader must be auto or no")
        if r["category"] not in CATEGORIES:
            errors.append(f"oriental-orthodox.csv line {i}: category must be one of {', '.join(CATEGORIES)}")
        try:
            lo, hi = float(r["low"]), float(r["high"])
            float(r["label_lon"]), float(r["label_lat"])
            if not 0 <= lo <= hi <= 100:
                errors.append(f"oriental-orthodox.csv line {i}: need 0 <= low <= high <= 100")
        except ValueError:
            errors.append(f"oriental-orthodox.csv line {i}: low, high, label_lon and label_lat must be numbers")
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1

    countries = json.loads(COUNTRIES.read_text(encoding="utf-8"))
    when = dt.datetime.fromtimestamp(DATA_CSV.stat().st_mtime)
    svg = render_svg(rows, countries, f"{MONTHS['nl'][when.month - 1]} {when.year}")
    if args.svg:
        p = Path(args.svg) / OUT.with_suffix(".svg").name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(svg, encoding="utf-8")
        print(f"Wrote {p}")
    try:
        render._convert(svg, ["--format=png", f"--width={W}", "--output", str(OUT)])
        print(f"Wrote {OUT.name} ({OUT.stat().st_size // 1024} KB, {W}x{H} px)")
        render._convert(map_only(svg), ["--format=png", f"--width={MAP_PNG_WIDTH}", "--output", str(OUT_MAP)])
        mh = math.ceil(MAP_PNG_WIDTH * MAP[3] / MAP[2])
        print(f"Wrote {OUT_MAP.name} ({OUT_MAP.stat().st_size // 1024} KB, {MAP_PNG_WIDTH}x{mh} px)")
        if not args.no_pdf:
            pdf = OUT.with_suffix(".pdf")
            h_mm = POSTER_WIDTH_MM * H / W
            render._convert(svg, ["--format=pdf", f"--width={POSTER_WIDTH_MM}mm", f"--height={h_mm:g}mm",
                                  "--output", str(pdf)])
            print(f"Wrote {pdf.name} ({pdf.stat().st_size // 1024} KB, {POSTER_WIDTH_MM} x {h_mm:g} mm)")
    except (RuntimeError, subprocess.CalledProcessError) as e:
        print(f"error: could not write the map: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
