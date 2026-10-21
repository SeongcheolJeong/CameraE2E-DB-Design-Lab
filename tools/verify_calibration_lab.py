"""Exercise a running local Calibration Lab with synthetic, explicitly labeled evidence.

Creates its own campaign; never activates a profile in an existing camera campaign.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

from camera_platform.sdk import PlatformClient


def verify(base_url, output, *, source_run=None, candidate=None):
    if bool(source_run) != bool(candidate):
        raise ValueError("source_run and candidate must be provided together")
    with PlatformClient(base_url, timeout=120) as client:
        source_context = None
        if source_run:
            from camera_platform.calibration_lab_engine import synthetic_fixture

            response = client.http.get(
                "/api/v1/calibration-lab/template",
                params={
                    "source_run": source_run,
                    "candidate": candidate,
                },
            )
            response.raise_for_status()
            source_context = response.json()["source_context"]
            generated = synthetic_fixture()
            campaign = client.create_calibration_campaign(
                {
                    **generated["campaign"],
                    "name": "DB 연결 · 합성 보정 흐름 검증",
                    "source": {"run_id": source_run, "candidate_id": candidate},
                }
            )
            assert campaign["source_context"] == source_context
            measurements = [
                client.import_calibration_measurement(
                    {
                        **{k: v for k, v in item.items() if k != "id"},
                        "campaign_id": campaign["id"],
                    }
                )
                for item in generated["measurements"]
            ]
            study = client.submit_calibration_study(
                {
                    "campaign_id": campaign["id"],
                    "measurement_ids": [
                        m["id"] for m in measurements if m["role"] != "final_validation"
                    ],
                    "base_profile_id": "baseline",
                    "seed": 42,
                    "max_iterations": 100,
                    "name": "DB 연결 · 8종 차트 보정 구현 검증",
                }
            )
            fixture = {"campaign": campaign, "measurements": measurements, "study": study}
        else:
            fixture = client.calibration_demo()
        campaign_id = fixture["campaign"]["id"]
        identity = fixture["study"]["id"]
        run = client.wait(identity, timeout=1860, expected_kind="calibration_lab")
        if run["status"] != "succeeded":
            raise AssertionError(run.get("error", run["status"]))
        summary = run["result"]["summary"]
        expected_charts = {"dark", "ptc", "shading", "mtf", "geometry", "color", "tone", "texture"}
        assert {r["chart"] for r in summary["comparisons"]} == expected_charts
        assert summary["fresh_simulation"] is True
        assert summary["physical_validation"] == "not_established"
        assert summary["evidence_kind"] == "implementation_verification"
        assert any(name.endswith("raw.npy") for name in summary["files"])
        checked = {}
        for relative, expected in summary["files"].items():
            response = client.http.get(summary["files_base"] + relative)
            response.raise_for_status()
            checksum = hashlib.sha256(response.content).hexdigest()
            assert checksum == expected, relative
            checked[relative] = checksum
        bundle = client.http.get(f"/api/v1/calibration-lab/studies/{identity}/bundle")
        bundle.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            assert json.loads(archive.read("study.json"))["id"] == identity
            for relative, checksum in checked.items():
                assert hashlib.sha256(archive.read("results/" + relative)).hexdigest() == checksum
            assert any(name.startswith("originals/") for name in archive.namelist())
        final_ids = [m["id"] for m in fixture["measurements"] if m["role"] == "final_validation"]
        validation = client.validate_calibration_study(identity, final_ids)
        final = client.wait(
            validation["id"], timeout=1860, expected_kind="calibration_lab_validation"
        )
        assert final["status"] == "succeeded", final.get("error")
        final_summary = final["result"]["summary"]
        assert final_summary["candidate_sha256"] == summary["candidate_sha256"]
        assert final_summary["validation"]["independent"] is True
        selected = client.apply_calibration_study(identity, campaign_id)
        profile_id = selected["profile"]["id"]
        try:
            assert client.calibration_campaign(campaign_id)["active_profile"]["id"] == profile_id
        finally:
            client.activate_calibration_profile(campaign_id, None)
        readback = client.calibration_campaign(campaign_id)
        assert readback["active_profile"] is None
        if source_context:
            assert readback["campaign"]["source_context"] == source_context
            source = client.get("reference-workshop/runs/" + source_run)
            assert source["content_hash"] == source_context["run_request_sha256"]
        report = {
            "schema": "camera_calibration_live_verification_v1",
            "verified_at_unix": time.time(),
            "base_url": base_url,
            "db_source_context": source_context,
            "evidence_kind": "synthetic_implementation_verification",
            "campaign_id": campaign_id,
            "study_id": identity,
            "final_validation_study_id": final["id"],
            "applied_profile_id": profile_id,
            "active_profile_after_verification": None,
            "source_code": summary["source_code"],
            "candidate_sha256": summary["candidate_sha256"],
            "charts": sorted(expected_charts),
            "verified_files": checked,
            "zip_sha256": hashlib.sha256(bundle.content).hexdigest(),
            "parameter_changes": summary["parameters"],
            "validation": final_summary["validation"],
            "url": f"{base_url}/calibration-lab.html?campaign={campaign_id}&study={identity}",
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        output.with_suffix(".zip").write_bytes(bundle.content)
        return {
            "report": str(output.resolve()),
            "url": report["url"],
            "study_id": identity,
            "final_validation_id": final["id"],
            "verified_file_count": len(checked),
            "physical_validation": "not_established",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-run", help="Completed DB Design Lab run to bind as provenance")
    parser.add_argument("--candidate", help="Exact candidate ID within --source-run")
    args = parser.parse_args()
    print(
        json.dumps(
            verify(
                args.base_url.rstrip("/"),
                args.output,
                source_run=args.source_run,
                candidate=args.candidate,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
