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


def check_publish_status(tdir, allow_draft=False):
    """Enforce the publish status gate. Returns (status, message).
    live = explicit opt-in; anything else blocks unless --allow-draft."""
    manifest = load_manifest(tdir)
    status = manifest.get("status")
    if not status:
        raise PlatformError(
            "manifest.json has no 'status' field — set it explicitly "
            "(e.g. 'draft' while working, 'live' to publish)"
        )
    if status == "live":
        return status, "publish allowed"
    if allow_draft:
        return status, f"publish allowed via --allow-draft (status '{status}')"
    raise PlatformError(
        f"manifest status is '{status}', not 'live' — set status to 'live' to "
        f"publish, or use --allow-draft"
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
