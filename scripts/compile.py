#!/usr/bin/env python3
"""Compile tournament module files into the app bundle (data.json).

This is the heart of the tournament platform: module JSON files → the exact
bundle shape the Sporting Jax Guide PWA already consumes. Semantically
identical output for unchanged content (verified against the v1 contract).

Usage:
    python3 scripts/compile.py <org>/<slug> [--out <path>]

Module files are optional (per MODULE_REGISTRY): a tournament without
hotels.json simply has no "hotels" key in the bundle. Unknown .json files in
the tournament directory (e.g. a typo'd "hotel.json") are reported as
warnings so silent content omission can't happen.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import (  # noqa: E402
    EXIT_CONFIG,
    MODULE_REGISTRY,
    PlatformError,
    parse_tournament_id,
    tournament_dir,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class CompileError(PlatformError):
    """Content problem that should surface as an actionable message."""

    def __init__(self, message):
        super().__init__(message, exit_code=EXIT_CONFIG)


def compile_bundle(tdir):
    """Assemble the bundle dict from module files.

    Returns (bundle, used_modules, unknown_modules). Unknown modules are
    .json files in the tournament dir that are neither registered modules
    nor known metadata (manifest.json) — reported as warnings.
    """
    bundle = {}
    used = []
    unknown = []
    for filename, keys, _required in MODULE_REGISTRY:
        path = os.path.join(tdir, filename)
        if not os.path.exists(path):
            continue
        try:
            module = load_json(path)
        except json.JSONDecodeError as e:
            raise CompileError(
                f"{filename} is not valid JSON: {e.msg} (line {e.lineno}, col {e.colno}). "
                f"Fix the syntax and re-run."
            ) from e
        for key in keys:
            if key not in module:
                raise CompileError(
                    f"{filename} exists but is missing expected key '{key}'. "
                    f"Add the key to the file, or delete the file if this module is "
                    f"not needed for this tournament."
                )
            bundle[key] = module[key]
        used.append(filename)

    # Detect unrecognized .json files (typo'd module names, stray files)
    from pipeline import KNOWN_NON_MODULES
    registered = {f for f, _k, _r in MODULE_REGISTRY}
    for fname in sorted(os.listdir(tdir)):
        if fname.endswith(".json") and fname not in registered and fname not in KNOWN_NON_MODULES:
            unknown.append(fname)

    return bundle, used, unknown


def serialize(obj):
    """Serialize exactly like the v1 app bundle: 2-space indent, raw UTF-8,
    trailing newline."""
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def content_digest(text):
    """Stable content identifier (not a security hash)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tournament", help="org/slug, e.g. savannah-united/sporting-jax-2026")
    ap.add_argument("--out", help="output file (default: <repo>/out/<org>/<slug>/data.json)")
    args = ap.parse_args()

    try:
        org, slug = parse_tournament_id(args.tournament)
        tdir = tournament_dir(org, slug)
        if not os.path.isdir(tdir):
            raise CompileError(f"no tournament dir at {tdir}")
        bundle, used, unknown = compile_bundle(tdir)
        if unknown:
            print(f"WARNING: unrecognized .json files in tournament dir (ignored): "
                  f"{', '.join(unknown)} — did you mean a registered module?",
                  file=sys.stderr)
        output = serialize(bundle)
    except PlatformError as e:
        print(f"COMPILE ERROR: {e}", file=sys.stderr)
        sys.exit(e.exit_code)

    if args.out:
        out_path = args.out
    else:
        out_path = os.path.join(REPO_ROOT, "out", org, slug, "data.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output)

    print(f"compiled {len(used)} modules → {out_path} ({len(output)} bytes)")
    print(f"  modules: {', '.join(used)}")
    print(f"  digest: {content_digest(output)}")


if __name__ == "__main__":
    main()
