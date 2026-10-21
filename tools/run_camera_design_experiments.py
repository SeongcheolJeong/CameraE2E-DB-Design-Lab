#!/usr/bin/env python3
"""Open an explicit-write design workbench in a NEW/dedicated development state.

Does not clone, migrate, publish or upgrade the installed primary. Without
--serve, prints the launch configuration and creates no database or process.
"""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def configuration(label="default", port=8798):
    if not label or len(label) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in label):
        raise ValueError("State label: 1..64 ASCII letters/digits, dash or underscore")
    if port in (8770, 8796, 8797) or not 1024 <= port <= 65535:
        raise ValueError("Use a development port distinct from primary/replay/read-only viewers")
    state = ROOT / "outputs" / ("camera-platform-design-experiments-" + label)
    if state.is_symlink() or state.resolve().parent != (ROOT / "outputs").resolve():
        raise ValueError("Development state must not redirect to another location")
    return state, {"state_dir": str(state), "url": f"http://127.0.0.1:{port}/design-experiments.html",
                   "explicit_submission_required": True, "primary_modified": False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--state-label", default="default")
    parser.add_argument("--port", type=int, default=8798)
    args=parser.parse_args()
    state, details=configuration(args.state_label,args.port)
    print(json.dumps(details,ensure_ascii=False),flush=True)
    if args.serve:
        from camera_platform.api import create_app
        import uvicorn
        uvicorn.run(create_app(state_dir=state,workspace=ROOT,start_worker=True,seed_demo=False),
                    host="127.0.0.1",port=args.port)


if __name__ == "__main__":
    main()
