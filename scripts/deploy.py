#!/usr/bin/env python3
"""Compile + validate + publish a tournament bundle to its target app repo.

Transactional safety (per external design review):
  - worktree must be CLEAN before anything is written — a staged app-shell
    change can never ride along in the publish commit
  - commit uses an explicit pathspec (git commit -- app/data.json) and the
    proposed commit diff is verified to contain exactly one permitted path
  - manifest.json is REQUIRED and status must be explicitly 'live'
    (missing manifest / missing status / unknown status = exit 2, never
    silently skipped)
  - the mirror is updated only AFTER push verification succeeds, via a
    temporary file + atomic replace
  - original file + starting HEAD are saved; pre-push failures restore the
    worktree; push failures print exact recovery instructions and never
    touch the mirror
  - every git return code is checked; local HEAD is verified == origin/main
    after push — a failed publish is never reported as success

Exit codes (see pipeline.py contract):
  0 success/no-op · 1 validation blocked · 2 config/usage · 3 publish/git
  4 external dependency (network)

Usage:
    python3 scripts/deploy.py <org>/<slug> [--dry-run] [--no-links]
                              [--refresh-links] [--allow-draft]
                              [--message "..."] [--json] [--quiet]
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from compile import compile_bundle, content_digest, serialize  # noqa: E402
from pipeline import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_CONFIG,
    EXIT_DEPENDENCY,
    EXIT_OK,
    EXIT_PUBLISH,
    PlatformError,
    check_publish_status,
    parse_tournament_id,
    resolve_target,
    tournament_dir,
)
from validate import Report, run_checks  # noqa: E402


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


class DeployResult:
    def __init__(self, status, message, exit_code, tournament, digest=None,
                 changed=False, blocking=0, warnings=0, destination=None):
        self.status = status            # noop | dryrun | published | blocked | error
        self.message = message
        self.exit_code = exit_code
        self.tournament = tournament
        self.digest = digest
        self.changed = changed
        self.blocking = blocking
        self.warnings = warnings
        self.destination = destination

    def to_dict(self):
        return {
            "status": self.status,
            "message": self.message,
            "exit_code": self.exit_code,
            "tournament": self.tournament,
            "digest": self.digest,
            "changed": self.changed,
            "blocking": self.blocking,
            "warnings": self.warnings,
            "destination": self.destination,
        }


def verify_workdir(target):
    """Verify the local git working copy: exists, right repo, main branch,
    and — critically — a CLEAN worktree and index. A staged or unstaged
    change (e.g. a pre-staged index.html) must abort the deploy."""
    workdir = os.path.expanduser(target.get("workDir", ""))
    if not workdir or not os.path.isdir(workdir):
        raise PlatformError(
            f"workDir '{workdir or '(not set)'}' missing. Clone the app repo:\n"
            f"  git clone https://github.com/{target['repo']}.git {workdir}",
            exit_code=EXIT_CONFIG)
    rc, out, err = run(["git", "remote", "get-url", "origin"], cwd=workdir)
    origin_url = out or err or "unknown"
    # Accept either the expected GitHub repo (owner/name appears in URL) or a
    # local filesystem path origin (tests, local workflows).
    repo_ok = target["repo"] in origin_url or origin_url.startswith(("/", ".", "file://"))
    if rc != 0 or not repo_ok:
        raise PlatformError(
            f"workDir {workdir} is not the {target['repo']} repo "
            f"(origin: {origin_url})", exit_code=EXIT_CONFIG)
    rc, out, _ = run(["git", "branch", "--show-current"], cwd=workdir)
    if rc != 0 or out != "main":
        raise PlatformError(
            f"workDir {workdir} is on branch '{out or '?'}' — expected 'main'",
            exit_code=EXIT_CONFIG)
    rc, out, _ = run(["git", "status", "--porcelain"], cwd=workdir)
    if rc != 0:
        raise PlatformError(f"git status failed in {workdir}", exit_code=EXIT_PUBLISH)
    if out:
        raise PlatformError(
            f"workDir {workdir} is NOT clean — deploy would risk committing "
            f"pre-staged or uncommitted files (e.g. an app-shell change):\n"
            f"{out}\n"
            f"Commit or stash those changes first (this is the sacred-shell "
            f"guard).", exit_code=EXIT_CONFIG)
    return workdir


def remote_bundle(workdir, app_path):
    rc, out, _ = run(["git", "show", f"origin/main:{app_path}"], cwd=workdir)
    return out if rc == 0 else None


def deploy_tournament(tournament, *, dry_run=False, run_link_checks=True,
                      refresh_links=False, allow_draft=False, message=None,
                      targets=None, workdir_override=None, tdir=None):
    """Full deploy pipeline. Returns a DeployResult; never raises for
    expected outcomes. Raises PlatformError only for config problems.
    tdir may be overridden (tests use scratch tournament copies)."""
    org, slug = parse_tournament_id(tournament)
    tdir = tdir or tournament_dir(org, slug)
    if not os.path.isdir(tdir):
        raise PlatformError(f"no tournament dir at {tdir}")

    # 1. Compile
    bundle, used, unknown = compile_bundle(tdir)
    output = serialize(bundle)
    digest = content_digest(output)
    bundle_data = json.loads(output)

    # 2. Validate — shared run_checks() path
    report = Report()
    run_checks(bundle_data, report, run_link_checks=run_link_checks,
               refresh_links=refresh_links, tdir=tdir)
    blocking = report.blocking()
    summary = report.summary()
    if blocking:
        return DeployResult(
            "blocked", "validation failed — see Guide Health Report",
            EXIT_BLOCKED, tournament, digest=digest, changed=False,
            blocking=summary["blocking"], warnings=summary["warnings"])

    # 3. Status gate — manifest REQUIRED, status explicitly live, and the
    #    current content digest must match the human-approved revision.
    #    (Skipped for dry-run: previews must show the diff even for
    #    unapproved content — the gate only blocks actual publishing.)
    if not dry_run:
        status_msg = check_publish_status(tdir, digest, allow_draft=allow_draft)
    else:
        status_msg = "preview (dry-run — gate not enforced)"

    # 4. Verify target + worktree
    target = resolve_target(tournament, targets=targets)
    workdir = workdir_override or verify_workdir(target)
    app_path = target["appPath"]
    git_data = os.path.join(workdir, *app_path.split("/"))

    # 5. Fetch + semantic diff vs origin/main
    rc, _, err = run(["git", "fetch", "origin"], cwd=workdir)
    if rc != 0:
        raise PlatformError(f"git fetch failed in {workdir}: {err or 'unknown error'}",
                            exit_code=EXIT_PUBLISH)
    remote = remote_bundle(workdir, app_path)
    if remote is None:
        changed = True
        diff_note = "no previous bundle at origin/main — initial publish"
    else:
        try:
            live = json.loads(remote)
            changed = live != bundle_data
        except json.JSONDecodeError:
            changed = True
            diff_note = "remote data.json unreadable — treating as change"
        else:
            diff_note = ("content differs" if changed
                         else "semantically identical — no content change")

    # 6. Dry-run or no-op
    if dry_run or not changed:
        status = "dryrun" if dry_run and changed else "noop"
        msg = (f"(dry-run) would write {app_path} in {workdir} and push to "
               f"{target['repo']}" if status == "dryrun"
               else "nothing to publish — app repo untouched")
        return DeployResult(status, msg, EXIT_OK, tournament, digest=digest,
                            changed=changed, warnings=summary["warnings"],
                            destination=f"{target['repo']}/{app_path}")

    # 7. Publish — transactionally
    # Save starting HEAD for rollback (worktree was verified clean, so a
    # hard reset to this commit fully restores file + index + branch).
    rc, start_head, _ = run(["git", "rev-parse", "HEAD"], cwd=workdir)
    start_head = start_head if rc == 0 else None

    try:
        os.makedirs(os.path.dirname(git_data), exist_ok=True)
        with open(git_data, "w", encoding="utf-8") as f:
            f.write(output)

        # Explicit pathspec — never `git add -A`
        rc, _, err = run(["git", "add", "--", app_path], cwd=workdir)
        if rc != 0:
            raise PlatformError(f"git add failed: {err}", exit_code=EXIT_PUBLISH)

        # Verify the staged diff contains EXACTLY the permitted path
        rc, staged, _ = run(["git", "diff", "--cached", "--name-only"], cwd=workdir)
        if rc != 0 or staged != app_path:
            raise PlatformError(
                f"staged diff is not exactly '{app_path}' — got: {staged or '(empty)'}. "
                f"Aborting to protect the app shell.", exit_code=EXIT_PUBLISH)

        msg = message or f"data: publish {slug} bundle (digest {digest[:10]})"
        rc, _, err = run(["git", "commit", "-m", msg, "--", app_path], cwd=workdir)
        if rc != 0:
            raise PlatformError(f"git commit failed: {err} (nothing was pushed)",
                                exit_code=EXIT_PUBLISH)

        rc, _, err = run(["git", "push", "origin", "main"], cwd=workdir)
        if rc != 0:
            raise PlatformError(
                f"git push FAILED: {err}\n"
                f"The local worktree has been rolled back to its starting state "
                f"and the mirror was NOT updated. Fix the push problem "
                f"(auth, remote URL, branch protection) and re-run deploy.",
                exit_code=EXIT_PUBLISH)

        # Post-push verification: local HEAD must equal origin/main
        rc, local_head, _ = run(["git", "rev-parse", "HEAD"], cwd=workdir)
        rc2, remote_head, _ = run(["git", "rev-parse", "origin/main"], cwd=workdir)
        if rc != 0 or rc2 != 0 or local_head != remote_head:
            raise PlatformError(
                "push reported ok but local HEAD ≠ origin/main — check the repo "
                "manually before trusting publication.", exit_code=EXIT_PUBLISH)

        # SUCCESS — only now update the mirror, atomically
        mirror = target.get("mirrorTo")
        if mirror:
            mirror_path = os.path.expanduser(mirror)
            os.makedirs(os.path.dirname(mirror_path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(mirror_path),
                                       prefix=".data.json.tmp.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(output)
                os.replace(tmp, mirror_path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

        # SUCCESS — record the published transition in the source manifest
        # (only when the tournament lives inside the content repo; tests
        # using scratch dirs skip this). Best-effort: publication itself
        # already succeeded — a manifest-record failure must not flip the
        # result to failure.
        try:
            _record_published(tdir, digest)
        except Exception as e:  # noqa: BLE001
            print(f"      warning: could not record published revision: {e}")

        return DeployResult(
            "published", f"published {digest[:10]} → {target['repo']}/{app_path}",
            EXIT_OK, tournament, digest=digest, changed=True,
            warnings=summary["warnings"],
            destination=f"{target['repo']}/{app_path}")
    except PlatformError:
        # Pre-push failure → roll back to starting state, never touch mirror
        _restore_worktree(workdir, start_head)
        raise
    except BaseException:
        _restore_worktree(workdir, start_head)
        raise


def _record_published(tdir, digest):
    """After a successful push, stamp the source manifest revision as
    published (with the exact digest + timestamp) and commit it to the
    content repo. Only runs when the tournament dir is inside the content
    repo (tests with scratch dirs are skipped)."""
    from pipeline import REVISION_PUBLISHED, write_revision
    # Only record when the tournament is actually part of the content repo
    repo_orgs = os.path.join(REPO_ROOT, "orgs")
    if os.path.commonpath([os.path.realpath(tdir), os.path.realpath(repo_orgs)]) \
            != os.path.realpath(repo_orgs):
        return
    manifest_path = os.path.join(tdir, "manifest.json")
    write_revision(tdir, REVISION_PUBLISHED, digest, manifest=None)
    # Commit the manifest change to the content repo (best-effort — the
    # app repo publication already succeeded; this is source-of-truth
    # bookkeeping so the revision workflow reflects reality).
    rc, _, err = run(["git", "add", os.path.relpath(manifest_path, REPO_ROOT)], cwd=REPO_ROOT)
    if rc != 0:
        raise RuntimeError(f"git add manifest failed: {err}")
    msg = f"revision: mark {os.path.basename(tdir)} published (digest {digest[:10]})"
    rc, _, err = run(["git", "commit", "-m", msg, "--",
                      os.path.relpath(manifest_path, REPO_ROOT)], cwd=REPO_ROOT)
    if rc != 0 and "nothing to commit" not in err:
        raise RuntimeError(f"git commit manifest failed: {err}")


def _restore_worktree(workdir, start_head):
    """Roll back a failed publish to the starting state. The worktree was
    verified clean before the deploy, so a hard reset to the starting HEAD
    fully restores file + index + branch."""
    if start_head:
        run(["git", "reset", "--hard", start_head], cwd=workdir)
    else:
        run(["git", "reset", "HEAD", "--", "."], cwd=workdir)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tournament", help="org/slug")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-links", action="store_true")
    ap.add_argument("--refresh-links", action="store_true")
    ap.add_argument("--allow-draft", action="store_true")
    ap.add_argument("--message", default=None, help="git commit message")
    ap.add_argument("--json", action="store_true", help="structured output")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        result = deploy_tournament(
            args.tournament,
            dry_run=args.dry_run,
            run_link_checks=not args.no_links,
            refresh_links=args.refresh_links,
            allow_draft=args.allow_draft,
            message=args.message,
        )
    except PlatformError as e:
        result = DeployResult("error", str(e), e.exit_code, args.tournament)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    elif not args.quiet:
        print(result.message)
    sys.exit(result.exit_code)


if __name__ == "__main__":
    main()
