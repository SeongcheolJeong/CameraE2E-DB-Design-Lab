#!/usr/bin/env python3
"""Run the local CameraE2E design workbench without a frontend build service."""

from __future__ import annotations

import argparse
import fcntl
import json
import mimetypes
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / "src"))

from pyisetcam.design_workbench import (  # noqa: E402
    METRICS,
    REPORTS,
    ConflictError,
    DesignWorkbench,
    contained_file,
    encode,
)


def make_handler(service: DesignWorkbench, port: int):
    token = secrets.token_urlsafe(32)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {"http://" + host for host in hosts}
    static = WORKSPACE / "simulations/design_workbench"

    class Handler(BaseHTTPRequestHandler):
        server_version = "CameraE2E/1"

        def log_message(self, format, *args):
            if len(args) > 1 and str(args[1]) not in ("200", "304"):
                super().log_message(format, *args)

        def respond(self, status, payload):
            content = encode(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def file(self, path, *, evidence=False, download=False):
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-cache")
            if evidence and mime in ("text/html", "image/svg+xml"):
                self.send_header(
                    "Content-Security-Policy",
                    "sandbox allow-scripts allow-downloads; default-src 'self' data:; "
                    "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                    "connect-src 'none'; form-action 'none'; base-uri 'none'",
                )
            if not evidence:
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'self'",
                )
            if download:
                self.send_header(
                    "Content-Disposition",
                    'attachment; filename="' + path.name.replace('"', "") + '"',
                )
            self.end_headers()
            with path.open("rb") as source:
                while block := source.read(65536):
                    self.wfile.write(block)

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def do_PATCH(self):
            self.dispatch("PATCH")

        def dispatch(self, method):
            try:
                if self.headers.get("Host") not in hosts:
                    return self.respond(403, {"error": "로컬 주소에서만 접근할 수 있습니다."})
                request = urlsplit(self.path)
                path = unquote(request.path)
                query = parse_qs(request.query)
                parts = [p for p in path.split("/") if p]
                payload = None
                if method != "GET":
                    if self.headers.get("Origin") not in origins or not secrets.compare_digest(
                        self.headers.get("X-Workbench-Token", ""), token
                    ):
                        return self.respond(
                            403, {"error": "현재 작업실에서 시작한 요청만 허용합니다."}
                        )
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        return self.respond(415, {"error": "JSON 요청이 필요합니다."})
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 1_048_576:
                        return self.respond(413, {"error": "요청 크기를 확인하세요."})
                    payload = json.loads(
                        self.rfile.read(length),
                        parse_constant=lambda x: (_ for _ in ()).throw(
                            ValueError("비유한 수치는 허용하지 않습니다.")
                        ),
                    )
                    if not isinstance(payload, dict):
                        raise ValueError("JSON 객체가 필요합니다.")
                if path == "/api/bootstrap" and method == "GET":
                    return self.respond(
                        200,
                        {
                            **service.bootstrap(),
                            "token": token,
                            "metrics": METRICS,
                            "server": {
                                "workspace": str(WORKSPACE),
                                "state_dir": str(service.state_dir),
                            },
                        },
                    )
                if path == "/api/components" and method == "GET":
                    return self.respond(
                        200,
                        service.components(query.get("q", [""])[0], query.get("family", [None])[0]),
                    )
                if len(parts) == 3 and parts[:2] == ["api", "components"] and method == "GET":
                    return self.respond(200, service.component(parts[2]))
                if len(parts) == 3 and parts[:2] == ["api", "reports"] and method == "GET":
                    return self.respond(200, service.report(parts[2]))
                if path == "/api/studies" and method == "POST":
                    return self.respond(201, service.create_study(payload))
                if len(parts) >= 3 and parts[:2] == ["api", "studies"]:
                    identity = parts[2]
                    if len(parts) == 3:
                        if method == "GET":
                            return self.respond(200, service.get_study(identity))
                        if method == "PATCH":
                            return self.respond(200, service.update_study(identity, payload))
                    if len(parts) == 4:
                        actions = {
                            "variants": service.add_variant,
                            "evidence": service.attach_evidence,
                            "decisions": service.add_decision,
                            "experiments": service.start_experiment,
                        }
                        if method == "POST" and parts[3] in actions:
                            return self.respond(201, actions[parts[3]](identity, payload))
                        if method == "GET" and parts[3] == "export":
                            doc = service.get_study(identity)
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json; charset=utf-8")
                            self.send_header(
                                "Content-Disposition",
                                f'attachment; filename="study-{identity}.json"',
                            )
                            self.end_headers()
                            return self.wfile.write(
                                encode(
                                    {"schema": "camerae2e_design_study_v1", "study": doc}
                                ).encode()
                            )
                if len(parts) == 3 and parts[:2] == ["api", "artifacts"] and method == "GET":
                    return self.file(service.resolve_artifact(parts[2]), evidence=True)
                if len(parts) >= 3 and parts[0] == "evidence" and method == "GET":
                    spec = REPORTS.get(parts[1])
                    if spec is None:
                        raise KeyError("자료를 찾을 수 없습니다.")
                    file = contained_file(
                        service.workspace / spec["directory"], "/".join(parts[2:])
                    )
                    if file.suffix.lower() not in {
                        ".html",
                        ".json",
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".svg",
                        ".css",
                        ".js",
                        ".csv",
                        ".npz",
                        ".pdf",
                    }:
                        raise ValueError("허용하지 않는 자료 형식입니다.")
                    return self.file(file, evidence=True)
                if len(parts) == 4 and parts[0] == "runs" and method == "GET":
                    study = service.get_study(parts[1])
                    run = next(
                        (
                            r
                            for r in study["experiments"]
                            if r["id"] == parts[2] and r["status"] == "complete"
                        ),
                        None,
                    )
                    if run is None or not any(
                        a["name"] == parts[3] for a in run["result"]["artifacts"]
                    ):
                        raise KeyError("완료된 실험의 자료를 찾을 수 없습니다.")
                    return self.file(
                        contained_file(service.state_dir / "runs" / parts[2], parts[3]),
                        evidence=True,
                    )
                if method == "GET" and path in (
                    "/",
                    "/index.html",
                    "/app.js",
                    "/style.css",
                    "/logic.mjs",
                ):
                    return self.file(
                        contained_file(static, "index.html" if path == "/" else path[1:])
                    )
                self.respond(404, {"error": "요청한 항목을 찾을 수 없습니다."})
            except ConflictError as exc:
                self.respond(409, {"error": str(exc)})
            except (FileNotFoundError, KeyError) as exc:
                self.respond(404, {"error": str(exc)})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.respond(400, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                print(f"Workbench error: {type(exc).__name__}: {exc}", file=sys.stderr)
                self.respond(500, {"error": "요청을 처리하지 못했습니다. 서버 로그를 확인하세요."})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--catalog-root", type=Path)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    state_dir = (args.state_dir or WORKSPACE / "outputs/design-workbench").resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    process_lock = (state_dir / ".server.lock").open("a")
    try:
        fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("이 저장소를 사용하는 작업실이 이미 실행 중입니다.")
    # Reserve the listener before recovering interrupted study records.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), BaseHTTPRequestHandler)
    service = DesignWorkbench(WORKSPACE, state_dir, args.catalog_root)
    server.RequestHandlerClass = make_handler(service, args.port)
    print(f"CameraE2E Design Workbench: http://127.0.0.1:{args.port}", flush=True)
    print(f"Study storage: {service.database}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.executor.shutdown(wait=True)
        process_lock.close()


if __name__ == "__main__":
    main()
