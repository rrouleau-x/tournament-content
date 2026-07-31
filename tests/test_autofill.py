"""autofill.py tests: fillers produce schema-valid draft modules and never
publish. Network fillers (weather, rules-from-URL) are tested with local
fixtures / mocks; schedule and hotels use real fixture files."""

import json
import os

import pytest

from autofill import fill_hotels, fill_rules, fill_schedule
from compile import compile_bundle
from conftest import FIXTURE_DIR
from validate import Report, run_checks

FIXTURE_GAMES = os.path.join(os.path.dirname(__file__), "fixtures", "games.json")
FIXTURE_HOTELS = os.path.join(os.path.dirname(__file__), "fixtures", "hotels-research.json")
FIXTURE_RULES_HTML = os.path.join(os.path.dirname(__file__), "fixtures", "rules-page.html")


@pytest.fixture()
def scratch(tmp_path):
    """A scratch tournament copy to fill (never the live one)."""
    import shutil
    dst = tmp_path / "tournament"
    shutil.copytree(FIXTURE_DIR, dst)
    return str(dst)


def assert_valid_module(tdir):
    """The filled tournament must still compile + validate with 0 blocking."""
    bundle, _, _ = compile_bundle(tdir)
    report = Report()
    run_checks(bundle, report, run_link_checks=False, tdir=tdir)
    assert not report.blocking(), [i[2] for i in report.items if i[0] == "fail"]


def test_schedule_fill(scratch):
    path, msg = fill_schedule(scratch, FIXTURE_GAMES)
    with open(path) as f:
        sched = json.load(f)["schedule"]
    assert len(sched["games"]) == 3
    assert sched["scheduleStatus"] == "partial"
    assert sched["games"][0]["opponent"] == "Jacksonville FC"
    assert_valid_module(scratch)


def test_schedule_fill_rejects_missing_fields(tmp_path):
    bad = tmp_path / "bad-games.json"
    bad.write_text(json.dumps({"games": [{"id": "g1"}]}))
    with pytest.raises(Exception, match="missing required fields"):
        fill_schedule(tmp_path, str(bad))


def test_hotels_fill(scratch):
    path, msg = fill_hotels(scratch, FIXTURE_HOTELS)
    with open(path) as f:
        hotels = json.load(f)["hotels"]
    assert hotels["stayToPlay"] is True
    assert len(hotels["official"]) == 1
    assert len(hotels["nonOfficial"]) == 1
    assert_valid_module(scratch)


def test_hotels_fill_rejects_missing_drive(tmp_path):
    bad = tmp_path / "bad-hotels.json"
    bad.write_text(json.dumps({"official": [{"name": "No Drive"}]}))
    with pytest.raises(Exception, match="drive"):
        fill_hotels(tmp_path, str(bad))


def test_rules_fill_from_html(scratch):
    path, msg = fill_rules(scratch, FIXTURE_RULES_HTML)
    with open(path) as f:
        rules = json.load(f)["rules"]
    labels = [k["label"] for k in rules.get("keyRules", [])]
    assert "Mercy Rule" in labels
    assert "Game Length" in labels
    # values are HTML-stripped (no tags leaking into the draft)
    for k in rules.get("keyRules", []):
        assert "<" not in k["value"]
    assert_valid_module(scratch)


def test_rules_fill_bad_source(tmp_path):
    with pytest.raises(Exception, match="not found"):
        fill_rules(tmp_path, "/nonexistent/rules.html")
