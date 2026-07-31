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
    import deploy as deploy_mod

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

    # Point module-level paths at the scratch copy
    admin_server.REPO_ROOT = str(scratch)
    deploy_mod.REPO_ROOT = str(scratch)
    import pipeline
    pipeline.REPO_ROOT = str(scratch)
    import compile as compile_mod
    compile_mod.REPO_ROOT = str(scratch)

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


def test_audit_log_written(admin_env):
    url = f"{admin_env['base']}/api/tournament/savannah-united/sporting-jax-2026/validate"
    s, _ = req("POST", url, token=admin_env["admin_token"], body={"no_links": True})
    assert s == 200
    log = os.path.join(admin_env["content"], "out", "audit.log")
    assert os.path.exists(log)
    with open(log) as f:
        lines = f.read().strip().splitlines()
    assert any("validate" in l for l in lines)
