"""Read-only evidence equivalence and paired local transport measurements."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

from camera_platform.registry import digest

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:8771")
parser.add_argument("--report", default="reports/camera_platform/workbench-http-verification.json")
args = parser.parse_args()
base = args.base_url.rstrip("/")
report = {
    "schema": "camera_workbench_transport_verification_v1",
    "base_url": base,
    "read_only": True,
    "checks": [],
    "timing_scope": "7 paired local GETs; full JSON "
    "read and parse, no parallel verifier, not a sustained-load p95 guarantee",
}


def get(path):
    started = time.perf_counter()
    with urlopen(base + "/api/v1/" + path, timeout=30) as response:
        raw = response.read()
    value = json.loads(raw)
    return value, {"bytes": len(raw), "seconds": time.perf_counter() - started}


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    report["checks"].append({"name": name, "status": "passed"})


full, _ = get("bootstrap")
index, _ = get("workbench")
report["version"], report["counts"] = index["version"], index["counts"]
check(
    "Identity and counts match full API",
    all(index[k] == full[k] for k in ("schema", "version", "workspace", "state_dir", "counts")),
)
check("Explicit projection contract", index["workbench_schema"] == "camera_workbench_v1")
verified = 0
for collection in index["projected_collections"]:
    originals = {r["id"]: r for r in full[collection]}
    check(
        collection + " preserves every identifier",
        set(originals) == {r["id"] for r in index[collection]},
    )
    for item in index[collection]:
        ref = item["_projection"]
        check_hash = ref["record_sha256"] == digest(originals[item["id"]])
        if not check_hash:
            raise AssertionError("Projection full-record hash mismatch: " + item["id"])
        wrapped, _ = get(ref["detail_path"].lstrip("/"))
        if (
            wrapped["record"] != originals[item["id"]]
            or wrapped["record_sha256"] != ref["record_sha256"]
        ):
            raise AssertionError("Detail evidence changed: " + item["id"])
        verified += 1
report["full_records_compared"] = verified
check("All projected details preserve exact original evidence", verified > 0)
session, session_timing = get("session")
check(
    "Session identity/token match without full history",
    all(session[k] == full[k] for k in ("schema", "version", "workspace", "state_dir", "token"))
    and session_timing["bytes"] < 1024,
)
report["measurements"] = {"bootstrap": [], "workbench": [], "session": []}
for i in range(7):
    for endpoint in ("bootstrap", "workbench") if i % 2 == 0 else ("workbench", "bootstrap"):
        _, timing = get(endpoint)
        report["measurements"][endpoint].append(timing)
    _, timing = get("session")
    report["measurements"]["session"].append(timing)
report["summary"] = {
    k: {
        "bytes": v[0]["bytes"],
        "median_seconds": statistics.median(x["seconds"] for x in v),
        "min_seconds": min(x["seconds"] for x in v),
        "max_seconds": max(x["seconds"] for x in v),
    }
    for k, v in report["measurements"].items()
}
report["payload_reduction_percent"] = 100 * (
    1 - report["summary"]["workbench"]["bytes"] / report["summary"]["bootstrap"]["bytes"]
)
check(
    "History payload reduced by at least 80 percent on fixture",
    report["payload_reduction_percent"] >= 80,
)
after, _ = get("workbench")
check("Verification created no records or jobs", after["counts"] == index["counts"])
report["status"] = "passed"
report["verified_at"] = datetime.now(UTC).isoformat()
Path(args.report).parent.mkdir(parents=True, exist_ok=True)
Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(
    json.dumps(
        {
            k: report[k]
            for k in ("status", "full_records_compared", "payload_reduction_percent", "summary")
        }
    )
)
