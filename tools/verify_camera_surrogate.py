"""Verify actual HTTP/worker surrogate training, held-out errors and batch predictions."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import httpx
import numpy as np

from camera_platform.registry import digest
from camera_platform.runtime import run_plugin
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint
from camera_platform.surrogate_math import evaluate


def verify(base_url, report_path, *, primary=False):
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append({"name": name, "status": "passed"})

    report = {
        "schema": "camera_surrogate_http_verification_v1",
        "status": "running",
        "base_url": base_url,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with PlatformClient(base_url) as client:
            bootstrap = client.get("bootstrap")
            check(
                "Expected workspace identity",
                bootstrap["state_dir"].endswith(
                    "/camera-platform" if primary else "/camera-platform-surrogate-qa"
                ),
            )
            check("Managed worker running", client.get("health")["worker"] == "running")
            demo = bootstrap["demo"]
            study_ids = []
            for name, exposures, noise in [
                ("Surrogate 학습 · 16개 조합", [0.002, 0.004, 0.006, 0.008], [1.0, 2.0, 3.0, 5.0]),
                ("Surrogate 검증 · 별도 9개 조건", [0.003, 0.005, 0.007], [1.5, 2.5, 4.0]),
            ]:
                spec = {
                    "name": name,
                    "question": "노출와 Read noise에 따른 고정 카메라 수치 모델 응답",
                    "assembly_ids": [demo["assembly_ids"][0]],
                    "test_id": demo["test_id"],
                    "design": "cartesian",
                    "axes": [
                        {"parameter": "exposure_s", "unit": "s", "values": exposures},
                        {"parameter": "read_noise_e", "unit": "electron", "values": noise},
                    ],
                    "objectives": [{"metric": "min_theoretical_snr_db", "direction": "maximize"}],
                    "frames": 4,
                    "output_size": [32, 32],
                }
                submitted = client.submit_study(client.plan_study(spec))
                result = client.wait_study(submitted["id"], timeout=180)
                check(
                    name + " executes real worker references",
                    result["coverage"]["succeeded"] == submitted["run_count"],
                )
                study_ids.append(submitted["id"])
            training, validation = [client.get("studies/" + i) for i in study_ids]
            validation_ids = [
                identity
                for identity, candidate in zip(
                    validation["run_ids"], validation["candidates"], strict=True
                )
                if not candidate["is_baseline"]
            ]
            spec = {
                "name": "노출 · Read noise 응답 근사",
                "purpose": "고정 카메라의 반복 설계 계산을 수치 검증된 근사 모델로 평가",
                "training": [{"study_id": training["id"], "run_ids": training["run_ids"]}],
                "validation": [{"study_id": validation["id"], "run_ids": validation_ids}],
                "metrics": [
                    {"metric": "min_theoretical_snr_db", "unit": "dB", "max_absolute_error": 0.3},
                    {
                        "metric": "mean_photoelectrons_e",
                        "unit": "electron/pixel",
                        "max_absolute_error": 1e-6,
                    },
                ],
            }
            before = client.get("bootstrap")["counts"]
            plan = client.plan_surrogate(spec)
            check("Planning creates no jobs/revisions", client.get("bootstrap")["counts"] == before)
            submitted = client.submit_surrogate(plan)
            model = client.wait_surrogate(submitted["id"])
            check(
                "Worker learns coefficients and passes frozen holdout criteria",
                model["status"] == "succeeded" and model["result"]["prediction_eligible"],
            )
            check(
                "Repeated submission reuses exactly the same model job",
                client.submit_surrogate(plan)["id"] == model["id"],
            )
            train_coords = {
                json.dumps(r["coordinates"], sort_keys=True)
                for r in model["result"]["datasets"]["training"]
            }
            check(
                "Different baseline Run is explicitly excluded from holdout",
                len(validation_ids) == 9
                and all(
                    json.dumps(r["coordinates"], sort_keys=True) not in train_coords
                    for r in model["result"]["datasets"]["validation"]
                ),
            )
            points = [
                {"exposure_s": float(e), "read_noise_e": float(n)}
                for e in np.linspace(0.002, 0.008, 32)
                for n in np.linspace(1, 5, 32)
            ]
            units = {"exposure_s": "s", "read_noise_e": "electron"}
            started = time.perf_counter()
            prediction = client.predict_surrogate(model["id"], points, units)
            api_seconds = time.perf_counter() - started
            check(
                "1024 batch predictions are persisted with exact model result hash",
                len(prediction["rows"]) == 1024
                and prediction["model_result_hash"] == digest(model["result"])
                and client.get("surrogate_predictions/" + prediction["id"]) == prediction,
            )
            count = client.get("bootstrap")["counts"]["surrogate_predictions"]
            try:
                client.predict_surrogate(
                    model["id"], [{"exposure_s": 0.009, "read_noise_e": 3.0}], units
                )
            except httpx.HTTPStatusError as exc:
                check(
                    "Out-of-domain inference rejected before persistence",
                    exc.response.status_code == 422
                    and client.get("bootstrap")["counts"]["surrogate_predictions"] == count,
                )
            else:
                raise AssertionError("Out-of-domain request was accepted")
            check(
                "No physical-validation evidence is invented",
                model["result"]["physical_validation"] == "not_established"
                and prediction["total_uncertainty"] == {"kind": "unknown"}
                and client.get("bootstrap")["counts"]["measurements"]
                == bootstrap["counts"]["measurements"],
            )
            check(
                "Benchmark uses the same executable environment as training",
                environment_fingerprint() == plan["environment"],
            )
            references = [client.get("runs/" + identity) for identity in validation_ids]
            coordinates = [
                [r["coordinates"][a["parameter"]] for a in model["result"]["axes"]]
                for r in model["result"]["datasets"]["validation"]
            ]
            # These are explicit local compute timings. HTTP/source loading/persistence
            # are excluded from BOTH kernels; full inference API is reported separately.
            run_plugin(references[0]["plugin_id"], references[0]["request"])
            evaluate(model["result"]["coefficients"], coordinates)
            direct, approximate = [], []
            for _ in range(7):
                started = time.perf_counter()
                for run in references:
                    run_plugin(run["plugin_id"], run["request"])
                direct.append(time.perf_counter() - started)
                started = time.perf_counter()
                evaluate(model["result"]["coefficients"], coordinates)
                approximate.append(time.perf_counter() - started)
            report.update(
                {
                    "status": "passed",
                    "checks": checks,
                    "version": bootstrap["version"],
                    "training_study_id": training["id"],
                    "validation_study_id": validation["id"],
                    "model_id": model["id"],
                    "prediction_id": prediction["id"],
                    "specification": spec,
                    "model": model,
                    "prediction_summary": {"points": len(points), "api_seconds": api_seconds},
                    "benchmark": {
                        "scope": (
                            "Nine identical held-out conditions; Response-DB includes RAW/RGB, "
                            "surrogate returns the selected metrics only"
                        ),
                        "repetitions": 7,
                        "reference_batch_seconds": direct,
                        "surrogate_batch_seconds": approximate,
                        "median_compute_speed_ratio": statistics.median(direct)
                        / statistics.median(approximate),
                        "excluded": (
                            "HTTP, queue, source lookup and persistence for both kernel timings"
                        ),
                        "environment": plan["environment"],
                    },
                }
            )
    finally:
        report["checks"] = checks
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    return {k: report[k] for k in ("status", "model_id", "prediction_id")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8771")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/camera_platform/surrogate-http-verification.json"),
    )
    parser.add_argument(
        "--primary",
        action="store_true",
        help="Explicitly create reference studies in the primary workspace",
    )
    args = parser.parse_args()
    print(json.dumps(verify(args.base_url, args.report, primary=args.primary), ensure_ascii=False))
