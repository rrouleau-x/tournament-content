"""Admin server HTTP integration tests — the internet-facing boundary.

Starts a real ThreadingHTTPServer against temp repos and exercises:
  - auth: 401 without token, 200 with token, wrong token rejected
  - traversal: /static/ removed, ../ and encoded paths rejected
  - module name validation: unknown modules rejected
  - org/slug validation: traversal identifiers rejected
  - publish: requires PUBLISH_TOKEN; allow_draft NOT accepted over HTTP
  - approve: refuses content with blocking validation issues
  - SSRF: rules autofill rejects private/loopback URLs
  - optimistic concurrency: stale PUT returns 409
  - audit log written on state-changing actions
"""

import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import FIXTURE_DIR, FIXTURE_TOURNAMENT, git


@pytest.fixture()
def admin_env(tmp_path, monkeypatch):
    """A scratch content repo + TEMP app repo, admin server started on port
    0. The scratch content's _targets.json is rewritten to point at the temp
    app repo so tests can never touch the real app repo."""
    import admin_server
    import compile as compile_mod
    import deploy as deploy_mod
    import pipeline

    # Scratch content tree (modules + scripts + schemas)
    scratch = tmp_path / "content"
    from conftest import REPO_ROOT as REAL_ROOT
    shutil.copytree(REAL_ROOT, scratch,
                    ignore=shutil.ignore_patterns(".venv", "out", "__pycache__",
                                                  ".git", ".pytest_cache",
                                                  ".admin-token", ".publish-token"))

    # TEMP app repo (bare origin + clone) — never the real one
    app_base = tmp_path / "app"
    origin = app_base / "origin.git"
    workdir = app_base / "work"
    origin.mkdir(parents=True)
    git(str(app_base), "init", "--bare", str(origin))
    git(str(app_base), "clone", str(origin), str(workdir))
    git(workdir, "config", "user.email", "test@example.com")
    git(workdir, "config", "user.name", "Test")
    os.makedirs(os.path.join(workdir, "app"), exist_ok=True)
    with open(os.path.join(workdir, "app", "data.json"), "w") as f:
        f.write('{"seed": true}\n')
    git(workdir, "add", "app/data.json")
    git(workdir, "commit", "-m", "seed")
    git(workdir, "branch", "-M", "main")
    git(workdir, "push", "-u", "origin", "main")

    # Point module-level paths at the scratch copy — via monkeypatch so
    # the globals are RESTORED after this test (a plain assignment would
    # leak deploy.REPO_ROOT into later test modules and corrupt them).
    monkeypatch.setattr(admin_server, "REPO_ROOT", str(scratch))
    monkeypatch.setattr(deploy_mod, "REPO_ROOT", str(scratch))
    monkeypatch.setattr(pipeline, "REPO_ROOT", str(scratch))
    monkeypatch.setattr(compile_mod, "REPO_ROOT", str(scratch))
    # Per-IP rate-limit stores are module-level and would accumulate
    # across tests in one process — clear them so the suite never trips
    # the request cap.
    monkeypatch.setattr(admin_server, "_req_times", {})
    monkeypatch.setattr(admin_server, "_fail_counts", {})

    # Rewrite _targets.json → temp app repo (isolate from real deploys)
    targets_path = os.path.join(scratch, "_targets.json")
    with open(targets_path) as f:
        targets = json.load(f)
    targets[FIXTURE_TOURNAMENT] = {
        "repo": "owner/app-repo",
        "appPath": "app/data.json",
        "workDir": str(workdir),
        "mirrorTo": "",
    }
    with open(targets_path, "w") as f:
        json.dump(targets, f, indent=2)

    # Tokens
    admin_server.ADMIN_TOKEN = "admin-test-token"
    admin_server.PUBLISH_TOKEN = "publish-test-token"

    # Start server on an ephemeral port
    server = admin_server.ThreadingHTTPServer(("127.0.0.1", 0), admin_server.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    yield {
        "base": base,
        "admin_token": "admin-test-token",
        "publish_token": "publish-test-token",
        "content": str(scratch),
        "app_workdir": str(workdir),
        "server": server,
    }
    server.shutdown()


def req(method, url, token=None, publish_token=None, body=None):
    """Raw request helper — returns (status, json_or_text)."""
    data = None
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if publish_token:
        headers["X-Publish-Token"] = publish_token
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def test_unauthorized_requests_rejected(admin_env):
    s, _ = req("GET", f"{admin_env['base']}/api/tournaments")
    assert s == 401
    s, _ = req("GET", f"{admin_env['base']}/api/tournaments", token="wrong")
    assert s == 401


def test_authorized_request_ok(admin_env):
    s, d = req("GET", f"{admin_env['base']}/api/tournaments", token=admin_env["admin_token"])
    assert s == 200
    assert isinstance(d.get("tournaments"), list)


def test_ui_served_without_token(admin_env):
    s, body = req("GET", f"{admin_env['base']}/")
    assert s == 200
    assert "<html" in body.lower()


def test_no_static_file_handler(admin_env):
    """/static/ is gone entirely — traversal via it must 404."""
    s, _ = req("GET", f"{admin_env['base']}/static/../.admin-token", token=admin_env["admin_token"])
    assert s == 404
    s, _ = req("GET", f"{admin_env['base']}/static/..%2f.admin-token", token=admin_env["admin_token"])
    assert s == 404


def test_tournament_path_traversal_rejected(admin_env):
    """Encoded traversal that survives client-side URL normalization must be
    rejected by the server's identifier + containment checks."""
    for evil in ("..%2F..", "%2e%2e%2f..", "a%2F..%2Fb", "..%5C.."):
        s, _ = req("GET", f"{admin_env['base']}/api/tournament/{evil}/x",
                   token=admin_env["admin_token"])
        assert s == 400, f"expected 400 for {evil}, got {s}"


def test_unknown_module_rejected(admin_env):
    s, d = req("PUT", f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026/module/evil.json",
               token=admin_env["admin_token"], body={"content": "{}"})
    assert s == 400
    assert "unknown module" in d.get("error", "")


def test_publish_requires_publish_token(admin_env):
    """Publish with only the editor token → 403. With publish token → gate
    still applies (content must be approved)."""
    url = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026/publish"
    s, d = req("POST", url, token=admin_env["admin_token"], body={})
    assert s == 403, d
    assert "publish token" in d.get("error", "").lower()


def test_no_allow_draft_over_http(admin_env):
    """allow_draft in the request body must be ignored — unapproved content
    can never publish over the API even with the publish token."""
    # First make the content unapproved: edit a module so the digest no
    # longer matches the approved revision.
    tdir = os.path.join(admin_env["content"], "orgs", "savannah-united",
                        "tournaments", "sporting-jax-2026")
    hotels_path = os.path.join(tdir, "hotels.json")
    with open(hotels_path) as f:
        hotels = json.load(f)
    hotels["hotels"]["official"][0]["rate"] = "$150/night"
    with open(hotels_path, "w") as f:
        json.dump(hotels, f, indent=2)
        f.write("\n")

    url = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026/publish"
    s, d = req("POST", url, token=admin_env["admin_token"],
               publish_token=admin_env["publish_token"],
               body={"allow_draft": True})
    assert s == 200  # API returns 200 with status error envelope
    assert d.get("status") == "error", d
    assert "digest mismatch" in d.get("message", "").lower() or "approve" in d.get("message", "").lower()


def test_approve_refuses_invalid_content(admin_env, tmp_path):
    """Approval endpoint must refuse content with blocking validation."""
    # Corrupt the fixture hotels drive format to force a blocker
    import copy
    tdir = os.path.join(admin_env["content"], "orgs", "savannah-united",
                        "tournaments", "sporting-jax-2026")
    with open(os.path.join(tdir, "hotels.json")) as f:
        hotels = json.load(f)
    hotels["hotels"]["official"][0]["drive"] = "7.9 miles away"
    with open(os.path.join(tdir, "hotels.json"), "w") as f:
        json.dump(hotels, f, indent=2)
        f.write("\n")
    url = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026/approve"
    s, d = req("POST", url, token=admin_env["admin_token"], body={})
    assert d.get("status") == "error"
    assert "blocking" in d.get("message", "").lower()


def test_rules_autofill_ssrf_blocked(admin_env):
    """Rules autofill fetching a loopback URL must be refused."""
    url = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026/autofill/rules.json"
    s, d = req("POST", url, token=admin_env["admin_token"],
               body={"url": "http://127.0.0.1:8899/secret"})
    assert s == 500 or d.get("error")
    err = str(d)
    assert any(w in err.lower() for w in ("blocked", "non-public", "url"))


def test_stale_put_conflict(admin_env):
    """Optimistic concurrency: saving with a stale baseDigest → 409."""
    url = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026/module/team.json"
    # First read gives the current digest
    s, data = req("GET", f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026",
                  token=admin_env["admin_token"])
    assert s == 200
    current = data["moduleDigests"]["team.json"]
    # Save once (updates the digest)
    s, d = req("PUT", url, token=admin_env["admin_token"],
               body={"content": json.dumps(data["moduleFiles"]["team.json"]), "baseDigest": current})
    assert s == 200, d
    # Save again with the OLD digest → 409
    s, d = req("PUT", url, token=admin_env["admin_token"],
               body={"content": json.dumps(data["moduleFiles"]["team.json"]), "baseDigest": current})
    assert s == 409, d


def test_put_without_digest_rejected_428(admin_env):
    """baseDigest is MANDATORY for existing modules — omitting it is a
    lost-update risk and must be refused with 428."""
    url = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026/module/team.json"
    s, d = req("GET", f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026",
               token=admin_env["admin_token"])
    content = d["moduleFiles"]["team.json"]
    s, d = req("PUT", url, token=admin_env["admin_token"], body={"content": content})
    assert s == 428, d
    assert "baseDigest" in d.get("error", "")


def test_concurrent_puts_single_winner(admin_env):
    """Two truly-simultaneous saves with the same baseDigest: exactly one
    must win (200), the other must get 409 — no silent last-write-wins."""
    url = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026/module/team.json"
    s, data = req("GET", f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026",
                  token=admin_env["admin_token"])
    assert s == 200
    current = data["moduleDigests"]["team.json"]
    base = data["moduleFiles"]["team.json"]

    results = {}
    def worker(tag):
        content = json.dumps(json.loads(base))  # same bytes, same digest
        s, d = req("PUT", url, token=admin_env["admin_token"],
                   body={"content": content, "baseDigest": current})
        results[tag] = s

    import threading
    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(results.values())
    assert codes == [200, 409], f"expected one 200 + one 409, got {results}"


def test_ui_smoke_csp_and_wiring(admin_env):
    """The CSP-compatible UI: index.html has NO inline script/onclick,
    app.js/app.css are served with CSP headers, and app.js contains the
    wiring that makes the dashboard functional."""
    s, html = req("GET", f"{admin_env['base']}/")
    assert s == 200
    assert "onclick=" not in html
    assert "<script>" not in html
    assert 'src="/static/app.js"' in html
    # No inline styles anywhere — the CSP is style-src 'self' (no
    # 'unsafe-inline'), so any style= attribute or .style.* JS write
    # would be silently blocked and the UI would break.
    assert "style=" not in html

    s, js = req("GET", f"{admin_env['base']}/static/app.js")
    assert s == 200
    assert ".style." not in js
    for marker in ('wire("btn-save"', 'wire("btn-publish"', 'DOMContentLoaded',
                   'showList', 'confirmDiscardChanges',
                   # Phase 3 form engine wiring
                   'function renderModuleEditor', 'function getPath',
                   'function setPath', 'function delPath', 'function renderForm',
                   'function renderField', 'btn-toggle-view',
                   # Phase 3.5 review fixes
                   'function validateField', 'function validateAll',
                   'state.formErrors', 'keyvalue',
                   # revision history
                   'function toggleHistory', 'e-history-card', 'history-row'):
        assert marker in js, f"app.js missing {marker}"
    s, css = req("GET", f"{admin_env['base']}/static/app.css")
    assert s == 200

    # CSP header present on HTML + JS
    r = urllib.request.Request(f"{admin_env['base']}/static/app.js")
    with urllib.request.urlopen(r, timeout=10) as resp:
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp


def test_audit_log_written(admin_env):
    url = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026/validate"
    s, _ = req("POST", url, token=admin_env["admin_token"], body={"no_links": True})
    assert s == 200
    log = os.path.join(admin_env["content"], "out", "audit.log")
    assert os.path.exists(log)
    with open(log) as f:
        lines = f.read().strip().splitlines()
    assert any("validate" in l for l in lines)
    assert admin_env["admin_token"][:8] not in "".join(lines)  # never raw token
    assert any('"actor": "root-admin"' in l for l in lines)


def test_user_registry_roles(admin_env):
    """Per-user tokens: editor can edit but NOT publish; publisher can
    publish; audit entries record the real username."""
    users = {
        "Jane": {"token": "editor-token-jane", "role": "editor"},
        "Keith": {"token": "publisher-token-keith", "role": "publisher"},
    }
    with open(os.path.join(admin_env["content"], "users.json"), "w") as f:
        json.dump(users, f)

    base = admin_env["base"]
    tdir = f"{base}/api/tournament/savannah-united/sporting-jax-2026"

    # Editor: authorized for read
    s, d = req("GET", tdir, token="editor-token-jane")
    assert s == 200
    # Editor: NOT authorized to publish (editor role, no publish token)
    s, d = req("POST", f"{tdir}/publish", token="editor-token-jane")
    assert s == 403
    assert "publish" in str(d.get("error", "")).lower()
    # Publisher: authorized to publish (role carries the privilege)
    s, d = req("POST", f"{tdir}/publish", token="publisher-token-keith",
               body={"no_links": True})
    assert s == 200, d
    assert d.get("status") in ("published", "noop"), d

    # Audit log carries the real names, not token prefixes
    with open(os.path.join(admin_env["content"], "out", "audit.log")) as f:
        content = f.read()
    assert '"actor": "Keith"' in content
    assert "editor-token-jane" not in content  # raw token never logged


def test_unknown_user_token_rejected(admin_env):
    s, _ = req("GET", f"{admin_env['base']}/api/tournaments",
               token="not-a-real-user")
    assert s == 401


def test_forms_endpoint_static_no_auth(admin_env):
    """Form models are static UI metadata: served without auth."""
    s, d = req("GET", f"{admin_env['base']}/api/forms/venue.json")
    assert s == 200
    assert d["module"] == "venue.json"
    assert d["title"] == "Venue"
    assert any(f["path"] == "venue.name" for f in d["fields"])

    s, d = req("GET", f"{admin_env['base']}/api/forms/schedule.json")
    assert s == 200
    games = next(f for f in d["fields"] if f["path"] == "games")
    assert games["widget"] == "repeater"

    s, _ = req("GET", f"{admin_env['base']}/api/forms/unknown.json")
    assert s == 404

    s, d = req("GET", f"{admin_env['base']}/api/forms")
    assert s == 200
    assert isinstance(d["forms"], list)
    assert len(d["forms"]) >= 3


def test_validate_proposed_endpoint(admin_env):
    """Candidates are validated WITHOUT saving; valid content passes,
    broken content is rejected with messages, and the file is untouched."""
    tdir = os.path.join(admin_env["content"], "orgs", "savannah-united",
                        "tournaments", "sporting-jax-2026")
    url = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026/module/venue.json"
    token = admin_env["admin_token"]

    # 1. Read the real venue module
    s, data = req("GET", f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026",
                  token=token)
    assert s == 200
    original = data["moduleFiles"]["venue.json"]

    # 2. Valid candidate → valid status, file untouched
    s, d = req("PUT", url, token=token,
               body={"action": "validate-proposed", "content": original})
    assert s == 200, d
    assert d["status"] == "valid", d
    with open(os.path.join(tdir, "venue.json")) as f:
        assert f.read() == original  # untouched

    # 3. Broken candidate (missing required name) → invalid + messages,
    #    file STILL untouched
    broken = json.loads(original)
    del broken["venue"]["name"]
    s, d = req("PUT", url, token=token,
               body={"action": "validate-proposed",
                     "content": json.dumps(broken)})
    assert s == 200, d
    assert d["status"] == "invalid", d
    assert d["blocking"] >= 1
    assert any("name" in m["detail"] for m in d["messages"])
    with open(os.path.join(tdir, "venue.json")) as f:
        assert f.read() == original  # still untouched


def test_publish_audit_records_authority(admin_env):
    """Publish audit entries must record WHO and the AUTHORITY PATH
    (user-role vs root-publish-header)."""
    users = {"Keith": {"token": "publisher-token-keith", "role": "publisher"}}
    with open(os.path.join(admin_env["content"], "users.json"), "w") as f:
        json.dump(users, f)

    tdir = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026"
    s, d = req("POST", f"{tdir}/publish", token="publisher-token-keith",
               body={"no_links": True})
    assert s == 200, d
    assert d.get("status") in ("published", "noop"), d

    with open(os.path.join(admin_env["content"], "out", "audit.log")) as f:
        content = f.read()
    assert '"actor": "Keith"' in content
    assert "authority=user-role:publisher" in content

    # Root publish header path records its own authority label
    s, d = req("POST", f"{tdir}/publish", token=admin_env["admin_token"],
               publish_token=admin_env["publish_token"], body={"no_links": True})
    assert s == 200, d
    with open(os.path.join(admin_env["content"], "out", "audit.log")) as f:
        content = f.read()
    assert "authority=root-publish-header" in content


def _seed_git(admin_env):
    """Init a git repo in the scratch content root + one commit so the
    history/diff endpoints have real data. Returns the commit SHA."""
    content = admin_env["content"]
    git(content, "init", "-q")
    git(content, "config", "user.email", "test@example.com")
    git(content, "config", "user.name", "Test")
    git(content, "add", "-A")
    git(content, "commit", "-q", "-m", "seed")
    return git(content, "rev-parse", "HEAD")


def test_history_endpoint(admin_env):
    base = admin_env["base"]
    token = admin_env["admin_token"]
    _seed_git(admin_env)

    # Whole tournament history
    s, d = req("GET", f"{base}/api/tournament/savannah-united/sporting-jax-2026/history",
               token=token)
    assert s == 200, d
    assert d["count"] >= 1
    assert d["history"][0]["message"] == "seed"
    assert len(d["history"][0]["sha"]) == 40  # full SHA for diff round-trip

    # Module-scoped history
    s, d = req("GET", f"{base}/api/tournament/savannah-united/sporting-jax-2026/history?module=venue.json",
               token=token)
    assert s == 200, d
    assert d["module"] == "venue.json"
    assert d["count"] >= 1

    # History requires auth
    s, _ = req("GET", f"{base}/api/tournament/savannah-united/sporting-jax-2026/history")
    assert s == 401


def test_history_endpoint_bad_module(admin_env):
    base = admin_env["base"]
    token = admin_env["admin_token"]
    _seed_git(admin_env)
    # Traversal attempt in module name → 400
    s, _ = req("GET", f"{base}/api/tournament/savannah-united/sporting-jax-2026/history?module=..%2F..%2Fsecret.json",
               token=token)
    assert s == 400


def test_diff_endpoint(admin_env, tmp_path):
    base = admin_env["base"]
    token = admin_env["admin_token"]
    sha = _seed_git(admin_env)

    # Modify venue.json in the working tree (uncommitted change)
    vpath = os.path.join(admin_env["content"], "orgs", "savannah-united",
                         "tournaments", "sporting-jax-2026", "venue.json")
    with open(vpath) as f:
        v = json.load(f)
    v["venue"]["parking"] = "NEW PARKING NOTE"
    with open(vpath, "w") as f:
        json.dump(v, f, indent=2); f.write("\n")

    # Diff committed SHA → working tree shows the change
    s, d = req("GET", f"{base}/api/tournament/savannah-united/sporting-jax-2026/diff/venue.json?from={sha}",
               token=token)
    assert s == 200, d
    assert d["changed"] is True
    assert "NEW PARKING NOTE" in d["diff"]

    # Diff with no args → both sides missing → 400
    s, d = req("GET", f"{base}/api/tournament/savannah-united/sporting-jax-2026/diff/venue.json",
               token=token)
    assert s == 400

    # Invalid SHA → 400 (no arbitrary git args from client)
    s, d = req("GET", f"{base}/api/tournament/savannah-united/sporting-jax-2026/diff/venue.json?from=abc",
               token=token)
    assert s == 400
    assert "sha" in str(d.get("error", ""))

    # Module that doesn't exist at the commit → clean empty diff, not crash
    s, d = req("GET", f"{base}/api/tournament/savannah-united/sporting-jax-2026/diff/offline.json?from={sha}",
               token=token)
    assert s == 200, d
    assert d["changed"] is False


def test_validate_proposed_never_touches_live_file(admin_env):
    """The candidate must never appear as real content — even while
    validation runs. Reads the module file DURING a validate-proposed
    call and asserts it stays byte-identical (regression for the
    swap-based implementation, which exposed the candidate to concurrent
    readers and could leave it installed on process death)."""
    tdir = os.path.join(admin_env["content"], "orgs", "savannah-united",
                        "tournaments", "sporting-jax-2026")
    vpath = os.path.join(tdir, "venue.json")
    original = open(vpath).read()
    url = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026/module/venue.json"
    token = admin_env["admin_token"]

    candidate = json.dumps({"venue": {"name": "CANDIDATE", "address": "X"}})
    observed = []

    def hammer():
        # Repeatedly read the live file while validation runs
        for _ in range(200):
            with open(vpath) as f:
                observed.append(f.read())

    import threading
    t = threading.Thread(target=hammer)
    t.start()
    s, d = req("PUT", url, token=token,
               body={"action": "validate-proposed", "content": candidate})
    t.join()

    assert s == 200, d
    # The live file NEVER contained the candidate (no swap ever happened)
    assert all(o == original for o in observed), "live file exposed candidate!"
    assert open(vpath).read() == original  # still pristine


def test_publish_authorization_combinations(admin_env):
    """Every bearer × publish-header combination, explicit policy:
    editor role alone → 403; editor + root publish header → allowed
    (capability composition, audited root-publish-header); unknown user +
    root publish header → allowed (header is the capability); no bearer +
    root publish header → allowed; wrong header → 403."""
    users = {
        "Jane": {"token": "editor-token-jane", "role": "editor"},
        "Keith": {"token": "publisher-token-keith", "role": "publisher"},
    }
    with open(os.path.join(admin_env["content"], "users.json"), "w") as f:
        json.dump(users, f)

    tdir = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026"
    pt = admin_env["publish_token"]
    cases = [
        # (bearer, publish_header, expected_status)
        ("editor-token-jane", None, 403),          # editor role alone: no
        ("editor-token-jane", pt, 200),            # editor + root header: yes (capability)
        ("publisher-token-keith", None, 200),      # publisher role alone: yes
        ("publisher-token-keith", pt, 200),        # publisher + header: yes
        ("unknown-user", pt, 401),                 # unknown bearer: not an authenticated principal (header ≠ API identity)
        (None, pt, 401),                           # no bearer: not authenticated at all
        (None, "wrong-publish-token", 401),        # no bearer: auth gate first
        ("editor-token-jane", "wrong-publish-token", 403),  # both wrong: no
    ]
    for bearer, header, expected in cases:
        s, d = req("POST", f"{tdir}/publish", token=bearer,
                   publish_token=header, body={"no_links": True})
        assert s == expected, f"bearer={bearer} header={header}: got {s} {d.get('error','')} (want {expected})"
    # The editor+header publish was audited with the capability path
    with open(os.path.join(admin_env["content"], "out", "audit.log")) as f:
        content = f.read()
    assert '"actor": "Jane"' in content
    assert "authority=root-publish-header" in content


def test_new_tournament_uses_template_not_live_copy(admin_env):
    """Scaffolding must come from _templates/tournament-v1 — never from a
    live tournament — and return an incompleteness checklist."""
    token = admin_env["admin_token"]
    base = admin_env["base"]

    s, d = req("POST", f"{base}/api/tournaments/new", token=token,
               body={"org": "new-org", "slug": "disney-showcase-2027",
                     "name": "Disney Showcase"})
    assert s == 201, d
    assert d["tournament"] == "new-org/disney-showcase-2027"
    assert d["status"] == "draft"
    # Checklist flags the required-but-empty fields
    assert d["checklist"], "expected an incompleteness checklist"
    fields = [c["field"] for c in d["checklist"]]
    assert "tournament.name" in fields
    assert "tournament.dates.start" in fields
    assert "tournament.dates.end" in fields
    assert "team.name" in fields
    assert "venue.name" in fields
    assert "venue.address" in fields
    assert "contacts.manager" in fields
    assert "contacts.coach" in fields

    # The new tournament must NOT contain live Sporting Jax content
    tdir = os.path.join(admin_env["content"], "orgs", "new-org",
                        "tournaments", "disney-showcase-2027")
    with open(os.path.join(tdir, "tournament.json")) as f:
        t = json.load(f)
    assert t["tournament"]["name"] == ""  # template empty, not live data
    # manifest has org + slug + draft + no revision
    with open(os.path.join(tdir, "manifest.json")) as f:
        m = json.load(f)
    assert m["org"] == "new-org"
    assert m["slug"] == "disney-showcase-2027"
    assert m["status"] == "draft"
    assert "revision" not in m


def test_template_compiles_and_fails_predictably(admin_env):
    """The scaffold template must COMPILE (correct shapes — no missing
    keys, no type mismatches) and produce EXACTLY the predictable schema/
    required failures the checklist reports. A template that can't even
    compile, or fails on unexpected checks, breaks the guided flow."""
    import compile as compile_mod
    from validate import Report, run_checks
    T = os.path.join(admin_env["content"], "_templates", "tournament-v1")
    bundle, used, unknown = compile_mod.compile_bundle(T)
    # All 14 registered modules compile (none missing keys/unknown files)
    assert len(used) == 14, f"expected all modules to compile, got {used}"
    assert unknown == []
    # Bundle has every required top-level key
    from pipeline import MODULE_REGISTRY
    for _f, keys, required in MODULE_REGISTRY:
        if required:
            for k in keys:
                assert k in bundle, f"required bundle key {k} missing"
    report = Report()
    run_checks(bundle, report, run_link_checks=False, tdir=T)
    # Exactly the 8 expected failures — no more, no fewer (a change here
    # means the template or schema drifted and the checklist is stale)
    fails = report.blocking()
    messages = {m for _s, _c, m in fails}
    assert len(fails) == 8, f"expected 8 blocking, got {len(fails)}: {messages}"
    expected_substrings = [
        "team/name",
        "tournament/dates/end",
        "tournament/dates/start",
        "tournament/name",
        "venue/address",
        "venue/name",
        "Team manager contact",
        "Head coach contact",
    ]
    for sub in expected_substrings:
        assert any(sub in m for m in messages), f"missing expected failure: {sub}"


def test_manifest_module_save_rejected(admin_env):
    """manifest.json is workflow-managed: neither a raw save nor a
    validate-proposed against it may go through the module endpoint."""
    token = admin_env["admin_token"]
    base = admin_env["base"]
    tdir = f"{base}/api/tournament/savannah-united/sporting-jax-2026"

    s, d = req("PUT", f"{tdir}/module/manifest.json", token=token,
               body={"content": json.dumps({"status": "live"})})
    assert s == 400, d
    assert "workflow" in d["error"]

    s, d = req("PUT", f"{tdir}/module/manifest.json", token=token,
               body={"action": "validate-proposed",
                     "content": json.dumps({"status": "live"})})
    assert s == 400, d
    assert "workflow" in d["error"]


def test_failed_auth_throttle(admin_env):
    """After AUTH_FAIL_LIMIT unauthorized attempts from one IP, the
    server must answer 429 (not 401) until the window expires. A VALID
    credential always succeeds — behind a tunnel all clients share one
    IP, so an IP lockout would self-DoS the real admin; the throttle
    slows guessing without ever locking out a legitimate token."""
    import admin_server as adm
    base = admin_env["base"]
    # First AUTH_FAIL_LIMIT attempts → 401
    for _ in range(adm.AUTH_FAIL_LIMIT):
        s, _d = req("GET", f"{base}/api/tournaments", token="wrong-token")
        assert s == 401
    # Next wrong-token attempt → throttled
    s, _d = req("GET", f"{base}/api/tournaments", token="wrong-token")
    assert s == 429, f"expected 429 after throttle, got {s}"
    # A VALID token is NOT throttled (the lock is on failed attempts,
    # never on credentials that succeed)
    s, _d = req("GET", f"{base}/api/tournaments",
                token=admin_env["admin_token"])
    assert s == 200
    # After the window expires, wrong tokens get 401 again
    import time
    with adm._rate_lock:
        adm._fail_counts.clear()
    s, _d = req("GET", f"{base}/api/tournaments", token="wrong-token")
    assert s == 401


def test_request_rate_cap(admin_env):
    """More than REQ_LIMIT requests per window from one IP → 429."""
    import admin_server as adm
    import time
    base = admin_env["base"]
    token = admin_env["admin_token"]
    # Drive the per-IP request store to the cap with CURRENT timestamps
    # (monotonic 0 would look stale and be evicted immediately)
    with adm._rate_lock:
        adm._req_times["127.0.0.1"] = [time.monotonic()] * adm.REQ_LIMIT
    s, _d = req("GET", f"{base}/api/tournaments", token=token)
    assert s == 429, f"expected 429 over cap, got {s}"
