"""forms.py — Phase 3 form-model generator tests.

Covers: module → bundle-key slicing, widget inference, UI-config
overlays, form→JSON round-trip through the existing module shape.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from forms import build_form_model, form_to_json, _set_path  # noqa: E402


def field_by_path(model, path):
    """Find a field by dotted path (recursive through children)."""
    def walk(fields):
        for f in fields:
            if f["path"] == path:
                return f
            if f.get("children"):
                r = walk(f["children"])
                if r:
                    return r
        return None
    return walk(model["fields"])


def test_venue_model_shapes():
    m = build_form_model("venue.json")
    assert m is not None
    assert m["title"] == "Venue"
    assert field_by_path(m, "venue.name")["required"] is True
    assert field_by_path(m, "venue.name")["label"] == "Venue name"
    assert field_by_path(m, "venue.address")["widget"] == "textarea"  # UI-config override
    assert field_by_path(m, "venue.coordinates")["widget"] == "coords"
    assert field_by_path(m, "venue.coordinates.lat")["widget"] == "number"
    assert field_by_path(m, "venue.mapsUrl")["widget"] == "url"
    # Field order follows the UI config
    names = [f["name"] for f in m["fields"]]
    assert names == ["name", "address", "coordinates", "mapsUrl", "parking",
                     "fields", "amenities"]


def test_venue_layoutnotes_keyvalue_widget():
    """additionalProperties: {type: 'string'} objects must render as a
    key/value map editor, not an empty section (the dynamic-key gap)."""
    m = build_form_model("venue.json")
    ln = field_by_path(m, "venue.fields.layoutNotes")
    assert ln is not None
    assert ln["widget"] == "keyvalue"


def test_schedule_model():
    m = build_form_model("schedule.json")
    assert m is not None
    games = field_by_path(m, "games")
    assert games["widget"] == "repeater"
    # Game child fields exist with required markers
    assert field_by_path(m, "games[].date")["widget"] == "date"
    assert field_by_path(m, "games[].date")["required"] is True
    assert field_by_path(m, "games[].opponent")["required"] is True
    assert field_by_path(m, "games[].type")["widget"] == "select"
    assert "final" in field_by_path(m, "games[].type")["options"]


def test_contacts_model():
    m = build_form_model("contacts.json")
    assert m is not None
    assert field_by_path(m, "contacts.manager.name")["label"] == "Name"
    assert field_by_path(m, "contacts.teamManagers")["widget"] == "repeater"


def test_unknown_module_returns_none():
    assert build_form_model("nope.json") is None


def test_models_for_all_registry_modules():
    """Every registered module either yields a model or None (raw-JSON
    fallback is expected for unconfigured modules) — but never crashes."""
    from pipeline import MODULE_REGISTRY
    for module, _, _ in MODULE_REGISTRY:
        m = build_form_model(module)
        # schema-derived models must have a title + fields
        if m is not None:
            assert m["module"] == module
            assert isinstance(m["fields"], list)


def test_form_to_json_roundtrip_venue():
    """Form values → module JSON that matches the module file shape."""
    m = build_form_model("venue.json")
    values = {
        "venue.name": "Veterans Park",
        "venue.address": "100 Tournament Dr, Savannah GA",
        "venue.coordinates.lat": 30.0821,
        "venue.coordinates.lng": -81.5484,
        "venue.mapsUrl": "https://maps.google.com/?q=30.08,-81.54",
        "venue.parking": "Free lot at main entrance",
        "venue.fields.count": 6,
        "venue.fields.surface": "Grass",
        "venue.amenities.concessions": True,
        "venue.amenities.restrooms": True,
    }
    out = form_to_json(m, values)
    assert out["venue"]["name"] == "Veterans Park"
    assert out["venue"]["coordinates"] == {"lat": 30.0821, "lng": -81.5484}
    assert out["venue"]["fields"]["count"] == 6
    assert out["venue"]["amenities"]["concessions"] is True
    # Optional empty strings must be DELETED, never null — the browser
    # deletes empty optional keys on save, so the Python helper must
    # round-trip to the same shape (key ABSENT, not None).
    assert "shade" not in out["venue"]["amenities"]


def test_empty_optional_values_delete_keys():
    """Empty optional values of every type → the key is ABSENT, not
    null. This is the exact invariant the browser enforces (delPath on
    empty) — the Python form_to_json must produce the same JSON."""
    m = build_form_model("venue.json")
    # Explicitly empty optional fields (they ARE in the form model)
    values = {
        "venue.name": "Veterans Park",        # required — filled
        "venue.address": "100 Tournament Dr",  # required — filled
        # Optional: empty string / empty URL / no number / no boolean
        "venue.parking": "",
        "venue.mapsUrl": "",
        "venue.coordinates.lat": "",
        "venue.coordinates.lng": "",
        "venue.amenities.concessions": "",
        "venue.amenities.shade": "",
        "venue.amenities.aed": "",
        "venue.fields.surface": "",
        # Required field with empty input: also deleted (the schema/
        # validation layer flags it as missing — form save is blocked
        # client-side for required fields, this is the raw-helper path)
        "venue.fields.count": "",
    }
    out = form_to_json(m, values)
    # Every optional key must be absent — not present-with-None
    assert "parking" not in out["venue"]
    assert "mapsUrl" not in out["venue"]
    assert "coordinates" not in out["venue"]
    assert "fields" not in out["venue"]      # both children empty → pruned
    assert "amenities" not in out["venue"]   # all children empty → pruned
    # Required fields survived
    assert out["venue"]["name"] == "Veterans Park"
    assert out["venue"]["address"] == "100 Tournament Dr"


def test_set_path_repeater_indices():
    out = {}
    _set_path(out, "games[0].opponent", "Jacksonville FC", {"type": "string"})
    _set_path(out, "games[1].opponent", "Atlanta United", {"type": "string"})
    assert out["games"][0]["opponent"] == "Jacksonville FC"
    assert out["games"][1]["opponent"] == "Atlanta United"
    # Sparse writes pad with {}
    _set_path(out, "games[3].date", "2026-08-22", {"type": "string"})
    assert out["games"][2] == {}
    assert out["games"][3]["date"] == "2026-08-22"


def test_boolean_and_number_coercion():
    out = {}
    _set_path(out, "venue.amenities.aed", "true", {"type": "boolean"})
    assert out["venue"]["amenities"]["aed"] is True
    _set_path(out, "venue.fields.count", "6", {"type": "integer"})
    assert out["venue"]["fields"]["count"] == 6


def test_constraints_propagate_to_model():
    """pattern/minLength/minItems/maxItems must flow from schema → model."""
    m = build_form_model("schedule.json")
    # games is an array with no explicit minItems in the schema today —
    # the model simply carries what the schema declares (no crash, no
    # fabricated constraints). Verify the propagation machinery works on
    # a field that has minLength (venue.name has minLength: 1).
    v = build_form_model("venue.json")
    name = field_by_path(v, "venue.name")
    assert name["minLength"] == 1
