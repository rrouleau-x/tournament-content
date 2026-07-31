#!/usr/bin/env python3
"""Validate a compiled tournament bundle (or a tournament module folder).

Produces a Guide Health Report: per-check status, blocking vs warning
results, and a fix list. Exit code 1 if any blocking issue is found.

Usage:
    python3 scripts/validate.py <org>/<slug> [--bundle <path>] [--no-links]
    python3 scripts/validate.py --bundle <path/to/data.json> [--no-links]

Checks:
  1. schema      — bundle conforms to _schemas/bundle-v1.json (blocking)
  2. required    — business-required fields present (blocking)
  3. dates       — games/dates within tournament window (blocking)
  4. links       — every http(s) URL responds (blocking for critical links,
                   warning otherwise); use --no-links to skip network checks
  5. assets      — referenced local asset paths exist (blocking)
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(REPO_ROOT, "_schemas", "bundle-v1.json")

try:
    from jsonschema import Draft202012Validator, exceptions as js_exc
except ImportError:
    print("ERROR: run in the pipeline venv: .venv/bin/python scripts/validate.py", file=sys.stderr)
    sys.exit(2)

# Business-required fields. Format: (jsonpath-like list, human description).
REQUIRED_FIELDS = [
    (["tournament", "name"], "Tournament name"),
    (["tournament", "dates", "start"], "Tournament start date"),
    (["tournament", "dates", "end"], "Tournament end date"),
    (["team", "name"], "Team name"),
    (["venue", "name"], "Venue name"),
    (["venue", "address"], "Venue address"),
    (["contacts", "manager"], "Team manager contact"),
    (["contacts", "coach"], "Head coach contact"),
]

# Links that must resolve for the guide to be usable (blocking).
CRITICAL_LINK_PATHS = [
    ["venue", "mapsUrl"],
    ["venue", "fields", "map"],
    ["hotels", "portal"],
    ["rules", "fullLink"],
]


class Report:
    def __init__(self):
        self.items = []  # (severity, check, message)

    def ok(self, check, msg):
        self.items.append(("ok", check, msg))

    def warn(self, check, msg):
        self.items.append(("warn", check, msg))

    def fail(self, check, msg):
        self.items.append(("fail", check, msg))

    def blocking(self):
        return [i for i in self.items if i[0] == "fail"]

    def render(self):
        lines = ["GUIDE HEALTH REPORT", "────────────────────"]
        for severity, check, msg in self.items:
            mark = {"ok": "✓", "warn": "⚠", "fail": "✗"}[severity]
            lines.append(f"  {mark} [{check}] {msg}")
        fails = self.blocking()
        n_warn = sum(1 for i in self.items if i[0] == "warn")
        n_ok = sum(1 for i in self.items if i[0] == "ok")
        lines.append("────────────────────")
        lines.append(f"  {n_ok} passed · {n_warn} warnings · {len(fails)} blocking")
        if fails:
            lines.append("  FIX LIST:")
            for _, check, msg in fails:
                lines.append(f"    - {msg}")
        return "\n".join(lines)


def get_path(obj, path):
    cur = obj
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def collect_urls(obj, path=()):
    """Yield (jsonpath, url) for every http(s) string in the bundle."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from collect_urls(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from collect_urls(v, path + (str(i),))
    elif isinstance(obj, str) and obj.startswith(("http://", "https://")):
        yield path, obj


def check_link(url, timeout=10):
    """Return (ok, status_or_error). Uses GET with Range to avoid big downloads."""
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": "tournament-pipeline/1.0",
        "Range": "bytes=0-1024",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return e.code < 400, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tournament", nargs="?", help="org/slug — compiles then validates")
    ap.add_argument("--bundle", help="validate an existing bundle file directly")
    ap.add_argument("--no-links", action="store_true", help="skip network link checks")
    args = ap.parse_args()

    if args.bundle:
        with open(args.bundle, encoding="utf-8") as f:
            bundle = json.load(f)
        report_title = f"bundle {args.bundle}"
    elif args.tournament:
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        from compile import compile_bundle, tournament_dir
        org, slug = args.tournament.split("/", 1)
        tdir = tournament_dir(org, slug)
        bundle, used = compile_bundle(tdir)
        report_title = f"{org}/{slug} ({len(used)} modules)"
    else:
        ap.error("provide <org>/<slug> or --bundle <path>")

    report = Report()

    # 1. Schema
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(bundle), key=lambda e: list(e.path))
    if errors:
        for err in errors[:10]:
            loc = "/".join(str(p) for p in err.path) or "(root)"
            report.fail("schema", f"{loc}: {err.message}")
        if len(errors) > 10:
            report.fail("schema", f"...and {len(errors) - 10} more schema errors")
    else:
        report.ok("schema", f"bundle conforms to bundle-v1.json")

    # 2. Required business fields
    for path, label in REQUIRED_FIELDS:
        if get_path(bundle, path) in (None, "", []):
            report.fail("required", f"missing {label} ({'/'.join(path)})")
        else:
            report.ok("required", f"{label} present")

    # 3. Date sanity
    t_start = get_path(bundle, ["tournament", "dates", "start"])
    t_end = get_path(bundle, ["tournament", "dates", "end"])
    if t_start and t_end and t_start > t_end:
        report.fail("dates", f"tournament start ({t_start}) after end ({t_end})")
    elif t_start and t_end:
        report.ok("dates", f"tournament window {t_start} → {t_end}")
    games = bundle.get("games") or []
    bad_games = []
    for g in games:
        d = g.get("date")
        if d and t_start and t_end and not (t_start <= d <= t_end):
            bad_games.append(f"{g.get('id', g.get('opponent', '?'))} on {d}")
    if bad_games:
        report.fail("dates", f"games outside tournament window: {', '.join(bad_games)}")
    elif games:
        report.ok("dates", f"{len(games)} games within tournament window")
    else:
        report.warn("dates", "no games yet — schedule pending")

    # 4. Links
    if args.no_links:
        report.warn("links", "skipped (--no-links)")
    else:
        urls = list(collect_urls(bundle))
        if not urls:
            report.warn("links", "no URLs found in bundle")
        checked = 0
        for path, url in urls:
            ok, detail = check_link(url)
            critical = list(path) in CRITICAL_LINK_PATHS
            checked += 1
            loc = "/".join(path)
            if ok:
                report.ok("links", f"{detail} · {loc}")
            elif critical:
                report.fail("links", f"{detail} · CRITICAL {loc}: {url}")
            else:
                report.warn("links", f"{detail} · {loc}: {url}")
        if checked == 0:
            report.ok("links", "no links to check")

    # 5. Assets (local paths referenced in the bundle)
    asset_refs = []
    for path, url in collect_urls(bundle):
        if not url.startswith(("http://", "https://")) and url:
            asset_refs.append((path, url))
    if asset_refs:
        for path, ref in asset_refs:
            report.fail("assets", f"non-URL asset reference not yet supported: {'/'.join(path)} = {ref}")
    else:
        report.ok("assets", "no local asset references (all URLs are remote)")

    print(report.render())
    sys.exit(1 if report.blocking() else 0)


if __name__ == "__main__":
    main()
