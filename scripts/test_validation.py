#!/usr/bin/env python3
"""Negative test battery: prove every hardening finding from the design
review actually blocks. Each test mutates a valid bundle and expects the
validation to FAIL with the right check label."""

import copy
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

from validate import Report, run_checks  # noqa: E402

VALID = json.load(open(os.path.join(REPO, "out", "savannah-united", "sporting-jax-2026", "data.json")))

TESTS = []


def test(name, mutate, expect_check, expect_severity="fail"):
    TESTS.append((name, mutate, expect_check, expect_severity))


# 1. Game entries must have required fields (date/time/opponent)
def t1(b):
    b["games"] = [{"id": "g1"}]  # missing date/time/opponent
    return b
test("game missing required fields", t1, "schema")

# 2. scheduleStatus 'confirmed' with empty games must block
def t2(b):
    b["scheduleStatus"] = "confirmed"
    b["games"] = []
    return b
test("confirmed status, no games", t2, "consistency")

# 3. Bad drive format (hyphen instead of ·, or no minutes)
def t3(b):
    b["hotels"]["official"][0]["drive"] = "7.9 miles away"
    return b
test("bad drive format", t3, "consistency")

# 4. Dead urgent-care link must be CRITICAL (blocking), not warning
#    (example.invalid fails DNS fast; link check is enabled for this test)
def t4(b):
    b["nearby"]["urgentCare"]["maps"] = "https://example.invalid/er"
    return b
test("dead urgent care link", t4, "links", "fail")

# 5. Nested typo (additionalProperties:false must catch 'chian')
def t5(b):
    b["hotels"]["official"][0]["chian"] = "Hilton"
    return b
test("nested typo'd field", t5, "schema")

# 6. Invalid calendar date (2026-13-45)
def t6(b):
    b["tournament"]["dates"]["start"] = "2026-13-45"
    return b
test("invalid calendar date", t6, "schema")

# 7. Games with invalid date but valid format
def t7(b):
    b["games"] = [{"date": "2026-02-30", "time": "9:00 AM", "opponent": "FC"}]
    return b
test("impossible calendar date in game", t7, "schema")

# 8. Date range: game outside tournament window
def t8(b):
    b["games"] = [{"date": "2026-09-01", "time": "9:00 AM", "opponent": "FC"}]
    return b
test("game outside tournament window", t8, "dates")

passed = 0
failed = 0
for name, mutate, expect_check, expect_severity in TESTS:
    b = copy.deepcopy(VALID)
    b = mutate(b)
    report = Report()
    # Only the link test hits the network (example.invalid fails fast);
    # everything else runs offline.
    run_links = name == "dead urgent care link"
    run_checks(b, report, run_link_checks=run_links)
    hits = [i for i in report.items if i[1] == expect_check and i[0] == expect_severity]
    if hits:
        passed += 1
        print(f"  PASS  {name}  → [{expect_check}] {hits[0][2][:70]}")
    else:
        failed += 1
        sevs = sorted(set(i[0] for i in report.items))
        print(f"  FAIL  {name}  → expected [{expect_check}/{expect_severity}], got severities {sevs}")
        for i in report.items[:5]:
            print(f"         {i[0]}/{i[1]}: {i[2][:70]}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
