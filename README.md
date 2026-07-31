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

_schemas/                           JSON Schema contracts (versioned)
  bundle-v1.json                    the compiled data.json contract (v1)
scripts/                            the build pipeline
  split.py                          one-time: bundle → module files
  compile.py                        module files → bundle (data.json)
  validate.py                       Guide Health Report (schema/required/dates/links/assets)
  deploy.py                         compile + validate + publish to app repo
out/                                compiled bundles (gitignored)
```

## The pipeline (one command)

```bash
.venv/bin/python scripts/compile.py <org>/<slug>          # → out/<org>/<slug>/data.json
.venv/bin/python scripts/validate.py <org>/<slug>         # Guide Health Report
.venv/bin/python scripts/deploy.py <org>/<slug> --dry-run # compile+validate+diff (safe)
.venv/bin/python scripts/deploy.py <org>/<slug>           # publish (commit+push app data.json)
```

- Validation **blocks** publish on: schema errors, missing business-required
  fields, games outside the tournament window, dead critical links.
- The app shell is never touched: deploy writes only `data.json` into the app
  repo, and only when content actually changed (byte-diff against live file).

## Editing content (non-technical path)

Edit a module file (e.g. `hotels.json`) → run `deploy.py --dry-run` → review
the Guide Health Report → run `deploy.py` to publish. The compiled bundle is
semantically identical in structure to what the app already consumes; the app
parses JSON, so whitespace formatting of the bundle is canonical (2-space,
raw UTF-8) and not required to match the old hand-compacted file byte-for-byte.

## Adding a new tournament

1. Copy an existing tournament folder (e.g. `sporting-jax-2026`) to
   `orgs/<org>/tournaments/<new-slug>/`.
2. Update `manifest.json` (slug, name, dates, status) and every module file.
3. Delete module files the tournament doesn't need (optional modules).
4. Run `compile.py` + `validate.py` — the Guide Health Report tells you what's missing.
5. `deploy.py` to publish.

## Contracts

- `_schemas/bundle-v1.json` is the authoritative contract between content and
  app. The app ignores unknown keys, so the contract is additive.
- Module files are optional: no `sponsors.json` → no sponsors section.
- Keep dates ISO (`2026-08-22`), drives as `"X mi · ~Y min"`.
