#!/usr/bin/env python3
"""Validate a compiled tournament bundle (or a tournament module folder).

Produces a Guide Health Report: per-check status, blocking vs warning
results, and a fix list. Exit code 1 if any blocking issue is found.

Usage:
    python3 scripts/validate.py <org>/<slug> [--no-links] [--refresh-links]
    python3 scripts/validate.py --bundle <path/to/data.json> [--no-links]

Checks:
  1. schema      — bundle conforms to _schemas/bundle-v1.json, including
                   real calendar dates (format: date) and URI/email formats (blocking)
  2. required    — business-required fields present, beyond what the schema
                   already enforces (blocking)
  3. consistency — cross-field rules: scheduleStatus "confirmed" requires
                   games; games present implies status not "pending" (blocking/warn)
  4. links       — every http(s) URL responds; critical links (venue maps,
                   field map, hotel portal, rules doc, urgent care) must be
                   reachable (blocking), others warning. Results cached for
                   6h and checked in parallel. --no-links to skip entirely.
  5. assets      — referenced local asset paths exist (blocking)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import (  # noqa: E402
    ASSET_FIELDS,
    EXIT_CONFIG,
    KNOWN_NON_MODULES,
    MODULE_REGISTRY,
    PlatformError,
    parse_tournament_id,
    tournament_dir,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(REPO_ROOT, "_schemas", "bundle-v1.json")
LINK_CACHE_PATH = os.path.join(REPO_ROOT, "out", ".link-cache.json")
LINK_CACHE_TTL = 6 * 3600  # 6 hours
LINK_TIMEOUT = 10
LINK_WORKERS = 8

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:
    print("ERROR: run in the pipeline venv: .venv/bin/python scripts/validate.py", file=sys.stderr)
    sys.exit(2)

# Business-required fields NOT already enforced by the schema. The schema
# requires tournament.name/dates, team.name, venue.name/address, etc. —
# keeping them here too would double-report one root cause.
REQUIRED_FIELDS = [
    (["contacts", "manager"], "Team manager contact"),
    (["contacts", "coach"], "Head coach contact"),
]

# Links that must resolve for the guide to be usable (blocking).
CRITICAL_LINK_PATHS = [
    ["venue", "mapsUrl"],
    ["venue", "fields", "map"],
    ["hotels", "portal"],
    ["rules", "fullLink"],
    ["nearby", "urgentCare", "maps"],
]

DRIVE_PATTERN = re.compile(r"^\d+(\.\d+)?\s*(mi|miles?)\s*[·•\-–—]\s*~?\d+\s*min$")


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

    def summary(self):
        return {
            "passed": sum(1 for i in self.items if i[0] == "ok"),
            "warnings": sum(1 for i in self.items if i[0] == "warn"),
            "blocking": len(self.blocking()),
        }

    def to_dict(self):
        """Structured output for a future validation dashboard / admin UI."""
        return {
            "summary": self.summary(),
            "results": [
                {"severity": s, "check": c, "message": m} for s, c, m in self.items
            ],
        }

    def render(self):
        lines = ["GUIDE HEALTH REPORT", "────────────────────"]
        for severity, check, msg in self.items:
            mark = {"ok": "✓", "warn": "⚠", "fail": "✗"}[severity]
            lines.append(f"  {mark} [{check}] {msg}")
        s = self.summary()
        lines.append("────────────────────")
        lines.append(f"  {s['passed']} passed · {s['warnings']} warnings · {s['blocking']} blocking")
        fails = self.blocking()
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


def check_link(url, timeout=LINK_TIMEOUT):
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


def _load_link_cache():
    try:
        with open(LINK_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_link_cache(cache):
    os.makedirs(os.path.dirname(LINK_CACHE_PATH), exist_ok=True)
    with open(LINK_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def check_links(urls, refresh=False):
    """Check a list of (path, url) tuples. Returns dict url -> (ok, detail).
    Cached for LINK_CACHE_TTL; checked in parallel."""
    cache = _load_link_cache()
    now = time.time()
    results = {}
    to_check = []
    for path, url in urls:
        entry = cache.get(url)
        if not refresh and entry and now - entry.get("at", 0) < LINK_CACHE_TTL:
            results[url] = (entry["ok"], entry["detail"])
        else:
            to_check.append(url)

    if to_check:
        with ThreadPoolExecutor(max_workers=LINK_WORKERS) as ex:
            futures = {ex.submit(check_link, u): u for u in to_check}
            for fut in futures:
                url = futures[fut]
                ok, detail = fut.result()
                results[url] = (ok, detail)
                cache[url] = {"ok": ok, "detail": detail, "at": now}
        _save_link_cache(cache)

    return results


def run_checks(bundle_data, report, run_link_checks=True, refresh_links=False,
               tdir=None):
    """Run the full validation battery against a bundle dict. Shared by the
    CLI and deploy.py so the two can never drift. tdir (tournament dir) is
    optional — enables local-asset existence checks and unknown-module
    detection."""

    # 1. Schema (with format checker: real dates, URIs, emails)
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(bundle_data), key=lambda e: list(e.path))
    if errors:
        for err in errors[:10]:
            loc = "/".join(str(p) for p in err.path) or "(root)"
            report.fail("schema", f"{loc}: {err.message}")
        if len(errors) > 10:
            report.fail("schema", f"...and {len(errors) - 10} more schema errors")
    else:
        report.ok("schema", "bundle conforms to bundle-v1.json")

    # 2. Required business fields (beyond schema-required)
    for path, label in REQUIRED_FIELDS:
        val = get_path(bundle_data, path)
        if val in (None, "", [], {}) or (isinstance(val, dict) and not val):
            report.fail("required", f"missing {label} ({'/'.join(path)})")
        else:
            report.ok("required", f"{label} present")

    # 3. Cross-field consistency
    t_start = get_path(bundle_data, ["tournament", "dates", "start"])
    t_end = get_path(bundle_data, ["tournament", "dates", "end"])
    if t_start and t_end and t_start > t_end:
        report.fail("dates", f"tournament start ({t_start}) after end ({t_end})")
    elif t_start and t_end:
        report.ok("dates", f"tournament window {t_start} → {t_end}")

    games = bundle_data.get("games") or []
    status = bundle_data.get("scheduleStatus")
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

    if status == "confirmed" and not games:
        report.fail("consistency", "scheduleStatus is 'confirmed' but games is empty")
    elif games and status == "pending":
        report.warn("consistency", f"{len(games)} games present but scheduleStatus is still 'pending' — set to 'partial' or 'confirmed'")
    elif status and games:
        report.ok("consistency", f"scheduleStatus '{status}' consistent with {len(games)} games")
    else:
        report.ok("consistency", "scheduleStatus consistent")

    # Drive format sanity (friendly error for a brittle-looking field)
    drive_issues = 0
    for section in ("official", "nonOfficial"):
        for i, hotel in enumerate((bundle_data.get("hotels") or {}).get(section, [])):
            drive = hotel.get("drive", "")
            if drive and not DRIVE_PATTERN.match(drive):
                drive_issues += 1
                report.fail(
                    "consistency",
                    f"hotels.{section}[{i}] drive '{drive}' — expected format like '7.9 mi · 15 min' (use 'mi', a · or - separator, and minutes)",
                )
    if not drive_issues and (bundle_data.get("hotels") or {}).get("official"):
        report.ok("consistency", "hotel drive formats valid")

    # 4. Links (cached, parallel)
    if not run_link_checks:
        report.warn("links", "skipped (--no-links)")
    else:
        urls = list(collect_urls(bundle_data))
        if not urls:
            report.warn("links", "no URLs found in bundle")
        else:
            results = check_links(urls, refresh=refresh_links)
            for path, url in urls:
                ok, detail = results[url]
                critical = list(path) in CRITICAL_LINK_PATHS
                loc = "/".join(path)
                if ok:
                    report.ok("links", f"{detail} · {loc}")
                elif critical:
                    report.fail("links", f"{detail} · CRITICAL {loc}: {url}")
                else:
                    report.warn("links", f"{detail} · {loc}: {url}")

    # 5. Local assets (fields that may reference a local file path)
    missing_assets = []
    for path in ASSET_FIELDS:
        val = get_path(bundle_data, path)
        if val and not val.startswith(("http://", "https://")):
            if tdir and os.path.exists(os.path.join(tdir, val)):
                report.ok("assets", f"local asset exists: {'/'.join(path)} = {val}")
            elif tdir:
                missing_assets.append(f"{'/'.join(path)} = {val}")
                report.fail("assets", f"local asset file missing: {'/'.join(path)} = {val} "
                                      f"(looked in {tdir})")
            else:
                report.warn("assets", f"local asset reference cannot be verified "
                                      f"without tournament dir: {'/'.join(path)} = {val}")
    has_local_asset = False
    for p in ASSET_FIELDS:
        v = get_path(bundle_data, p)
        if v and not v.startswith(("http://", "https://")):
            has_local_asset = True
            break
    if not missing_assets and not has_local_asset:
        report.ok("assets", "no local asset references (all URLs are remote)")

    # 6. Unknown module files (typo'd module names must not silently vanish)
    if tdir and os.path.isdir(tdir):
        registered = {f for f, _k, _r in MODULE_REGISTRY}
        unknown = sorted(
            f for f in os.listdir(tdir)
            if f.endswith(".json") and f not in registered and f not in KNOWN_NON_MODULES
        )
        if unknown:
            report.warn("modules", f"unrecognized .json files in tournament dir (ignored): "
                                   f"{', '.join(unknown)}")
        else:
            report.ok("modules", "all module files recognized")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tournament", nargs="?", help="org/slug — compiles then validates")
    ap.add_argument("--bundle", help="validate an existing bundle file directly")
    ap.add_argument("--no-links", action="store_true", help="skip network link checks")
    ap.add_argument("--refresh-links", action="store_true", help="bypass link cache")
    ap.add_argument("--json", action="store_true", help="emit structured JSON report")
    args = ap.parse_args()

    if args.bundle:
        with open(args.bundle, encoding="utf-8") as f:
            bundle = json.load(f)
        report_title = f"bundle {args.bundle}"
        tdir = None
    elif args.tournament:
        from compile import compile_bundle
        try:
            org, slug = parse_tournament_id(args.tournament)
            tdir = tournament_dir(org, slug)
            if not os.path.isdir(tdir):
                raise PlatformError(f"no tournament dir at {tdir}")
            bundle, used, _unknown = compile_bundle(tdir)
        except PlatformError as e:
            print(f"VALIDATION ERROR: {e}", file=sys.stderr)
            sys.exit(e.exit_code)
        report_title = f"{org}/{slug} ({len(used)} modules)"
    else:
        ap.error("provide <org>/<slug> or --bundle <path>")

    report = Report()
    run_checks(bundle, report, run_link_checks=not args.no_links,
               refresh_links=args.refresh_links, tdir=tdir)

    if args.json:
        print(json.dumps({"title": report_title, **report.to_dict()}, indent=2))
    else:
        print(report.render())
        print(f"  title: {report_title}")
    sys.exit(1 if report.blocking() else 0)


if __name__ == "__main__":
    main()
