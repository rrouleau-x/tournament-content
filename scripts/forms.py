#!/usr/bin/env python3
"""Schema-slice form models for the admin UI — Phase 3 form editor.

DESIGN (per external review): do NOT point a generic generator at the
compiled bundle schema — module files don't map 1:1 to bundle keys
(schedule.json feeds games/scheduleStatus/scheduleExpected). Instead:

  1. Slice the bundle schema per MODULE_REGISTRY entry (module → its
     bundle keys) → the schema SUBSET that module owns.
  2. Walk that subset into a JSON-serializable form model (fields with
     type/label/required/options/min/max/children + inferred widgets:
     enum→select, format:date→date, format:uri→url, boolean→checkbox,
     lat/lng pair→coords, arrays→repeater).
  3. Overlay a small hand-authored per-module UI config (label overrides,
     field ordering, widget upgrades like textarea/select with options,
     help text, hide-from-form fields). This is where UX lives; the
     schema supplies structure, the config supplies the product.

The admin server exposes GET /api/forms/<module> (static, no auth — it
is UI metadata, not content), and the admin UI renders these models as
forms. Saving a form compiles back to the SAME module JSON the raw
textarea produces, so the backend (PUT + baseDigest, compile, validate,
approve, publish) is untouched.
"""

import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(REPO_ROOT, "_schemas", "bundle-v1.json")


def _humanize(key):
    """snake_case / camelCase → Title Case."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key)
    s = s.replace("_", " ").replace("-", " ")
    return s.strip().title() or key


def _find_bundle_properties(schema):
    """The bundle object's properties from the compiled-bundle schema."""
    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict) and "tournament" in node["properties"]:
                return node["properties"]
            for v in node.values():
                r = walk(v)
                if r:
                    return r
        return None
    return walk(schema)


def _widget_for(schema, ui_field, name):
    """Infer the input widget from the schema subset + UI config."""
    if ui_field.get("widget"):
        return ui_field["widget"]
    t = schema.get("type")
    if t == "boolean":
        return "checkbox"
    if t == "array":
        return "repeater"
    if t in ("number", "integer"):
        return "number"
    if schema.get("enum"):
        return "select"
    fmt = schema.get("format")
    if fmt == "date":
        return "date"
    if fmt == "uri":
        return "url"
    if fmt == "email":
        return "email"
    # lat/lng pair in an object → single coords widget
    if t == "object":
        props = (schema.get("properties") or {})
        if set(props) == {"lat", "lng"}:
            return "coords"
        return "section"
    # name/long text hints
    low = name.lower()
    if any(k in low for k in ("notes", "description", "layout", "rules", "guidance")):
        return "textarea"
    return "text"


def _walk_fields(schema, path, required_set, ui_fields):
    """Recursively build the form-model field list for one schema node."""
    fields = []
    props = schema.get("properties") or {}
    ui_order = ui_fields.get("order")
    keys = ui_order if ui_order else list(props.keys())
    for name in keys:
        if name not in props:
            continue
        sub = props[name]
        ui_field = ui_fields.get(name) or {}
        if ui_field.get("hidden"):
            continue
        fpath = f"{path}.{name}" if path else name
        field = {
            "path": fpath,
            "name": name,
            "label": ui_field.get("label") or _humanize(name),
            "type": sub.get("type", "string"),
            "required": name in (required_set or ()),
            "widget": _widget_for(sub, ui_field, name),
            "help": ui_field.get("help"),
        }
        if sub.get("enum"):
            field["options"] = sub["enum"]
        for k in ("minimum", "maximum", "minLength"):
            if k in sub:
                field[k] = sub[k]
        # Repeater → child fields from items schema
        if field["widget"] == "repeater":
            items = sub.get("items") or {}
            field["children"] = _walk_fields(
                items, f"{fpath}[]", items.get("required"), ui_field)
        elif field["widget"] == "section":
            field["children"] = _walk_fields(sub, fpath, sub.get("required"), ui_field)
        elif field["widget"] == "coords":
            field["children"] = _walk_fields(sub, fpath, sub.get("required"), {})
        fields.append(field)
    return fields


def build_form_model(module, bundle_schema=None):
    """Form model for one module file (e.g. 'venue.json'). Returns a dict
    or None if the module isn't in the registry / has no schema subset.

    Model shape (serializable; consumed by the admin UI):
      { module, title, fields: [Field], help }
      Field = { path, name, label, type, required, widget, help,
                options?, minimum?, maximum?, minLength?, children? }
    """
    import sys
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    from pipeline import MODULE_REGISTRY

    entry = next((m for m in MODULE_REGISTRY if m[0] == module), None)
    if not entry:
        return None
    _, bundle_keys, _ = entry

    schema = bundle_schema or json.load(open(SCHEMA_PATH))
    bundle = _find_bundle_properties(schema)
    if bundle is None:
        return None

    ui = UI_CONFIG.get(module, {})
    fields = []
    for key in bundle_keys:
        if key not in bundle:
            continue
        sub = bundle[key]
        ui_key = ui.get(key, {})
        # array at the top level (games) → repeater directly
        if sub.get("type") == "array":
            items = sub.get("items") or {}
            fields.append({
                "path": key, "name": key,
                "label": ui_key.get("label") or _humanize(key),
                "type": "array", "required": True,
                "widget": "repeater",
                "help": ui_key.get("help") or sub.get("description"),
                "children": _walk_fields(items, f"{key}[]", items.get("required"),
                                         ui_key),
            })
        else:
            fields.extend(_walk_fields(sub, key, sub.get("required"), ui_key))

    return {
        "module": module,
        "title": ui.get("_title") or _humanize(module.replace(".json", "")),
        "help": ui.get("_help"),
        "fields": fields,
    }


def form_to_json(model, values):
    """Values (flat-ish dict of paths → user input) → the module's JSON
    content (the same shape the raw editor produces). Round-trips through
    the existing PUT path unchanged."""
    # Build nested structure from dotted paths
    out = {}
    for f in _flatten_fields(model["fields"]):
        if f["path"] not in values:
            continue
        v = values[f["path"]]
        _set_path(out, f["path"], v, f)
    return out


def _flatten_fields(fields):
    """All fields including nested children. Model paths are already
    absolute (e.g. 'venue.coordinates.lat') — no prefix joining, which
    would double the path for children."""
    out = []
    for f in fields:
        out.append(f)
        if f.get("children") and f["widget"] != "repeater":
            out.extend(_flatten_fields(f["children"]))
    return out


def _set_path(obj, path, value, field):
    """path like 'venue.coordinates.lat' or 'games[].opponent' — repeater
    indices are handled by the UI passing indexed paths (games[0].opponent)."""
    parts = path.split(".")
    cur = obj
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        m = re.match(r"^(.+)\[(\d+)\]$", part)
        if m:
            key, idx = m.group(1), int(m.group(2))
            cur.setdefault(key, [])
            while len(cur[key]) <= idx:
                cur[key].append({})
            if is_last:
                cur[key][idx] = _coerce(value, field)
            else:
                cur = cur[key][idx]
        else:
            if is_last:
                cur[part] = _coerce(value, field)
            else:
                cur.setdefault(part, {})
                cur = cur[part]
    return obj


def _coerce(value, field):
    if value is None or value == "":
        # Don't emit empty strings for optional fields the schema forbids
        return None
    if field["type"] in ("number", "integer"):
        try:
            return int(value) if field["type"] == "integer" else float(value)
        except (TypeError, ValueError):
            return value
    if field["type"] == "boolean":
        return bool(value) if isinstance(value, bool) else value in (True, "true", "on", "1")
    return value


# ── Per-module UI config ────────────────────────────────────────────────
# Hand-authored overlays: labels, order, widget upgrades, help text.
# Structure: module → bundle-key → {order?, fields?} | top-level field cfg.
# Only modules listed here get a polished form; the rest can use the raw
# JSON editor (and will get configs in later iterations).
UI_CONFIG = {
    "venue.json": {
        "_title": "Venue",
        "_help": "Where the tournament is played — parents navigate to this.",
        "venue": {
            "order": ["name", "address", "coordinates", "mapsUrl", "parking", "fields", "amenities"],
            "name": {"label": "Venue name", "help": "Official complex name, e.g. Veterans Park"},
            "address": {"label": "Street address", "widget": "textarea", "help": "Full address parents paste into their GPS"},
            "coordinates": {"label": "Map coordinates", "help": "Pin the main entrance, not the middle of the park"},
            "mapsUrl": {"label": "Google Maps link", "help": "Share link from Google Maps (optional — derived from coordinates if blank)"},
            "parking": {"label": "Parking notes", "widget": "textarea", "help": "Where to park, cost, shuttle — tournament-specific, NOT hotel parking"},
            "fields": {
                "label": "Fields",
                "count": {"label": "Number of fields", "help": "How many pitches are used"},
                "surface": {"label": "Surface", "help": "Grass, turf, etc."},
                "map": {"label": "Field map link"},
                "layoutNotes": {"label": "Field layout notes", "widget": "textarea"},
            },
            "amenities": {
                "label": "Amenities",
                "concessions": {"label": "Concessions"},
                "restrooms": {"label": "Restrooms"},
                "shade": {"label": "Shade"},
                "aed": {"label": "AED on site"},
                "playground": {"label": "Playground"},
            },
        },
    },
    "contacts.json": {
        "_title": "Contacts",
        "_help": "Who to call for what. Parents see these in the guide.",
        "contacts": {
            "order": ["manager", "coach", "teamManagers", "tournamentDirector", "emergency"],
            "manager": {"label": "Team manager", "children": {
                "name": {"label": "Name"},
                "role": {"label": "Role"},
                "phone": {"label": "Phone"},
                "email": {"label": "Email"},
            }},
            "coach": {"label": "Coach", "children": {
                "name": {"label": "Name"},
                "role": {"label": "Role"},
                "phone": {"label": "Phone"},
                "email": {"label": "Email"},
            }},
            "teamManagers": {"label": "Team managers", "children": {
                "name": {"label": "Name"},
                "phone": {"label": "Phone"},
                "email": {"label": "Email"},
            }},
            "tournamentDirector": {"label": "Tournament director", "children": {
                "name": {"label": "Name"},
                "email": {"label": "Email"},
                "phone": {"label": "Phone"},
            }},
            "emergency": {"label": "Emergency contacts", "children": {
                "name": {"label": "Name"},
                "phone": {"label": "Phone"},
            }},
        },
    },
    "schedule.json": {
        "_title": "Schedule",
        "_help": "Games — the single most important thing parents check.",
        "games": {
            "label": "Games",
            "help": "Add one row per game. Date/time/opponent are required for every game.",
            "children": {
                "order": ["date", "time", "opponent", "venue", "field", "type"],
                "date": {"label": "Date", "widget": "date"},
                "time": {"label": "Kickoff time", "help": "Player report time is 45 min earlier — the guide computes it"},
                "opponent": {"label": "Opponent"},
                "venue": {"label": "Venue"},
                "field": {"label": "Field"},
                "type": {"label": "Round", "help": "Group stage, quarterfinal, etc."},
            },
        },
        "scheduleStatus": {"label": "Schedule status", "help": "Pending = not released, partial = some games known, confirmed = complete"},
        "scheduleExpected": {"label": "Expected release date", "help": "When the schedule is promised to be out"},
    },
}


def load_ui_config():
    return UI_CONFIG


if __name__ == "__main__":
    import sys
    for mod in ("venue.json", "contacts.json", "schedule.json", "hotels.json"):
        m = build_form_model(mod)
        if m:
            print(f"=== {mod} → {len(m['fields'])} top-level fields ===")
        else:
            print(f"=== {mod} → NO MODEL (raw JSON editor) ===")
