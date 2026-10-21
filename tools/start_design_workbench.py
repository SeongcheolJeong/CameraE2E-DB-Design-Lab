#!/usr/bin/env python3
"""Start or reopen the local workbench independently of the invoking terminal."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

WORKSPACE = Path(__file__).resolve().parents[1]
STATE_DIR = WORKSPACE / "outputs/design-workbench"


def is_ready(url: str) -> bool:
    """Only reuse this workspace's service, never another application on the port."""
    try:
        with build_opener(ProxyHandler({})).open(url + "api/bootstrap", timeout=2) as response:
            data = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"{url} 주소에서 다른 서비스가 응답했습니다 (HTTP {exc.code}).") from exc
    except (URLError, TimeoutError, ConnectionError):
        return False
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError(f"{url} 주소가 작업실 응답을 반환하지 않았습니다.") from exc
    if (
        not isinstance(data, dict)
        or data.get("schema") != "camerae2e_design_workbench_v1"
        or data.get("server") != {"workspace": str(WORKSPACE), "state_dir": str(STATE_DIR)}
        or not isinstance(data.get("studies"), list)
    ):
        raise RuntimeError(f"{url} 주소를 다른 서비스 또는 다른 저장소가 사용 중입니다.")
    return True


def start(port: int) -> str:
    url = f"http://127.0.0.1:{port}/"
    if is_ready(url):
        return url
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / "server.log"
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(WORKSPACE / "tools/run_design_workbench.py"),
                "--port",
                str(port),
            ],
            cwd=WORKSPACE,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if is_ready(url):
            print(f"서버가 준비되었습니다. 로그: {log_path}", flush=True)
            return url
        if process.poll() is not None:
            detail = log_path.read_text(errors="replace")[-2000:]
            raise RuntimeError(f"서버를 시작하지 못했습니다.\n{detail}\n로그: {log_path}")
        time.sleep(0.25)
    raise RuntimeError(f"서버 준비가 지연되고 있습니다. 잠시 후 다시 실행하세요. 로그: {log_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--no-open", action="store_true", help="Start without opening a browser")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        url = start(args.port)
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"CameraE2E Design Workbench: {url}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
