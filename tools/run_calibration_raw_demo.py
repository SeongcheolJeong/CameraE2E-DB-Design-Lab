"""Complete an image-based synthetic calibration and export its standard report.

Every observation is extracted by the normal NPY importer. Hidden truth is never
submitted to the fitter. The separate held-out acquisition is evaluated once.
Only this new synthetic campaign receives the saved active candidate profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
import zipfile
from copy import deepcopy
from pathlib import Path

import numpy as np

from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient


def recovery_evaluation(candidate, manifest):
    """Evaluator-only comparison after fitting; never sent to the calibration API."""
    truth = manifest["truth_profile"]

    def lookup(profile, path):
        value = profile
        for part in path.split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
        return np.asarray(value, dtype=float)

    checks = []
    for parameter, tolerance in manifest["recovery_tolerances"].items():
        expected, actual = lookup(truth, parameter), lookup(candidate, parameter)
        if parameter == "raw_response.tone_output":
            expected = np.interp(
                candidate["raw_response"]["tone_input"],
                truth["raw_response"]["tone_input"],
                expected,
            )
        error = float(np.max(np.abs(actual - expected)))
        limit = tolerance.get("absolute", tolerance.get("max_absolute"))
        if limit is None:
            limit = tolerance["relative"] * float(np.max(np.abs(expected)))
        checks.append(
            {
                "parameter": parameter,
                "truth": expected.tolist(),
                "candidate": actual.tolist(),
                "max_absolute_error": error,
                "allowed_absolute_error": limit,
                "unit": tolerance["unit"],
                "status": "pass" if error <= limit else "fail",
            }
        )
    return {
        "schema": "camera_synthetic_parameter_recovery_v1",
        "checks": checks,
        "status": "pass" if all(c["status"] == "pass" for c in checks) else "fail",
        "scope": "Evaluator-only same-model synthetic recovery; not physical validation",
    }


def run(base_url, output, *, seed=None, dataset=None):
    from camera_platform.calibration_synthetic_raw import generate_dataset

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    witness_path = output / "execution.json"
    if witness_path.exists():
        raise ValueError(
            "Output contains a run: preserve its IDs. A new acquisition needs a new seed."
        )
    if dataset is None and seed is None:
        seed = secrets.randbelow(2**32)
    manifest = (
        json.loads(Path(dataset).read_text())
        if dataset
        else generate_dataset(output / "inputs", seed=seed)
    )
    if seed is not None and seed != manifest["seed"]:
        raise ValueError("Execution seed differs from the frozen dataset seed")
    seed = manifest["seed"]
    witness = {
        "schema": "camera_calibration_raw_completion_execution_v1",
        "base_url": base_url,
        "started_at_unix": time.time(),
        "dataset_manifest_sha256": digest(manifest),
        "dataset_seed": manifest["seed"],
        "evidence_kind": "synthetic_image_implementation_verification",
        "physical_validation": "not_established",
        "status": "preparing",
    }

    def checkpoint(status, **values):
        witness.update(status=status, **values)
        witness_path.write_text(json.dumps(witness, ensure_ascii=False, indent=2) + "\n")
        print(status, flush=True, file=sys.stderr)

    checkpoint("dataset_frozen")
    try:
        with PlatformClient(base_url, timeout=180) as client:
            campaign = client.create_calibration_campaign(manifest["campaign"])
            checkpoint("campaign_created", campaign_id=campaign["id"])
            measurements = []
            for entry in manifest["entries"]:
                metadata = deepcopy(entry["metadata"])
                metadata["campaign_id"] = campaign["id"]
                if metadata.get("observations") or metadata["source_kind"] != "synthetic":
                    raise ValueError("Synthetic image inputs require empty observations")
                path = Path(entry["path"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                    raise ValueError("Generated image changed before import")
                measurement = client.import_calibration_measurement(metadata, path)
                if measurement["import_status"] != "ready":
                    raise ValueError(f"Incomplete RAW observation: {measurement}")
                measurements.append(measurement)
            checkpoint("raw_images_imported", measurement_ids=[m["id"] for m in measurements])
            study = client.submit_calibration_study(
                {
                    "campaign_id": campaign["id"],
                    "base_profile_id": "baseline",
                    "measurement_ids": [m["id"] for m in measurements if m["role"] == "training"],
                    "seed": seed,
                    "max_iterations": 160,
                    "name": "합성 RAW 이미지 · 파라미터 추정 및 재시뮬레이션",
                }
            )
            checkpoint("fitting", study_id=study["id"])
            fitted = client.wait(study["id"], timeout=1860, expected_kind="calibration_lab")
            if fitted["status"] != "succeeded":
                raise RuntimeError(fitted.get("error", fitted["status"]))
            candidate = fitted["result"]["summary"]["candidate_profile"]
            checkpoint("candidate_frozen", candidate_sha256=digest(candidate))
            validation = client.validate_calibration_study(
                study["id"], [m["id"] for m in measurements if m["role"] == "final_validation"]
            )
            checkpoint("independent_validation", validation_id=validation["id"])
            checked = client.wait(
                validation["id"], timeout=1860, expected_kind="calibration_lab_validation"
            )
            if checked["status"] != "succeeded":
                raise RuntimeError(checked.get("error", checked["status"]))
            final = checked["result"]["summary"]
            if final["candidate_sha256"] != digest(candidate):
                raise AssertionError("Independent validation changed the frozen candidate")
            checkpoint("validation_evaluated", validation=final["validation"])
            if not final["validation"]["numerical_tolerance_pass"]:
                raise RuntimeError("Held-out validation did not pass; candidate was not applied")
            recovery = recovery_evaluation(candidate, manifest)
            (output / "synthetic-recovery.json").write_text(
                json.dumps(recovery, ensure_ascii=False, indent=2) + "\n"
            )
            checkpoint("truth_recovery_evaluated", synthetic_recovery=recovery)
            if recovery["status"] != "pass":
                raise RuntimeError("Synthetic truth recovery exceeded the frozen tolerance")
            selected = client.apply_calibration_study(study["id"], campaign["id"])
            profile = selected["profile"]
            checkpoint("final_profile_saved", profile_id=profile["id"])
            report = client.create_calibration_completion_report(
                study["id"], validation["id"], profile["id"]
            )
            exported = {}
            for filename, checksum in report["files"].items():
                route = (
                    f"/api/v1/calibration-lab/completion-reports/{report['id']}/files/{filename}"
                )
                response = client.http.get(route)
                response.raise_for_status()
                if hashlib.sha256(response.content).hexdigest() != checksum:
                    raise AssertionError("Report export hash mismatch")
                (output / filename).write_bytes(response.content)
                exported[filename] = checksum
            response = client.http.get(report["bundle_url"])
            response.raise_for_status()
            package = output / "calibration-evidence.zip"
            package.write_bytes(response.content)
            with zipfile.ZipFile(package) as archive:
                assert archive.testzip() is None
                for filename, checksum in exported.items():
                    assert hashlib.sha256(archive.read(filename)).hexdigest() == checksum
                originals = [
                    n
                    for n in archive.namelist()
                    if n.endswith(".npy") and n.startswith("originals/")
                ]
                assert len(originals) == len(manifest["entries"])
            readback = client.calibration_campaign(campaign["id"])
            assert readback["active_profile"]["id"] == profile["id"]
            assert any(r["id"] == report["id"] for r in readback["completion_reports"])
            (output / "truth-profile.json").write_text(
                json.dumps(manifest["truth_profile"], indent=2, ensure_ascii=False) + "\n"
            )
            checkpoint(
                "completed",
                completed_at_unix=time.time(),
                report_id=report["id"],
                report_url=base_url.rstrip("/") + report["report_url"],
                lab_url=base_url.rstrip("/")
                + "/calibration-lab.html?campaign="
                + campaign["id"]
                + "&study="
                + study["id"],
                exported_files=exported,
                package_sha256=hashlib.sha256(response.content).hexdigest(),
                raw_original_count=len(originals),
                parameter_changes=fitted["result"]["summary"]["parameters"],
                fit_diagnostics=fitted["result"]["summary"]["fit"]["diagnostics"],
                active_profile_after_completion=profile["id"],
            )
            return witness
    except Exception as exc:
        checkpoint("failed", error=str(exc))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--seed", type=int, help="Fresh random seed by default; never reuse holdout"
    )
    parser.add_argument("--dataset", type=Path, help="Existing frozen generator manifest.json")
    args = parser.parse_args()
    result = run(args.base_url, args.output, seed=args.seed, dataset=args.dataset)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "status",
                    "campaign_id",
                    "study_id",
                    "validation_id",
                    "profile_id",
                    "report_url",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
