#!/usr/bin/env python3
"""Export tournament platform content into the parent app.

The app is one PWA serving N events. This turns each tournament directory in
orgs/<org>/tournaments/ into an app-ready bundle and writes the registry the
app's picker reads:

    <out>/tournaments.json          {"defaultTournament": ..., "tournaments": [...]}
    <out>/tournaments/<slug>.json   the full compiled bundle for one event

Only modules the app understands are emitted (the compiled bundle already has
that shape), so the app needs no per-event code. Drafts are exported too — a
staged tournament should be visible/chooseable before its schedule drops.

Usage:
    python3 scripts/app_export.py --out ~/.hermes/www/app [--org savannah-united]
                                 [--default sporting-jax-2026] [--dry-run]
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from compile import compile_bundle, serialize  # noqa: E402
from pipeline import REPO_ROOT, tournament_dir  # noqa: E402


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default if default is not None else {}


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def collect(org):
    base = os.path.join(REPO_ROOT, "orgs", org, "tournaments")
    if not os.path.isdir(base):
        raise SystemExit(f"ERROR: no tournaments dir at {base}")
    out = []
    for slug in sorted(os.listdir(base)):
        tdir = os.path.join(base, slug)
        if not os.path.isdir(tdir) or slug.startswith("_"):
            continue
        manifest = load_json(os.path.join(tdir, "manifest.json"))
        if not manifest:
            continue          # not a tournament dir (no manifest)
        out.append((slug, tdir, manifest))
    return out


def entry(slug, tdir, manifest, bundle):
    t = bundle.get("tournament") or {}
    v = bundle.get("venue") or {}
    dates = t.get("dates") or {}
    city = ""
    addr = v.get("address") or ""
    # "Jacksonville, FL (venue not yet announced)" -> "Jacksonville, FL"
    if "(" in addr:
        addr = addr.split("(")[0]
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    if len(parts) >= 2:
        city = f"{parts[-2]}, {parts[-1]}"
    elif parts:
        city = parts[0]
    return {
        "slug": slug,
        "name": t.get("name") or manifest.get("name") or slug,
        "short": t.get("shortName") or (t.get("name") or slug)[:28],
        "dates": {"start": dates.get("start", ""), "end": dates.get("end", "")},
        "city": city,
        "status": manifest.get("status", "draft"),
        "teams": [bundle.get("team", {}).get("shortName")
                  or bundle.get("team", {}).get("name", "")],
    }



def to_app_contract(bundle, teams_registry, default_team):
    """Shape a compiled bundle for the Carolina-style app shell.

    The shell reads a specific contract (venue.assigned/city/mapsUrl,
    hotels.portal, rules.tiebreakersPending, contacts.coaches, offline.*,
    teams[] + defaultTeam). Our modules already match most of it; this fills the
    gaps from the bundle itself and never from another tournament's data.
    """
    b = dict(bundle)
    t = b.get("tournament") or {}
    v = dict(b.get("venue") or {})
    addr = str(v.get("address") or "")
    known = bool(addr) and not re.search(r"not yet announced|venue tbd|^tbd", addr, re.I)
    core = re.split(r"\s*\(", addr)[0].strip()   # drop "(fields not yet assigned)" style asides
    parts = [x.strip() for x in core.split(",") if x.strip()]
    v["assigned"] = known
    v.setdefault("city", f"{parts[-2]}, {parts[-1]}" if len(parts) >= 2 else (parts[0] if parts else ""))
    if known and not v.get("mapsUrl"):
        v["mapsUrl"] = "https://www.google.com/maps/?q=" + urllib.parse.quote(core)
    v.setdefault("coordinates", None)
    v.setdefault("complexes", [])
    b["venue"] = v

    b["tournament"] = dict(t, build=(t.get("build") or "v1"))

    h = dict(b.get("hotels") or {})
    h.setdefault("portal", "")
    h.setdefault("updatedAt", "")
    h.setdefault("source", "")
    h.setdefault("official", [])
    h.setdefault("nonOfficial", [])
    h.setdefault("stayToPlay", False)
    b["hotels"] = h

    r = dict(b.get("rules") or {})
    r.setdefault("keyRules", [])
    r.setdefault("tiebreakers", [])
    r.setdefault("tiebreakersPending", not bool(r.get("tiebreakers")))
    r.setdefault("directorContact", "")
    r.setdefault("bracketContact", "")
    b["rules"] = r

    c = dict(b.get("contacts") or {})
    staff = []
    if c.get("coach"):
        staff.append(c["coach"])
    staff.extend(c.get("premierStaff") or [])
    c.setdefault("coaches", staff)
    c.setdefault("tournamentDirector", {})
    c.setdefault("emergency", "911")
    b["contacts"] = c

    o = dict(b.get("offline") or {})
    o.setdefault("pdfGuideUrl", "")
    o.setdefault("icsUrl", "")
    o.setdefault("lastCached", "")
    b["offline"] = o

    ups = []
    for u in (b.get("updates") or []):
        ups.append({"type": u.get("type", "info"), "title": u.get("title", ""),
                    "description": u.get("description", ""), "time": u.get("time", ""),
                    "posted": u.get("posted", "")})
    b["updates"] = ups

    w = dict(b.get("weather") or {})
    w.setdefault("summary", "")
    w.setdefault("details", "")
    w.setdefault("days", [])
    w.setdefault("forecastLink", "")
    w.setdefault("source", "")
    w.setdefault("updatedAt", "")
    b["weather"] = w
    b.setdefault("venueRules", {})
    b.setdefault("checklist", {"player": [], "weather": [], "parent": [], "emergency": []})
    b.setdefault("nearby", {})
    b["teams"] = teams_registry
    b["defaultTeam"] = default_team
    return b


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="savannah-united")
    ap.add_argument("--out", default=os.path.expanduser("~/.hermes/www/app"))
    ap.add_argument("--default", default=None,
                    help="slug the app opens with when no ?t= is given")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="also export tournaments that already finished")
    args = ap.parse_args(argv)

    out = os.path.expanduser(args.out)
    today = datetime.date.today().isoformat()
    # team registry is needed while writing bundles (the app shell reads teams[])
    _tsrc = os.path.join(REPO_ROOT, "orgs", args.org, "teams.json")
    _teams = load_json(_tsrc, default=None) if os.path.exists(_tsrc) else None
    teams_registry = (_teams or {}).get("teams", [])
    teams_default = (_teams or {}).get("defaultTeam", "")
    rows, skipped = [], []
    for slug, tdir, manifest in collect(args.org):
        try:
            bundle, used, unknown = compile_bundle(tdir)
        except Exception as e:                     # noqa: BLE001 - report, don't die
            skipped.append((slug, f"{type(e).__name__}: {e}"))
            continue
        row = entry(slug, tdir, manifest, bundle)
        # A picker full of finished events is noise; parents need what's next.
        if not args.all and (row["dates"]["end"] or row["dates"]["start"]) < today:
            skipped.append((slug, f"finished {(row['dates']['end'] or row['dates']['start'])}"))
            continue
        rows.append(row)
        if not args.dry_run:
            write_json(os.path.join(out, "tournaments", f"{slug}.json"),
                       to_app_contract(bundle, teams_registry, teams_default))

    # chronological: the next event first
    rows.sort(key=lambda r: (r["dates"]["start"] or "9999", r["slug"]))
    default = args.default
    if not default:
        upcoming = [r for r in rows if (r["dates"]["start"] or "") >= today]
        default = (upcoming[0]["slug"] if upcoming
                   else (rows[0]["slug"] if rows else ""))
    index = {"defaultTournament": default, "count": len(rows), "tournaments": rows}
    if not args.dry_run:
        write_json(os.path.join(out, "tournaments.json"), index)

    # Team registry (4 travel squads). Shared across every event, so it lives at
    # org level and is copied verbatim — the app overlays it on each bundle.
    teams_src = os.path.join(REPO_ROOT, "orgs", args.org, "teams.json")
    teams = load_json(teams_src, default=None) if os.path.exists(teams_src) else None
    if teams and not args.dry_run:
        write_json(os.path.join(out, "teams.json"), teams)
    elif teams is None:
        print(f"  (no team registry at {os.path.relpath(teams_src, REPO_ROOT)})")

    print(f"exported {len(rows)} tournaments -> {out}")
    for r in rows:
        print(f"  {r['status']:6s} {r['dates']['start'] or '   ?    '}  "
              f"{r['slug']:42s} {r['city']}")
    for slug, why in skipped:
        print(f"  SKIP {slug}: {why}")
    print(f"default: {default}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
