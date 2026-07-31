# Tournament Content Repo

The content layer of the Tournament Platform. All tournament-specific data
lives here as structured module JSON files. The app shell (in the separate
`sporting-jax-guide` repo) is **never** modified from this repo — it only
consumes compiled bundles.

```
orgs/<org>/tournaments/<slug>/     one folder per tournament
  manifest.json                     metadata (org, slug, schemaVersion, status)
  sport.json                        sport + sportConfig (labels)
  tournament.json                   name, dates, organizer
  team.json                         team name, colors, logo
  venue.json                        venue, fields, parking, amenities
  schedule.json                     games + scheduleStatus + scheduleExpected
  hotels.json                       stay-to-play + official/non-official
  weather.json                      summary, details, forecastLink
  rules.json                        fullLink, keyRules, tiebreakers
  updates.json                      parent-facing update feed
  contacts.json                     manager, coach, staff, tournament director
  venue-rules.json                  tents, pets, food/cooler, chairs, etc.
  checklist.json                    player/weather/parent/emergency lists
  nearby.json                       urgent care, stores, food, errands
  offline.json                      PDF guide URL, cache metadata

_targets.json                       org/slug → app repo publish target mapping
_schemas/                           JSON Schema contracts (versioned)
  bundle-v1.json                    the compiled data.json contract (v1)
scripts/                            the build pipeline
  split.py                          one-time: bundle → module files
  compile.py                        module files → bundle (data.json)
  validate.py                       Guide Health Report (schema/required/consistency/links/assets)
  deploy.py                         compile + validate + publish to target app repo
out/                                compiled bundles + link cache (gitignored)
```

## The pipeline (one command)

```bash
.venv/bin/python scripts/compile.py <org>/<slug>          # → out/<org>/<slug>/data.json
.venv/bin/python scripts/validate.py <org>/<slug>         # Guide Health Report
.venv/bin/python scripts/deploy.py <org>/<slug> --dry-run # compile+validate+diff (safe)
.venv/bin/python scripts/deploy.py <org>/<slug>           # publish (commit+push to target)
```

- Validation **blocks** publish on: schema errors (including real calendar
  dates and URI/email formats), missing business-required fields, games
  outside the tournament window, scheduleStatus/games contradictions, bad
  hotel drive formats, dead **critical** links (venue map, field map, hotel
  portal, rules doc, urgent care).
- **Status gate:** a tournament whose `manifest.json` status is not `live`
  cannot be published (use `--allow-draft` to override). Draft content never
  reaches parents by accident.
- **Publish targets:** `_targets.json` maps each tournament to its app repo
  (repo, file path, local git working copy). Any number of tournaments/apps
  can be published; the mapping is data, not code.
- **Deploy safety:** the pipeline fetches `origin`, compares the compiled
  bundle **semantically** (JSON equality) against `origin/main`'s data.json,
  and only commits+publishes when content actually changed. Every git
  return code is checked — a failed push is reported as a failure, and local
  HEAD is verified against origin/main after push. A dead/failed publish
  never touches the app, and never *claims* success it didn't achieve.
- The app shell is never touched: deploy writes only the target `appPath`
  file (data.json) into the app repo.

## Editing content (non-technical path)

Edit a module file (e.g. `hotels.json`) → run `deploy.py --dry-run` → review
the Guide Health Report → run `deploy.py` to publish. The compiled bundle is
semantically identical in structure to what the app already consumes; the app
parses JSON, so whitespace formatting of the bundle is canonical (2-space,
raw UTF-8) and not required to match the old hand-compacted file byte-for-byte.

## Adding a new tournament

1. Copy an existing tournament folder (e.g. `sporting-jax-2026`) to
   `orgs/<org>/tournaments/<new-slug>/`.
2. Update `manifest.json` (slug, name, dates, status: `draft`) and every module file.
3. Delete module files the tournament doesn't need (optional modules).
4. Add a publish target to `_targets.json` (repo + appPath + workDir).
5. Run `compile.py` + `validate.py` — the Guide Health Report tells you what's missing.
6. Set status to `live`, then `deploy.py` to publish.

## Contracts & schema evolution

- `_schemas/bundle-v1.json` is the authoritative contract between content and
  app. The app ignores unknown keys, so the contract is additive.
- Module files are optional: no `sponsors.json` → no sponsors section.
- Keep dates ISO (`2026-08-22`), drives as `"X mi · ~Y min"`.
- **Schema migration:** `schemaVersion` (manifest + bundle meta) is the hook
  for contract evolution. v1→v2 plan: ship the new schema alongside the old
  (`bundle-v2.json`), compile both while the app supports both, then drop v1
  once no live tournament uses it. validate.py validates against the version
  declared by the tournament — never guess.

## Roadmap (from the Phase 1 design doc)

- **Phase 2:** admin UI (wizard + module forms + validation dashboard
  consuming `validate.py --json`), AI-assisted content generation (hotel
  research, schedule import, rules extraction). AI drafts land on a branch /
  `status: draft` and require human review before `live`.
- **Team-specific content:** per-team compiled bundles (`data-<team>.json`),
  selected via `?team=` — avoids duplicating whole tournament folders.
- **Concurrency:** multiple admins/AI agents write via branches + PR review
  (git already gives us diff/revert/audit); a review gate independent of
  "passes schema" is required before AI autofill touches deploy.
- **CI:** `validate every tournament on push` workflow is ready (see
  `tournament-companion-pwa` skill) — needs a workflow-scoped PAT to commit.
