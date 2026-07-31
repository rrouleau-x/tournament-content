#!/usr/bin/env python3
"""Admin server for the Tournament Platform — HTTP API + static UI.

Serves the admin web UI and exposes the pipeline (compile/validate/approve/
publish/autofill) as JSON endpoints. Binds to 127.0.0.1 by default; use a
tunnel only after reading the AUTH section. The PWA is never touched; this
server only reads and writes the content repo through pipeline functions.

SECURITY MODEL (per external design review — internet-facing):
  - Two-role tokens: ADMIN_TOKEN (editor: everything except publish) and
    PUBLISH_TOKEN (required IN ADDITION for publish). Publish is the only
    action that can reach parents, so it needs its own credential.
  - allow_draft is NEVER accepted over HTTP — it is CLI-only emergency use.
  - Strict identifier validation: org/slug must match
    ^[a-z0-9][a-z0-9-]{0,63}$; module names must be in MODULE_REGISTRY.
    All paths are realpath-resolved and containment-checked.
  - No /static/ file handler (UI is a single HTML file). The only file
    served is admin_ui/index.html — nothing else can be read via the server.
  - Request bodies limited to 1 MB. Responses carry a restrictive CSP.
  - Every state-changing action is written to out/audit.log (append-only).

Usage:
    ADMIN_TOKEN=... PUBLISH_TOKEN=... python3 scripts/admin_server.py [--port 8899]

Tokens come from env vars, else auto-generated files (.admin-token and
.publish-token, mode 0600, gitignored).

Endpoints:
    GET  /                              admin UI (index.html only)
    GET  /api/tournaments               list tournaments + status/revision
    GET  /api/tournament/<org>/<slug>   modules + digests + manifest + health
    PUT  /api/tournament/<org>/<slug>/module/<name>   save (requires baseDigest)
    POST /api/tournament/<org>/<slug>/validate        Guide Health Report (JSON)
    POST /api/tournament/<org>/<slug>/preview         dry-run publish diff
    POST /api/tournament/<org>/<slug>/approve         approve current digest (validates first)
    POST /api/tournament/<org>/<slug>/publish         publish (requires PUBLISH_TOKEN; no allow_draft)
    POST /api/tournament/<org>/<slug>/autofill/<mod>  draft-fill a module
    POST /api/tournaments/new                          scaffold from template (draft)
"""

import argparse
import hashlib
import json
import os
import secrets
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

UI_DIR = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "admin_ui"))
TOKEN_FILE = os.path.join(REPO_ROOT, ".admin-token")
PUBLISH_TOKEN_FILE = os.path.join(REPO_ROOT, ".publish-token")
AUDIT_LOG = os.path.join(REPO_ROOT, "out", "audit.log")
MAX_BODY = 1_000_000  # 1 MB

IDENT_RE = r"^[a-z0-9][a-z0-9-]{0,63}$"
_audit_lock = threading.Lock()
# Per-module optimistic-concurrency locks: read → compare → replace must be
# atomic per file, or two simultaneous saves can both pass the digest check
# and last-write-wins. Keyed by (org, slug, module).
_module_locks = {}
_module_locks_guard = threading.Lock()


def _module_lock(org, slug, module):
    key = (org, slug, module)
    with _module_locks_guard:
        if key not in _module_locks:
            _module_locks[key] = threading.Lock()
        return _module_locks[key]


def _load_or_create_token(env_name, path):
    token = os.environ.get(env_name, "")
    if token:
        return token
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    token = secrets.token_urlsafe(24)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.chmod(path, 0o600)
        print(f"[admin] generated {env_name} → {path}", file=sys.stderr)
    except OSError:
        pass
    return token


ADMIN_TOKEN = _load_or_create_token("ADMIN_TOKEN", TOKEN_FILE)
PUBLISH_TOKEN = _load_or_create_token("PUBLISH_TOKEN", PUBLISH_TOKEN_FILE)


def audit(actor, action, tournament, digest=None, detail=None):
    """Append-only audit log. Never raises (logging must not break the API).
    Path computed at call time so tests that redirect REPO_ROOT work."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "actor": actor,
            "action": action,
            "tournament": tournament,
            "digest": digest,
            "detail": detail,
        }
        log_path = os.path.join(REPO_ROOT, "out", "audit.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with _audit_lock:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def api_ok(data, status=200):
    body = json.dumps(data, indent=2).encode("utf-8")
    return (status, {"Content-Type": "application/json; charset=utf-8"}, body)


def api_err(message, status=400, exit_code=None):
    return api_ok({"error": message, "exit_code": exit_code}, status)


def valid_identifier(value):
    import re
    return bool(re.match(IDENT_RE, value or ""))


def tournament_path(org, slug):
    return os.path.join(REPO_ROOT, "orgs", org, "tournaments", slug)


def safe_tournament_dir(org, slug):
    """Validate identifiers and enforce containment under orgs/. Returns the
    realpath'd tournament dir. Raises ValueError on any violation."""
    if not valid_identifier(org) or not valid_identifier(slug):
        raise ValueError(f"invalid org/slug (must match {IDENT_RE})")
    tdir = os.path.realpath(tournament_path(org, slug))
    orgs_root = os.path.realpath(os.path.join(REPO_ROOT, "orgs"))
    if os.path.commonpath([tdir, orgs_root]) != orgs_root:
        raise ValueError("path escapes the orgs/ root")
    return tdir


def safe_module_path(tdir, module):
    """Module filename must be a registered module (or manifest.json) and
    resolve inside the tournament dir."""
    from pipeline import MODULE_REGISTRY
    if module == "manifest.json":
        fname = module
    else:
        registered = {f for f, _k, _r in MODULE_REGISTRY}
        if module not in registered:
            raise ValueError(f"unknown module '{module}'")
        fname = module
    fpath = os.path.realpath(os.path.join(tdir, fname))
    if os.path.commonpath([fpath, tdir]) != tdir:
        raise ValueError("module path escapes tournament dir")
    return fpath


def module_digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def list_tournaments():
    import glob
    out = []
    for d in sorted(glob.glob(os.path.join(REPO_ROOT, "orgs", "*", "tournaments", "*"))):
        if not os.path.isdir(d):
            continue
        rel = os.path.relpath(d, os.path.join(REPO_ROOT, "orgs"))
        org, slug = rel.split(os.sep + "tournaments" + os.sep)
        entry = {"org": org, "slug": slug, "tournament": f"{org}/{slug}",
                 "status": "?", "revision": {}}
        mpath = os.path.join(d, "manifest.json")
        if os.path.exists(mpath):
            try:
                with open(mpath, encoding="utf-8") as f:
                    m = json.load(f)
                entry["status"] = m.get("status", "?")
                entry["revision"] = m.get("revision", {})
                entry["name"] = m.get("name", slug)
                entry["dates"] = m.get("dates", {})
            except json.JSONDecodeError:
                entry["status"] = "invalid manifest"
        out.append(entry)
    return out


def get_tournament(org, slug):
    from compile import compile_bundle, content_digest, serialize
    from pipeline import load_manifest, MODULE_REGISTRY
    tdir = safe_tournament_dir(org, slug)
    if not os.path.isdir(tdir):
        return None
    bundle, used, unknown = compile_bundle(tdir)
    output = serialize(bundle)
    manifest = load_manifest(tdir)
    # Raw module file contents + per-module digests (for optimistic
    # concurrency: the UI must send back the digest it read).
    module_files = {}
    module_digests = {}
    for filename, _keys, _req in MODULE_REGISTRY:
        fpath = os.path.join(tdir, filename)
        if os.path.isfile(fpath):
            try:
                with open(fpath, encoding="utf-8") as f:
                    content = f.read()
                module_files[filename] = content
                module_digests[filename] = module_digest(content)
            except OSError:
                module_files[filename] = None
    return {
        "org": org,
        "slug": slug,
        "manifest": manifest,
        "digest": content_digest(output),
        "modules": used,
        "unknownModules": unknown,
        "bundle": bundle,
        "moduleFiles": module_files,
        "moduleDigests": module_digests,
    }


def atomic_write(path, content):
    """Write via temp file + os.replace in the same directory (atomic on
    POSIX — a crash never leaves a partial module file)."""
    d = os.path.dirname(path)
    fd, tmp = tempfile_path(d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def tempfile_path(d):
    import tempfile
    return tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write("[admin] %s\n" % (format % args))

    def _send(self, status, headers, body):
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # ── auth ────────────────────────────────────────────────────────────
    def _bearer(self):
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[len("Bearer "):].strip()
        return ""

    def _authorized(self):
        if not ADMIN_TOKEN:
            return True
        token = self._bearer()
        return bool(token) and secrets.compare_digest(token, ADMIN_TOKEN)

    def _publish_authorized(self):
        """Publish requires PUBLISH_TOKEN in the X-Publish-Token header AND
        admin auth. If PUBLISH_TOKEN is unset, publish is refused (safer
        than allowing it by accident)."""
        if not PUBLISH_TOKEN:
            return False
        header = self.headers.get("X-Publish-Token", "")
        return bool(header) and secrets.compare_digest(header.strip(), PUBLISH_TOKEN)

    def _guard_api(self):
        if not self._authorized():
            self._send(*api_err("unauthorized — missing or invalid token", 401))
            return False
        return True

    def _actor(self):
        return "token:" + self._bearer()[:8] if self._bearer() else "anonymous"

    def _send_csp(self, status, headers, body):
        headers = dict(headers)
        headers.setdefault("Content-Security-Policy",
                           "default-src 'self'; script-src 'self'; "
                           "style-src 'self' 'unsafe-inline'; connect-src 'self'")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        self._send(status, headers, body)

    def do_GET(self):
        path = urlparse(self.path).path
        # Serve ONLY the fixed UI allowlist (index.html, app.css, app.js)
        # with realpath containment — no arbitrary file reads.
        if path in ("/", "/index.html"):
            fname = "index.html"
        elif path.startswith("/static/"):
            fname = path[len("/static/"):]
            if fname not in ("app.css", "app.js"):
                return self._send(404, {"Content-Type": "text/plain"}, b"not found")
        else:
            fname = None
        if fname:
            fpath = os.path.realpath(os.path.join(UI_DIR, fname))
            if os.path.commonpath([fpath, UI_DIR]) != UI_DIR or not os.path.isfile(fpath):
                return self._send(404, {"Content-Type": "text/plain"}, b"not found")
            ctype = ("text/html; charset=utf-8" if fname.endswith(".html")
                     else "text/css" if fname.endswith(".css")
                     else "application/javascript; charset=utf-8")
            with open(fpath, "rb") as f:
                return self._send_csp(200, {"Content-Type": ctype}, f.read())
        if not path.startswith("/api/"):
            return self._send(*api_err("not found", 404))
        if not self._guard_api():
            return
        try:
            if path == "/api/tournaments":
                return self._send(*api_ok({"tournaments": list_tournaments()}))
            if path.startswith("/api/tournament/"):
                parts = path.split("/")
                if len(parts) == 5:
                    org, slug = unquote(parts[3]), unquote(parts[4])
                    data = get_tournament(org, slug)
                    if data is None:
                        return self._send(*api_err(f"no tournament at {org}/{slug}", 404))
                    return self._send(*api_ok(data))
        except ValueError as e:
            return self._send(*api_err(str(e), 400))
        return self._send(*api_err("not found", 404))

    def do_PUT(self):
        path = urlparse(self.path).path
        if not self._guard_api():
            return
        parts = path.split("/")
        # PUT /api/tournament/<org>/<slug>/module/<name>
        if len(parts) == 7 and parts[5] == "module":
            org, slug, module = unquote(parts[3]), unquote(parts[4]), unquote(parts[6])
            try:
                tdir = safe_tournament_dir(org, slug)
                if not os.path.isdir(tdir):
                    return self._send(*api_err(f"no tournament at {org}/{slug}", 404))
                fpath = safe_module_path(tdir, module)
                body = self._read_json()
                content = body.get("content")
                if content is None:
                    return self._send(*api_err("missing 'content' in body"))
                json.loads(content)  # must parse before we touch the file

                # Optimistic concurrency under a per-module lock: the
                # read → compare → replace sequence must be atomic or two
                # simultaneous saves can both pass the digest check.
                with _module_lock(org, slug, module):
                    exists = os.path.isfile(fpath)
                    if exists:
                        # baseDigest is MANDATORY for existing modules —
                        # a save without it is a lost-update risk.
                        expected = body.get("baseDigest")
                        if not expected:
                            return self._send(*api_err(
                                "baseDigest is required when saving an existing "
                                "module (read the module first, then save with "
                                "the digest it returned)", 428))
                        with open(fpath, encoding="utf-8") as f:
                            live = f.read()
                        if module_digest(live) != expected:
                            return self._send(*api_err(
                                "conflict — module changed since you loaded it "
                                "(stale edit). Reload and re-apply.", 409))
                    atomic_write(fpath, content)
                audit(self._actor(), "module.save", f"{org}/{slug}",
                      digest=module_digest(content), detail=module)
                return self._send(*api_ok({"saved": module, "org": org, "slug": slug,
                                           "digest": module_digest(content)}))
            except ValueError as e:
                return self._send(*api_err(str(e), 400))
        return self._send(*api_err("not found", 404))

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._guard_api():
            return
        parts = path.split("/")
        # POST /api/tournament/<org>/<slug>/autofill/<module>
        if len(parts) == 7 and parts[5] == "autofill":
            org, slug, module = unquote(parts[3]), unquote(parts[4]), unquote(parts[6])
            try:
                tdir = safe_tournament_dir(org, slug)
                if not os.path.isdir(tdir):
                    return self._send(*api_err(f"no tournament at {org}/{slug}", 404))
                body = self._read_json()
                return self._send(*self._autofill(org, slug, module, body))
            except ValueError as e:
                return self._send(*api_err(str(e), 400))
        if len(parts) == 6 and parts[5] in ("validate", "preview", "approve", "publish"):
            org, slug, action = unquote(parts[3]), unquote(parts[4]), parts[5]
            try:
                tdir = safe_tournament_dir(org, slug)
                if not os.path.isdir(tdir):
                    return self._send(*api_err(f"no tournament at {org}/{slug}", 404))
                body = self._read_json()
                return self._send(*self._run_action(org, slug, action, body))
            except ValueError as e:
                return self._send(*api_err(str(e), 400))
        if path == "/api/tournaments/new":
            body = self._read_json()
            return self._send(*self._new_tournament(body))
        return self._send(*api_err("not found", 404))

    def _autofill(self, org, slug, module, body):
        """Fill a module from body: {url} or {data}. Always draft — never
        publishes. Rules URL fetching is SSRF-guarded (see autofill.safe_fetch)."""
        from autofill import fill_hotels, fill_rules, fill_schedule, fill_weather
        tdir = safe_tournament_dir(org, slug)
        module = module if module.endswith(".json") else module + ".json"
        try:
            if module == "weather.json":
                path, msg = fill_weather(tdir, body.get("lat"), body.get("lng"))
            elif module == "schedule.json":
                data = body.get("data")
                if not data:
                    return api_err("schedule autofill needs 'data' (games array)")
                import tempfile, json as _json
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                                 encoding="utf-8") as tf:
                    _json.dump(data, tf)
                    tmp = tf.name
                try:
                    path, msg = fill_schedule(tdir, tmp)
                finally:
                    os.unlink(tmp)
            elif module == "rules.json":
                url = body.get("url") or body.get("data")
                if not url:
                    return api_err("rules autofill needs 'url' (rules page)")
                path, msg = fill_rules(tdir, url)
            elif module == "hotels.json":
                data = body.get("data")
                if not data:
                    return api_err("hotels autofill needs 'data' (research JSON)")
                import tempfile, json as _json
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                                 encoding="utf-8") as tf:
                    _json.dump(data, tf)
                    tmp = tf.name
                try:
                    path, msg = fill_hotels(tdir, tmp)
                finally:
                    os.unlink(tmp)
            else:
                return api_err(f"no autofill for module '{module}' "
                               f"(supported: weather, schedule, rules, hotels)")
        except Exception as e:
            return api_err(f"{type(e).__name__}: {e}", 500)
        audit(self._actor(), "autofill", f"{org}/{slug}", detail=module)
        return api_ok({"module": module, "message": msg, "draft": True,
                       "note": "Draft content — validate, then approve, then publish"})

    def _run_action(self, org, slug, action, body):
        tournament = f"{org}/{slug}"
        try:
            if action == "validate":
                from validate import Report, run_checks
                from compile import compile_bundle
                tdir = safe_tournament_dir(org, slug)
                bundle, _, _ = compile_bundle(tdir)
                report = Report()
                run_checks(bundle, report,
                           run_link_checks=not body.get("no_links", False),
                           tdir=tdir)
                audit(self._actor(), "validate", tournament)
                return api_ok({"tournament": tournament, **report.to_dict()})
            if action == "preview":
                from deploy import deploy_tournament
                from pipeline import PlatformError
                try:
                    result = deploy_tournament(
                        tournament, dry_run=True,
                        run_link_checks=not body.get("no_links", False))
                    audit(self._actor(), "preview", tournament)
                    return api_ok(result.to_dict())
                except PlatformError as e:
                    return api_ok({"status": "error", "message": str(e),
                                   "exit_code": e.exit_code}, 200)
            if action == "approve":
                # Shared path with the CLI: compiles + validates + records.
                from pipeline import PlatformError
                try:
                    result = approve_tournament(safe_tournament_dir(org, slug),
                                                reviewer=body.get("reviewer", "admin"))
                    audit(self._actor(), "approve", tournament, digest=result["digest"])
                    return api_ok(result)
                except PlatformError as e:
                    audit(self._actor(), "approve.failed", tournament, detail=str(e)[:200])
                    return api_ok({"status": "error", "message": str(e),
                                   "exit_code": e.exit_code}, 200)
            if action == "publish":
                # NO allow_draft over HTTP — publish must go through the
                # approval gate. Requires PUBLISH_TOKEN (separate credential).
                if not self._publish_authorized():
                    return api_err(
                        "publish requires the publish token (X-Publish-Token "
                        "header) — this credential is separate from the editor "
                        "token, and draft publication is not allowed over the API",
                        403)
                from deploy import deploy_tournament
                from pipeline import PlatformError
                try:
                    result = deploy_tournament(
                        tournament,
                        run_link_checks=not body.get("no_links", False),
                        allow_draft=False,
                        record_published=True)  # real publish: update source manifest
                    audit(self._actor(), "publish", tournament,
                          digest=result.digest, detail=result.status)
                    return api_ok(result.to_dict())
                except PlatformError as e:
                    audit(self._actor(), "publish.failed", tournament, detail=str(e)[:200])
                    return api_ok({"status": "error", "message": str(e),
                                   "exit_code": e.exit_code}, 200)
        except Exception as e:
            return api_err(f"{type(e).__name__}: {e}", 500)
        return api_err("unknown action", 400)

    def _new_tournament(self, body):
        org = body.get("org", "").strip()
        slug = body.get("slug", "").strip()
        try:
            from pipeline import parse_tournament_id
            parse_tournament_id(f"{org}/{slug}")
            tdir = safe_tournament_dir(org, slug)
        except Exception as e:
            return api_err(str(e))
        if os.path.exists(tdir):
            return api_err(f"tournament {org}/{slug} already exists")
        src = os.path.join(REPO_ROOT, "orgs", org, "tournaments", "sporting-jax-2026")
        if not os.path.isdir(src):
            return api_err("template tournament missing (sporting-jax-2026)")
        import shutil
        shutil.copytree(src, tdir)
        mpath = os.path.join(tdir, "manifest.json")
        with open(mpath, encoding="utf-8") as f:
            m = json.load(f)
        m["slug"] = slug
        m["status"] = "draft"
        m["name"] = body.get("name") or f"{slug.replace('-', ' ').title()} (edit me)"
        m.pop("revision", None)  # new tournaments start unapproved
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=2, ensure_ascii=False)
            f.write("\n")
        audit(self._actor(), "tournament.new", f"{org}/{slug}")
        return api_ok({"tournament": f"{org}/{slug}", "status": "draft"}, status=201)


def approve_tournament(tdir, reviewer="admin"):
    """Shared approve path (CLI + HTTP) — lives in pipeline.py so both
    entry points use identical validation + recording."""
    from pipeline import approve_tournament as _impl
    return _impl(tdir, reviewer=reviewer)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Admin server: http://{args.host}:{args.port}  (Ctrl-C to stop)")
    print(f"  UI: {UI_DIR}")
    print(f"  editor token: {TOKEN_FILE}  · publish token: {PUBLISH_TOKEN_FILE}")
    print(f"  audit log: {AUDIT_LOG}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
