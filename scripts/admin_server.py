#!/usr/bin/env python3
"""Admin server for the Tournament Platform — local-only HTTP API + static UI.

Serves the admin web UI and exposes the pipeline (compile/validate/approve/
publish) as JSON endpoints. Binds to 127.0.0.1 by default — this is admin
tooling, NOT parent-facing. The PWA is never touched; this server only reads
and writes the content repo through the existing pipeline functions.

AUTH: if ADMIN_TOKEN is set (env or ~/.hermes/www/content/.admin-token),
every /api/* request must include `Authorization: Bearer <token>`. The UI
prompts for the token once and stores it in localStorage. Set a token before
exposing the server through any tunnel — the API can publish content.

Usage:
    ADMIN_TOKEN=secret python3 scripts/admin_server.py [--port 8899] [--host 127.0.0.1]

Endpoints:
    GET  /                              admin UI (static files)
    GET  /api/tournaments               list tournaments + status/revision
    GET  /api/tournament/<org>/<slug>   modules + manifest + digest + health
    PUT  /api/tournament/<org>/<slug>/module/<name>   save a module file
    POST /api/tournament/<org>/<slug>/validate        Guide Health Report (JSON)
    POST /api/tournament/<org>/<slug>/preview         dry-run publish diff
    POST /api/tournament/<org>/<slug>/approve         approve current digest
    POST /api/tournament/<org>/<slug>/publish         transactional publish
    POST /api/tournaments/new                          scaffold from template (draft)
"""

import argparse
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "admin_ui")
TOKEN_FILE = os.path.join(REPO_ROOT, ".admin-token")


def load_token():
    """Token from env ADMIN_TOKEN, else from .admin-token file. If neither
    exists, generate one and write it to the file (so the operator can find
    it). Returns the token or None if auth is disabled."""
    token = os.environ.get("ADMIN_TOKEN", "")
    if token:
        return token
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    token = secrets.token_urlsafe(24)
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.chmod(TOKEN_FILE, 0o600)
        print(f"[admin] generated token → {TOKEN_FILE}", file=sys.stderr)
    except OSError:
        pass
    return token


ADMIN_TOKEN = load_token()


def api_ok(data, status=200):
    body = json.dumps(data, indent=2).encode("utf-8")
    return (status, {"Content-Type": "application/json; charset=utf-8"}, body)


def api_err(message, status=400, exit_code=None):
    return api_ok({"error": message, "exit_code": exit_code}, status)


def list_tournaments():
    import glob
    out = []
    for d in sorted(glob.glob(os.path.join(REPO_ROOT, "orgs", "*", "tournaments", "*"))):
        if not os.path.isdir(d):
            continue
        rel = os.path.relpath(d, os.path.join(REPO_ROOT, "orgs"))
        org, slug = rel.split(os.sep + "tournaments" + os.sep)
        entry = {"org": org, "slug": slug, "tournament": f"{org}/{slug}", "status": "?", "revision": {}}
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


def tournament_path(org, slug):
    return os.path.join(REPO_ROOT, "orgs", org, "tournaments", slug)


def get_tournament(org, slug):
    from compile import compile_bundle, content_digest, serialize
    from pipeline import load_manifest, MODULE_REGISTRY
    tdir = tournament_path(org, slug)
    if not os.path.isdir(tdir):
        return None
    bundle, used, unknown = compile_bundle(tdir)
    output = serialize(bundle)
    manifest = load_manifest(tdir)
    # Raw module file contents (source of truth for editing). The UI edits
    # these files directly — saving writes them back verbatim.
    module_files = {}
    for filename, _keys, _req in MODULE_REGISTRY:
        fpath = os.path.join(tdir, filename)
        if os.path.isfile(fpath):
            try:
                with open(fpath, encoding="utf-8") as f:
                    module_files[filename] = f.read()
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
    }


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
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _authorized(self):
        """Require Bearer token on /api/* routes (unless auth disabled)."""
        if not ADMIN_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {ADMIN_TOKEN}"
        return secrets.compare_digest(header, expected)

    def _guard_api(self):
        if not self._authorized():
            self._send(*api_err("unauthorized — missing or invalid token", 401))
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            fpath = os.path.join(UI_DIR, "index.html")
            if not os.path.exists(fpath):
                return self._send(404, {"Content-Type": "text/plain"}, b"admin UI not built")
            with open(fpath, "rb") as f:
                return self._send(200, {"Content-Type": "text/html; charset=utf-8"}, f.read())
        if path.startswith("/static/"):
            fpath = os.path.join(UI_DIR, path[len("/static/"):])
            if os.path.isfile(fpath):
                ctype = "text/javascript" if fpath.endswith(".js") else (
                    "text/css" if fpath.endswith(".css") else "application/octet-stream")
                with open(fpath, "rb") as f:
                    return self._send(200, {"Content-Type": ctype}, f.read())
            return self._send(404, {"Content-Type": "text/plain"}, b"not found")
        if not path.startswith("/api/"):
            return self._send(*api_err("not found", 404))
        if not self._guard_api():
            return
        if path == "/api/tournaments":
            return self._send(*api_ok({"tournaments": list_tournaments()}))
        if path.startswith("/api/tournament/"):
            parts = path.split("/")
            # /api/tournament/<org>/<slug>
            if len(parts) == 5:
                org, slug = unquote(parts[3]), unquote(parts[4])
                data = get_tournament(org, slug)
                if data is None:
                    return self._send(*api_err(f"no tournament at {org}/{slug}", 404))
                return self._send(*api_ok(data))
        return self._send(*api_err("not found", 404))

    def do_PUT(self):
        path = urlparse(self.path).path
        if not self._guard_api():
            return
        parts = path.split("/")
        # PUT /api/tournament/<org>/<slug>/module/<name>
        if len(parts) == 7 and parts[5] == "module":
            org, slug, module = unquote(parts[3]), unquote(parts[4]), unquote(parts[6])
            tdir = tournament_path(org, slug)
            if not os.path.isdir(tdir):
                return self._send(*api_err(f"no tournament at {org}/{slug}", 404))
            body = self._read_json()
            content = body.get("content")
            if content is None:
                return self._send(*api_err("missing 'content' in body"))
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as e:
                return self._send(*api_err(f"invalid JSON: {e.msg} (line {e.lineno})"))
            fpath = os.path.join(tdir, module)
            if not os.path.isfile(fpath):
                return self._send(*api_err(f"module {module} does not exist"))
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(parsed, f, indent=2, ensure_ascii=False)
                f.write("\n")
            return self._send(*api_ok({"saved": module, "org": org, "slug": slug}))
        return self._send(*api_err("not found", 404))

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._guard_api():
            return
        parts = path.split("/")
        if len(parts) == 6 and parts[5] in ("validate", "preview", "approve", "publish"):
            org, slug, action = unquote(parts[3]), unquote(parts[4]), parts[5]
            body = self._read_json()
            tdir = tournament_path(org, slug)
            if not os.path.isdir(tdir):
                return self._send(*api_err(f"no tournament at {org}/{slug}", 404))
            return self._send(*self._run_action(org, slug, action, body))
        if path == "/api/tournaments/new":
            body = self._read_json()
            return self._send(*self._new_tournament(body))
        return self._send(*api_err("not found", 404))

    def _run_action(self, org, slug, action, body):
        tournament = f"{org}/{slug}"
        try:
            if action == "validate":
                from validate import Report, run_checks
                from compile import compile_bundle
                tdir = tournament_path(org, slug)
                bundle, _, _ = compile_bundle(tdir)
                report = Report()
                run_checks(bundle, report,
                           run_link_checks=not body.get("no_links", False),
                           tdir=tdir)
                return api_ok({"tournament": tournament, **report.to_dict()})
            if action == "preview":
                from deploy import deploy_tournament
                from pipeline import PlatformError
                try:
                    result = deploy_tournament(
                        tournament, dry_run=True,
                        run_link_checks=not body.get("no_links", False))
                    return api_ok(result.to_dict())
                except PlatformError as e:
                    return api_ok({"status": "error", "message": str(e),
                                   "exit_code": e.exit_code}, 200)
            if action == "approve":
                from compile import compile_bundle, content_digest, serialize
                from pipeline import REVISION_APPROVED, write_revision
                tdir = tournament_path(org, slug)
                bundle, _, _ = compile_bundle(tdir)
                digest = content_digest(serialize(bundle))
                write_revision(tdir, REVISION_APPROVED, digest,
                               reviewer=body.get("reviewer", "admin"))
                return api_ok({"tournament": tournament, "digest": digest,
                               "workflow": REVISION_APPROVED})
            if action == "publish":
                from deploy import deploy_tournament
                from pipeline import PlatformError
                try:
                    result = deploy_tournament(
                        tournament,
                        run_link_checks=not body.get("no_links", False),
                        allow_draft=body.get("allow_draft", False))
                    return api_ok(result.to_dict())
                except PlatformError as e:
                    return api_ok({"status": "error", "message": str(e),
                                   "exit_code": e.exit_code}, 200)
        except Exception as e:  # keep the UI alive; surface the error
            return api_err(f"{type(e).__name__}: {e}", 500)
        return api_err("unknown action", 400)

    def _new_tournament(self, body):
        org = body.get("org", "").strip()
        slug = body.get("slug", "").strip()
        if not org or not slug:
            return api_err("org and slug are required")
        from pipeline import PlatformError, parse_tournament_id
        try:
            parse_tournament_id(f"{org}/{slug}")
        except PlatformError as e:
            return api_err(str(e))
        dst = tournament_path(org, slug)
        if os.path.exists(dst):
            return api_err(f"tournament {org}/{slug} already exists")
        src = os.path.join(REPO_ROOT, "orgs", org, "tournaments", "sporting-jax-2026")
        if not os.path.isdir(src):
            return api_err("template tournament missing (sporting-jax-2026)")
        import shutil
        shutil.copytree(src, dst)
        mpath = os.path.join(dst, "manifest.json")
        with open(mpath, encoding="utf-8") as f:
            m = json.load(f)
        m["slug"] = slug
        m["status"] = "draft"
        m["name"] = body.get("name") or f"{slug.replace('-', ' ').title()} (edit me)"
        m.pop("revision", None)  # new tournaments start unapproved
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(m, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return api_ok({"tournament": f"{org}/{slug}", "status": "draft"},
                      status=201)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Admin server: http://{args.host}:{args.port}  (Ctrl-C to stop)")
    print(f"  UI: {os.path.abspath(UI_DIR)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
