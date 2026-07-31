#!/usr/bin/env python3
"""Shared platform module: single source of truth for module definitions,
tournament ID parsing, manifest/status rules, publish targets, and the
platform error/exit-code contract.

All pipeline scripts import from here so conventions can't drift
(compile.py and split.py share MODULE_REGISTRY; every script shares the
same tournament-ID parser and error boundary).

Exit-code contract (all commands):
  0  success — including unchanged/no-op
  1  content validation blocked
  2  configuration or usage error (bad ID, missing manifest, bad target)
  3  publication / git failure
  4  external dependency failure (network link checks)
"""

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS_PATH = os.path.join(REPO_ROOT, "_targets.json")

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_CONFIG = 2
EXIT_PUBLISH = 3
EXIT_DEPENDENCY = 4

# Canonical module registry — the ONE source of truth.
# (filename, [top-level bundle keys contributed], required-in-bundle?)
MODULE_REGISTRY = [
    ("sport.json",        ["sport", "sportConfig"],               True),
    ("tournament.json",   ["tournament"],                          True),
    ("team.json",         ["team"],                                True),
    ("venue.json",        ["venue"],                               True),
    ("schedule.json",     ["games", "scheduleStatus", "scheduleExpected"], True),
    ("hotels.json",       ["hotels"],                              False),
    ("weather.json",      ["weather"],                             False),
    ("rules.json",        ["rules"],                               False),
    ("updates.json",      ["updates"],                             True),
    ("contacts.json",     ["contacts"],                            True),
    ("venue-rules.json",  ["venueRules"],                          False),
    ("checklist.json",    ["checklist"],                           True),
    ("nearby.json",       ["nearby"],                              False),
    ("offline.json",      ["offline"],                             False),
]

# Files allowed in a tournament directory that are NOT content modules.
KNOWN_NON_MODULES = {"manifest.json"}

# Asset-bearing fields: values may be a local file path (relative to the
# tournament directory) or a full URL. The validator checks local paths
# actually exist.
ASSET_FIELDS = [
    ["team", "logo"],
    ["offline", "pdfGuideUrl"],
]


# Revision workflow states (manifest.revision.workflow).
# draft → in_review → approved → published. Approval is tied to a specific
# content digest: publishing requires the CURRENT content to be the one that
# was human-approved.
REVISION_DRAFT = "draft"
REVISION_IN_REVIEW = "in_review"
REVISION_APPROVED = "approved"
REVISION_PUBLISHED = "published"
REVISION_WORKFLOWS = (REVISION_DRAFT, REVISION_IN_REVIEW, REVISION_APPROVED, REVISION_PUBLISHED)


class PlatformError(Exception):
    """Expected input/configuration failure → stable message + exit code."""

    def __init__(self, message, exit_code=EXIT_CONFIG):
        super().__init__(message)
        self.exit_code = exit_code


def parse_tournament_id(raw):
    """'org/slug' → (org, slug). Raises PlatformError on malformed input."""
    if not raw or "/" not in raw:
        raise PlatformError(
            f"invalid tournament id '{raw}' — expected format <org>/<slug> "
            f"(e.g. savannah-united/sporting-jax-2026)"
        )
    org, slug = raw.split("/", 1)
    org, slug = org.strip(), slug.strip()
    if not org or not slug or "/" in slug:
        raise PlatformError(
            f"invalid tournament id '{raw}' — expected format <org>/<slug> "
            f"(exactly one slash)"
        )
    return org, slug


def tournament_dir(org, slug):
    return os.path.join(REPO_ROOT, "orgs", org, "tournaments", slug)


def load_json(path, what):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise PlatformError(f"{what} not found: {path}") from None
    except json.JSONDecodeError as e:
        raise PlatformError(
            f"{what} is not valid JSON: {e.msg} (line {e.lineno}, col {e.colno})"
        ) from None


def load_manifest(tdir):
    """Load manifest.json. Missing or invalid manifest is a config error —
    the deploy status gate must never be skipped because metadata is absent."""
    return load_json(os.path.join(tdir, "manifest.json"), "manifest.json")


def load_revision(tdir):
    """Return the manifest revision object ({} if absent)."""
    manifest = load_manifest(tdir)
    rev = manifest.get("revision")
    return rev if isinstance(rev, dict) else {}


def write_revision(tdir, workflow, digest, reviewer=None, manifest=None):
    """Write the manifest revision object. Returns the updated manifest.
    workflow must be one of REVISION_WORKFLOWS."""
    if workflow not in REVISION_WORKFLOWS:
        raise PlatformError(f"invalid revision workflow '{workflow}' — expected "
                            f"one of {', '.join(REVISION_WORKFLOWS)}")
    from datetime import datetime, timezone
    manifest = manifest if manifest is not None else load_manifest(tdir)
    rev = dict(manifest.get("revision") or {})
    rev["workflow"] = workflow
    rev["digest"] = digest
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if workflow == REVISION_APPROVED:
        rev["reviewer"] = reviewer or "admin"
        rev["approvedAt"] = now
    elif workflow == REVISION_PUBLISHED:
        rev["publishedAt"] = now
    manifest["revision"] = rev
    with open(os.path.join(tdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return manifest


def check_publish_status(tdir, digest, allow_draft=False):
    """Enforce the publish gate: manifest REQUIRED, lifecycle status explicit,
    and the current content digest must match the human-approved revision.

    Returns (status, message). 'live' lifecycle + approved digest = publish
    allowed. Missing manifest / missing status / missing revision / digest
    mismatch / non-live status all block unless --allow-draft."""
    manifest = load_manifest(tdir)
    status = manifest.get("status")
    if not status:
        raise PlatformError(
            "manifest.json has no 'status' field — set it explicitly "
            "(e.g. 'draft' while working, 'live' to publish)"
        )
    rev = manifest.get("revision") or {}
    workflow = rev.get("workflow")
    approved_digest = rev.get("digest")

    # Lifecycle gate first: only 'live' tournaments publish (unless --allow-draft)
    if status != "live" and not allow_draft:
        raise PlatformError(
            f"manifest status is '{status}', not 'live' — set status to 'live' "
            f"to publish, or use --allow-draft"
        )
    if allow_draft:
        return status, (f"publish allowed via --allow-draft (status '{status}', "
                        f"revision '{workflow or 'none'}')")

    # Then revision gate: current content must be the approved/published digest
    if workflow == REVISION_PUBLISHED and approved_digest == digest:
        return status, f"revision '{workflow}' matches current content (digest {digest[:10]})"
    if workflow == REVISION_APPROVED and approved_digest == digest:
        return status, f"revision '{workflow}' — current content approved (digest {digest[:10]})"
    if workflow in (None, REVISION_DRAFT, REVISION_IN_REVIEW):
        raise PlatformError(
            f"revision workflow is '{workflow or 'none'}', not 'approved' — "
            f"approve this content first (guide.py approve <org>/<slug>), "
            f"or use --allow-draft"
        )
    raise PlatformError(
        f"revision digest mismatch: current content {digest[:10]} ≠ approved "
        f"{approved_digest[:10] if approved_digest else 'none'}. The content "
        f"changed after approval — re-approve (guide.py approve <org>/<slug>), "
        f"or use --allow-draft"
    )


def load_targets():
    if not os.path.exists(TARGETS_PATH):
        raise PlatformError(f"no {TARGETS_PATH} — add a publish target for this tournament")
    return load_json(TARGETS_PATH, TARGETS_PATH)


def resolve_target(tournament_id, targets=None):
    """Return the publish target dict for a tournament. Logical routing
    (repo, appPath) lives in _targets.json; machine-specific paths
    (workDir, mirrorTo) may be overridden by env vars so multiple
    machines/CI runners can share the repo."""
    targets = targets if targets is not None else load_targets()
    if tournament_id not in targets:
        raise PlatformError(
            f"no publish target for '{tournament_id}' in {TARGETS_PATH} — add one, "
            f"e.g. {{\"{tournament_id}\": {{\"repo\": \"owner/repo\", "
            f"\"appPath\": \"app/data.json\", \"workDir\": \"/path/to/clone\"}}}}"
        )
    t = dict(targets[tournament_id])
    t["workDir"] = os.environ.get("TOURNAMENT_WORKDIR", t.get("workDir", ""))
    t["mirrorTo"] = os.environ.get("TOURNAMENT_MIRROR", t.get("mirrorTo", ""))
    return t
