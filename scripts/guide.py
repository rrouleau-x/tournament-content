#!/usr/bin/env python3
"""Operator-friendly wrapper around the pipeline. This is the supported
interface for non-technical users — no need to know about venvs, Python
paths, or which script does what.

Usage:
    python3 scripts/guide.py new <org>/<slug> [--from <existing-slug>]
    python3 scripts/guide.py check <org>/<slug> [--no-links]
    python3 scripts/guide.py preview <org>/<slug> [--no-links]
    python3 scripts/guide.py publish <org>/<slug> [--no-links] [--allow-draft]
    python3 scripts/guide.py list

Internally: check = compile + validate; preview = check + dry-run deploy
diff; publish = check + status gate + transactional publish.
"""

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import (  # noqa: E402
    PlatformError,
    parse_tournament_id,
    tournament_dir,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_SLUG = "sporting-jax-2026"


def safe_to_publish(tournament, report, deploy_info=None):
    """Human-readable operator summary (the 'SAFE TO PUBLISH' screen)."""
    s = report.summary()
    lines = [
        "SAFE TO PUBLISH" if s["blocking"] == 0 else "NOT SAFE TO PUBLISH",
        f"Tournament: {tournament}",
        f"Blocking issues: {s['blocking']}",
        f"Warnings: {s['warnings']}",
        f"Checks passed: {s['passed']}",
    ]
    if deploy_info:
        lines.append(f"Content changed: {'Yes' if deploy_info.changed else 'No'}")
        if deploy_info.destination:
            lines.append(f"Destination: {deploy_info.destination}")
    return "\n".join(lines)


def cmd_new(args):
    org, slug = parse_tournament_id(args.tournament)
    src = tournament_dir(org, TEMPLATE_SLUG)
    dst = tournament_dir(org, slug)
    if os.path.exists(dst):
        print(f"ERROR: tournament already exists at {dst}", file=sys.stderr)
        return 2
    if not os.path.isdir(src):
        print(f"ERROR: template tournament missing at {src}", file=sys.stderr)
        return 2
    shutil.copytree(src, dst)
    # New tournaments start as drafts — never publishable by accident
    manifest_path = os.path.join(dst, "manifest.json")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest["slug"] = slug
    manifest["status"] = "draft"
    manifest["name"] = f"{slug.replace('-', ' ').title()} (edit me)"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Created {org}/{slug} from template ({TEMPLATE_SLUG}).")
    print(f"  Status: draft — set manifest status to 'live' before publishing.")
    print(f"  Edit module files in {dst}")
    return 0


def cmd_check(args):
    from compile import compile_bundle, content_digest, serialize
    from validate import Report, run_checks

    org, slug = parse_tournament_id(args.tournament)
    tdir = tournament_dir(org, slug)
    if not os.path.isdir(tdir):
        print(f"ERROR: no tournament dir at {tdir}", file=sys.stderr)
        return 2
    bundle, used, unknown = compile_bundle(tdir)
    output = serialize(bundle)
    report = Report()
    run_checks(bundle, report, run_link_checks=not args.no_links,
               refresh_links=args.refresh_links, tdir=tdir)
    print(report.render())
    print(f"  digest: {content_digest(output)} · modules: {len(used)}")
    if args.json:
        print(json.dumps({"tournament": args.tournament, **report.to_dict()}, indent=2))
    print()
    print(safe_to_publish(args.tournament, report))
    return 1 if report.blocking() else 0


def cmd_preview(args):
    from deploy import deploy_tournament
    from validate import Report, run_checks
    from compile import compile_bundle, content_digest, serialize

    org, slug = parse_tournament_id(args.tournament)
    tdir = tournament_dir(org, slug)
    bundle, used, unknown = compile_bundle(tdir)
    output = serialize(bundle)
    report = Report()
    run_checks(bundle, report, run_link_checks=not args.no_links,
               refresh_links=args.refresh_links, tdir=tdir)
    if report.blocking():
        print(report.render())
        print("\nNOT SAFE TO PUBLISH — fix blocking issues first.")
        return 1
    result = deploy_tournament(args.tournament, dry_run=True,
                               run_link_checks=not args.no_links,
                               refresh_links=args.refresh_links)
    print(report.render())
    print()
    print(safe_to_publish(args.tournament, report, deploy_info=result))
    print(result.message)
    return 0


def cmd_approve(args):
    """Approve the current content digest — required before publish.
    Shared with the admin server (validates first, never approves broken
    content)."""
    from pipeline import approve_tournament

    org, slug = parse_tournament_id(args.tournament)
    tdir = tournament_dir(org, slug)
    if not os.path.isdir(tdir):
        print(f"ERROR: no tournament dir at {tdir}", file=sys.stderr)
        return 2
    result = approve_tournament(tdir, reviewer=args.reviewer)
    print(f"Approved {args.tournament} (digest {result['digest'][:10]}).")
    print(f"  Status: set manifest status to 'live' if not already, then guide.py publish")
    return 0


def cmd_publish(args):
    from deploy import deploy_tournament
    from validate import Report, run_checks
    from compile import compile_bundle, content_digest, serialize

    org, slug = parse_tournament_id(args.tournament)
    tdir = tournament_dir(org, slug)
    bundle, used, unknown = compile_bundle(tdir)
    output = serialize(bundle)
    report = Report()
    run_checks(bundle, report, run_link_checks=not args.no_links,
               refresh_links=args.refresh_links, tdir=tdir)
    if report.blocking():
        print(report.render())
        print("\nNOT SAFE TO PUBLISH — fix blocking issues first.")
        return 1
    result = deploy_tournament(args.tournament, run_link_checks=not args.no_links,
                               refresh_links=args.refresh_links,
                               allow_draft=args.allow_draft,
                               record_published=True)  # real publish: update source manifest
    print(report.render())
    print()
    print(safe_to_publish(args.tournament, report, deploy_info=result))
    print(result.message)
    return result.exit_code


def cmd_list(_args):
    import glob
    tournaments = sorted(
        os.path.relpath(d, os.path.join(REPO_ROOT, "orgs"))
        for d in glob.glob(os.path.join(REPO_ROOT, "orgs", "*", "tournaments", "*"))
        if os.path.isdir(d)
    )
    if not tournaments:
        print("No tournaments found.")
        return 0
    print("Tournaments:")
    for t in tournaments:
        org, slug = t.split("/tournaments/")
        mpath = os.path.join(REPO_ROOT, "orgs", org, "tournaments", slug, "manifest.json")
        status = "?"
        if os.path.exists(mpath):
            try:
                with open(mpath, encoding="utf-8") as f:
                    status = json.load(f).get("status", "?")
            except json.JSONDecodeError:
                status = "invalid manifest"
        print(f"  {org}/{slug}  [status: {status}]")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="scaffold a tournament from the template")
    p_new.add_argument("tournament", help="org/slug")
    p_new.set_defaults(func=cmd_new)

    p_check = sub.add_parser("check", help="compile + validate (Guide Health Report)")
    p_check.add_argument("tournament", help="org/slug")
    p_check.add_argument("--no-links", action="store_true")
    p_check.add_argument("--refresh-links", action="store_true")
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=cmd_check)

    p_preview = sub.add_parser("preview", help="check + dry-run publish diff")
    p_preview.add_argument("tournament", help="org/slug")
    p_preview.add_argument("--no-links", action="store_true")
    p_preview.add_argument("--refresh-links", action="store_true")
    p_preview.set_defaults(func=cmd_preview)

    p_pub = sub.add_parser("publish", help="check + status gate + publish")
    p_pub.add_argument("tournament", help="org/slug")
    p_pub.add_argument("--no-links", action="store_true")
    p_pub.add_argument("--refresh-links", action="store_true")
    p_pub.add_argument("--allow-draft", action="store_true")
    p_pub.set_defaults(func=cmd_publish)

    p_appr = sub.add_parser("approve", help="approve current content digest (required before publish)")
    p_appr.add_argument("tournament", help="org/slug")
    p_appr.add_argument("--no-links", action="store_true")
    p_appr.add_argument("--refresh-links", action="store_true")
    p_appr.add_argument("--reviewer", default="admin", help="who is approving (default: admin)")
    p_appr.set_defaults(func=cmd_approve)

    p_list = sub.add_parser("list", help="list tournaments with status")
    p_list.set_defaults(func=cmd_list)

    args = ap.parse_args()
    try:
        sys.exit(args.func(args))
    except PlatformError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(e.exit_code)


if __name__ == "__main__":
    main()
