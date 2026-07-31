#!/usr/bin/env python3
"""Compile + validate + publish a tournament bundle to its target app repo.

Multi-tournament aware: each tournament maps to a publish target via
_targets.json at the repo root:

    {
      "<org>/<slug>": {
        "repo": "rrouleau-x/sporting-jax-guide",   # GitHub repo (owner/name)
        "appPath": "app/data.json",                 # file inside the repo to replace
        "workDir": "/tmp/sporting-jax-guide",       # local git working copy
        "mirrorTo": "~/.hermes/www/app/data.json"   # optional extra local copy
      }
    }

The app shell (index.html, sw.js, manifest.json) is NEVER touched — only the
appPath file (data.json) is replaced, and only when content actually changed
(semantic JSON comparison against origin/main after a fetch).

Safety rails:
  - validation must pass (blocking failures abort; shared run_checks() path)
  - manifest status must be "live" (use --allow-draft to override)
  - workDir is verified: exists, right remote, right branch, clean-ish
  - git fetch happens before diffing; every git command's return code is
    checked — a failed push is reported as failure, never as success
  - --dry-run performs compile + validate + diff but never writes or pushes

Usage:
    python3 scripts/deploy.py <org>/<slug> [--dry-run] [--no-links]
                              [--allow-draft] [--message "..."]

Exit codes: 0 = published/no-op · 1 = validation blocked · 2 = setup/error
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS_PATH = os.path.join(REPO_ROOT, "_targets.json")

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))


def run(cmd, cwd=None):
    """Run a command; return (returncode, stdout, stderr)."""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def die(code, msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def load_targets():
    if not os.path.exists(TARGETS_PATH):
        die(2, f"no {TARGETS_PATH} — add a publish target for this tournament")
    with open(TARGETS_PATH, encoding="utf-8") as f:
        return json.load(f)


def verify_workdir(target):
    """Verify the local git working copy is the right repo, on main, usable."""
    workdir = os.path.expanduser(target.get("workDir", ""))
    if not workdir or not os.path.isdir(workdir):
        die(2,
            f"workDir '{workdir or '(not set)'}' missing. Clone the app repo:\n"
            f"  git clone https://github.com/{target['repo']}.git {workdir}")
    rc, out, err = run(["git", "remote", "get-url", "origin"], cwd=workdir)
    if rc != 0 or target["repo"] not in (out or ""):
        die(2, f"workDir {workdir} is not the {target['repo']} repo "
               f"(origin: {out or err or 'unknown'})")
    rc, out, _ = run(["git", "branch", "--show-current"], cwd=workdir)
    if rc != 0 or out != "main":
        die(2, f"workDir {workdir} is on branch '{out or '?'}' — expected 'main'")
    return workdir


def remote_bundle(workdir, app_path):
    """Return the data.json content at origin/main, or None if absent."""
    rc, out, _ = run(["git", "show", f"origin/main:{app_path}"], cwd=workdir)
    return out if rc == 0 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tournament", help="org/slug")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-links", action="store_true")
    ap.add_argument("--refresh-links", action="store_true")
    ap.add_argument("--allow-draft", action="store_true")
    ap.add_argument("--message", default=None, help="git commit message")
    args = ap.parse_args()

    org, slug = args.tournament.split("/", 1)
    targets = load_targets()
    if args.tournament not in targets:
        die(2, f"no publish target for '{args.tournament}' in {TARGETS_PATH} — "
               f"add one, e.g. {{\"{args.tournament}\": {{\"repo\": \"...\", "
               f"\"appPath\": \"app/data.json\", \"workDir\": \"...\"}}}}")
    target = targets[args.tournament]

    # 1. Compile (clean, actionable errors)
    from compile import CompileError, compile_bundle, serialize, tournament_dir
    tdir = tournament_dir(org, slug)
    try:
        bundle, used = compile_bundle(tdir)
    except CompileError as e:
        die(1, f"compile: {e}")
    output = serialize(bundle)
    digest = hashlib.sha1(output.encode("utf-8")).hexdigest()
    print(f"[1/5] compiled {len(used)} modules → sha1 {digest[:10]}")

    # 2. Validate — shared run_checks() path (same code the CLI uses)
    from validate import Report, run_checks
    bundle_data = json.loads(output)
    report = Report()
    run_checks(bundle_data, report,
               run_link_checks=not args.no_links,
               refresh_links=args.refresh_links)
    print(report.render())
    blocking = report.blocking()
    if blocking:
        print(f"\nDEPLOY ABORTED — {len(blocking)} blocking issue(s).")
        sys.exit(1)
    print("[2/5] validation passed (0 blocking)")

    # 3. Status gate: drafts must not reach parents
    manifest_path = os.path.join(tdir, "manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        status = manifest.get("status", "live")
        if status != "live" and not args.allow_draft:
            print(f"[3/5] DEPLOY BLOCKED — manifest status is '{status}', not 'live'. "
                  f"Set status to 'live' to publish, or use --allow-draft.")
            sys.exit(1)
        print(f"[3/5] manifest status '{status}' — publish allowed")
    else:
        print(f"[3/5] no manifest.json (status gate skipped)")

    # 4. Verify working copy + fetch + semantic diff vs origin/main
    workdir = verify_workdir(target)
    app_path = target["appPath"]
    rc, out, err = run(["git", "fetch", "origin"], cwd=workdir)
    if rc != 0:
        die(2, f"git fetch failed in {workdir}: {err or out}")
    print(f"[4/5] fetched origin in {workdir}")

    remote = remote_bundle(workdir, app_path)
    if remote is None:
        changed = True
        print(f"[4/5] no previous bundle at origin/main:{app_path} — initial publish")
    else:
        try:
            live = json.loads(remote)
            changed = live != bundle_data
        except json.JSONDecodeError:
            changed = True  # remote file unreadable → treat as change
        if changed:
            print(f"[4/5] bundle content differs from origin/main:{app_path} — change detected")
        else:
            print(f"[4/5] bundle semantically identical to origin/main:{app_path} — no content change")

    if args.dry_run or not changed:
        if changed:
            print(f"[5/5] (dry-run) would write {app_path} in {workdir} and push to {target['repo']}")
        else:
            print("[5/5] (no-op) nothing to publish — app repo untouched")
        print("DONE")
        sys.exit(0)

    # 5. Write, mirror, commit, push — every git return code checked
    git_data = os.path.join(workdir, *app_path.split("/"))
    os.makedirs(os.path.dirname(git_data), exist_ok=True)
    with open(git_data, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"[5/5] wrote {git_data} ({len(output)} bytes)")

    mirror = target.get("mirrorTo")
    if mirror:
        mirror_path = os.path.expanduser(mirror)
        os.makedirs(os.path.dirname(mirror_path), exist_ok=True)
        shutil.copyfile(git_data, mirror_path)
        print(f"      mirrored to {mirror_path}")

    msg = args.message or f"data: publish {slug} bundle (sha1 {digest[:10]})"
    rc, _, err = run(["git", "add", app_path], cwd=workdir)
    if rc != 0:
        die(2, f"git add failed: {err}")
    rc, out, err = run(["git", "commit", "-m", msg], cwd=workdir)
    if rc != 0:
        die(2, f"git commit failed: {err or out} (nothing was pushed)")
    print(f"      commit: {out.splitlines()[-1] if out else 'ok'}")

    rc, out, err = run(["git", "push", "origin", "main"], cwd=workdir)
    if rc != 0:
        die(2, f"git push FAILED — commit exists locally but is NOT on GitHub: {err or out}")
    print(f"      push: {out.splitlines()[-1] if out else 'ok'}")

    # Post-push verification: local HEAD must match remote
    rc, local_head, _ = run(["git", "rev-parse", "HEAD"], cwd=workdir)
    rc2, remote_head, _ = run(["git", "rev-parse", "origin/main"], cwd=workdir)
    if rc == 0 and rc2 == 0 and local_head == remote_head:
        print("      verified: local HEAD == origin/main")
        print("DONE — published to GitHub (Pages CDN ~1-2 min)")
        sys.exit(0)
    die(2, "push reported ok but local HEAD ≠ origin/main — check the repo manually")


if __name__ == "__main__":
    main()
