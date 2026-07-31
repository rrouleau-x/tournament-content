#!/usr/bin/env python3
"""Compile tournament module files into the app bundle (data.json).

This is the heart of the tournament platform: module JSON files → the exact
bundle shape the Sporting Jax Guide PWA already consumes. Byte-identical
output for unchanged content (verified against the v1 contract).

Usage:
    python3 scripts/compile.py <org>/<slug> [--out <path>] [--with-meta]

    --with-meta  adds a "meta" block (buildId, compiledAt, dataStatus) to the
                 bundle. The app ignores unknown keys, but the v1 contract
                 check compares bundles without meta — omit for byte-identical
                 reproduction.

Module files are optional: a tournament without hotels.json simply has no
"hotels" key in the bundle. The app renders empty/absent sections gracefully
(its empty-state design), and validation reports missing modules.

Canonical key order (app contract v1):
    sport, sportConfig, tournament, team, venue, games, scheduleStatus,
    scheduleExpected, hotels, weather, rules, updates, contacts, venueRules,
    checklist, nearby, offline
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Canonical bundle key order = app contract v1.
# (module_filename, [top-level keys contributed by that module])
MODULE_ORDER = [
    ("sport.json",        ["sport", "sportConfig"]),
    ("tournament.json",   ["tournament"]),
    ("team.json",         ["team"]),
    ("venue.json",        ["venue"]),
    ("schedule.json",     ["games", "scheduleStatus", "scheduleExpected"]),
    ("hotels.json",       ["hotels"]),
    ("weather.json",      ["weather"]),
    ("rules.json",        ["rules"]),
    ("updates.json",      ["updates"]),
    ("contacts.json",     ["contacts"]),
    ("venue-rules.json",  ["venueRules"]),
    ("checklist.json",    ["checklist"]),
    ("nearby.json",       ["nearby"]),
    ("offline.json",      ["offline"]),
]


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def tournament_dir(org, slug):
    return os.path.join(REPO_ROOT, "orgs", org, "tournaments", slug)


def compile_bundle(tdir):
    """Assemble the bundle dict from module files. Returns (bundle, used_modules)."""
    bundle = {}
    used = []
    for filename, keys in MODULE_ORDER:
        path = os.path.join(tdir, filename)
        if not os.path.exists(path):
            continue
        module = load_json(path)
        for key in keys:
            if key not in module:
                raise ValueError(
                    f"{filename} exists but is missing expected key '{key}'"
                )
            bundle[key] = module[key]
        used.append(filename)
    return bundle, used


def serialize(obj):
    """Serialize exactly like the v1 app bundle: 2-space indent, raw UTF-8."""
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tournament", help="org/slug, e.g. savannah-united/sporting-jax-2026")
    ap.add_argument("--out", help="output file (default: <repo>/out/<org>/<slug>/data.json)")
    ap.add_argument("--with-meta", action="store_true", help="include meta block")
    args = ap.parse_args()

    org, slug = args.tournament.split("/", 1)
    tdir = tournament_dir(org, slug)
    if not os.path.isdir(tdir):
        print(f"ERROR: no tournament dir at {tdir}", file=sys.stderr)
        sys.exit(1)

    bundle, used = compile_bundle(tdir)

    if args.with_meta:
        raw = serialize(bundle)
        bundle = {
            "meta": {
                "schemaVersion": 1,
                "tournamentId": f"{org}/{slug}",
                "compiledAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "buildId": hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10],
                "modules": used,
            },
            **bundle,
        }

    output = serialize(bundle)

    if args.out:
        out_path = args.out
    else:
        out_path = os.path.join(REPO_ROOT, "out", org, slug, "data.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output)

    print(f"compiled {len(used)} modules → {out_path} ({len(output)} bytes)")
    print(f"  modules: {', '.join(used)}")
    if not args.with_meta:
        print(f"  sha1: {hashlib.sha1(output.encode('utf-8')).hexdigest()}")


if __name__ == "__main__":
    main()
