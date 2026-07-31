#!/usr/bin/env python3
"""One-time migration tool: split a compiled bundle (e.g. the app's
data.json) into per-module content files.

Usage:
    python3 scripts/split.py <bundle.json> <org> <slug>

Writes orgs/<org>/tournaments/<slug>/ — one module file per registered
module (see pipeline.MODULE_REGISTRY) plus manifest.json metadata.

This is migration tooling, not a daily command — use scripts/guide.py for
day-to-day operations.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import (  # noqa: E402
    MODULE_REGISTRY,
    PlatformError,
    parse_tournament_id,
    tournament_dir,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dump_json(obj, path):
    """Write JSON matching the app bundle style: 2-space indent, raw UTF-8,
    trailing newline."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bundle", help="path to a compiled data.json")
    ap.add_argument("tournament", help="org/slug to write into")
    args = ap.parse_args()

    try:
        org, slug = parse_tournament_id(args.tournament)
        with open(args.bundle, encoding="utf-8") as f:
            bundle = json.load(f)
    except (PlatformError, OSError, json.JSONDecodeError) as e:
        print(f"SPLIT ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    tdir = tournament_dir(org, slug)
    os.makedirs(tdir, exist_ok=True)

    for filename, keys, _required in MODULE_REGISTRY:
        module = {k: bundle[k] for k in keys if k in bundle}
        if not module:
            print(f"  skip {filename} (no keys present in bundle)")
            continue
        dump_json(module, os.path.join(tdir, filename))
        print(f"  wrote {filename}")

    manifest = {
        "org": org,
        "slug": slug,
        "schemaVersion": 1,
        "status": "draft",  # split output must be explicitly promoted to live
        "sport": bundle.get("sport", ""),
        "name": bundle.get("tournament", {}).get("name", slug),
        "shortName": bundle.get("tournament", {}).get("shortName", ""),
        "dates": bundle.get("tournament", {}).get("dates", {}),
        "compiledFrom": os.path.basename(args.bundle),
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    dump_json(manifest, os.path.join(tdir, "manifest.json"))
    print(f"  wrote manifest.json (status: draft — set to 'live' to publish)")
    print(f"\nDone. Modules in {tdir}")


if __name__ == "__main__":
    main()
