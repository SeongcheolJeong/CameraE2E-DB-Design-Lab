"""Recreate the selected Sensor DB HDR/LFM mode-source audit.

This reads current report revisions and verifies every simulated result via
sensor_simulation_db_get. It does not inspect historical runs or external
vendor/measurement files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from pyisetcam.sensor_simulation_db import (
    sensor_simulation_db_get,
    sensor_simulation_db_path,
    sensor_simulation_db_search,
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def audit(root: Path) -> dict:
    db_path = sensor_simulation_db_path(root)
    rows = sensor_simulation_db_search(root=root)
    counts = Counter(row["status"] for row in rows)
    totals = Counter()
    proxy_rows = []
    for row in rows:
        if row["status"] != "simulated":
            continue
        saved = sensor_simulation_db_get(
            row["report_code"], root=root, run_id=row["run_id"]
        )
        readout = saved["metadata"]["readout_profile"]
        provenance = readout["provenance"]
        conditions = provenance.get("conditions") or {}
        classification = provenance["classification"]
        totals[f"readout_{classification}"] += 1
        for name, field in (
            ("exposure_condition_present", "exposure_s"),
            ("fps_condition_present", "readout_rate_fps"),
        ):
            totals[name] += conditions.get(field) is not None
        totals["conversion_mode_specific"] += conditions.get("conversion_mode") not in {
            None, "unspecified"
        }
        if classification == "camera_proxy_with_assumptions":
            proxy_rows.append({
                "report_code": row["report_code"],
                "run_id": row["run_id"],
                "result_sha256": row["result_sha256"],
                "result_bytes_sha256": row["result_sha256"],
                "classification": classification,
                "profile": readout["profile"],
                "operating_point": readout["operating_point"],
                "source_conditions": {
                    key: conditions.get(key) for key in (
                        "conversion_mode", "exposure_s", "readout_rate_fps",
                        "temperature_c",
                    )
                },
                "source_ids": sorted(provenance["sources"]),
            })
    try:
        relative_db_path = str(db_path.relative_to(Path.cwd()))
    except ValueError:
        relative_db_path = str(db_path)
    return {
        "schema": "camerae2e_hdr_lfm_sensor_mode_audit_v1",
        "scope": "current selected report per sensor in local sensor_simulations.sqlite; "
        "not all historical runs or external vendor modes",
        "db_path": relative_db_path,
        "db_sha256": _sha256(db_path),
        "counts": {
            "total_current_reports": len(rows),
            "status_simulated": counts["simulated"],
            "status_excluded": counts["excluded"],
            "readout_unmeasured_assumption": totals["readout_unmeasured_assumption"],
            "readout_camera_proxy_with_assumptions": totals[
                "readout_camera_proxy_with_assumptions"
            ],
            "exposure_condition_present": totals["exposure_condition_present"],
            "fps_condition_present": totals["fps_condition_present"],
            "conversion_mode_specific": totals["conversion_mode_specific"],
        },
        "camera_proxy_rows": sorted(proxy_rows, key=lambda row: row["report_code"]),
        "finding": "No selected simulated run has a recorded source exposure condition "
        "or readout FPS. Camera proxy records are not vendor HDR/LFM timing or "
        "physical ground truth. Source-hashed timing declarations used by "
        "separate Lens Sensor LFM jobs do not verify numeric timing values.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("camerae2e_db"))
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    result = audit(arguments.root.resolve())
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
