#!/usr/bin/env python3
"""One-time migration: split the app's data.json into per-module content files.

Reads a compiled bundle (e.g. the current app data.json) and writes module
files under orgs/<org>/tournaments/<slug>/ so the compile pipeline can
reproduce the bundle byte-for-byte from modules.

Usage:
    python3 scripts/split.py <bundle.json> <org> <slug> [--manifest-only]

Writes:
    <repo>/orgs/<org>/tournaments/<slug>/
        manifest.json      (metadata, not compiled into the bundle)
        sport.json         sport + sportConfig
        tournament.json    tournament
        team.json          team
        venue.json         venue
        schedule.json      games + scheduleStatus + scheduleExpected
        hotels.json        hotels
        weather.json       weather
        rules.json         rules
        updates.json       updates
        contacts.json      contacts
        venue-rules.json   venueRules
        checklist.json     checklist
        nearby.json        nearby
        offline.json       offline
"""

import json
import os
import sys
from datetime import datetime, timezone

# Canonical bundle key order (matches the app contract v1).
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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dump_json(obj, path):
    """Write JSON matching the app bundle style: 2-space indent, raw UTF-8,
    trailing newline."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    bundle_path, org, slug = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(bundle_path, encoding="utf-8") as f:
        bundle = json.load(f)

    tdir = os.path.join(REPO_ROOT, "orgs", org, "tournaments", slug)
    os.makedirs(tdir, exist_ok=True)

    # Split modules by key groups, preserving key order within each module.
    for filename, keys in MODULE_ORDER:
        module = {k: bundle[k] for k in keys if k in bundle}
        if not module:
            print(f"  skip {filename} (no keys present in bundle)")
            continue
        dump_json(module, os.path.join(tdir, filename))
        print(f"  wrote {filename}")

    # Manifest: repo metadata only — never compiled into the bundle.
    manifest = {
        "org": org,
        "slug": slug,
        "schemaVersion": 1,
        "status": "live",          # draft | staging | live | complete
        "sport": bundle.get("sport", ""),
        "name": bundle.get("tournament", {}).get("name", slug),
        "shortName": bundle.get("tournament", {}).get("shortName", ""),
        "dates": bundle.get("tournament", {}).get("dates", {}),
        "compiledFrom": os.path.basename(bundle_path),
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    dump_json(manifest, os.path.join(tdir, "manifest.json"))
    print(f"  wrote manifest.json")
    print(f"\nDone. Modules in {tdir}")


if __name__ == "__main__":
    main()
