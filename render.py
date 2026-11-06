"""Draw the church map and its legend as one SVG, and convert it to PNG.

Used by build.py. Standard library only; the PNG conversion calls the
`rsvg-convert` command from librsvg (macOS: `brew install librsvg` or
`port install librsvg`; Debian/Ubuntu: `apt install librsvg2-bin`).
"""
import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

# ---------------------------------------------------------------- layout
# Title, legend and footnote are laid out on a 2400 x 2000 grid and scaled up by S.
# The map itself is drawn at full resolution, so markers can stay small relative
# to the map and dense areas (Amsterdam, Twente, Den Haag) keep room to breathe.
# To get an even sharper image, raise S; to give clusters more room, lower R.
S = 1.5                           # output scale: 2400 x 2000 grid -> 3600 x 3000 px
W, H = int(2400 * S), int(2000 * S)
MAP_X, MAP_Y = 40, 190            # map frame on the grid
MAP_W, MAP_H = 1480, 1770
PANEL_X = MAP_X + MAP_W + 50      # legend panel left edge on the grid
R = 11                            # marker radius in output pixels
GAP = 3                           # minimum gap between markers in output pixels

# ---------------------------------------------------------------- colours / type
BG = "#f3f4f6"
SEA = "#dce5ee"
LAND = "#fbfbf8"
BORDER = "#aab3c0"
INK = "#1b2230"
MUTED = "#5d6676"
SERIF = "Georgia, 'Times New Roman', serif"
SANS = "'Helvetica Neue', Helvetica, Arial, sans-serif"

# All text on the image, per language. Jurisdiction names come from data/jurisdictions.csv.
TEXT = {
    "en": {
        "title": "Orthodox Churches of the Netherlands",
        "subtitle": "{total} parishes, monasteries and communities in {jurs} jurisdictions: "
                    "{east} Eastern Orthodox and {orient} Oriental Orthodox · {updated}",
        "legend": "Jurisdictions",
        "east": "EASTERN ORTHODOX",
        "orient": "ORIENTAL ORTHODOX",
        "key": ["Eastern Orthodox", "Oriental Orthodox", "Monastery or skete", "Community without its own church"],
        "notes": [
            "Locations from the churches' own parish lists, the Benelux church directory (orthodoxconnect.be)",
            "and Kerk op de Kaart; geocoded with the PDOK address register. Where no street address is",
            "published the marker sits at the town centre. Markers that are close together have been",
            "shifted slightly so that all of them remain visible.",
        ],
        "towns": {},  # e.g. {"'s-Gravenhage": "The Hague"} to rename a town label
    },
    "nl": {
        "title": "Orthodoxe kerken in Nederland",
        "subtitle": "{total} parochies, kloosters en geloofsgemeenschappen in {jurs} jurisdicties: "
                    "{east} oosters-orthodox en {orient} oriëntaals-orthodox · {updated}",
        "legend": "Jurisdicties",
        "east": "OOSTERS-ORTHODOX",
        "orient": "ORIËNTAALS-ORTHODOX",
        "key": ["Oosters-orthodox", "Oriëntaals-orthodox", "Klooster of skite", "Gemeenschap zonder eigen kerk"],
        "notes": [
            "Locaties uit de parochielijsten van de kerken zelf, de Benelux-kerkengids (orthodoxconnect.be)",
            "en Kerk op de Kaart; gegeocodeerd met het adressenregister van PDOK. Waar geen straatadres",
            "bekend is, staat de markering in het centrum van de plaats. Markeringen die dicht bij elkaar",
            "liggen, zijn iets verschoven zodat ze allemaal zichtbaar blijven.",
        ],
        "towns": {},
    },
}

# Town labels for orientation: (name as in the address register, label, unused).
# The side of each label is chosen automatically to avoid markers.
TOWN_LABELS = [
    ("Amsterdam", "Amsterdam", "w"), ("Rotterdam", "Rotterdam", "sw"), ("'s-Gravenhage", "Den Haag", "w"),
    ("Utrecht", "Utrecht", "se"), ("Groningen", "Groningen", "n"), ("Leeuwarden", "Leeuwarden", "n"),
    ("Zwolle", "Zwolle", "ne"), ("Enschede", "Enschede", "s"), ("Arnhem", "Arnhem", "n"),
    ("Nijmegen", "Nijmegen", "s"), ("Eindhoven", "Eindhoven", "e"), ("Maastricht", "Maastricht", "w"),
    ("Breda", "Breda", "s"), ("Middelburg", "Middelburg", "s"), ("Haarlem", "Haarlem", "w"),
    ("Hengelo", "Hengelo", "n"), ("Amersfoort", "Amersfoort", "ne"),
]


def mercator(lon, lat):
    return math.radians(lon), math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


class Projection:
    def __init__(self, features, x, y, w, h, pad=30):
        xs, ys = [], []
        for ring in iter_rings(features):
            for lon, lat in ring:
                px, py = mercator(lon, lat)
                xs.append(px); ys.append(py)
        self.x0, self.x1, self.y0, self.y1 = min(xs), max(xs), min(ys), max(ys)
        self.k = min((w - 2 * pad) / (self.x1 - self.x0), (h - 2 * pad) / (self.y1 - self.y0))
        self.ox = x + (w - self.k * (self.x1 - self.x0)) / 2
        self.oy = y + (h - self.k * (self.y1 - self.y0)) / 2

    def __call__(self, lon, lat):
        px, py = mercator(lon, lat)
        return self.ox + (px - self.x0) * self.k, self.oy + (self.y1 - py) * self.k

    def km(self, lat):
        """Pixels per kilometre at a given latitude."""
        return self.k / (6371.0 * math.cos(math.radians(lat)))


def iter_rings(features):
    for f in features:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for poly in polys:
            yield from poly


def relax(points):
    """Nudge overlapping markers apart while keeping each close to its true spot.

    The shifts are small (on average about one marker radius), so no lines are
    drawn back to the true location; the footnote mentions the shift instead.

    points: list of (x, y) anchors. Returns new positions (same order).
    Deterministic, so the same data always gives the same image.
    """
    n = len(points)
    pos = [list(p) for p in points]
    # Separate exact duplicates with a tiny, index-based offset.
    for i in range(n):
        a = 2.399963 * i  # golden angle
        pos[i][0] += 0.01 * math.cos(a)
        pos[i][1] += 0.01 * math.sin(a)
    min_d = 2 * R + GAP
    for it in range(600):
        moved = False
        for i in range(n):
            xi, yi = pos[i]
            for j in range(i + 1, n):
                dx, dy = pos[j][0] - xi, pos[j][1] - yi
                if abs(dx) >= min_d or abs(dy) >= min_d:
                    continue
                d = math.hypot(dx, dy)
                if d < min_d:
                    push = (min_d - d) / 2 + 0.01
                    ux, uy = (dx / d, dy / d) if d > 1e-9 else (1.0, 0.0)
                    pos[i][0] -= ux * push; pos[i][1] -= uy * push
                    pos[j][0] += ux * push; pos[j][1] += uy * push
                    xi, yi = pos[i]
                    moved = True
        pull = 0.04 if it < 500 else 0.0  # final rounds: only resolve overlaps
        for i in range(n):
            pos[i][0] += (points[i][0] - pos[i][0]) * pull
            pos[i][1] += (points[i][1] - pos[i][1]) * pull
        if not moved and it >= 500:
            break
    return pos


def marker(x, y, color, oriental, kind, scale=1.0):
    r = R * scale
    parts = []
    hollow = kind == "community"
    fill = LAND if hollow else color
    stroke, sw = (color, 2.6 * scale) if hollow else ("#ffffff", 1.6 * scale)
    if oriental:
        s = r * 0.86
        parts.append(f'<rect x="{x - s:.1f}" y="{y - s:.1f}" width="{2 * s:.1f}" height="{2 * s:.1f}" '
                     f'transform="rotate(45 {x:.1f} {y:.1f})" fill="{fill}" stroke="{stroke}" stroke-width="{sw:.1f}"/>')
    else:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="{sw:.1f}"/>')
    if kind == "monastery":
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{3.2 * scale:.1f}" fill="#ffffff"/>')
    return "".join(parts)


def text(x, y, s, size, color=INK, family=SANS, weight=400, anchor="start", extra=""):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
            f'fill="{color}" text-anchor="{anchor}" {extra}>{escape(s)}</text>')


LABEL_SIZE = 21 * S


def place_labels(towns, proj, markers, names=None):
    """Put each town name on the side of the town with the fewest markers and labels in the way."""
    size = LABEL_SIZE
    d = R + 6 * S
    cands = []
    for step in (1.0, 1.8, 2.6):  # close to the town first, then a bit further out
        dd = d * step
        cands += [(dd, size * 0.35, "start"), (-dd, size * 0.35, "end"),
                  (0, -dd, "middle"), (0, dd + size * 0.7, "middle"),
                  (dd * 0.8, -dd * 0.6, "start"), (-dd * 0.8, -dd * 0.6, "end"),
                  (dd * 0.8, dd * 0.6 + size * 0.7, "start"), (-dd * 0.8, dd * 0.6 + size * 0.7, "end")]
    placed, out = [], []
    x0, y0, x1, y1 = MAP_X * S, MAP_Y * S, (MAP_X + MAP_W) * S, (MAP_Y + MAP_H) * S
    for reg, label, _ in TOWN_LABELS:
        if reg not in towns:
            continue
        label = (names or {}).get(reg, label)
        tx, ty = proj(towns[reg][1], towns[reg][0])
        w = len(label) * size * 0.56
        best = None
        for n, (dx, dy, anchor) in enumerate(cands):
            lx = tx + dx - (w if anchor == "end" else w / 2 if anchor == "middle" else 0)
            box = (lx - 4, ty + dy - size * 0.8, lx + w + 4, ty + dy + size * 0.25)
            score = n * 0.02  # prefer candidates close to the town
            for mx, my in markers:
                cx, cy = min(max(mx, box[0]), box[2]), min(max(my, box[1]), box[3])
                if math.hypot(mx - cx, my - cy) < R + 2:
                    score += 1
            for b in placed:
                if not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3]):
                    score += 5
            if box[0] < x0 or box[2] > x1 or box[1] < y0 or box[3] > y1:
                score += 10
            if best is None or score < best[0]:
                best = (score, box, tx + dx, ty + dy, anchor)
        _, box, lx, ly, anchor = best
        placed.append(box)
        out.append(text(lx, ly, label, f"{size:.0f}", MUTED, weight=500, anchor=anchor,
                        extra=f'stroke="{LAND}" stroke-width="{5 * S:.1f}" stroke-linejoin="round" paint-order="stroke"'))
    return out


def render_svg(records, jurs, provinces, towns, updated, lang="en"):
    """records: church dicts with lat/lon; jurs: legend rows; towns: {register name: (lat, lon)}."""
    T = TEXT[lang]
    feats = provinces["features"]
    proj = Projection(feats, MAP_X * S, MAP_Y * S, MAP_W * S, MAP_H * S)
    J = {j["k"]: j for j in jurs}
    order = {j["k"]: i for i, j in enumerate(jurs)}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
           f'<rect width="{W}" height="{H}" fill="{BG}"/>']

    grid = [f'<g transform="scale({S})">']   # title, legend and footnote on the 2400 x 2000 grid
    # Title block
    n_e = sum(1 for r in records if J[r["j"]]["fam"] == "E")
    n_o = len(records) - n_e
    grid.append(text(MAP_X, 92, T["title"], 60, family=SERIF, weight=600))
    grid.append(text(MAP_X, 142, T["subtitle"].format(total=len(records), jurs=len(jurs), east=n_e,
                                                     orient=n_o, updated=updated), 26, MUTED))

    # Map frame: sea, provinces
    out.append(f'<rect x="{MAP_X * S}" y="{MAP_Y * S}" width="{MAP_W * S}" height="{MAP_H * S}" rx="{14 * S}" fill="{SEA}"/>')
    out.append(f'<g fill="{LAND}" stroke="{BORDER}" stroke-width="{1.4 * S:.1f}" stroke-linejoin="round" fill-rule="evenodd">')
    for f in feats:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        d = []
        for poly in polys:
            for ring in poly:
                pts = [proj(lon, lat) for lon, lat in ring]
                d.append("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z")
        out.append(f'<path d="{"".join(d)}"/>')
    out.append("</g>")

    # Markers: draw Eastern then Oriental, each in legend order, so colours layer consistently.
    recs = sorted(records, key=lambda r: (order[r["j"]], r["place"], r["name"]))
    anchors = [proj(r["lon"], r["lat"]) for r in recs]
    pos = relax(anchors)

    labels = place_labels(towns, proj, pos, T["towns"])

    out.append("<g>")
    for r, (x, y) in zip(recs, pos):
        j = J[r["j"]]
        out.append(marker(x, y, j["c"], j["fam"] == "O", r["kind"], scale=R / 9.5))
    out.append("</g>")
    out.append("<g>" + "".join(labels) + "</g>")

    # Scale bar (50 km) in the lower-left of the map
    lat_mid = 52.0
    px50 = 50 * proj.km(lat_mid)
    sx, sy = (MAP_X + 40) * S, (MAP_Y + MAP_H - 50) * S
    t = 8 * S
    out.append(f'<g stroke="{INK}" stroke-width="{2.5 * S}"><line x1="{sx}" y1="{sy}" x2="{sx + px50:.1f}" y2="{sy}"/>'
               f'<line x1="{sx}" y1="{sy - t}" x2="{sx}" y2="{sy + t}"/>'
               f'<line x1="{sx + px50:.1f}" y1="{sy - t}" x2="{sx + px50:.1f}" y2="{sy + t}"/></g>')
    out.append(text(sx + px50 / 2, sy - 16 * S, "50 km", 20 * S, INK, anchor="middle"))

    # Legend panel
    px, y = PANEL_X, MAP_Y + 6
    pw = 2400 - PANEL_X - 40   # panel width on the grid
    grid.append(text(px, y + 34, T["legend"], 38, family=SERIF, weight=600))
    y += 70
    counts = {}
    for r in records:
        counts[r["j"]] = counts.get(r["j"], 0) + 1
    for fam, title in (("E", T["east"]), ("O", T["orient"])):
        fam_n = sum(counts.get(j["k"], 0) for j in jurs if j["fam"] == fam)
        y += 12
        grid.append(text(px, y + 18, title, 19, MUTED, weight=700, extra='letter-spacing="2"'))
        grid.append(text(px + pw, y + 18, str(fam_n), 19, MUTED, weight=700, anchor="end"))
        grid.append(f'<line x1="{px}" y1="{y + 32}" x2="{px + pw}" y2="{y + 32}" stroke="#d3d8df" stroke-width="1.5"/>')
        y += 50
        for j in (j for j in jurs if j["fam"] == fam):
            grid.append(marker(px + 14, y + 12, j["c"], fam == "O", "parish", scale=1.45))
            grid.append(text(px + 46, y + 19, j["n"], 25, INK, weight=600))
            grid.append(text(px + 46, y + 46, j["s"], 19, MUTED))
            grid.append(text(px + pw, y + 20, str(counts.get(j["k"], 0)), 25, INK, weight=600, anchor="end",
                            extra='font-variant-numeric="tabular-nums"'))
            y += 74
        y += 6

    # Symbol key
    y += 6
    grid.append(f'<line x1="{px}" y1="{y}" x2="{px + pw}" y2="{y}" stroke="#d3d8df" stroke-width="1.5"/>')
    y += 22
    key = list(zip(("circle", "diamond", "circle", "circle"), ("parish", "parish", "monastery", "community"), T["key"]))
    for i, (shape, kind, label) in enumerate(key):
        kx = px + (i % 2) * (pw / 2)
        ky = y + (i // 2) * 44
        grid.append(marker(kx + 14, ky + 12, MUTED, shape == "diamond", kind, scale=1.3))
        grid.append(text(kx + 40, ky + 20, label, 20, INK))
    y += 2 * 44 + 6

    # Footnote
    notes = T["notes"]
    fy = MAP_Y + MAP_H - 18 - 30 * (len(notes) - 1)
    for i, line in enumerate(notes):
        grid.append(text(px, fy + 30 * i, line, 18, MUTED))

    grid.append("</g>")
    out.extend(grid)
    out.append("</svg>")
    return "\n".join(out)


def svg_to_png(svg, png_path):
    exe = shutil.which("rsvg-convert")
    if not exe:
        raise RuntimeError("rsvg-convert not found. Install librsvg: `brew install librsvg` (macOS), "
                           "`port install librsvg` (MacPorts) or `apt install librsvg2-bin` (Debian/Ubuntu).")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "map.svg"
        src.write_text(svg, encoding="utf-8")
        subprocess.run([exe, "--format=png", f"--width={W}", "--output", str(png_path), str(src)], check=True)
