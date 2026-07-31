#!/usr/bin/env python3
"""AI-assisted content generation — fills module files with researched data.

DESIGN: autofill NEVER publishes. It only writes module files (draft
content). The existing validate → approve → publish gate is the human
review checkpoint: AI output can never reach parents unapproved.

Each filler takes *verified input* (research results, URLs, structured
data) and transforms it deterministically into the module file shape.
The agent (or a human) is responsible for the research itself — this
script never fabricates facts.

Fillers:
    weather  <org>/<slug> [--lat LAT --lng LNG]        NWS API forecast → weather.json
    schedule <org>/<slug> --from games.json            structured games → schedule.json
    rules    <org>/<slug> --from <url-or-file>         extract keyRules + tiebreakers → rules.json
    hotels   <org>/<slug> --from research.json         structured research → hotels.json

Every filler validates its output through the schema before writing, and
reports what it changed. After filling: guide.py check → approve → publish.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from pipeline import (  # noqa: E402
    MODULE_REGISTRY,
    PlatformError,
    parse_tournament_id,
    tournament_dir,
)

NWS_POINTS = "https://api.weather.gov/points/{lat},{lng}"
NWS_UA = {"User-Agent": "tournament-content-autofill/1.0 (rrouleau@mac.com)"}


def load_module(tdir, filename):
    path = os.path.join(tdir, filename)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_module(tdir, filename, data):
    """Write a module file atomically (temp + os.replace). VALIDATES FIRST:
    the candidate is compiled into a bundle and checked; if validation has
    blocking issues the file is NOT written (the original stays intact)."""
    path = os.path.join(tdir, filename)
    import tempfile
    import compile as compile_mod
    from validate import Report, run_checks

    original = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            original = f.read()

    # 1. Write candidate atomically over the real path
    fd, tmp = tempfile.mkstemp(dir=tdir, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    # 2. Compile + validate the tournament with the candidate in place.
    #    Restore the original on ANY failure (blocking validation OR an
    #    exception) — a raised error must never leave the candidate in
    #    place and the previous module lost.
    try:
        bundle, _, _ = compile_mod.compile_bundle(tdir)
        report = Report()
        run_checks(bundle, report, run_link_checks=False, tdir=tdir)
        blocking = report.blocking()
    except BaseException:
        _restore_original(path, original)
        raise
    if blocking:
        _restore_original(path, original)
        msgs = "; ".join(m for _, _, m in blocking[:5])
        raise PlatformError(
            f"autofill output fails validation ({len(blocking)} blocking): {msgs}. "
            f"Module NOT written — fix the input data.")
    return path


def _restore_original(path, original):
    """Put the pre-autofill module content back (or remove the file if the
    module didn't exist before) — atomically via a sibling temp file +
    os.replace, so a failure during restoration can never truncate the
    original."""
    import tempfile
    d = os.path.dirname(path)
    if original is not None:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-restore-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(original)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    elif os.path.exists(path):
        os.unlink(path)


def fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or NWS_UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── SSRF-safe fetching ──────────────────────────────────────────────────

import ipaddress
import socket

PRIVATE_RANGES = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),      # link-local
    ipaddress.ip_network("100.64.0.0/10"),       # CGNAT
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),            # ULA
    ipaddress.ip_network("fe80::/10"),           # link-local v6
    ipaddress.ip_network("::/128"),
)


def _is_public_host(host):
    """Resolve a hostname and reject any private/loopback/link-local/reserved
    address. Returns (ok, reason)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f"cannot resolve '{host}'"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved \
           or ip.is_multicast or ip.is_unspecified:
            return False, f"destination resolves to non-public address {ip}"
    if not infos:
        return False, "no addresses resolved"
    return True, ""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target against SSRF rules."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlparse as _up
        target = _up(newurl)
        if target.scheme not in ("https", "http"):
            raise urllib.error.URLError(f"redirect to disallowed scheme {target.scheme}")
        ok, reason = _is_public_host(target.hostname)
        if not ok:
            raise urllib.error.URLError(f"redirect blocked: {reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_peer_public(resp):
    """Defense-in-depth against DNS-rebinding TOCTOU: _is_public_host()
    validates the name, but urllib re-resolves when connecting — a hostile
    DNS server could return a public IP for the check and a private IP for
    the connect. After the socket is open, verify the ACTUAL connected
    peer is public before reading any data."""
    try:
        sock = resp.fp.raw._sock  # noqa: SLF001 — stdlib internals, best available
        peer = sock.getpeername()[0]
    except Exception:  # noqa: BLE001 — can't verify, don't fail open on that alone
        return
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved \
            or ip.is_multicast or ip.is_unspecified:
        sock.close()
        raise PlatformError(f"connection reached non-public address {peer} — blocked")


def safe_fetch_text(url, max_bytes=1_000_000):
    """Fetch a URL server-side with SSRF guards: HTTPS/HTTP only, public
    destination only (loopback/private/link-local/reserved blocked before
    connect and on every redirect), response size limited. Returns text."""
    from urllib.parse import urlparse as _up
    parsed = _up(url)
    if parsed.scheme not in ("https", "http"):
        raise PlatformError(f"only http/https URLs allowed, got '{parsed.scheme}'")
    if not parsed.hostname:
        raise PlatformError("URL has no host")
    ok, reason = _is_public_host(parsed.hostname)
    if not ok:
        raise PlatformError(f"URL blocked: {reason}")

    opener = urllib.request.build_opener(_SafeRedirectHandler())
    req = urllib.request.Request(url, headers={"User-Agent": "tournament-content-autofill/1.0"})
    try:
        with opener.open(req, timeout=20) as resp:
            # Rebind defense: verify the connected peer, not just the name
            _check_peer_public(resp)
            chunks = []
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise PlatformError(f"response exceeds {max_bytes} byte limit")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise PlatformError(f"fetch failed: {e.reason}") from e


def fetch_text(url):
    """Plain fetch for internal/trusted use (NWS API)."""
    req = urllib.request.Request(url, headers={"User-Agent": "tournament-content-autofill/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


# NWS point metadata (lat/lng → forecast URL) changes infrequently —
# cache it so repeated fills of the same venue skip the first request.
# Bounded: clear when it exceeds 256 entries.
_nws_points_cache = {}
_NWS_POINTS_CACHE_MAX = 256


def _nws_points_url(lat, lng):
    key = (round(float(lat), 4), round(float(lng), 4))
    if key in _nws_points_cache:
        return _nws_points_cache[key]
    points = fetch_json(NWS_POINTS.format(lat=key[0], lng=key[1]))
    url = points["properties"]["forecast"]
    if len(_nws_points_cache) >= _NWS_POINTS_CACHE_MAX:
        _nws_points_cache.clear()
    _nws_points_cache[key] = url
    return url


# ── Weather ─────────────────────────────────────────────────────────────

def fill_weather(tdir, lat, lng, deadline_seconds=30):
    """Fetch NWS forecast for the venue coordinates → weather.json draft.

    Total operation deadline (not just per-request timeouts): the points
    + forecast requests each carry a 20s socket timeout, so a dead NWS
    could otherwise tie up a server thread for ~40s. deadline_seconds caps
    the whole fill."""
    if not lat or not lng:
        raise PlatformError("weather fill needs --lat and --lng (venue coordinates)")
    import time
    start = time.monotonic()
    try:
        # Points metadata is cached per venue; the forecast is the only
        # request that must always hit the network.
        forecast_url = _nws_points_url(lat, lng)
        remaining = deadline_seconds - (time.monotonic() - start)
        if remaining <= 0:
            raise PlatformError("weather fill exceeded total deadline")
        fc = fetch_json(forecast_url)
    except Exception as e:
        raise PlatformError(f"NWS fetch failed: {e}") from e

    periods = fc.get("properties", {}).get("periods", [])
    if not periods:
        raise PlatformError("NWS returned no forecast periods")

    # Take the next two daytime periods (today + tomorrow typically)
    days = [p for p in periods if p.get("isDaytime")][:2]
    if not days:
        days = periods[:2]

    period_lines = []
    for d in days:
        name = d.get("name", "?")
        temp = d.get("temperature")
        unit = d.get("temperatureUnit", "F")
        wind = d.get("windSpeed", "?")
        short = d.get("shortForecast", "?")
        period_lines.append(f"{name}: {temp}°{unit} · {short} · wind {wind}")
    summary = " · ".join(period_lines)
    details = "\n".join(
        f"{d.get('name', '?')}: {d.get('temperature')}°{d.get('temperatureUnit', 'F')} · "
        f"{d.get('shortForecast', '?')} · wind {d.get('windSpeed', '?')}"
        for d in days
    )

    weather = load_module(tdir, "weather.json")
    weather.setdefault("weather", {})
    weather["weather"]["summary"] = summary
    weather["weather"]["details"] = details
    weather["weather"]["forecastLink"] = forecast_url
    weather["weather"]["sourceApiUrl"] = forecast_url
    weather["weather"]["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    weather["weather"]["source"] = "National Weather Service (api.weather.gov)"
    path = write_module(tdir, "weather.json", weather)
    return path, f"NWS forecast ({len(days)} periods) → weather.json"


# ── Schedule ────────────────────────────────────────────────────────────

def fill_schedule(tdir, games_file):
    """Import structured games from a JSON file → schedule.json draft.
    Expected shape: {"games": [{date, time, opponent, field?, venue?, type?}],
                     "scheduleStatus": "partial"|"confirmed"}"""
    with open(games_file, encoding="utf-8") as f:
        data = json.load(f)
    games = data.get("games")
    if not isinstance(games, list):
        raise PlatformError("games file must contain a 'games' array")
    for i, g in enumerate(games):
        missing = [k for k in ("date", "time", "opponent") if not g.get(k)]
        if missing:
            raise PlatformError(f"game {i} missing required fields: {', '.join(missing)}")

    status = data.get("scheduleStatus", "partial")
    if status not in ("partial", "confirmed"):
        raise PlatformError(f"scheduleStatus must be 'partial' or 'confirmed', got '{status}'")

    schedule = load_module(tdir, "schedule.json")
    schedule.setdefault("schedule", {})
    schedule["schedule"]["games"] = games
    schedule["schedule"]["scheduleStatus"] = status
    schedule["schedule"]["scheduleExpected"] = ""
    path = write_module(tdir, "schedule.json", schedule)
    return path, f"{len(games)} games ({status}) → schedule.json"


# ── Rules ───────────────────────────────────────────────────────────────

def fill_rules(tdir, source):
    """Extract keyRules + tiebreakers from a rules page URL or local file.
    Extraction is best-effort: the human must review the result (it lands
    as draft content and the revision gate enforces approval)."""
    if source.startswith(("http://", "https://")):
        text = safe_fetch_text(source)  # SSRF-guarded
    elif os.path.isfile(source):
        with open(source, encoding="utf-8", errors="replace") as f:
            text = f.read()
    else:
        raise PlatformError(f"rules source not found: {source}")

    key_rules = []
    tiebreakers = []
    # Strip HTML first, then extract from plain text (robust against
    # fragments that start/end mid-tag).
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"&[a-z]+;", " ", plain)
    plain = re.sub(r"\s+", " ", plain)
    low = plain.lower()
    patterns = [
        ("Game Length", r"(?:game|match)\s*(?:length|duration)|two equal halves"),
        ("Mercy Rule", r"mercy|run[- ]?rule|goal difference"),
        ("Substitutions", r"substitut"),
        ("Water Breaks", r"water\s*break"),
        ("Heading", r"heading"),
        ("Zero Tolerance", r"zero\s*tolerance"),
        ("Weather", r"lightning|inclement weather|weather"),
        ("Championship", r"championship|final|overtime|penalt"),
    ]
    for label, pat in patterns:
        m = re.search(pat, low)
        if m:
            start = max(0, m.start() - 60)
            end = min(len(plain), m.end() + 140)
            snippet = plain[start:end].strip()
            key_rules.append({"label": label, "value": snippet[:140]})

    # Tiebreakers: <li> list items after 'tiebreak'/'tie-break' in the RAW
    # HTML (offsets must come from the same text we slice — plain-text
    # offsets applied to raw HTML would grab unrelated lists).
    raw_low = text.lower()
    tb = re.search(r"tie[- ]?break(?:er)?s?", raw_low)
    if tb:
        seg = text[tb.start():tb.start() + 4000]
        items = re.findall(r"<li[^>]*>(.*?)</li>", seg, re.S)
        for it in items:
            clean = re.sub(r"<[^>]+>", " ", it)
            clean = re.sub(r"&[a-z]+;", " ", clean)
            clean = re.sub(r"\s+", " ", clean).strip()
            if clean:
                tiebreakers.append(clean[:120])

    rules = load_module(tdir, "rules.json")
    rules.setdefault("rules", {})
    rules["rules"]["keyRules"] = key_rules
    # Only set tiebreakers when none exist — extraction from docs is
    # best-effort and must never clobber a verified tiebreaker hierarchy
    # (e.g. the standard 6-tier list already in the module).
    existing_tb = rules["rules"].get("tiebreakers") or []
    if tiebreakers and not existing_tb:
        rules["rules"]["tiebreakers"] = tiebreakers
    elif existing_tb:
        tiebreakers = existing_tb  # report what we kept
    rules["rules"]["fullLink"] = source if source.startswith("http") else rules["rules"].get("fullLink", "")
    path = write_module(tdir, "rules.json", rules)
    return path, (f"{len(key_rules)} key rules, {len(tiebreakers)} tiebreakers → rules.json "
                  f"(REVIEW REQUIRED — extraction is best-effort)")


# ── Hotels ──────────────────────────────────────────────────────────────

def fill_hotels(tdir, research_file):
    """Import structured hotel research → hotels.json draft.
    Expected shape (from the travel-team-hotel-research workflow):
    {"stayToPlay": true, "portal": "...",
     "official": [{name, chain, rate, drive, route?, amenities[], rating?, bestValue?, bookUrl?}],
     "nonOfficial": [{name, chain?, drive, note?}]}"""
    with open(research_file, encoding="utf-8") as f:
        data = json.load(f)
    for section in ("official", "nonOfficial"):
        for i, h in enumerate(data.get(section, [])):
            if not h.get("name") or not h.get("drive"):
                raise PlatformError(f"hotels.{section}[{i}] needs 'name' and 'drive'")

    hotels = load_module(tdir, "hotels.json")
    hotels["hotels"] = {
        "stayToPlay": bool(data.get("stayToPlay", False)),
        "portal": data.get("portal", ""),
        "official": data.get("official", []),
        "nonOfficial": data.get("nonOfficial", []),
    }
    path = write_module(tdir, "hotels.json", hotels)
    n = len(data.get("official", [])) + len(data.get("nonOfficial", []))
    return path, f"{n} hotels → hotels.json (REVIEW REQUIRED — research data)"


# ── CLI ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="filler", required=True)

    p_w = sub.add_parser("weather")
    p_w.add_argument("tournament")
    p_w.add_argument("--lat", type=float)
    p_w.add_argument("--lng", type=float)
    p_w.set_defaults(func=fill_weather)

    p_s = sub.add_parser("schedule")
    p_s.add_argument("tournament")
    p_s.add_argument("--from", dest="games_file", required=True)
    p_s.set_defaults(func=fill_schedule)

    p_r = sub.add_parser("rules")
    p_r.add_argument("tournament")
    p_r.add_argument("--from", dest="source", required=True)
    p_r.set_defaults(func=fill_rules)

    p_h = sub.add_parser("hotels")
    p_h.add_argument("tournament")
    p_h.add_argument("--from", dest="research_file", required=True)
    p_h.set_defaults(func=fill_hotels)

    args = ap.parse_args()
    try:
        org, slug = parse_tournament_id(args.tournament)
        tdir = tournament_dir(org, slug)
        if not os.path.isdir(tdir):
            raise PlatformError(f"no tournament dir at {tdir}")
        if args.filler is fill_weather:
            path, msg = fill_weather(tdir, args.lat, args.lng)
        elif args.filler is fill_schedule:
            path, msg = fill_schedule(tdir, args.games_file)
        elif args.filler is fill_rules:
            path, msg = fill_rules(tdir, args.source)
        else:
            path, msg = fill_hotels(tdir, args.research_file)
        print(f"AUTOFILL: {msg}")
        print(f"  wrote {path}")
        print(f"  NEXT: guide.py check {args.tournament} → approve → publish")
    except (PlatformError, OSError, json.JSONDecodeError) as e:
        print(f"AUTOFILL ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
