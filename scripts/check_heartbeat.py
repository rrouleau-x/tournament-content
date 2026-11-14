#!/usr/bin/env python3
"""Fail when the publisher's heartbeat is stale or the publisher reports itself blocked.

Runs on GitHub's runners (see .github/workflows/publisher-heartbeat.yml): the Mac cannot report
its own death, so an external check has to notice the silence.
"""
import json
import sys
from datetime import datetime, timezone

LIMIT_MIN = 20


def main(path: str = "health/_health/publisher.json") -> int:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"::error::no readable heartbeat at {path}: {exc}")
        return 1
    hb = d.get("heartbeat", "")
    try:
        ts = datetime.fromisoformat(hb)
    except ValueError:
        print(f"::error::heartbeat is not a timestamp: {hb!r}")
        return 1
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    mins = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 60
    print(f"heartbeat age {mins:.1f} min · state {d.get('state')} · host {d.get('host')}")
    for p in d.get("problems") or []:
        print(f"::warning::{p}")
    if mins > LIMIT_MIN:
        print(f"::error::PUBLISHER SILENT for {mins:.0f} min (limit {LIMIT_MIN}).")
        return 1
    if d.get("state") == "blocked":
        print("::error::Publisher BLOCKED: " + "; ".join(d.get("problems") or ["unknown"]))
        return 1
    print("publisher healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
