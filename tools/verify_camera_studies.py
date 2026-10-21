#!/usr/bin/env python3
"""Actual HTTP/worker study with authored model variants and no measurement imports."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def verify(base_url):
    report = {
        "schema": "camera_study_http_verification_v1",
        "checks": [],
        "base_url": base_url,
        "physical_validation": "not_established",
    }

    def passed(name):
        report["checks"].append({"name": name, "status": "passed"})

    with PlatformClient(base_url, timeout=120) as client:
        boot = client.get("bootstrap")
        specification = {
            "name": "노출 · Read noise 설계 검토",
            "question": "최저 이론 SNR 25 dB를 만족하면서 노출을 줄일 수 있는가?",
            "assembly_ids": [boot["demo"]["assembly_ids"][0]],
            "test_id": boot["demo"]["test_id"],
            "axes": [
                {"parameter": "exposure_s", "unit": "s", "values": [0.002, 0.004, 0.008]},
                {"parameter": "read_noise_e", "unit": "electron", "values": [1.0, 2.0, 5.0]},
            ],
            "objectives": [
                {"metric": "exposure_ms", "direction": "minimize"},
                {"metric": "min_theoretical_snr_db", "direction": "maximize"},
            ],
            "constraints": [
                {"metric": "min_theoretical_snr_db", "operator": ">=", "value": 25.0, "unit": "dB"},
                {
                    "metric": "min_saturation_margin_e",
                    "operator": ">=",
                    "value": 0.0,
                    "unit": "electron/pixel",
                },
            ],
        }
        plan = client.plan_study(specification)
        assert plan["run_count"] == 5
        assert client.get("bootstrap")["counts"] == boot["counts"]
        assert plan["environment"] == environment_fingerprint()
        passed("whole_plan_preflight_preserves_registry_and_pins_current_environment")
        study = client.submit_study(plan)
        analysis = client.wait_study(study["id"], timeout=120)
        assert analysis["coverage"]["succeeded"] == 5
        assert not analysis["pareto_provisional"]
        runs = [client.get("runs/" + r["run_id"]) for r in analysis["rows"]]
        assert all(r["environment"] == plan["environment"] for r in runs)
        assert all(
            digest(run["result"]) == row["result_hash"]
            for row, run in zip(analysis["rows"], runs, strict=True)
        )
        passed("five_actual_worker_runs_keep_exact_input_and_result_hashes")
        base = next(r for r in analysis["rows"] if r["is_baseline"])
        half = next(r for r in analysis["rows"] if r["changed"].get("exposure_s") == 0.002)
        assert (
            abs(
                half["metrics"]["mean_photoelectrons_e"] / base["metrics"]["mean_photoelectrons_e"]
                - 0.5
            )
            < 1e-12
        )
        assert half["feasibility"] == "infeasible" and half["pareto_status"] == "excluded"
        assert len(analysis["sensitivities"]) == 4
        assert analysis["total_uncertainty"] == {"kind": "unknown"}
        passed("charge_scaling_constraints_pareto_and_secants_preserve_unknown_accuracy")
        count = client.get("bootstrap")["counts"]["runs"]
        assert client.submit_study(plan)["id"] == study["id"]
        assert client.get("bootstrap")["counts"]["runs"] == count
        passed("resubmission_reuses_same_study_without_duplicate_runs")
        final = client.get("bootstrap")
        assert all(final["counts"][k] == boot["counts"][k] for k in ["captures", "measurements"])
        audit = client.get("audit")
        assert audit["integrity"] == "verified"
        passed("original_measurement_counts_and_audit_integrity_preserved")
        report.update(
            status="passed",
            verified_at=datetime.now(UTC).isoformat(),
            environment=plan["environment"],
            study_id=study["id"],
            plan=plan,
            analysis=analysis,
            counts=final["counts"],
            audit_head=audit["head_hash"],
        )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/camera_platform/studies-http-verification.json"),
    )
    args = parser.parse_args()
    report = verify(args.base_url)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": len(report["checks"]),
                "study_id": report["study_id"],
            }
        )
    )
