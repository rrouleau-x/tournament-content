#!/usr/bin/env python3
"""Live-updates checker — watches a tournament's HOTSPOT LINKS (official
external sources parents should be notified about when they change) and
drafts update entries.

What it watches (per tournament, from the module data):
  - hotels.portal          — the stay-to-play / booking portal
  - rules.fullLink         — the official rules document
  - scheduleExpected       — where the schedule will be posted
  - updates.watchUrls      — extra URLs to watch (optional, hand-added)
  - venue.mapsUrl, venue.fields.map — venue/field-map pages

How it works:
  1. Fetch each watched URL (SSRF-guarded, same safe_fetch as autofill).
  2. Hash the content (first 64 KB, normalized) and compare with the
     last-seen state stored in out/link-state.json (gitignored).
  3. On change: append a DRAFT update entry to updates.json via the same
     validate-before-write path as autofill (write_module). The entry
     says which source changed and when — a HUMAN reviews and approves
     before it reaches parents.
  4. Never auto-publishes. The revision gate stays authoritative.

Usage:
    python3 scripts/updates.py check <org>/<slug> [--apply] [--json]

  --apply   actually write draft update entries (default: report only)
  --json    machine-readable output

Design notes:
  - First run seeds the state (nothing to report). Subsequent runs
    report only REAL changes.
  - Content hash is normalized (whitespace-collapsed) so trivial
    re-renders don't spam updates.
  - State file lives in out/ (gitignored) — it is runtime state, not
    content. The updates.json draft entries ARE content and go through
    the normal git/digest pipeline.
"""

import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from pipeline import (  # noqa: E402
    PlatformError,
    parse_tournament_id,
    tournament_dir,
)
from autofill import safe_fetch_text  # noqa: E402 — module-level so tests can patch it

STATE_PATH = os.path.join(REPO_ROOT, "out", "link-state.json")
MAX_FETCH = 65536  # hash the first 64 KB — enough to detect real changes


def _normalize(text):
    """Collapse a page into a STABLE fingerprint: volatile bits that
    change on every render (timestamps, session tokens, counters, ad
    content) are stripped so only real content changes are detected."""
    import html as _html
    # Drop scripts/styles and tag bodies entirely
    t = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    t = re.sub(r"<style[\s\S]*?</style>", " ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t)
    # Volatile patterns: timestamps, dates, hex/session tokens,
    # counters, long number runs, cache-busting query params
    t = re.sub(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", " ", t)          # dates
    t = re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?\b", " ", t)               # times
    t = re.sub(r"\b[0-9a-f]{16,}\b", " ", t, flags=re.I)            # tokens
    t = re.sub(r"\b\d{3,}\b", " ", t)                                # big numbers
    t = re.sub(r"[?&](?:_\d+|cb=\d+|ts=\d+|\d+)=?", " ", t)          # cache-bust params
    t = re.sub(r"\s+", " ", t)
    return t.strip()[:4000]  # fingerprint window: first 4 KB of stable text


def _hash_content(text):
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()[:16]


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def watched_urls(tdir):
    """Collect the hotspot URLs from the tournament's own module data."""
    def load(name):
        p = os.path.join(tdir, name)
        if not os.path.exists(p):
            return {}
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    urls = []
    hotels = load("hotels.json").get("hotels", {})
    if hotels.get("portal"):
        urls.append(("stay-to-play portal", hotels["portal"]))
    rules = load("rules.json").get("rules", {})
    if rules.get("fullLink"):
        urls.append(("rules document", rules["fullLink"]))
    # NOTE: venue.mapsUrl / fields.map are NOT watched — map UIs re-render
    # with volatile session content on every visit, so hashing them would
    # spam false "changed" alerts. A real map change is a URL change in
    # the content repo, which git already tracks. Only CONTENT sources
    # (portal, rules, explicit watchUrls) are fingerprint-watched.
    updates = load("updates.json").get("updates", [])
    for u in updates:
        for wu in u.get("watchUrls", []):
            urls.append((u.get("title", "update source"), wu))
    return urls


def check_tournament(tdir, apply=False):
    """Fetch every watched URL, diff against last-seen state, and return
    the list of changes. With apply=True, drafts an update entry per
    change (validate-before-write via autofill.write_module)."""
    from datetime import datetime, timezone

    state = _load_state()
    key = os.path.relpath(tdir, REPO_ROOT)
    seen = state.setdefault(key, {})
    changes = []

    for label, url in watched_urls(tdir):
        try:
            # Fetch up to 2 MB (GotSport stores, Google Docs/Slides pub
            # pages are legitimately large); hash the first 64 KB.
            text = safe_fetch_text(url, max_bytes=2_000_000)
            digest = _hash_content(text[:MAX_FETCH])
        except PlatformError as e:
            # A fetch failure is NOT a change — the source may be down;
            # keep the last state and note it.
            changes.append({"source": label, "url": url,
                            "status": "unreachable", "detail": str(e)[:120]})
            continue
        prev = seen.get(url)
        if prev is None:
            changes.append({"source": label, "url": url,
                            "status": "seeded", "detail": "first check — baseline recorded"})
        elif prev != digest:
            changes.append({"source": label, "url": url,
                            "status": "changed", "detail": f"content changed ({prev} → {digest})"})
        seen[url] = digest

    _save_state(state)

    if apply:
        drafted = _draft_updates(tdir, [c for c in changes if c["status"] == "changed"])
        for c in changes:
            if c["status"] == "changed":
                c["drafted"] = True
    return changes


def _draft_updates(tdir, changes):
    """Append one draft update entry per real change, via the validated
    write path. Returns the list of entries drafted."""
    if not changes:
        return []
    from autofill import load_module, write_module
    from datetime import datetime
    from zoneinfo import ZoneInfo

    updates = load_module(tdir, "updates.json")
    updates.setdefault("updates", [])
    drafted = []
    # Parent-visible timestamps are EASTERN TIME (America/New_York) —
    # the family's home zone, matching the app's ET rendering.
    now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%dT%H:%M:%S%z")
    for c in changes:
        entry = {
            "type": "info",
            "title": f"{c['source']} updated",
            "description": (
                f"The {c['source']} ({c['url']}) has new content. "
                f"Review before confirming to parents."),
            "time": now,
            "posted": "link-watcher",
            "actionRequired": True,
        }
        updates["updates"].append(entry)
        drafted.append(entry)
    write_module(tdir, "updates.json", updates)
    return drafted


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["check"])
    ap.add_argument("tournament", help="org/slug")
    ap.add_argument("--apply", action="store_true",
                    help="write draft update entries (default: report only)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        org, slug = parse_tournament_id(args.tournament)
        tdir = tournament_dir(org, slug)
        if not os.path.isdir(tdir):
            raise PlatformError(f"no tournament dir at {tdir}")
        changes = check_tournament(tdir, apply=args.apply)
    except PlatformError as e:
        print(f"UPDATES ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({"tournament": args.tournament, "changes": changes}, indent=2))
    else:
        if not changes:
            print(f"no changes in watched sources ({args.tournament})")
        for c in changes:
            mark = {"seeded": "seed", "changed": "CHANGED", "unreachable": "unreachable"}[c["status"]]
            extra = f" → drafted update" if c.get("drafted") else ""
            print(f"  [{mark}] {c['source']}: {c['detail']}{extra}")
        if args.apply:
            print("draft updates written — validate, approve, publish to reach parents")


if __name__ == "__main__":
    main()
