#!/usr/bin/env python3
"""Actual HTTP/SDK/Worker verification of reusable analysis over preserved QA RAW."""

import argparse
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from camera_platform.sdk import PlatformClient


def verify(base_url, source_report, output):
    prior = json.loads(Path(source_report).read_text())
    report = {
        "schema": "camera_capture_analysis_http_verification_v1",
        "verified_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "state_dir": prior["state_dir"],
        "checks": [],
        "scope": "Preserved synthetic QA acquisition; no independent physical validation",
    }

    def passed(name):
        report["checks"].append({"name": name, "status": "passed"})

    with PlatformClient(base_url) as client:
        health = client.get("health")
        expected = Path(prior["state_dir"]).resolve()
        if Path(health["state_dir"]).resolve() != expected or expected.name == "camera-platform":
            raise ValueError("Requires the explicit isolated QA state; main cannot be mutated")
        original = client.get("captures/" + prior["records"]["capture"])
        if original["source_kind"] != "simulated":
            raise ValueError("Verifier only uses preserved synthetic QA acquisitions")
        before = client.get("bootstrap")["counts"]
        program_id = prior["records"]["program"]
        results = []
        for observable in ("green_mean_normalized", "exposure_ns", "ae_command_latency_ns"):
            draft = client.response_test_template(program_id, observable=observable)
            draft["name"] = "Acquisition reuse QA · " + observable
            test = client.post("tests", draft)
            request = {
                "capture_id": original["id"],
                "test_id": test["id"],
                "name": "Same preserved RAW · " + observable,
                "uncertainty": {
                    "kind": "unknown",
                    "unit": test["unit"],
                    "reason": "Total observation bound remains unestablished",
                },
                "parent_analysis_id": results[-1]["analysis"]["id"] if results else None,
            }
            observed = client.analyze_capture(**request)
            repeated = client.analyze_capture(**request)
            assert observed == repeated
            assert observed["capture"] == original
            assert observed["measurement"]["dataset_ids"] == original["dataset_ids"]
            assert observed["analysis"]["independence"]["adds_independent_acquisition"] is False
            queued = client.submit(
                original["assembly_id"],
                test["id"],
                plugin_id="closed_loop_scene_v1",
                control_program_id=program_id,
                parameters={"seed": 123},
            )
            run = client.wait(queued["id"], timeout=60, poll_interval=0.1)
            assert run["status"] == "succeeded", run.get("error")
            comparison = client.compare(run["id"], observed["measurement"]["id"], test["id"])
            assert comparison["status"] == "INCONCLUSIVE"
            assert not comparison["physical_validation_pass"]
            assert all(v == 0 for v in comparison["residuals"]["values"])
            results.append({**observed, "run": run, "validation": comparison})
            passed("preserved_raw_" + observable + "_new_test_worker_comparison_and_idempotence")
        assert client.get("captures/" + original["id"]) == original
        after = client.get("bootstrap")["counts"]
        assert after["captures"] == before["captures"]
        assert after["capture_analyses"] == before["capture_analyses"] + 3
        assert after["measurements"] == before["measurements"] + 3
        passed("one_acquisition_three_analysis_revisions_original_record_unchanged")
        release = client.post(
            "releases",
            {
                "name": "Shared acquisition response evidence QA",
                "run_ids": [r["run"]["id"] for r in results],
                "validation_ids": [r["validation"]["id"] for r in results],
            },
        )
        assert release["status"] == "blocked"
        response = client.http.get("/api/v1/releases/" + release["id"] + "/bundle")
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
            manifest = json.loads(bundle.read("manifest.json"))
            assert manifest["captures"] == [original]
            assert len(manifest["capture_analyses"]) == 3
            for record in [*manifest["captures"], *manifest["capture_analyses"]]:
                for artifact in record["artifacts"]:
                    assert bundle.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
        passed("bundle_preserves_one_original_all_analysis_revisions_and_blocked_release")
        report.update(
            status="passed",
            counts_before=before,
            counts_after=client.get("bootstrap")["counts"],
            original_capture_hash=original["content_hash"],
            original_capture_schema=original.get("capture_schema", "legacy_coupled_capture"),
            records={
                "capture": original["id"],
                "program": program_id,
                "release": release["id"],
                "analyses": [r["analysis"]["id"] for r in results],
                "tests": [r["test"]["id"] for r in results],
                "measurements": [r["measurement"]["id"] for r in results],
                "runs": [r["run"]["id"] for r in results],
            },
        )
    Path(output).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {"status": report["status"], "checks": len(report["checks"]), "report": str(output)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    verify(args.base_url, args.source_report, args.output)
