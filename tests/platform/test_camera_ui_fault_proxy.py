"""Real loopback HTTP checks for the browser QA transport harness, not product gates."""

import gzip
import http.client
import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def proxy_module():
    path = Path(__file__).resolve().parents[2] / "tools/camera_ui_fault_proxy.py"
    spec = importlib.util.spec_from_file_location("camera_ui_fault_proxy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


@pytest.fixture
def harness(proxy_module, tmp_path):
    requests = []
    payload = gzip.compress(b"immutable binary response\x00\xff", mtime=0)

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def __getattr__(self, name):
            if name.startswith("do_"):
                return self.respond
            raise AttributeError(name)

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append({
                "method": self.command, "path": self.path, "body": body,
                "headers": list(self.headers.raw_items()),
            })
            code = 418 if self.path.startswith("/upstream-error") else 202
            self.send_response_only(code, "QA upstream status")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Encoding", "gzip")
            chunked = self.path == "/chunked-response"
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(payload)))
            self.send_header("Set-Cookie", "a=first")
            self.send_header("Set-Cookie", "b=second")
            self.send_header("ETag", '"immutable-hash"')
            self.send_header("Connection", "close, X-Upstream-Hop")
            self.send_header("X-Upstream-Hop", "omit-me")
            self.end_headers()
            if self.command != "HEAD":
                if chunked:
                    self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
                else:
                    self.wfile.write(payload)

    control = tmp_path / "control.json"
    control.write_text("{}")
    log = tmp_path / "proxy.jsonl"
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    proxy = proxy_module.FaultProxyServer(0, upstream.server_port, control, log)
    threads = [
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        for server in (upstream, proxy)
    ]
    for thread in threads:
        thread.start()

    def configure(value):
        temporary = tmp_path / "next-control.json"
        temporary.write_text(json.dumps(value))
        temporary.replace(control)

    def entries():
        # A log writer can currently be appending its last line.
        lines = log.read_text().splitlines(keepends=True)
        return [json.loads(line) for line in lines if line.endswith("\n")]

    def request(method="GET", path="/api/v1/workbench", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=4)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    try:
        yield SimpleNamespace(
            proxy=proxy, upstream=upstream, requests=requests, payload=payload,
            control=control, log=log, configure=configure, entries=entries, request=request,
        )
    finally:
        proxy.stopping.set()
        for server in (proxy, upstream):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def test_passthrough_preserves_method_payload_status_and_end_to_end_headers(harness, method):
    original_control = harness.control.read_bytes()
    body = b"request\x00\xffprivate-body-token"
    status, headers, result = harness.request(
        method, "/api/v1/runs?token=private-query-token", body,
        {"Authorization": "Bearer private-header-token", "Cookie": "private-cookie-token",
         "Host": "127.0.0.1:49999", "Origin": "http://127.0.0.1:49999",
         "Content-Type": "application/octet-stream", "Connection": "X-Request-Hop",
         "X-Request-Hop": "must-not-pass"},
    )
    assert status == 202
    assert result == (b"" if method == "HEAD" else harness.payload)
    assert [value for key, value in headers if key.lower() == "set-cookie"] == [
        "a=first", "b=second",
    ]
    assert dict(headers)["Content-Encoding"] == "gzip"
    assert dict(headers)["Content-Length"] == str(len(harness.payload))
    assert dict(headers)["ETag"] == '"immutable-hash"'
    assert not any(name.lower() == "x-upstream-hop" for name, _ in headers)
    assert len(harness.requests) == 1
    captured = harness.requests[0]
    assert captured["method"] == method
    assert captured["path"] == "/api/v1/runs?token=private-query-token"
    assert captured["body"] == body
    forwarded_headers = {key.lower(): value for key, value in captured["headers"]}
    assert forwarded_headers["authorization"] == "Bearer private-header-token"
    assert forwarded_headers["cookie"] == "private-cookie-token"
    assert forwarded_headers["host"] == "127.0.0.1:49999"
    assert forwarded_headers["origin"] == "http://127.0.0.1:49999"
    assert "x-request-hop" not in forwarded_headers
    entry = wait_until(harness.entries)[-1]
    assert entry["forward_count"] == 1
    assert entry["route"] == "/api/v1/runs"
    assert "private-" not in harness.log.read_text()
    assert harness.control.read_bytes() == original_control


def test_upstream_error_status_and_body_are_preserved(harness):
    status, _headers, body = harness.request(path="/upstream-error")
    assert status == 418
    assert body == harness.payload
    assert len(harness.requests) == 1


def test_chunked_upstream_response_preserves_payload_and_removes_only_framing(harness):
    status, headers, body = harness.request(path="/chunked-response")
    assert status == 202 and body == harness.payload
    assert "transfer-encoding" not in {key.lower() for key, _value in headers}
    assert dict(headers)["Content-Length"] == str(len(harness.payload))


@pytest.mark.parametrize("route", [
    "/api/v1/scene_authoring/templates",
    "/api/v1/scene_authoring/projection-template",
    "/api/v1/scene_renders/run-0123456789abcdef",
])
def test_exact_get_faults_do_not_forward_and_recover_on_control_change(harness, route):
    harness.configure({"get_503": [route]})
    status, _headers, body = harness.request(path=route + "?secret=must-not-log")
    assert status == 503
    assert json.loads(body) == {"error": "QA transport unavailable"}
    assert harness.requests == []
    # A fault for an exact GET must not accidentally block other methods/child resources.
    assert harness.request("POST", route, b"same route other method")[0] == 202
    assert harness.request(path=route + "/bundle")[0] == 202
    harness.configure({})
    assert harness.request(path=route)[0] == 202
    assert len(harness.requests) == 3
    entries = wait_until(lambda: len(harness.entries()) == 4 and harness.entries())
    assert entries[0]["forward_count"] == 0
    assert "must-not-log" not in harness.log.read_text()


@pytest.mark.parametrize("raw", [
    b"{", b"", b"null", b"[]", b'{"unsupported": true}',
    b'{"get_503": "/api/v1/scene_authoring/templates"}',
    b'{"get_503": ["/api/v1/runs"]}', b'{"get_503": [1]}',
    b'{"hold_post_runs": 1}', b'{"hold_post_runs": null}',
    b'{"hold_post_runs": true, "hold_post_runs": false}', b"\xff", b" " * 65537,
])
def test_malformed_control_fails_closed_without_upstream_side_effect(harness, raw):
    harness.control.write_bytes(raw)
    status, _headers, _body = harness.request("POST", "/api/v1/runs", b"private-body")
    assert status == 503
    assert harness.requests == []
    assert wait_until(harness.entries)[-1]["forward_count"] == 0


def test_missing_control_fails_closed(harness):
    harness.control.unlink()
    assert harness.request()[0] == 503
    assert harness.requests == []


def test_post_response_is_held_after_exactly_one_forward_and_released_unchanged(harness):
    harness.configure({"hold_post_runs": True})
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(harness.request, "POST", "/api/v1/runs", b"private-run-body")
        held_entry = wait_until(lambda: next(
            (entry for entry in harness.entries() if entry["status"] == "held"), None,
        ))
        assert held_entry["forward_count"] == 1
        assert len(harness.requests) == 1
        assert not pending.done()
        # Navigation/static/API requests are not blocked behind the delayed response.
        assert harness.request(path="/api/v1/workbench")[0] == 202
        assert not pending.done()
        harness.configure({"hold_post_runs": False})
        status, _headers, body = pending.result(timeout=3)
    assert status == 202
    assert body == harness.payload
    posts = [request for request in harness.requests if request["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["body"] == b"private-run-body"
    final = wait_until(lambda: next((
        entry for entry in harness.entries()
        if entry["method"] == "POST" and entry["status"] == 202
    ), None))
    assert final["held_ms"] > 0
    assert final["forward_count"] == 1
    assert "private-run-body" not in harness.log.read_text()


def test_response_hold_is_bounded_without_control_change(harness, proxy_module, monkeypatch):
    monkeypatch.setattr(proxy_module, "MAX_HOLD_SECONDS", 0.15)
    harness.configure({"hold_post_runs": True})
    started = time.monotonic()
    status, _headers, body = harness.request("POST", "/api/v1/runs", b"once")
    elapsed = time.monotonic() - started
    assert 0.15 <= elapsed < 2
    assert status == 202 and body == harness.payload
    assert len(harness.requests) == 1


def test_invalid_control_during_hold_fails_closed_without_repeating_post(harness):
    harness.configure({"hold_post_runs": True})
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(harness.request, "POST", "/api/v1/runs", b"once")
        wait_until(lambda: any(entry["status"] == "held" for entry in harness.entries()))
        harness.control.write_text("{")
        status, _headers, body = pending.result(timeout=3)
    assert status == 503
    assert body != harness.payload
    assert len(harness.requests) == 1
    entries = wait_until(lambda: len(harness.entries()) == 2 and harness.entries())
    assert entries[-1]["forward_count"] == 1


def test_downstream_disconnect_never_retries_side_effecting_request(harness):
    harness.configure({"hold_post_runs": True})
    connection = http.client.HTTPConnection("127.0.0.1", harness.proxy.server_port, timeout=3)
    connection.request("POST", "/api/v1/runs", body=b"once even if abandoned")
    wait_until(lambda: any(entry["status"] == "held" for entry in harness.entries()))
    connection.close()
    harness.configure({})
    wait_until(lambda: len(harness.entries()) == 2)
    assert len(harness.requests) == 1


def test_chunked_request_body_is_preserved_with_valid_forward_framing(harness):
    connection = http.client.HTTPConnection("127.0.0.1", harness.proxy.server_port, timeout=3)
    try:
        connection.request(
            "PATCH", "/api/v1/runs", body=iter([b"first\x00", b"second\xff"]),
            encode_chunked=True,
        )
        response = connection.getresponse()
        assert response.status == 202
        assert response.read() == harness.payload
    finally:
        connection.close()
    assert len(harness.requests) == 1
    assert harness.requests[0]["body"] == b"first\x00second\xff"
    headers = {key.lower(): value for key, value in harness.requests[0]["headers"]}
    assert "transfer-encoding" not in headers
    assert headers["content-length"] == "13"


def test_ambiguous_request_framing_is_not_forwarded(harness):
    status, _headers, _body = harness.request(
        "POST", "/api/v1/runs", body=b"0\r\n\r\n",
        headers={"Transfer-Encoding": "chunked", "Content-Length": "5"},
    )
    assert status == 400
    assert harness.requests == []


@pytest.mark.parametrize("target", [
    "http://example.test/api/v1/runs", "//example.test/api/v1/runs",
    "/api/v1/runs#fragment",
])
def test_proxy_never_follows_absolute_or_foreign_request_targets(harness, target):
    assert harness.request("POST", target, b"do not forward")[0] == 400
    assert harness.requests == []


def test_unavailable_upstream_is_not_retried(harness):
    harness.upstream.shutdown()
    harness.upstream.server_close()
    assert harness.request()[0] == 502
    entry = wait_until(harness.entries)[-1]
    assert entry["forward_count"] == 1
    assert harness.requests == []


def test_binding_is_loopback_and_log_uses_allowlisted_metadata_only(harness):
    assert harness.proxy.server_address[0] == "127.0.0.1"
    harness.request(path="/api/v1/runs/private-path-token?token=private-query-token")
    entry = wait_until(harness.entries)[-1]
    assert set(entry) == {"route", "method", "status", "elapsed_ms", "held_ms", "forward_count"}
    assert entry["route"] == "/api/v1/runs/{item}"
    assert "private-" not in harness.log.read_text()


def test_unsafe_configuration_is_rejected_before_binding(proxy_module, tmp_path):
    control = tmp_path / "control.json"
    control.write_text("{}")
    log = tmp_path / "log.jsonl"
    with pytest.raises(ValueError, match="differ"):
        proxy_module.FaultProxyServer(49999, 49999, control, log)
    with pytest.raises(ValueError, match="differ"):
        proxy_module.FaultProxyServer(0, 49999, control, control)
    with pytest.raises(ValueError, match="ports"):
        proxy_module.FaultProxyServer(0, 0, control, log)
    assert control.read_text() == "{}"
