# Tournament Content Repo

The content layer of the Tournament Platform. All tournament-specific data
lives here as structured module JSON files. The app shell (in the separate
`sporting-jax-guide` repo) is **never** modified from this repo — it only
consumes compiled bundles.

## Quick start

Requires **Python 3.11+**. Set up once:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Run tests:

```bash
.venv/bin/python -m pytest tests/
```

## The pipeline

**Supported operator interface (one command, no Python knowledge needed):**

```bash
.venv/bin/python scripts/guide.py new <org>/<slug>          # scaffold from template (status: draft)
.venv/bin/python scripts/guide.py check <org>/<slug>        # compile + validate → Guide Health Report
.venv/bin/python scripts/guide.py preview <org>/<slug>      # check + dry-run publish diff
.venv/bin/python scripts/guide.py publish <org>/<slug>      # check + status gate + publish
.venv/bin/python scripts/guide.py list                      # tournaments + status
```

**Lower-level scripts** (same code, more knobs):

```bash
.venv/bin/python scripts/compile.py <org>/<slug>            # modules → out/<org>/<slug>/data.json
.venv/bin/python scripts/validate.py <org>/<slug>           # Guide Health Report
.venv/bin/python scripts/deploy.py <org>/<slug> --dry-run   # safe preview
.venv/bin/python scripts/deploy.py <org>/<slug>             # publish
.venv/bin/python scripts/split.py <bundle.json> <org>/<slug>  # migration: bundle → module files
```

**Exit codes (all commands):** `0` success/no-op · `1` validation blocked ·
`2` config/usage error · `3` publish/git failure · `4` external dependency.

## Layout

```
orgs/<org>/tournaments/<slug>/     one folder per tournament
  manifest.json                     REQUIRED metadata (org, slug, schemaVersion,
                                    status: draft|live). No manifest → no publish.
  sport.json · tournament.json · team.json · venue.json · schedule.json
  hotels.json · weather.json · rules.json · updates.json · contacts.json
  venue-rules.json · checklist.json · nearby.json · offline.json
_targets.json                       org/slug → app repo publish target mapping
_schemas/bundle-v1.json             the compiled data.json contract (v1)
scripts/                            pipeline (compile, validate, deploy, split, guide)
tests/                              pytest suite (62 tests: compile/validate/autofill/deploy/admin-server HTTP)
out/                                compiled bundles + link cache (gitignored)
```

## Editing content (non-technical path)

Edit a module file (e.g. `hotels.json`) → `guide.py check` → review the
Guide Health Report → `guide.py preview` → `guide.py publish`. The compiled
bundle is semantically identical in structure to what the app consumes; the
app parses JSON, so whitespace formatting is canonical (2-space, raw UTF-8)
and not required to match the old hand-compacted file byte-for-byte.

## Adding a new tournament

1. `guide.py new <org>/<slug>` — scaffolds from the template, status **draft**.
2. Edit the module files; delete modules the tournament doesn't need.
3. Add a publish target to `_targets.json` (see below).
4. `guide.py check` — the report tells you what's missing.
5. Set `manifest.json` status to `live`, then `guide.py publish`.

## Publish targets & deploy safety

`_targets.json` maps each tournament to its app repo — logical routing only:

```json
{
  "savannah-united/sporting-jax-2026": {
    "repo": "rrouleau-x/sporting-jax-guide",
    "appPath": "app/data.json",
    "workDir": "/tmp/sporting-jax-guide",
    "mirrorTo": "~/.hermes/www/app/data.json"
  }
}
```

`workDir`/`mirrorTo` are machine-specific; override per environment with
`TOURNAMENT_WORKDIR` / `TOURNAMENT_MIRROR` env vars so the repo stays
portable across machines/CI runners.

Deploy is **transactional** — each guarantee is enforced in code and covered
by tests (`tests/test_deploy.py`):

- **Clean worktree required.** A staged or uncommitted change in the app
  repo (e.g. a pre-staged index.html) aborts the deploy before anything is
  written — a shell change can never ride along in the publish commit.
- **Explicit pathspec commit.** Only the target `appPath` file is added and
  committed (`git commit -- app/data.json`); the staged diff is verified to
  contain exactly that one path.
- **Manifest + status gate.** `manifest.json` is REQUIRED; status must be
  explicitly `live` (`--allow-draft` overrides). Missing manifest, missing
  status, or unknown status = exit 2 — the gate can't be skipped by deleting
  metadata.
- **Semantic diff vs origin/main.** The pipeline fetches `origin` and
  compares the compiled bundle by JSON equality against `origin/main`'s
  data.json. No content change → no commit, no push, exit 0.
- **Mirror after success.** `mirrorTo` is written (temp file + atomic
  replace) only after push verification succeeds (local HEAD == origin/main).
- **Rollback on failure.** Pre-push failures reset the worktree to its
  starting state; push failures report exact recovery instructions and never
  touch the mirror. Every git return code is checked — a failed publish is
  never reported as success.

**The app shell is never touched:** deploy writes only the target `appPath`
file (data.json) into the app repo. If the backend dies, parents keep the
last published guide.

## Validation

`guide.py check` / `validate.py` produce a Guide Health Report. Blocking
failures (exit 1) prevent publish:

- Schema conformance to `_schemas/bundle-v1.json` — including real calendar
  dates (format checker) and URI/email formats
- Required business fields (team manager + coach contacts)
- Consistency: scheduleStatus/games contradictions, hotel drive format
- Games within the tournament window
- Local asset files exist
- Unknown module files reported (typo'd `hotel.json` can't silently vanish)
- Links: every URL checked (6h cache, parallel); **critical** links (venue
  map, field map, hotel portal, rules doc, urgent care) must resolve —
  others warn

`validate.py --json` emits structured output for the future admin dashboard.

## Contracts & schema evolution

- `_schemas/bundle-v1.json` is the authoritative contract between content
  and app. The app ignores unknown keys, so the contract is additive.
- Module files are optional: no `sponsors.json` → no sponsors section.
- Keep dates ISO (`2026-08-22`), drives as `"X mi · ~Y min"`.
- **Schema migration (future plan):** `schemaVersion` (manifest) is the hook
  for contract evolution. When v2 is real: ship `bundle-v2.json` alongside
  v1, add a version registry in the validator keyed by manifest
  `schemaVersion`, compile both while the app supports both, then drop v1
  once no live tournament uses it. Today the validator always loads v1 —
  the version registry is not yet implemented.

## Admin server & revision workflow (implemented — not roadmap)

The admin server (`scripts/admin_server.py`, localhost:8899 by default) is
**implemented and operational**: tournament list, module editor (raw JSON),
validate/preview/approve/publish, autofill, and new-tournament scaffolding.
The revision workflow (`manifest.revision`: draft → in_review → approved →
published, digest-tied) is **live**: publish requires status `live` AND the
current content digest matching the approved revision.

Security model (internet-tunnel-ready):
- Two-role tokens: `ADMIN_TOKEN` (editor: everything except publish) and
  `PUBLISH_TOKEN` (required for publish). Both auto-generated to
  `.admin-token` / `.publish-token` (0600, gitignored) or via env vars.
- `allow_draft` is CLI-only; the HTTP API can never publish unapproved
  content, and publish always passes `allow_draft=False`.
- Strict identifiers (`^[a-z0-9][a-z0-9-]{0,63}$`), realpath containment on
  every filesystem route, fixed static-file allowlist (index.html/app.css/
  app.js), restrictive CSP, 1MB request cap, SSRF-guarded autofill URL
  fetching (public-IP + per-redirect revalidation).
- Optimistic concurrency: module saves require `baseDigest` (stale → 409)
  under a per-module lock; manifest revision transitions use a file lock +
  compare-and-swap (`expected_manifest_digest`).
- Append-only audit log at `out/audit.log` (actor/action/tournament/
  digest/timestamp, including failures).

## Roadmap (from the Phase 1 design doc)

- **Form-based admin UI** (wizard + module forms + dashboard consuming
  `validate.py --json`): raw JSON editing works for technical operators but
  is not the final experience for non-technical club admins. This is the
  next milestone.
- **AI-assisted content generation:** autofill (weather via NWS, schedule,
  rules extraction, hotels) is implemented and lands as draft content
  requiring human approval. Deeper integration (research-driven autofill
  orchestration) remains.
- **Team-specific content:** per-team compiled bundles (`data-<team>.json`),
  selected via `?team=`.
- **Concurrency:** per-file locks + optimistic concurrency are in place for
  single-machine multi-admin; branches + PR review for distributed
  multi-admin/AI agents remains future work.
- **CI:** `validate every tournament on push` workflow is ready (see
  `tournament-companion-pwa` skill) — needs a workflow-scoped PAT to commit.
