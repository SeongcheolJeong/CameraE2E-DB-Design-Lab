#!/usr/bin/env python3
"""Prove the dev15 common-scene workflows from a byte-matched installed wheel.

Run with the installed site-packages ahead of the source tree on PYTHONPATH.
The API starts its own child Worker; this script never executes jobs inline.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import time
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _artifact, _client, _verify_wheel

from camera_platform.api import create_app
from camera_platform.registry import canonical
from pyisetcam.dataset import _sha256_array

SLOTS = ("L1S1", "L1S2", "L2S1", "L2S2")
LENSES = {
    "L1": "6c48e967-74b4-5901-8186-ba6fafe4797b",  # P0014
    "L2": "4daeca8f-5d81-57b0-8ae3-928f23de2b36",  # P0248
}
SENSORS = {
    "S1": ("DEF-2512-801", "360dfe5370ada20b81c085d5859e9b0ae54be66df0a5e7e8eed0bffe7c6dc601"),
    "S2": ("DEF-2412-804", "89a3d6955daa5cbd5208c301e5e78c53a7397489c4e4d707f4d3cfab4c6bbde6"),
}
SOURCE_FOV_DEG = 0.12
SOURCE_SIZE = [32, 32]


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _finished(client, queued: dict, kind: str, *, timeout: int = 180) -> dict:
    finished = client.wait(queued["id"], expected_kind=kind, timeout=timeout)
    if finished["status"] != "succeeded":
        raise RuntimeError(f"Installed {kind} failed: {finished.get('error')}")
    return finished


def _source_proof(path: Path) -> tuple[dict, str]:
    content = path.read_bytes()
    proof = json.loads(content)
    if (
        content != (canonical(proof) + "\n").encode()
        or proof.get("schema") != "camerae2e_common_input_scene_2x2_source_proof_v1"
        or proof.get("execution_level") != "source_direct_python_jobs_not_installed_wheel"
        or set(proof.get("cases", {})) != set(SLOTS)
        or proof.get("quality_comparison", {}).get("matched_horizontal_fov") is not True
        or proof.get("quality_comparison", {}).get("matched_native_output_angular_grid")
        is not False
        or proof.get("quality_comparison", {}).get("edge_mtf50_diagnostic_delta_allowed")
        is not False
    ):
        raise ValueError("Source proof is not the bounded common-scene 2 x 2 run.")
    for slot in SLOTS:
        case = proof["cases"][slot]
        sensor_code, sensor_run = SENSORS[slot[2:]]
        scene = case["scene"]
        grid = scene["angular_grid_contract"]
        descriptor = case["raw_artifact"]
        if (
            case["lens_identity"]["revision_id"] != LENSES[slot[:2]]
            or case["sensor_identity"]["report_code"] != sensor_code
            or case["sensor_identity"]["run_id"] != sensor_run
            or scene["horizontal_fov_deg"] != SOURCE_FOV_DEG
            or grid["mode"] != "explicit_common_input_scene"
            or grid["source_shape"] != [64, 64]
            or grid["native_output_grid_alignment"] != "not_established"
            or descriptor["dtype"] != "int32"
            or descriptor["shape"] != SOURCE_SIZE
            or len(descriptor["sha256"]) != 64
        ):
            raise ValueError(f"Source proof {slot} lost its pinned RAW/scene identity.")
    return proof, _sha(content)


def _matrix(client, transport: TestClient, source: dict) -> tuple[dict, dict]:
    template = client.lens_sensor_raw_template(scope="central_bounded_common_scene")
    if template["specification"]["schema"] != "camerae2e_lens_sensor_raw_preview_study_v4":
        raise RuntimeError("Installed common-scene RAW template is not v4.")
    catalog = {(row["report_code"], row["run_id"]) for row in template["sensor_catalog"]}
    if not set(SENSORS.values()) <= catalog:
        raise RuntimeError("Pinned GC50F6/Reno12 saved runs are missing from the wheel catalog.")
    entries = []
    originals = {}
    original_bytes = {}
    input_grids = []
    for slot in SLOTS:
        source_case = source["cases"][slot]
        sensor_code, sensor_run = SENSORS[slot[2:]]
        spec = client.lens_sensor_raw_template(
            scope="central_bounded_common_scene"
        )["specification"]
        spec.update(
            name=f"v4 common scene GC50F6 Reno12 {slot}",
            lens_revision_id=LENSES[slot[:2]],
            sensor_revision={"identifier": sensor_code, "run_id": sensor_run},
            scene_kind="slanted_edge",
            sensor_size=SOURCE_SIZE.copy(),
            scene_horizontal_fov_deg=SOURCE_FOV_DEG,
            luminance_cd_m2=1000.0,
            exposure_s=0.004,
            noise_flag=0,
            seed=7,
            accept_approximate_lens_radiometry=True,
        )
        started = time.monotonic()
        queued = client.submit_lens_sensor_raw(spec)
        resource = queued.get("resource_preflight") or {}
        if (
            resource.get("status") != "within_development_budget"
            or resource.get("scope") != "central_bounded_strict_raytrace"
            or resource.get("scene_fov_source") != "explicit_common_input_scene"
            or resource.get("scene_horizontal_fov_deg") != SOURCE_FOV_DEG
        ):
            raise RuntimeError(f"Installed {slot} resource preflight is not frozen v4.")
        finished = _finished(client, queued, "lens_sensor_raw")
        detail = client.lens_sensor_raw(finished["id"])
        if detail["result"]["summary"] != finished["result"]["summary"]:
            raise RuntimeError(f"Installed {slot} RAW changed on re-read.")
        summary = detail["result"]["summary"]
        preview = summary["preview"]
        psf = preview["lens_model"]["psf_execution"]
        grid = preview["scene"]["angular_grid_contract"]
        native = preview["capture"]["native_output_angular_grid"]
        raw_descriptor = preview["raw_artifact"]
        raw_bytes = _artifact(transport, raw_descriptor)
        raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False)
        raw_hash = _sha256_array(raw)
        if (
            summary["schema"] != spec["schema"]
            or summary["specification"] != spec
            or summary["source_identities"]["lens"] != source_case["lens_identity"]
            or summary["source_identities"]["sensor"] != source_case["sensor_identity"]
            or summary["resource_preflight"] != resource
            or preview["resource_preflight"] != resource
            or psf["mode"] != "strict_bounded_streaming"
            or psf["complete_precomputed_stack"] is not False
            or psf["peak_cached_kernels"] > 4
            or preview["physical_validation"] != "not_established"
            or preview["scene"]["horizontal_fov_deg"] != SOURCE_FOV_DEG
            or grid != source_case["scene"]["angular_grid_contract"]
            or native != source_case["native_output_angular_grid"]
            or native["sensor_resize"] is not False
            or native["status"]
            != "paraxial_center_only_full_distortion_map_not_established"
            or raw.dtype != np.dtype("int32")
            or list(raw.shape) != SOURCE_SIZE
            or raw.tolist() != preview["raw"]["values"]
            or _sha(raw_bytes) != source_case["raw_artifact"]["sha256"]
            or [int(raw.min()), int(raw.max())] != source_case["raw_range_dn"]
        ):
            raise RuntimeError(f"Installed {slot} differs from exact source RAW/scene proof.")
        result_artifact = next(
            item for item in detail["artifacts"]
            if item["name"] == "lens-sensor-raw-preview-result.json"
        )
        if _artifact(transport, result_artifact) != canonical(summary).encode():
            raise RuntimeError(f"Installed {slot} result JSON differs from job summary.")
        originals[slot] = raw
        original_bytes[slot] = raw_bytes
        input_grids.append(grid)
        entries.append({
            "slot": slot,
            "job_id": finished["id"],
            "lens_revision_id": spec["lens_revision_id"],
            "sensor_report_code": sensor_code,
            "sensor_run_id": sensor_run,
            "elapsed_s": time.monotonic() - started,
            "raw_content_sha256": raw_hash,
            "raw_artifact_sha256": raw_descriptor["sha256"],
            "result_artifact_sha256": result_artifact["sha256"],
            "input_horizontal_fov_deg": preview["scene"]["horizontal_fov_deg"],
            "source_angular_center_step_deg": grid["center_angular_step_deg_row_col"][1],
            "native_output_horizontal_fov_deg": native["horizontal_fov_deg"],
            "native_output_angular_center_step_deg": native[
                "center_angular_step_deg_row_col"
            ][1],
            "resource_preflight": resource,
            "psf_execution": psf,
            "original_raw_exact": True,
        })
    if not all(grid == input_grids[0] for grid in input_grids):
        raise RuntimeError("Installed 2 x 2 input angular scene grids differ.")
    if len({item["native_output_horizontal_fov_deg"] for item in entries}) != 4:
        raise RuntimeError("Installed 2 x 2 unexpectedly reused one native output grid.")
    return {"cases": entries}, {"arrays": originals, "npy_bytes": original_bytes}


def _quality(client, transport: TestClient, matrix: dict, originals: dict, source: dict) -> dict:
    template = client.lens_sensor_quality_template()
    spec = template["specification"]
    if spec["schema"] != "camerae2e_lens_sensor_quality_study_v1":
        raise RuntimeError("Installed 2 x 2 quality contract is missing.")
    spec.update(
        source_jobs={item["slot"]: item["job_id"] for item in matrix["cases"]},
        acknowledge_simulation_limits=True,
    )
    finished = _finished(
        client, client.submit_lens_sensor_quality(spec), "lens_sensor_quality_study"
    )
    detail = client.lens_sensor_quality(finished["id"])
    summary = detail["result"]["summary"]
    comparison = summary["comparison"]
    reasons = comparison.get("reasons") or []
    if (
        set(summary["cases"]) != set(SLOTS)
        or comparison["matched_source_controls"] is not True
        or comparison["matched_horizontal_fov"] is not True
        or comparison["matched_native_output_angular_grid"] is not False
        or comparison["input_scene_coarser_than_native_output_by_slot"]
        != dict.fromkeys(SLOTS, True)
        or comparison["edge_mtf50_diagnostic_delta_allowed"] is not False
        or comparison["temporal_snr_delta_allowed"] is not False
        or comparison["product_ranking_allowed"] is not False
        or reasons != source["quality_comparison"]["reasons"]
        or any(pair["edge_mtf50_delta_available"] is not False
               or "right_minus_left_cycles_per_output_pixel" in pair
               for pair in comparison["pairwise_diagnostic_deltas"])
    ):
        raise RuntimeError("Installed quality job overclaimed the common input scene.")
    if not all(
        math.isclose(comparison["horizontal_fov_deg_by_slot"][slot], SOURCE_FOV_DEG,
                     rel_tol=0, abs_tol=1e-12)
        for slot in SLOTS
    ):
        raise RuntimeError("Installed quality job lost the common input FOV.")
    with np.load(io.BytesIO(_artifact(transport, summary["evidence_arrays"])),
                 allow_pickle=False) as arrays:
        for slot in SLOTS:
            original = originals["arrays"][slot]
            retained = np.asarray(arrays[f"{slot}_raw_dn"])
            if retained.dtype != np.dtype("int32") or retained.tobytes() != original.tobytes():
                raise RuntimeError(f"Installed quality job altered {slot} original RAW.")
    for slot in SLOTS:
        edge = summary["cases"][slot]["edge_mtf50"]
        expected = source["quality_cases"][slot]["edge_mtf50"]
        if edge["available"] != expected["available"]:
            raise RuntimeError(f"Installed {slot} edge availability differs from source proof.")
        if edge["available"] and not math.isclose(
            edge["value"], expected["value"], rel_tol=1e-9, abs_tol=1e-12
        ):
            raise RuntimeError(f"Installed {slot} edge MTF differs from source proof.")
        if summary["cases"][slot]["temporal_snr_db"]["available"] is not False:
            raise RuntimeError("Single-frame quality job claimed temporal SNR.")
    result_artifact = next(
        item for item in detail["artifacts"]
        if item["name"] == "lens-sensor-quality-study-result.json"
    )
    if _artifact(transport, result_artifact) != canonical(summary).encode():
        raise RuntimeError("Installed quality result artifact differs from summary.")
    return {
        "job_id": finished["id"],
        "status": finished["status"],
        "case_count": len(summary["cases"]),
        "comparison": comparison,
        "edge_mtf50": {
            slot: summary["cases"][slot]["edge_mtf50"] for slot in SLOTS
        },
        "original_raw_exact": True,
        "evidence_npz_sha256": summary["evidence_arrays"]["sha256"],
        "result_artifact_sha256": result_artifact["sha256"],
    }


def _dataset(client, transport: TestClient, source_case: dict, original: np.ndarray) -> dict:
    template = client.retained_raw_dataset_template(source_kind="lens_sensor_raw")
    spec = template["specification"]
    if spec["schema"] != "camerae2e_retained_raw_dataset_study_v2":
        raise RuntimeError("Installed exact Lens RAW dataset template is missing.")
    spec.update(
        source_job_id=source_case["job_id"],
        split="train", labels="unlabeled", acknowledge_simulation_limits=True,
    )
    finished = _finished(
        client, client.submit_retained_raw_dataset(spec), "retained_raw_dataset", timeout=120
    )
    detail = client.retained_raw_dataset(finished["id"])
    summary = detail["result"]["summary"]
    if (
        summary["source_kind"] != "lens_sensor_raw"
        or summary["source_binding"]["source_lens_sensor_job_id"] != source_case["job_id"]
        or summary["source_binding"]["source_raw_content_sha256"]
        != source_case["raw_content_sha256"]
        or summary["raw_content_sha256"] != source_case["raw_content_sha256"]
        or summary["raw_shape"] != SOURCE_SIZE
        or summary["raw_dtype"] != "int32"
        or summary["raw_values_byte_equal"] is not True
        or summary["labels"] != "unlabeled"
        or summary["dataset_validation"]["ok"] is not True
    ):
        raise RuntimeError("Installed exact RAW dataset lost its source binding.")
    bundle_bytes = _artifact(transport, summary["bundle"])
    with ZipFile(io.BytesIO(bundle_bytes)) as archive:
        with np.load(io.BytesIO(archive.read("raw/retained_0000.npz")),
                     allow_pickle=False) as arrays:
            retained = np.asarray(arrays["raw"])
        manifest = json.loads(archive.read("manifest.json"))
    if (
        retained.dtype != np.dtype("int32")
        or retained.tobytes(order="C") != original.tobytes(order="C")
        or _sha256_array(retained) != source_case["raw_content_sha256"]
        or manifest["source_binding"] != summary["source_binding"]
        or manifest["split_policy"]["holdout_independence_claimed"] is not False
    ):
        raise RuntimeError("Installed dataset ZIP altered the original RAW or source binding.")
    result_artifact = next(
        item for item in detail["artifacts"] if item["name"] == "retained-raw-dataset-result.json"
    )
    if _artifact(transport, result_artifact) != canonical(summary).encode():
        raise RuntimeError("Installed dataset result JSON differs from summary.")
    return {
        "job_id": finished["id"],
        "status": finished["status"],
        "source_job_id": source_case["job_id"],
        "source_raw_content_sha256": summary["raw_content_sha256"],
        "bundle_sha256": summary["bundle"]["sha256"],
        "result_artifact_sha256": result_artifact["sha256"],
        "original_raw_exact": True,
        "labels": summary["labels"],
        "holdout_independence_claimed": False,
    }


def _adaptive(client, transport: TestClient, source_case: dict, original: np.ndarray) -> dict:
    template = client.lens_sensor_adaptive_demosaic_template(
        source_lens_sensor_job_id=source_case["job_id"]
    )
    spec = template["specification"]
    selected = template["selected_source_contract"]
    if (
        spec["schema"] != "camerae2e_lens_sensor_adaptive_demosaic_study_v1"
        or spec["method"] != "adaptive_laplacian_v1"
        or spec["source_lens_sensor_job_id"] != source_case["job_id"]
        or selected["native_cfa_contract"] != "three_channel_rggb_2x2"
        or selected["raw_shape"] != SOURCE_SIZE
        or selected["source_binding"]["source_raw_content_sha256"]
        != source_case["raw_content_sha256"]
    ):
        raise RuntimeError("Installed Adaptive Laplacian template lost exact RAW binding.")
    spec["acknowledge_simulation_limits"] = True
    finished = _finished(
        client, client.submit_lens_sensor_adaptive_demosaic(spec),
        "lens_sensor_adaptive_demosaic", timeout=120,
    )
    detail = client.lens_sensor_adaptive_demosaic(finished["id"])
    summary = detail["result"]["summary"]
    report = summary["report"]
    if (
        summary["source_binding"] != selected["source_binding"]
        or summary["native_cfa_contract"] != "three_channel_rggb_2x2"
        or summary["raw_content_sha256"] != source_case["raw_content_sha256"]
        or summary["method_binding"] != selected["method_binding"]
        or report["raw_values_byte_equal"] is not True
        or report["before_after"]["valid_pixels"] <= 0
        or report["before_after"]["quality_improvement_claimed"] is not False
    ):
        raise RuntimeError("Installed Adaptive Laplacian result overclaimed or lost RAW.")
    json_bytes = _artifact(transport, summary["evidence"]["json"])
    npz_bytes = _artifact(transport, summary["evidence"]["npz"])
    if (
        json.loads(json_bytes) != report
        or report["artifacts"]["npz_sha256"] != _sha(npz_bytes)
    ):
        raise RuntimeError("Installed Adaptive JSON/NPZ artifact binding differs.")
    with np.load(io.BytesIO(npz_bytes), allow_pickle=False) as arrays:
        retained = np.asarray(arrays["source_raw_dn_frame"])
        adaptive = np.asarray(arrays["adaptive_laplacian_sensor_rgb"])
        reference = np.asarray(arrays["reference_bilinear_sensor_rgb"])
        valid = np.asarray(arrays["comparison_valid_mask"])
    if (
        retained.dtype != np.dtype("int32")
        or retained.shape != tuple(SOURCE_SIZE)
        or retained.tobytes(order="C") != original.tobytes(order="C")
        or adaptive.shape != (*SOURCE_SIZE, 3)
        or reference.shape != adaptive.shape
        or valid.shape != tuple(SOURCE_SIZE)
        or valid.dtype != np.dtype("bool")
        or not np.isfinite(adaptive).all()
        or not np.isfinite(reference).all()
    ):
        raise RuntimeError("Installed Adaptive NPZ changed source RAW or RGB evidence.")
    result_artifact = next(
        item for item in detail["artifacts"]
        if item["name"] == "lens-sensor-adaptive-demosaic-result.json"
    )
    if _artifact(transport, result_artifact) != canonical(summary).encode():
        raise RuntimeError("Installed Adaptive result JSON differs from summary.")
    return {
        "job_id": finished["id"],
        "status": finished["status"],
        "source_job_id": source_case["job_id"],
        "raw_content_sha256": summary["raw_content_sha256"],
        "method_binding": summary["method_binding"],
        "valid_pixels": report["before_after"]["valid_pixels"],
        "quality_improvement_claimed": False,
        "evidence_json_sha256": summary["evidence"]["json"]["sha256"],
        "evidence_npz_sha256": summary["evidence"]["npz"]["sha256"],
        "result_artifact_sha256": result_artifact["sha256"],
        "original_raw_exact": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--source-proof", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-version", default="0.16.2.dev15")
    args = parser.parse_args()
    if args.state.exists():
        raise RuntimeError("A fresh isolated installed state path is required.")
    if args.output.exists():
        raise RuntimeError("Choose a new proof output path; existing evidence is immutable.")
    source, source_sha256 = _source_proof(args.source_proof)
    package = _verify_wheel(args.installed_root, args.wheel)
    if package["version"] != args.expected_version:
        raise RuntimeError("Installed wheel version is not the requested dev15 version.")
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.pid == os.getpid() or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start independently.")
        matrix, originals = _matrix(client, transport, source)
        quality = _quality(client, transport, matrix, originals, source)
        selected = next(item for item in matrix["cases"] if item["slot"] == "L1S1")
        original = originals["arrays"]["L1S1"]
        dataset = _dataset(client, transport, selected, original)
        adaptive = _adaptive(client, transport, selected, original)
        reread = client.lens_sensor_raw(selected["job_id"])
        descriptor = reread["result"]["summary"]["preview"]["raw_artifact"]
        if (
            descriptor["sha256"] != selected["raw_artifact_sha256"]
            or _artifact(transport, descriptor) != originals["npy_bytes"]["L1S1"]
            or worker.poll() is not None
        ):
            raise RuntimeError("Installed child Worker or original RAW changed downstream.")
        proof = {
            "schema": "camerae2e_installed_joint_dev15_proof_v1",
            "package": package,
            "source_proof_sha256": source_sha256,
            "source_proof_schema": source["schema"],
            "worker_child_pid": worker.pid,
            "matrix": matrix,
            "quality": quality,
            "exact_raw_dataset": dataset,
            "adaptive_laplacian": adaptive,
            "original_raw_unchanged_after_downstream": True,
            "physical_validation": "not_established",
            "product_readiness": "not_established",
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "version": package["version"],
        "raw_cases": len(matrix["cases"]),
        "quality_case_count": quality["case_count"],
        "quality_pairwise_delta_allowed": quality["comparison"][
            "edge_mtf50_diagnostic_delta_allowed"
        ],
        "dataset": dataset["status"],
        "adaptive_laplacian": adaptive["status"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
