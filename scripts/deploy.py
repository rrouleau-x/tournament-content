#!/usr/bin/env python3
"""Compile + validate + publish a tournament bundle to the app repo.

Wraps the existing app deploy workflow (see tournament-companion-pwa skill):
  1. compile modules → bundle
  2. validate (blocking failures abort the deploy)
  3. write bundle to the app source dir (~/.hermes/www/app/data.json)
  4. copy to the app git working copy (/tmp/sporting-jax-guide/app/data.json)
  5. commit + push (only when content actually changed)

The app shell (index.html, sw.js, manifest.json) is NEVER touched — this
script only replaces the data.json content file.

Usage:
    python3 scripts/deploy.py <org>/<slug> [--dry-run] [--no-links] [--message "..."]

    --dry-run   run compile + validate + diff checks, but do not commit/push.
                Reports whether the app repo would have any diff.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_SOURCE_DIR = os.path.expanduser("~/.hermes/www/app")
APP_GIT_DIR = "/tmp/sporting-jax-guide"
APP_DATA_REL = os.path.join("app", "data.json")

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tournament", help="org/slug")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-links", action="store_true")
    ap.add_argument("--message", default=None, help="git commit message")
    args = ap.parse_args()

    org, slug = args.tournament.split("/", 1)

    # 1. Compile
    from compile import compile_bundle, serialize, tournament_dir

    tdir = tournament_dir(org, slug)
    bundle, used = compile_bundle(tdir)
    output = serialize(bundle)
    digest = hashlib.sha1(output.encode("utf-8")).hexdigest()
    print(f"[1/5] compiled {len(used)} modules → sha1 {digest[:10]}")

    # 2. Validate (reuses validate.py checks)
    from validate import Report, check_link, collect_urls, get_path
    from validate import CRITICAL_LINK_PATHS, REQUIRED_FIELDS, SCHEMA_PATH
    from jsonschema import Draft202012Validator

    bundle_data = json.loads(output)
    report = Report()
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(bundle_data), key=lambda e: list(e.path))
    if errors:
        for err in errors[:10]:
            loc = "/".join(str(p) for p in err.path) or "(root)"
            report.fail("schema", f"{loc}: {err.message}")
    else:
        report.ok("schema", "bundle conforms to bundle-v1.json")
    for path, label in REQUIRED_FIELDS:
        if get_path(bundle_data, path) in (None, "", []):
            report.fail("required", f"missing {label} ({'/'.join(path)})")
        else:
            report.ok("required", f"{label} present")
    if not args.no_links:
        for path, url in collect_urls(bundle_data):
            ok, detail = check_link(url)
            critical = list(path) in CRITICAL_LINK_PATHS
            if ok:
                report.ok("links", f"{detail} · {'/'.join(path)}")
            elif critical:
                report.fail("links", f"{detail} · CRITICAL {'/'.join(path)}")
            else:
                report.warn("links", f"{detail} · {'/'.join(path)}")
    else:
        report.warn("links", "skipped (--no-links)")
    print(report.render())
    blocking = report.blocking()
    if blocking:
        print(f"\nDEPLOY ABORTED — {len(blocking)} blocking issue(s).")
        sys.exit(1)
    print("[2/5] validation passed (0 blocking)")

    # 3. Compare against the live app source — semantically (JSON equality).
    #    Whitespace/formatting differences do NOT count as content changes:
    #    the app parses JSON, and Phase 1 must keep the app repo at zero
    #    commits. Real content edits (schedule, hotel rates, …) do publish.
    app_data = os.path.join(APP_SOURCE_DIR, "data.json")
    changed = False
    if not os.path.exists(app_data):
        changed = True
    else:
        try:
            with open(app_data, encoding="utf-8") as f:
                live = json.load(f)
            changed = live != bundle_data
        except json.JSONDecodeError:
            changed = True  # live file unreadable → treat as change
    if not changed:
        print(f"[3/5] bundle semantically identical to {app_data} — no content change to publish")
    else:
        print(f"[3/5] bundle content differs from {app_data} — content change detected")

    # 4. Dry-run or no-op
    if args.dry_run or not changed:
        if changed:
            print(f"[4/5] (dry-run) would write {app_data} and push to app repo")
        else:
            print("[4/5] (no-op) app repo stays untouched")
        print("[5/5] DONE")
        sys.exit(0)

    # 5. Write to app source, copy to git working copy, commit, push
    os.makedirs(APP_SOURCE_DIR, exist_ok=True)
    with open(app_data, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"[4/5] wrote {app_data} ({len(output)} bytes)")

    if not os.path.isdir(APP_GIT_DIR):
        print(f"ERROR: app git copy missing at {APP_GIT_DIR}", file=sys.stderr)
        sys.exit(1)
    git_data = os.path.join(APP_GIT_DIR, APP_DATA_REL)
    os.makedirs(os.path.dirname(git_data), exist_ok=True)
    shutil.copyfile(app_data, git_data)

    msg = args.message or f"data: publish {slug} bundle (sha1 {digest[:10]})"
    r = run(["git", "status", "--porcelain"], cwd=APP_GIT_DIR)
    if not r.stdout.strip():
        print("[5/5] app repo has no diff — nothing to commit (content unchanged)")
        print("DONE")
        sys.exit(0)
    run(["git", "add", APP_DATA_REL], cwd=APP_GIT_DIR)
    r = run(["git", "commit", "-m", msg], cwd=APP_GIT_DIR)
    print(f"[5/5] commit: {r.stdout.strip() or r.stderr.strip()}")
    r = run(["git", "push", "origin", "main"], cwd=APP_GIT_DIR)
    print(f"      push: {r.stdout.strip() or r.stderr.strip()}")
    print("DONE — published to GitHub Pages (CDN ~1-2 min)")


if __name__ == "__main__":
    main()
