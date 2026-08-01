"""updates.py tests — the hotspot-link watcher.

Covers: watched-URL collection (content sources only, maps excluded),
first-run seeding, stable fingerprints across runs, change detection
with --apply drafting an ET-stamped update entry through the validated
write path, and unreachable-source handling (not a change).
"""

import json
import os

import pytest

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys  # noqa: E402
sys.path.insert(0, os.path.join(sys_path, "scripts"))  # noqa: E402

import updates  # noqa: E402
from conftest import REPO_ROOT  # noqa: E402


@pytest.fixture()
def tdir(tmp_path, monkeypatch):
    """A tournament dir built from the REAL schema-valid template (so the
    validated write path passes), with the watcher-relevant modules set.
    STATE_PATH is redirected to the tmp tree — tests never touch the
    real out/link-state.json."""
    import shutil
    template = os.path.join(REPO_ROOT, "_templates", "tournament-v1")
    for f in os.listdir(template):
        if f.endswith(".json"):
            shutil.copy(os.path.join(template, f), tmp_path / f)
    monkeypatch.setattr(updates, "STATE_PATH", str(tmp_path / "link-state.json"))
    def write(name, data):
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")

    write("hotels.json", {"hotels": {"stayToPlay": True,
                                     "portal": "https://example.com/portal"}})
    write("rules.json", {"rules": {"fullLink": "https://example.com/rules"}})
    write("venue.json", {"venue": {"name": "V",
                                   "address": "1 Main St",
                                   "mapsUrl": "https://example.com/map",
                                   "fields": {"count": 2,
                                              "map": "https://example.com/fieldmap"}}})
    write("updates.json", {"updates": []})
    # The validated write path needs a VALID bundle — fill the required
    # fields (the template is deliberately empty).
    write("tournament.json", {"tournament": {"name": "T",
                                             "dates": {"start": "2026-09-01",
                                                       "end": "2026-09-02"}}})
    write("team.json", {"team": {"name": "Team"}})
    write("contacts.json", {"contacts": {"manager": {"name": "M"},
                                         "coach": {"name": "C"}}})
    write("checklist.json", {"checklist": {"player": [], "weather": [],
                                           "parent": [], "emergency": []}})
    return tmp_path


def test_watched_urls_content_sources_only(tdir):
    """Portal + rules are watched; venue/field MAP links are NOT (their
    UIs are volatile and a real map change is a URL change git tracks)."""
    urls = updates.watched_urls(str(tdir))
    labels = [l for l, _ in urls]
    assert "stay-to-play portal" in labels
    assert "rules document" in labels
    assert "venue map" not in labels
    assert "field map" not in labels
    # Explicit watchUrls are honored
    (tdir / "updates.json").write_text(json.dumps(
        {"updates": [{"title": "Watch", "watchUrls": ["https://example.com/x"]}]}),
        encoding="utf-8")
    urls = updates.watched_urls(str(tdir))
    assert any(u == "https://example.com/x" for _l, u in urls)


def test_normalize_strips_volatility():
    """Timestamps, tokens, numbers, scripts, and cache-bust params must
    not make a fingerprint look 'changed'."""
    a = updates._normalize(
        "<script>window.x=1</script><div>Hello <b>World</b> 2026-07-31 "
        "12:34:56 abc123def4567890 12345</div>?cb=998877")
    b = updates._normalize(
        "<div>Hello <b>World</b> 2026-08-01 13:00:00 fedcba0987654321 99999</div>?cb=111222")
    assert a == b, "volatile content changed the fingerprint"


def test_check_seeds_then_stable(tdir, monkeypatch):
    """First run seeds baselines; a second run with identical content
    reports NO changes (stable fingerprints)."""
    calls = {"n": 0}
    def fake_fetch(url, max_bytes=2_000_000):
        calls["n"] += 1
        return f"<html>stable content for {url} <script>var t={calls['n']}</script></html>"
    monkeypatch.setattr(updates, "safe_fetch_text", fake_fetch, raising=False)

    changes1 = updates.check_tournament(str(tdir))
    assert all(c["status"] == "seeded" for c in changes1)
    changes2 = updates.check_tournament(str(tdir))
    assert changes2 == [], f"stable content reported changes: {changes2}"


def test_check_reports_change_and_drafts_et_stamp(tdir, monkeypatch):
    """A real content change is reported; --apply drafts an update entry
    with an EASTERN TIME timestamp through the validated write path."""
    def fake_fetch(url, max_bytes=2_000_000):
        return "<html>v1 content</html>"
    monkeypatch.setattr(updates, "safe_fetch_text", fake_fetch, raising=False)

    updates.check_tournament(str(tdir))  # seed

    def changed_fetch(url, max_bytes=2_000_000):
        return "<html>v2 content — schedule released!</html>"
    monkeypatch.setattr(updates, "safe_fetch_text", changed_fetch, raising=False)

    changes = updates.check_tournament(str(tdir), apply=True)
    changed = [c for c in changes if c["status"] == "changed"]
    assert changed, "expected a change after content flip"
    assert all(c.get("drafted") for c in changed)

    mod = json.load(open(os.path.join(str(tdir), "updates.json"), encoding="utf-8"))
    entries = mod["updates"]
    assert entries, "expected a drafted update entry"
    titles = {e["title"] for e in entries}
    assert titles == {c["source"] + " updated" for c in changed}, titles
    assert all(e["actionRequired"] is True for e in entries)
    # ET timestamp: -0400 (EDT) or -0500 (EST), never bare Z
    assert all(e["time"].endswith(("-0400", "-0500")) for e in entries), entries[-1]["time"]


def test_unreachable_source_is_not_a_change(tdir, monkeypatch):
    """A fetch failure is reported as unreachable, NOT as a change, and
    does not draft an update."""
    def fail(url, max_bytes=2_000_000):
        from pipeline import PlatformError
        raise PlatformError("fetch failed: timeout")
    monkeypatch.setattr(updates, "safe_fetch_text", fail, raising=False)

    changes = updates.check_tournament(str(tdir), apply=True)
    assert all(c["status"] == "unreachable" for c in changes)
    mod = json.load(open(os.path.join(str(tdir), "updates.json"), encoding="utf-8"))
    assert mod["updates"] == []
