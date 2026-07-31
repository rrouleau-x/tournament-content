"""validate.py tests: the 8-blocker negative battery (parametrized) plus
local-asset and unknown-module checks."""

import copy
import json
import os

import pytest

from compile import compile_bundle, serialize
from conftest import FIXTURE_DIR
from validate import Report, run_checks


@pytest.fixture()
def valid_bundle():
    bundle, _, _ = compile_bundle(FIXTURE_DIR)
    return bundle


def check(bundle, links=False, tdir=FIXTURE_DIR):
    report = Report()
    run_checks(bundle, report, run_link_checks=links, tdir=tdir)
    return report


# (name, mutate) — each must produce a blocking failure
NEGATIVE_CASES = [
    ("game missing required fields",
     lambda b: b.__setitem__("games", [{"id": "g1"}])),
    ("confirmed status, no games",
     lambda b: b.__setitem__("scheduleStatus", "confirmed")),
    ("bad drive format",
     lambda b: b["hotels"]["official"][0].__setitem__("drive", "7.9 miles away")),
    ("nested typo'd field",
     lambda b: b["hotels"]["official"][0].__setitem__("chian", "Hilton")),
    ("invalid calendar date",
     lambda b: b["tournament"]["dates"].__setitem__("start", "2026-13-45")),
    ("impossible calendar date in game",
     lambda b: b.__setitem__("games", [{"date": "2026-02-30", "time": "9:00 AM",
                                        "opponent": "FC"}])),
    ("game outside tournament window",
     lambda b: b.__setitem__("games", [{"date": "2026-09-01", "time": "9:00 AM",
                                        "opponent": "FC"}])),
    ("missing required contact",
     lambda b: b["contacts"].__setitem__("manager", {})),
    ("bad sport enum",
     lambda b: b.__setitem__("sport", "hockey")),
    ("extra root key",
     lambda b: b.__setitem__("surprise", True)),
]


@pytest.mark.parametrize("name,mutate", NEGATIVE_CASES,
                         ids=[c[0] for c in NEGATIVE_CASES])
def test_blocker_fires(valid_bundle, name, mutate):
    b = copy.deepcopy(valid_bundle)
    mutate(b)
    report = check(b)
    assert report.blocking(), f"expected a blocker for: {name}"
    assert report.summary()["blocking"] >= 1


def test_valid_bundle_has_zero_blocking(valid_bundle):
    report = check(valid_bundle)
    assert report.summary()["blocking"] == 0
    # no-games warning is expected (schedule pending)
    assert report.summary()["warnings"] >= 1


def test_required_checks_not_duplicated(valid_bundle):
    """One root cause must produce one failure, not schema+required dupes."""
    b = copy.deepcopy(valid_bundle)
    del b["tournament"]  # schema-required AND business-required fields
    report = check(b)
    schema_fails = [i for i in report.items if i[0] == "fail" and i[1] == "schema"]
    required_fails = [i for i in report.items if i[0] == "fail" and i[1] == "required"]
    # tournament name/dates are schema-required only; REQUIRED_FIELDS holds
    # only contacts.manager/coach — so no double-report of the same path
    assert len(required_fails) == 0
    assert len(schema_fails) >= 1


def test_dead_urgent_care_link_is_blocking(valid_bundle):
    b = copy.deepcopy(valid_bundle)
    b["nearby"]["urgentCare"]["maps"] = "https://example.invalid/er"
    report = check(b, links=True)
    assert report.blocking()
    assert any("CRITICAL" in m for _, c, m in report.items if c == "links")


def test_local_asset_missing_is_blocking(valid_bundle, tmp_path):
    b = copy.deepcopy(valid_bundle)
    b["team"]["logo"] = "assets/logo.png"  # does not exist
    report = check(b, tdir=str(tmp_path))
    assert report.blocking()
    assert any("logo" in m for _, c, m in report.items if c == "assets")


def test_local_asset_present_passes(valid_bundle, tmp_path):
    b = copy.deepcopy(valid_bundle)
    os.makedirs(tmp_path / "assets")
    (tmp_path / "assets" / "logo.png").write_bytes(b"fake")
    b["team"]["logo"] = "assets/logo.png"
    report = check(b, tdir=str(tmp_path))
    assert not report.blocking()


def test_unknown_module_reported(valid_bundle, tmp_path):
    b = copy.deepcopy(valid_bundle)
    (tmp_path / "hotel.json").write_text('{"hotels": {}}')
    report = check(b, tdir=str(tmp_path))
    warns = [m for s, c, m in report.items if c == "modules" and s == "warn"]
    assert any("hotel.json" in m for m in warns)


def test_report_json_structure(valid_bundle):
    report = check(valid_bundle)
    d = report.to_dict()
    assert "summary" in d and "results" in d
    assert set(d["summary"]) == {"passed", "warnings", "blocking"}
    assert all(set(r) == {"severity", "check", "message"} for r in d["results"])
