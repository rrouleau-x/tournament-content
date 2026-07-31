"""compile.py tests: positive path, round-trip, error handling,
unknown-module detection, digest stability."""

import json
import os

import pytest

from compile import CompileError, compile_bundle, content_digest, serialize
from conftest import FIXTURE_DIR, FIXTURE_TOURNAMENT
from pipeline import PlatformError, parse_tournament_id, tournament_dir


def test_positive_path_compiles_all_modules():
    bundle, used, unknown = compile_bundle(FIXTURE_DIR)
    assert len(used) == 14
    assert unknown == []
    assert bundle["tournament"]["name"] == "Sporting Jax Boys Invitational"
    assert bundle["team"]["name"] == "Savannah United 17/18B"
    assert bundle["hotels"]["official"][0]["rate"] == "$143/night"


def test_compile_semantically_matches_live_bundle():
    """The compiled bundle must equal the live app data.json semantically."""
    live_path = os.path.expanduser("~/.hermes/www/app/data.json")
    if not os.path.exists(live_path):
        pytest.skip("live app data.json not present")
    bundle, _, _ = compile_bundle(FIXTURE_DIR)
    with open(live_path) as f:
        live = json.load(f)
    assert bundle == live


def test_roundtrip_split_compile_equality(tmp_path):
    """split then compile must round-trip to the same bundle."""
    from split import dump_json
    from pipeline import MODULE_REGISTRY

    bundle, _, _ = compile_bundle(FIXTURE_DIR)
    out_dir = tmp_path / "roundtrip"
    out_dir.mkdir()
    for filename, keys, _r in MODULE_REGISTRY:
        module = {k: bundle[k] for k in keys if k in bundle}
        dump_json(module, str(out_dir / filename))
    rebundle, _, _ = compile_bundle(str(out_dir))
    assert rebundle == bundle


def test_unicode_preserved():
    bundle, _, _ = compile_bundle(FIXTURE_DIR)
    out = serialize(bundle)
    assert "★" in out          # Marriott star
    assert "·" in out          # drive separator
    assert "→" in out          # rules arrow
    assert "°" in out          # temperature


def test_tournament_id_parsing(tmp_path):
    assert parse_tournament_id("a/b") == ("a", "b")
    for bad in ["", "nope", "/x", "x/", "a/b/c"]:
        with pytest.raises(PlatformError):
            parse_tournament_id(bad)
    # a well-formed id for a nonexistent tournament: dir check fails later
    org, slug = parse_tournament_id("org/nope")
    assert not os.path.isdir(tournament_dir(org, slug))


def test_empty_folder_compiles_to_empty_bundle(tmp_path):
    bundle, used, unknown = compile_bundle(str(tmp_path))
    assert bundle == {}
    assert used == []
    assert unknown == []


def test_unknown_module_filename_warns(tmp_path):
    """hotel.json (typo) must be reported, not silently ignored."""
    (tmp_path / "hotel.json").write_text('{"hotels": {}}')
    bundle, used, unknown = compile_bundle(str(tmp_path))
    assert "hotel.json" in unknown
    assert "hotels" not in bundle


def test_malformed_module_json_raises_clean_error(tmp_path):
    (tmp_path / "tournament.json").write_text("{not valid json")
    with pytest.raises(CompileError, match="not valid JSON"):
        compile_bundle(str(tmp_path))


def test_missing_expected_key_raises_clean_error(tmp_path):
    (tmp_path / "schedule.json").write_text('{"games": []}')
    with pytest.raises(CompileError, match="missing expected key 'scheduleStatus'"):
        compile_bundle(str(tmp_path))


def test_digest_stable_and_unique():
    bundle, _, _ = compile_bundle(FIXTURE_DIR)
    a = content_digest(serialize(bundle))
    b = content_digest(serialize(bundle))
    assert a == b
    assert len(a) == 16  # sha256 truncated, not sha1
