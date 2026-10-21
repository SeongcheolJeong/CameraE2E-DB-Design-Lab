#!/usr/bin/env python3
"""Loopback-only transport faults for manual browser QA of an installed workbench.

Example control file (write atomically when changing it)::

    {"get_503": ["/api/v1/scene_authoring/templates"], "hold_post_runs": false}

An empty object passes traffic through. ``get_503`` accepts only the two scene
template routes or an exact scene-render detail route. ``hold_post_runs`` holds
the *response* of POST /api/v1/runs after forwarding the request exactly once.
Clear that flag to deliver the original response; a hold expires after 30 s.
Missing, malformed, or unsupported control files deny requests with 503. There
is no HTTP control endpoint and this process never writes the control file.

JSONL logs contain normalized routes, method, status, timing and forward count
only. They exclude headers, query strings, IDs, request/response bodies and error
details. A held response emits an entry immediately so QA can navigate away
before releasing it. This harness does not modify application code or results.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK = "127.0.0.1"
MAX_HOLD_SECONDS = 30.0
POLL_SECONDS = 0.05
IO_TIMEOUT_SECONDS = 30.0
CONTROL_LIMIT = 65536
HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "proxy-connection",
})
TEMPLATE_ROUTES = frozenset({
    "/api/v1/scene_authoring/templates", "/api/v1/scene_authoring/projection-template",
})
DETAIL_ROUTE = re.compile(r"/api/v1/scene_renders/[A-Za-z0-9_-]+\Z")
LOG_COLLECTIONS = frozenset({
    "health", "workbench", "assets", "models", "assemblies", "tests", "runs",
    "factory_jobs", "scene_assets", "scene_renders", "scene_projections",
    "scene_projection_publications", "scene_camera_preparations", "spectral_images",
    "captures", "measurements", "validations", "releases", "studies", "surrogates",
    "replays", "control_policies", "control_programs", "scene_sequences",
    "control_sequences", "artifacts", "blobs",
})


class InvalidControlError(ValueError):
    """The control file cannot authorize pass-through traffic."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidControlError("duplicate control key")
        result[key] = value
    return result


def read_control(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            raw = stream.read(CONTROL_LIMIT + 1)
        if len(raw) > CONTROL_LIMIT:
            raise InvalidControlError("oversized control")
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, ValueError, UnicodeError) as exc:
        raise InvalidControlError("unreadable control") from exc
    if not isinstance(value, dict) or set(value) - {"get_503", "hold_post_runs"}:
        raise InvalidControlError("unsupported control")
    denied = value.get("get_503", [])
    hold = value.get("hold_post_runs", False)
    if type(hold) is not bool or not isinstance(denied, list):
        raise InvalidControlError("invalid control types")
    if any(not isinstance(route, str) or (
        route not in TEMPLATE_ROUTES and DETAIL_ROUTE.fullmatch(route) is None
    ) for route in denied):
        raise InvalidControlError("unsupported fault route")
    return {"get_503": denied, "hold_post_runs": hold}


def normalized_route(path: str) -> str:
    """Keep useful route categories without recording query values or object IDs."""
    if path in TEMPLATE_ROUTES or path == "/":
        return path
    parts = path.split("/")
    if len(parts) >= 4 and parts[1:3] == ["api", "v1"] and parts[3] in LOG_COLLECTIONS:
        return "/api/v1/" + parts[3] + ("/{item}" if len(parts) > 4 else "")
    if path in {"/app.js", "/scenes.js", "/styles.css", "/favicon.ico", "/index.html"}:
        return path
    return "/{other}"


def excluded_headers(headers) -> set[str]:
    result = set(HOP_HEADERS)
    for name, value in headers:
        if name.lower() == "connection":
            result.update(item.strip().lower() for item in value.split(","))
    return result


class FaultProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, listen_port: int, upstream_port: int, control_file: Path, log: Path):
        if not 0 <= listen_port <= 65535 or not 1 <= upstream_port <= 65535:
            raise ValueError("ports outside valid range")
        if listen_port and listen_port == upstream_port:
            raise ValueError("proxy and upstream ports must differ")
        if control_file.resolve() == log.resolve():
            raise ValueError("control and log files must differ")
        self.upstream_port = upstream_port
        self.control_file = control_file
        self.log = log
        self.log_lock = threading.Lock()
        self.stopping = threading.Event()
        # Create/check the log before binding. Never touch application state.
        with log.open("a", encoding="utf-8"):
            pass
        super().__init__((LOOPBACK, listen_port), FaultProxyHandler)

    def write_log(self, route, method, status, started, forwards, hold_seconds=0.0):
        entry = {
            "route": normalized_route(route),
            "method": method if re.fullmatch(r"[A-Z]{1,16}", method) else "OTHER",
            "status": status,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "held_ms": round(hold_seconds * 1000, 3),
            "forward_count": forwards,
        }
        with self.log_lock, self.log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def server_close(self):
        self.stopping.set()
        super().server_close()


class FaultProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(IO_TIMEOUT_SECONDS)

    def log_message(self, *_args):
        # BaseHTTP logging includes raw paths and must never be enabled here.
        pass

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._proxy
        raise AttributeError(name)

    def _body(self) -> bytes:
        lengths = self.headers.get_all("Content-Length", [])
        encodings = self.headers.get_all("Transfer-Encoding", [])
        if encodings:
            if lengths or len(encodings) != 1 or encodings[0].lower().strip() != "chunked":
                raise ValueError("ambiguous request framing")
            chunks = []
            while True:
                line = self.rfile.readline(8193)
                if not line.endswith(b"\r\n") or len(line) > 8192:
                    raise ValueError("bad chunk header")
                size_text = line[:-2].split(b";", 1)[0]
                if not re.fullmatch(rb"[0-9a-fA-F]+", size_text):
                    raise ValueError("bad chunk size")
                size = int(size_text, 16)
                if not size:
                    while True:
                        trailer = self.rfile.readline(8193)
                        if trailer == b"\r\n":
                            return b"".join(chunks)
                        if not trailer.endswith(b"\r\n") or len(trailer) > 8192:
                            raise ValueError("bad chunk trailer")
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    raise ValueError("short chunk")
                chunks.append(chunk)
        if not lengths:
            return b""
        if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
            raise ValueError("invalid content length")
        size = int(lengths[0])
        body = self.rfile.read(size)
        if len(body) != size:
            raise ValueError("short body")
        return body

    def _send(self, status, reason, headers, body):
        excluded = excluded_headers(headers) | {"content-length"}
        self.send_response_only(status, reason)
        for name, value in headers:
            if name.lower() not in excluded:
                self.send_header(name, value)
        if self.command == "HEAD" or status == 304:
            for name, value in headers:
                if name.lower() == "content-length":
                    self.send_header(name, value)
        elif status not in {204} and status >= 200:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD" and status not in {204, 304} and status >= 200:
            self.wfile.write(body)

    def _error(self, status):
        body = json.dumps({"error": "QA transport unavailable"}).encode()
        self._send(status, None, [("Content-Type", "application/json")], body)

    def _proxy(self):
        started = time.monotonic()
        route = "/{other}"
        forwards = 0
        held = 0.0
        status = 503
        upstream = None
        try:
            # BaseHTTPRequestHandler normalizes a leading // in self.path. Inspect
            # the original request target so it cannot masquerade as a local route.
            target = self.requestline.split()[1]
            parsed = urlsplit(target)
            if not target.startswith("/") or parsed.scheme or parsed.netloc or parsed.fragment:
                status = 400
                self._error(status)
                return
            route = parsed.path
            control = read_control(self.server.control_file)
            if self.command == "GET" and route in control["get_503"]:
                self._error(status)
                return
            body = self._body()
            headers = list(self.headers.raw_items())
            excluded = excluded_headers(headers) | {"content-length"}
            upstream = http.client.HTTPConnection(
                LOOPBACK, self.server.upstream_port, timeout=IO_TIMEOUT_SECONDS,
            )
            # No request retry. Even a disconnected downstream must not duplicate a POST.
            forwards = 1
            upstream.putrequest(self.command, target, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                if name.lower() not in excluded:
                    upstream.putheader(name, value)
            if body or self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding"):
                upstream.putheader("Content-Length", str(len(body)))
            upstream.putheader("Connection", "close")
            upstream.endheaders(body)
            response = upstream.getresponse()
            status, reason = response.status, response.reason
            response_headers = response.getheaders()
            response_body = response.read()
            if self.command == "POST" and route == "/api/v1/runs" and control["hold_post_runs"]:
                hold_start = time.monotonic()
                # This entry proves the upstream answered before navigation/release.
                self.server.write_log(route, self.command, "held", started, forwards)
                while not self.server.stopping.is_set():
                    held = time.monotonic() - hold_start
                    if held >= MAX_HOLD_SECONDS:
                        break
                    # Invalid control while held is fail-closed, never an implicit release.
                    if not read_control(self.server.control_file)["hold_post_runs"]:
                        break
                    self.server.stopping.wait(min(POLL_SECONDS, MAX_HOLD_SECONDS - held))
                if self.server.stopping.is_set():
                    status = 503
                    self._error(status)
                    return
            self._send(status, reason, response_headers, response_body)
        except InvalidControlError:
            status = 503
            self._error(status)
        except (ValueError, OverflowError):
            status = 400
            self._error(status)
        except (OSError, http.client.HTTPException):
            status = 502
            try:
                self._error(status)
            except OSError:
                pass
        finally:
            if upstream is not None:
                upstream.close()
            self.server.write_log(route, self.command, status, started, forwards, held)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream-port", type=int, required=True)
    parser.add_argument("--control-file", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.listen_port <= 65535:
        parser.error("--listen-port must be 1..65535")
    try:
        server = FaultProxyServer(
            args.listen_port, args.upstream_port, args.control_file, args.log,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
