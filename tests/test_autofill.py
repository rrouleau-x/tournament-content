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


def test_weather_fill_mocked_nws(scratch, monkeypatch):
    """NWS weather fill with a mocked API response: provenance fields are
    written, summary contains two distinct daytime periods, and the module
    stays schema-valid."""
    from autofill import fill_weather

    calls = {}

    def fake_fetch_json(url, timeout=20):
        calls["url"] = url
        if "/points/" in url:
            return {"properties": {"forecast": "https://api.weather.gov/fake/forecast"}}
        return {"properties": {"periods": [
            {"name": "Saturday", "isDaytime": True, "temperature": 91,
             "temperatureUnit": "F", "windSpeed": "8 mph",
             "shortForecast": "Sunny", "detailedForecast": "Hot."},
            {"name": "Sunday", "isDaytime": True, "temperature": 89,
             "temperatureUnit": "F", "windSpeed": "10 mph",
             "shortForecast": "Scattered Thunderstorms",
             "detailedForecast": "Afternoon storms."},
            {"name": "Saturday Night", "isDaytime": False, "temperature": 74,
             "temperatureUnit": "F", "windSpeed": "5 mph",
             "shortForecast": "Clear"},
        ]}}

    import autofill
    monkeypatch.setattr(autofill, "fetch_json", fake_fetch_json)

    path, msg = fill_weather(scratch, 30.0821, -81.5484)
    with open(path) as f:
        w = json.load(f)["weather"]
    # Two daytime periods in the summary, night period excluded
    assert "Saturday" in w["summary"] and "Sunday" in w["summary"]
    assert "Saturday Night" not in w["summary"]
    assert "91" in w["summary"] and "89" in w["summary"]
    # Provenance
    assert w["source"] == "National Weather Service (api.weather.gov)"
    assert w["sourceApiUrl"] == "https://api.weather.gov/fake/forecast"
    assert w["forecastLink"] == "https://api.weather.gov/fake/forecast"
    assert "updatedAt" in w
    assert_valid_module(scratch)


def test_weather_fill_restores_on_compile_exception(scratch, monkeypatch):
    """If compilation raises during validation, the candidate must NEVER
    reach disk (regression: the old design wrote the candidate first and
    restored on failure; the new design validates IN MEMORY before any
    write, so a raise leaves the original untouched)."""
    from autofill import fill_weather
    import compile as compile_mod

    before = open(os.path.join(scratch, "weather.json")).read()

    def fake_fetch_json(url, timeout=20):
        if "/points/" in url:
            return {"properties": {"forecast": "https://api.weather.gov/fake/forecast"}}
        return {"properties": {"periods": [
            {"name": "Saturday", "isDaytime": True, "temperature": 91,
             "temperatureUnit": "F", "windSpeed": "8 mph",
             "shortForecast": "Sunny"}]}}

    def boom(tdir, overrides=None):
        raise RuntimeError("simulated compile failure")

    import autofill
    monkeypatch.setattr(autofill, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(compile_mod, "compile_bundle", boom)

    with pytest.raises(RuntimeError, match="simulated"):
        fill_weather(scratch, 30.0821, -81.5484)

    # Original file restored byte-for-byte
    after = open(os.path.join(scratch, "weather.json")).read()
    assert after == before


def test_weather_deadline_threads_timeouts(scratch, monkeypatch):
    """The total deadline must bound BOTH network calls: the points
    lookup gets at most the whole deadline, the forecast gets the
    remaining budget (no 1s floor, no independent 20s default)."""
    import autofill
    from autofill import fill_weather

    timeouts = {}

    def fake_fetch_json(url, timeout=20.0):
        timeouts[url] = timeout
        if "/points/" in url:
            return {"properties": {"forecast": "https://api.weather.gov/fake/forecast"}}
        return {"properties": {"periods": [
            {"name": "Saturday", "isDaytime": True, "temperature": 91,
             "temperatureUnit": "F", "windSpeed": "8 mph",
             "shortForecast": "Sunny"}]}}

    monkeypatch.setattr(autofill, "fetch_json", fake_fetch_json)
    # Short custom deadline: the points request must NOT get its old
    # independent 20s default — it's capped to the whole deadline.
    autofill._nws_points_cache.clear()
    fill_weather(scratch, 30.0, -81.0, deadline_seconds=5)
    points_timeout = timeouts.get("https://api.weather.gov/points/30.0,-81.0")
    forecast_timeout = [t for u, t in timeouts.items() if "/points/" not in u][0]
    assert points_timeout == 5.0, f"points timeout {points_timeout} not capped to deadline"
    assert 0 < forecast_timeout <= 5.0, f"forecast timeout {forecast_timeout} out of budget"
    # No 1s floor: a fresh points lookup must be allowed at most the
    # deadline, and the forecast at most what remains — neither should
    # ever be 1.0 purely from the old max(1.0, ...) floor.
    assert 1.0 not in (points_timeout, forecast_timeout)


def test_weather_deadline_short_budget_still_attempts(scratch, monkeypatch):
    """A sub-second deadline still allows a real (tiny) attempt via the
    0.05s floor — it must not fail before connecting, and must not get a
    full one-second timeout."""
    import autofill
    from autofill import fill_weather

    timeouts = {}

    def fake_fetch_json(url, timeout=20.0):
        timeouts[url] = timeout
        if "/points/" in url:
            return {"properties": {"forecast": "https://api.weather.gov/fake/forecast"}}
        return {"properties": {"periods": [
            {"name": "Saturday", "isDaytime": True, "temperature": 91,
             "temperatureUnit": "F", "windSpeed": "8 mph",
             "shortForecast": "Sunny"}]}}

    monkeypatch.setattr(autofill, "fetch_json", fake_fetch_json)
    autofill._nws_points_cache.clear()
    fill_weather(scratch, 30.0, -81.0, deadline_seconds=0.2)
    times = list(timeouts.values())
    assert all(0 < t <= 0.2 for t in times), f"timeouts exceed 0.2s budget: {times}"
    assert all(t < 1.0 for t in times), "1s floor leaked back in"
