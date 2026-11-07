"""Run with python3 -m skill_editor.server --port 8765."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import urllib.parse

from . import ai, case_ai
from .cases import CaseStore
from .core import EditorError, SkillStore


def make_handler(store: SkillStore):
    cases = CaseStore(store)

    class Handler(BaseHTTPRequestHandler):
        server_version = "SkillEditor/0.1"

        def log_message(self, format, *args):
            # No request payloads, API tokens, or provider responses are logged.
            pass

        def _check_origin(self):
            port = self.server.server_address[1]
            allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in allowed_hosts:
                raise EditorError("Loopback Host required.", 403)
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in allowed_hosts}:
                raise EditorError("Cross-origin access denied.", 403)

        def _reply(self, status, payload, content_type="application/json; charset=utf-8"):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if content_type.startswith("application/json") else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(raw)

        def _payload(self):
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise EditorError("Use application/json.", 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise EditorError("Invalid content length.")
            if length <= 0 or length > 250_000:
                raise EditorError("Request body must be between 1 and 250000 bytes.", 413)
            try:
                payload = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError):
                raise EditorError("Request body must be JSON.")
            if not isinstance(payload, dict):
                raise EditorError("Request body must be an object.")
            return payload

        def _dispatch(self):
            try:
                self._check_origin()
                url = urllib.parse.urlsplit(self.path)
                path = url.path
                query = urllib.parse.parse_qs(url.query)
                if self.command == "GET":
                    if path == "/api/config":
                        return self._reply(200, ai.config())
                    if path == "/api/health":
                        return self._reply(200, {"ok": True, "version": "0.1.0"})
                    if path == "/api/inventory":
                        return self._reply(200, store.inventory())
                    if path == "/api/cases":
                        return self._reply(200, cases.list())
                    if path == "/api/case":
                        return self._reply(200, cases.read(query.get("id", [""])[0]))
                    if path == "/api/skill":
                        return self._reply(200, store.read(query.get("id", [""])[0]))
                    if path == "/api/intent":
                        identifier = query.get("id", [""])[0]
                        return self._reply(200, {"id": identifier, "markdown": store.intent(identifier)})
                    if path.startswith("/api/"):
                        raise EditorError("Unknown endpoint.", 404)
                    web = (store.project / "web").resolve()
                    relative = urllib.parse.unquote(path).lstrip("/") or "index.html"
                    target = (web / relative).resolve()
                    if web not in target.parents or not target.is_file():
                        raise EditorError("File not found.", 404)
                    return self._reply(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                payload = self._payload()
                if self.command == "PUT" and path == "/api/intent":
                    return self._reply(200, store.save_intent(payload.get("id"), payload.get("markdown")))
                if self.command == "PUT" and path == "/api/case":
                    return self._reply(200, cases.update(query.get("id", [""])[0], payload.get("patch"), payload.get("baselineSha")))
                if self.command == "POST":
                    if path == "/api/cases":
                        return self._reply(201, cases.create(payload))
                    if path == "/api/case/preview":
                        return self._reply(200, cases.preview(payload.get("caseId"), payload.get("skillId"), payload.get("baselineSha")))
                    if path == "/api/case/generate":
                        return self._reply(200, case_ai.generate(cases, payload))
                    if path == "/api/case/predict":
                        return self._reply(200, case_ai.predict(cases, payload))
                    if path == "/api/inventory/refresh":
                        return self._reply(200, store.refresh_inventory())
                    if path == "/api/preview":
                        return self._reply(200, store.preview(payload.get("id"), payload.get("description"), payload.get("baselineSha")))
                    if path == "/api/apply":
                        return self._reply(200, store.apply_description(payload.get("id"), payload.get("description"), payload.get("baselineSha")))
                    if path == "/api/analyze":
                        return self._reply(200, ai.analyze(store, payload))
                raise EditorError("Unknown endpoint or method.", 404)
            except EditorError as exc:
                self._reply(exc.status, {"error": str(exc)})
            except (OSError, UnicodeError):
                self._reply(500, {"error": "A filesystem operation failed. Check paths and permissions."})
            except Exception:
                self._reply(500, {"error": "The operation failed. No diagnostic secrets are returned."})

        do_GET = _dispatch
        do_POST = _dispatch
        do_PUT = _dispatch

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Local Skill Editor")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    store = SkillStore(args.project)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    print(f"Skill Editor: http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
