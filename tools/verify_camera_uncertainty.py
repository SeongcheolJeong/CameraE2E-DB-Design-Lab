#!/usr/bin/env python3
"""Actual HTTP/Worker input-bound propagation; all data are authored simulation fixtures."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tempfile
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from camera_platform.contracts import AssemblyCreate, ModelCreate, TestCreate
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def fields(record, contract):
    return {k: deepcopy(v) for k, v in record.items() if k in contract.model_fields}


def definition(value, unit):
    return {
        "kind": "absolute_bound",
        "unit": unit,
        "value": value,
        "source_kind": "estimated",
        "source_uri": "fixture://uncertainty-http",
        "method_and_version": "authored-parameter-bounds/1",
    }


def verify(base_url, *, include_capture=True):
    stamp = datetime.now(UTC).strftime("uq-%Y%m%dT%H%M%S%fZ")
    report = {
        "schema": "camera_uncertainty_http_verification_v1",
        "base_url": base_url,
        "environment": environment_fingerprint(),
        "checks": [],
        "physical_validation": "not_established",
    }

    def passed(name):
        report["checks"].append({"name": name, "status": "passed"})

    with PlatformClient(base_url, timeout=120) as client:
        boot = client.get("bootstrap")
        original = client.get("assemblies/" + boot["demo"]["assembly_ids"][0])
        baseline_test = client.get("tests/" + boot["demo"]["test_id"])
        original_models = [client.get("models/" + i) for i in original["model_ids"]]
        selected = []
        for model in original_models:
            if model["kind"] == "isp":
                selected.append(model)
                continue
            payload = fields(model, ModelCreate)
            payload.update(
                name=model["name"] + " · 입력 경계 선언", revision=stamp, parents=[model["id"]]
            )
            if model["kind"] == "sensor":
                response = np.zeros_like(
                    model["payload"]["sensor_angular_response"]["relative_response"], dtype=float
                )
                response[..., 1:] = 0.01
                payload["parameter_uncertainty"] = {
                    "qe": definition(0.005, "electron/photon"),
                    "sensor_angular_response.relative_response": definition(response.tolist(), "1"),
                }
            else:
                ri = np.full_like(model["payload"]["relative_illumination"], 0.01, dtype=float)
                ri[..., 0] = 0
                payload["parameter_uncertainty"] = {
                    "relative_illumination": definition(ri.tolist(), "1")
                }
            selected.append(client.post("models", payload))
        specification = fields(baseline_test, TestCreate)
        specification.update(name="입력 경계 전파 · 합성 평탄장", revision=stamp)
        specification["parameter_uncertainty"] = {
            "conditions.exposure_s.value": definition(0.0001, "s"),
            "stimulus.spectral_irradiance_w_m2_nm": definition(
                float(min(specification["stimulus"]["spectral_irradiance_w_m2_nm"])) * 0.03,
                "W/m^2/nm",
            ),
        }
        test = client.post("tests", specification)
        payload = fields(original, AssemblyCreate)
        payload.update(
            name="입력 경계 전파 · Lens/Sensor",
            revision=stamp,
            model_ids=[m["id"] for m in selected],
        )
        assembly = client.post("assemblies", payload)
        passed("bounded_model_and_test_revisions_registered_with_original_parents")
        baseline = client.wait(client.submit(original["id"], baseline_test["id"])["id"])
        run = client.wait(client.submit(assembly["id"], test["id"])["id"])
        assert run["status"] == baseline["status"] == "succeeded"
        assert run["environment"] == report["environment"]
        budget = run["result"]["prediction"]["uncertainty_budget"]
        assert len(budget["propagated_paths"]) == 5 and budget["status"] == "partial"
        values = np.asarray(run["result"]["summary"]["channel_ratios"])
        ranges = budget["conditional_intervals"]["channel_ratios"]
        assert (np.asarray(ranges["lower"]) < values).all() and (
            np.asarray(ranges["upper"]) > values
        ).all()
        assert budget["total_uncertainty"] == {"kind": "unknown"}
        passed("real_worker_propagates_five_declared_inputs_and_preserves_unknown_contributions")

        def download(record, name):
            artifact = next(a for a in record["artifacts"] if a["name"] == name)
            result = client.http.get(artifact["url"])
            result.raise_for_status()
            assert hashlib.sha256(result.content).hexdigest() == artifact["sha256"]
            return result.content

        raw = download(run, "raw_frames.npy")
        assert raw == download(baseline, "raw_frames.npy")
        assert download(run, "rgb.npy") == download(baseline, "rgb.npy")
        assert client.submit(assembly["id"], test["id"])["id"] == run["id"]
        repeated = client.wait(client.submit(assembly["id"], test["id"], force=True)["id"])
        assert repeated["status"] == "succeeded" and download(repeated, "raw_frames.npy") == raw
        assert repeated["result"]["prediction"]["uncertainty_budget"] == budget
        passed("nominal_raw_rgb_unchanged_and_seeded_reexecution_reproduces_same_budget")
        source = budget["sources"]["qe"]
        saved_model = client.get("models/" + source["source_record_id"])
        assert saved_model["content_hash"] == source["source_content_hash"]
        before = client.get("bootstrap")["counts"]["models"]
        malformed = fields(saved_model, ModelCreate)
        malformed["parameter_uncertainty"]["qe"]["kind"] = "standard_deviation"
        refused = client.http.post("/api/v1/models", json=malformed)
        assert refused.status_code == 422
        assert client.get("bootstrap")["counts"]["models"] == before
        passed("source_hashes_match_registered_revisions_and_sigma_is_refused_before_write")
        if not include_capture:
            report.update(
                status="passed",
                verified_at=datetime.now(UTC).isoformat(),
                records={"assembly": assembly["id"], "test": test["id"], "run": run["id"]},
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                budget=budget,
                scope="Model/test/run fixtures only; no capture or measurement imports",
            )
            return report
        existing = next(
            (
                c
                for c in client.get("captures")
                if any(
                    a["name"] == "raw-capture.npy"
                    and a["sha256"] == hashlib.sha256(raw).hexdigest()
                    for a in c["artifacts"]
                )
            ),
            None,
        )
        if existing:
            assert existing["source_kind"] == "simulated" and existing["source_uri"].startswith(
                "fixture://uncertainty-raw/"
            )
            imported = {
                "capture": existing,
                "test": client.get("tests/" + existing["test_id"]),
                "measurement": next(
                    m
                    for m in client.get("measurements")
                    if m.get("raw_capture_id") == existing["id"]
                ),
            }
            report["reused_original_capture"] = existing["id"]
        else:
            with tempfile.TemporaryDirectory(prefix="camera-uq-capture-") as temporary:
                path = Path(temporary) / "raw.npy"
                path.write_bytes(raw)
                metadata = client.get("templates/capture")
                metadata.update(
                    name="합성 RAW · UQ 관측 연결",
                    revision=stamp,
                    source_kind="simulated",
                    source_uri="fixture://uncertainty-raw/" + run["id"],
                    assembly_id=assembly["id"],
                    test_id=test["id"],
                    dataset_ids=[stamp],
                )
                imported = client.import_capture(metadata, path)
        observed = client.wait(
            client.submit(imported["capture"]["assembly_id"], imported["test"]["id"])["id"]
        )
        assert observed["status"] == "succeeded", observed.get("error")
        prediction = observed["result"]["prediction"]
        roi_budget = prediction["uncertainty_budget"]
        assert roi_budget["axes"] == prediction["axes"]
        means = np.asarray(prediction["channel_means_dn"])
        bounds = roi_budget["conditional_intervals"]["channel_means_dn"]
        assert (np.asarray(bounds["lower"]) < means).all() and (
            np.asarray(bounds["upper"]) > means
        ).all()
        comparison = client.compare(
            observed["id"], imported["measurement"]["id"], imported["test"]["id"]
        )
        assert comparison["status"] == "INCONCLUSIVE", comparison
        assert any(i["code"] == "incomplete_uncertainty_budget" for i in comparison["issues"])
        passed("raw_import_uses_same_roi_operator_and_unknown_total_keeps_comparison_inconclusive")
        response = client.http.get("/api/v1/runs/" + observed["id"] + "/bundle")
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            artifact = next(a for a in observed["artifacts"] if a["name"] == "summary.json")
            summary = json.loads(archive.read("artifacts/" + artifact["sha256"] + "/summary.json"))
            assert summary["prediction"]["uncertainty_budget"] == roi_budget
        passed("downloaded_evidence_preserves_exact_input_sources_and_roi_budget")
        report.update(
            status="passed",
            verified_at=datetime.now(UTC).isoformat(),
            records={
                "assembly": assembly["id"],
                "test": test["id"],
                "run": run["id"],
                "observed": observed["id"],
                "validation": comparison["id"],
            },
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            budget=budget,
            roi_budget=roi_budget,
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--run-only", action="store_true", help="Preserve an empty measurement registry"
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/camera_platform/uncertainty-http-verification.json"),
    )
    args = parser.parse_args()
    report = verify(args.base_url, include_capture=not args.run_only)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": len(report["checks"]),
                "report": str(args.report),
            }
        )
    )


if __name__ == "__main__":
    main()
