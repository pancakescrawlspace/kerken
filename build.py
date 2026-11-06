#!/usr/bin/env python3
"""Build the Orthodox churches map from the CSV data in data/.

Usage:
    python3 build.py              # validate, geocode new addresses, write orthodox-churches-nl.png
    python3 build.py --check      # validate and geocode only, write no map
    python3 build.py --svg PATH   # also save the intermediate SVG (vector version of the same image)
    python3 build.py --offline    # never call the geocoder; fail if an address is not cached yet
    python3 build.py --refresh    # ignore the cache and geocode every address again

Only the Python standard library is used, plus the `rsvg-convert` command
(librsvg) to turn the drawing into a PNG. Geocoding uses the PDOK Locatieserver
(the Dutch government's address register, no API key needed). Results are cached
in data/geocode-cache.json so that rebuilding is fast, works offline and gives
the same map every time.
"""
import argparse
import csv
import datetime as dt
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

import render

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CHURCHES_CSV = DATA / "churches.csv"
JURISDICTIONS_CSV = DATA / "jurisdictions.csv"
PROVINCES = DATA / "provinces.geojson"
CACHE = DATA / "geocode-cache.json"
OUT = ROOT / "orthodox-churches-nl.png"    # the map and legend as one image

PDOK = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free?"
KINDS = {"parish", "monastery", "community"}
COLUMNS = ["jurisdiction", "name", "kind", "building", "street", "postcode", "place", "note", "lat", "lon", "source"]
# Town names as written in the CSV -> official name in the address register.
PLACE_ALIASES = {"Den Haag": "'s-Gravenhage", "The Hague": "'s-Gravenhage"}
POSTCODE = re.compile(r"^\d{4} ?[A-Z]{2}$")
NL_BOUNDS = (50.7, 53.7, 3.3, 7.3)  # lat min/max, lon min/max


class BuildError(Exception):
    pass


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_jurisdictions():
    rows = read_csv(JURISDICTIONS_CSV)
    out, errors = [], []
    for i, r in enumerate(rows, start=2):
        if r["family"] not in ("Eastern", "Oriental"):
            errors.append(f"jurisdictions.csv line {i}: family must be Eastern or Oriental, got {r['family']!r}")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", r["color"]):
            errors.append(f"jurisdictions.csv line {i}: color must look like #1f57d6, got {r['color']!r}")
        out.append({"k": r["key"], "fam": r["family"][0], "c": r["color"], "n": r["name"], "s": r["subtitle"]})
    keys = [j["k"] for j in out]
    for k, n in Counter(keys).items():
        if n > 1:
            errors.append(f"jurisdictions.csv: key {k!r} is used {n} times")
    return out, errors


def validate_churches(rows, jur_keys):
    errors, warnings = [], []
    if rows and list(rows[0].keys()) != COLUMNS:
        errors.append(f"churches.csv: header must be exactly: {','.join(COLUMNS)}")
        return errors, warnings
    seen = Counter()
    for i, r in enumerate(rows, start=2):
        where = f"churches.csv line {i} ({r['name'] or 'no name'})"
        r = {k: (v or "").strip() for k, v in r.items()}
        if r["jurisdiction"] not in jur_keys:
            errors.append(f"{where}: unknown jurisdiction {r['jurisdiction']!r}; use one of {', '.join(sorted(jur_keys))}")
        if not r["name"]:
            errors.append(f"{where}: name is empty")
        if r["kind"] not in KINDS:
            errors.append(f"{where}: kind must be one of {', '.join(sorted(KINDS))}, got {r['kind']!r}")
        if not r["place"]:
            errors.append(f"{where}: place (town) is empty")
        if r["postcode"] and not POSTCODE.match(r["postcode"]):
            errors.append(f"{where}: postcode {r['postcode']!r} should look like 1234 AB")
        if r["postcode"] and not r["street"]:
            warnings.append(f"{where}: postcode given without street; the marker is placed by town only")
        if bool(r["lat"]) != bool(r["lon"]):
            errors.append(f"{where}: fill in both lat and lon, or neither")
        if r["lat"]:
            try:
                lat, lon = float(r["lat"]), float(r["lon"])
                if not (NL_BOUNDS[0] <= lat <= NL_BOUNDS[1] and NL_BOUNDS[2] <= lon <= NL_BOUNDS[3]):
                    errors.append(f"{where}: lat/lon {lat},{lon} is outside the Netherlands (lat and lon swapped?)")
            except ValueError:
                errors.append(f"{where}: lat/lon must be decimal numbers such as 52.37 and 4.90")
        if r["source"] and not r["source"].startswith(("http://", "https://")):
            errors.append(f"{where}: source must be a web link starting with https://")
        if not r["source"]:
            warnings.append(f"{where}: no source link")
        seen[(r["jurisdiction"], r["name"], r["place"])] += 1
    for (j, n, p), c in seen.items():
        if c > 1:
            warnings.append(f"churches.csv: {n!r} in {p} ({j}) appears {c} times")
    return errors, warnings


def pdok(query, kind):
    params = {"q": query, "fq": f"type:{kind}", "rows": 1, "fl": "weergavenaam,centroide_ll,postcode,woonplaatsnaam"}
    req = urllib.request.Request(PDOK + urllib.parse.urlencode(params), headers={"User-Agent": "orthodox-church-map-build"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        docs = json.load(resp)["response"]["docs"]
    if not docs:
        return None
    lon, lat = map(float, re.findall(r"-?[\d.]+", docs[0]["centroide_ll"]))
    return {"lat": round(lat, 6), "lon": round(lon, 6), "found": docs[0]["weergavenaam"],
            "postcode": docs[0].get("postcode", ""), "town": docs[0].get("woonplaatsnaam", "")}


def norm_pc(pc):
    return pc.replace(" ", "").upper()


def geocode(rows, cache, offline, refresh):
    """Fill lat/lon for every row. Returns (records, errors, warnings)."""
    errors, warnings, records, calls = [], [], [], 0
    for i, r in enumerate(rows, start=2):
        r = {k: (v or "").strip() for k, v in r.items()}
        where = f"churches.csv line {i} ({r['name']})"
        town = PLACE_ALIASES.get(r["place"], r["place"])
        if r["lat"]:
            lat, lon, approx = float(r["lat"]), float(r["lon"]), not r["street"]
        else:
            if r["street"]:
                kind, query = "adres", f"{r['street']} {r['postcode']} {town}".replace("  ", " ")
            else:
                kind, query = "woonplaats", town
            key = f"{kind}|{query}"
            hit = None if refresh else cache.get(key)
            if hit is None:
                if offline:
                    errors.append(f"{where}: {query!r} is not in the geocode cache (run without --offline)")
                    continue
                try:
                    hit = pdok(query, kind)
                    calls += 1
                    time.sleep(0.05)
                except Exception as e:  # network problems should name the row
                    errors.append(f"{where}: geocoding {query!r} failed: {e}")
                    continue
                if hit is None:
                    errors.append(f"{where}: the address register has no match for {query!r}. "
                                  "Check the spelling, or fill in lat and lon yourself.")
                    continue
                cache[key] = hit
            # Guard against the geocoder silently picking a similar address elsewhere.
            if kind == "adres":
                if r["postcode"] and norm_pc(hit["postcode"]) != norm_pc(r["postcode"]):
                    errors.append(f"{where}: {query!r} resolved to {hit['found']!r} (other postcode). "
                                  "Fix the street/postcode, or fill in lat and lon yourself.")
                    continue
                if not r["postcode"] and town.lower() not in hit["found"].lower():
                    errors.append(f"{where}: {query!r} resolved to {hit['found']!r} (other town). "
                                  "Add the postcode, or fill in lat and lon yourself.")
                    continue
                if r["street"].split()[-1].lower() not in hit["found"].lower():
                    warnings.append(f"{where}: house number differs: {r['street']!r} placed at {hit['found']!r}")
            lat, lon, approx = hit["lat"], hit["lon"], kind == "woonplaats"
        if r["street"]:
            addr = ", ".join(x for x in [r["building"], r["street"], f"{r['postcode']} {r['place']}".strip()] if x)
        else:
            addr = None
        records.append({"j": r["jurisdiction"], "name": r["name"], "place": r["place"], "addr": addr,
                        "kind": r["kind"], "note": r["note"], "source": r["source"],
                        "lat": lat, "lon": lon, "approx": approx})
    return records, errors, warnings, calls


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="validate and geocode only; do not write the map")
    ap.add_argument("--offline", action="store_true", help="use only cached coordinates")
    ap.add_argument("--refresh", action="store_true", help="geocode every address again")
    ap.add_argument("--svg", metavar="PATH", help="also save the SVG version of the image")
    args = ap.parse_args()

    jurs, errors = load_jurisdictions()
    rows = read_csv(CHURCHES_CSV)
    e2, warnings = validate_churches(rows, {j["k"] for j in jurs})
    errors += e2
    if errors:
        return report(errors, warnings)

    cache = {} if not CACHE.exists() else json.loads(CACHE.read_text(encoding="utf-8"))
    records, e3, w3, calls = geocode(rows, cache, args.offline, args.refresh)
    errors += e3
    warnings += w3
    if calls:
        CACHE.write_text(json.dumps(dict(sorted(cache.items())), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if errors:
        return report(errors, warnings)

    used = {r["j"] for r in records}
    for j in jurs:
        if j["k"] not in used:
            warnings.append(f"jurisdictions.csv: {j['k']!r} has no churches and will show 0 in the legend")

    counts = Counter(r["j"] for r in records)
    print(f"{len(records)} entries in {len(used)} jurisdictions "
          f"({sum(1 for r in records if r['approx'])} placed by town only); geocoder calls: {calls}")
    print("  " + ", ".join(f"{j['k']} {counts.get(j['k'], 0)}" for j in jurs))
    report([], warnings)
    if args.check:
        return 0

    # Town labels for orientation are geocoded through the same cache.
    towns = {}
    for reg, _label, _side in render.TOWN_LABELS:
        key = f"woonplaats|{reg}"
        hit = cache.get(key)
        if hit is None and not args.offline:
            hit = pdok(reg, "woonplaats")
            if hit:
                cache[key] = hit
                calls += 1
        if hit:
            towns[reg] = (hit["lat"], hit["lon"])
    if calls:
        CACHE.write_text(json.dumps(dict(sorted(cache.items())), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    updated = dt.datetime.fromtimestamp(CHURCHES_CSV.stat().st_mtime).strftime("%B %Y")
    provinces = json.loads(PROVINCES.read_text(encoding="utf-8"))
    svg = render.render_svg(records, jurs, provinces, towns, updated)
    if args.svg:
        Path(args.svg).write_text(svg, encoding="utf-8")
        print(f"Wrote {args.svg}")
    try:
        render.svg_to_png(svg, OUT)
    except (RuntimeError, subprocess.CalledProcessError) as e:
        raise BuildError(f"could not write the PNG: {e}")
    print(f"Wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size // 1024} KB, {render.W}x{render.H} px)")
    return 0


def report(errors, warnings):
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    if errors:
        print(f"\nBuild stopped: {len(errors)} error(s). Nothing was written.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
