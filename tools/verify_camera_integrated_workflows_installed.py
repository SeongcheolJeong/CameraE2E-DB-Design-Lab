#!/usr/bin/env python3
"""Exercise bounded DB integration jobs from an installed wheel with a real Worker.

Run from a clean interpreter whose PYTHONPATH names the installed wheel target,
not the development ``src`` tree. The DB and upstream assets stay in an explicit
external workspace. This writes a development proof, not a product approval.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tempfile
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from fastapi.testclient import TestClient

import camera_platform
import pyisetcam
from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient
from pyisetcam.dataset import _sha256_array


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _submitted(client, submit, specification, kind):
    queued = submit(specification)
    completed = client.wait(queued["id"], expected_kind=kind, timeout=300)
    if completed["status"] != "succeeded":
        raise RuntimeError(f"{kind} {queued['id']}: {completed.get('error')}")
    return completed


def _artifact(transport, descriptor):
    response = transport.get(descriptor["url"])
    response.raise_for_status()
    if _sha(response.content) != descriptor["sha256"]:
        raise RuntimeError("Installed artifact SHA-256 differs from Registry descriptor")
    return response.content


def _sensor(template, code):
    selected = next(
        (row for row in template["sensor_catalog"] if row["report_code"] == code),
        None,
    )
    if selected is None or selected["status"] != "simulated":
        raise RuntimeError(f"Required saved sensor run unavailable: {code}")
    return {"identifier": code, "run_id": selected["run_id"]}


def verify(workspace: Path, installed_root: Path, state_dir: Path):
    installed_root = installed_root.resolve()
    for module in (camera_platform, pyisetcam):
        if not Path(module.__file__).resolve().is_relative_to(installed_root):
            raise RuntimeError(f"Development module leaked into installed proof: {module.__file__}")
    app = create_app(state_dir, workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        if transport.get("/openapi.json").status_code != 200:
            raise RuntimeError("Installed API schema unavailable")
        if transport.get("/integration-workbench.html").status_code != 200:
            raise RuntimeError("Installed integration workbench unavailable")
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("A separate installed Worker process did not start")

        quality_template = client.saved_sensor_quality_template(query="DEF-1801-802")
        def_sensor = _sensor(quality_template, "DEF-1801-802")
        quality_spec = quality_template["specification"]
        quality_spec.update(
            name="installed saved RAW source",
            sensor_revisions=[def_sensor],
            scenes=["uniform"],
            output_size=[16, 16],
            frames=2,
            acknowledge_simulation_limits=True,
        )
        quality = _submitted(
            client, client.submit_saved_sensor_quality, quality_spec, "saved_sensor_quality"
        )
        source_report = quality["result"]["summary"]["sensor_reports"][0]
        source_case = source_report["cases"][0]
        source_arrays = _artifact(transport, source_report["artifacts"]["npz"])
        with np.load(io.BytesIO(source_arrays), allow_pickle=False) as arrays:
            source_raw = np.asarray(arrays[source_case["array_prefix"] + "_raw_dn_frame"]).copy()
        if source_raw.dtype != np.dtype("int32"):
            raise RuntimeError("Saved source RAW is not int32")

        dataset_spec = client.retained_raw_dataset_template()["specification"]
        dataset_spec.update(
            name="installed exact RAW dataset",
            source_quality_job_id=quality["id"],
            acknowledge_simulation_limits=True,
        )
        dataset = _submitted(
            client, client.submit_retained_raw_dataset, dataset_spec, "retained_raw_dataset"
        )
        dataset_summary = dataset["result"]["summary"]
        dataset_bytes = _artifact(transport, dataset_summary["bundle"])
        with ZipFile(io.BytesIO(dataset_bytes)) as archive:
            dataset_stream = io.BytesIO(archive.read("raw/retained_0000.npz"))
            with np.load(dataset_stream, allow_pickle=False) as arrays:
                dataset_raw = np.asarray(arrays["raw"])
                if dataset_raw.dtype != source_raw.dtype or not np.array_equal(
                    dataset_raw, source_raw
                ):
                    raise RuntimeError("Installed dataset changed retained RAW")

        isp_spec = client.saved_sensor_isp_replay_template()["specification"]
        isp_spec.update(
            name="installed saved RAW ISP replay",
            source_quality_job_id=quality["id"],
            acknowledge_simulation_limits=True,
        )
        isp_spec["profile"]["white_balance"]["gains_rgb"] = [1.2, 1.0, 0.8]
        isp_spec["profile"]["linear_sharpen"]["mode"] = "bounded_detail_v1"
        isp = _submitted(
            client, client.submit_saved_sensor_isp_replay, isp_spec, "saved_sensor_isp_replay"
        )
        isp_summary = isp["result"]["summary"]
        isp_bytes = _artifact(transport, isp_summary["evidence"]["npz"])
        with np.load(io.BytesIO(isp_bytes), allow_pickle=False) as arrays:
            if not np.array_equal(arrays["source_raw_dn_frame"], source_raw):
                raise RuntimeError("Installed ISP changed its retained RAW input")
            if np.array_equal(arrays["candidate_rgb"], arrays["reference_default_rgb"]):
                raise RuntimeError("Installed ISP candidate did not change RGB")

        calibrated_template = client.saved_sensor_calibrated_isp_template(
            source_quality_job_id=quality["id"], sensor_report_index=0, case_index=0
        )
        calibration_binding = calibrated_template["expected_calibration_binding"]
        if calibration_binding["source_raw_content_sha256"] != _sha256_array(source_raw):
            raise RuntimeError("Installed calibration template lost the exact source RAW")
        rows, cols = calibration_binding["geometry"]["full_sensor_shape"]
        calibration_record = calibrated_template["calibration_document_template"]
        calibration_record["provenance"] = {
            "source_kind": "estimated",
            "source_uri": "fixture://installed-declared-calibration",
            "method_and_version": "authored_numeric_install_fixture:1",
            "source_dataset_id": "separate-install-fixture",
            "source_dataset_sha256": _sha(b"separate authored calibration fixture"),
            "target_raw_used_for_fitting": False,
            "uncertainty": {"kind": "unknown", "reason": "Authored numbers, not measured."},
        }
        calibration_record["payload"] = {
            "schema": "camera_isp_calibration_v1",
            "sites": ["R", "Gr", "Gb", "B"],
            "lsc": {
                "schema": "native_four_site_grid_v1",
                "row_nodes": [0, rows - 1],
                "column_nodes": [0, cols - 1],
                "gains": [
                    [[1.0, 1.1, 1.2, 1.3], [1.1, 1.2, 1.3, 1.4]],
                    [[1.2, 1.3, 1.4, 1.5], [1.3, 1.4, 1.5, 1.6]],
                ],
                "valid_mask": [[[True] * 4 for _ in range(2)] for _ in range(2)],
                "interpolation": "bilinear",
                "normalization": "declared_per_site_reference",
                "reference_roi": {
                    "row_start": 0,
                    "row_end": rows,
                    "column_start": 0,
                    "column_end": cols,
                },
            },
            "noise": {
                "schema": "native_four_site_noise_v1",
                "slope_dn": [0.5, 0.6, 0.7, 0.8],
                "intercept_dn2": [4.0, 5.0, 6.0, 7.0],
                "signal_interval_dn": [-64, 4000],
                "reference": "black_subtracted_dn_before_lsc",
                "variance_model": "slope_times_max_signal_zero_plus_intercept",
                "spatial_independence": "assumed",
            },
        }
        calibration_path = state_dir / "installed-declared-calibration.json"
        calibration_path.write_text(
            json.dumps(calibration_record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        calibration_import = client.import_saved_sensor_isp_calibration(calibration_path)
        calibrated_spec = calibrated_template["specification"]
        calibrated_spec.update(
            name="installed declared-calibration RAW ISP",
            source_quality_job_id=quality["id"],
            calibration_sha256=calibration_import["calibration_sha256"],
            acknowledge_simulation_limits=True,
        )
        calibrated_spec["profile"]["calibration_id"] = (
            "sha256:" + calibration_import["calibration_sha256"]
        )
        calibrated_spec["profile"]["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
        calibrated_spec["profile"]["raw_denoise"].update(
            mode="calibrated_same_site_filter_v1", strength=0.8, range_floor_dn2=3.0
        )
        calibrated_isp = _submitted(
            client,
            client.submit_saved_sensor_calibrated_isp,
            calibrated_spec,
            "saved_sensor_calibrated_isp",
        )
        calibrated_summary = calibrated_isp["result"]["summary"]
        calibrated_bytes = _artifact(transport, calibrated_summary["evidence"]["npz"])
        with np.load(io.BytesIO(calibrated_bytes), allow_pickle=False) as arrays:
            if not np.array_equal(arrays["source_raw_dn_frame"], source_raw):
                raise RuntimeError("Installed calibrated ISP changed its source RAW")
        if (
            not calibrated_summary["candidate_isp"]["stage_outputs"]["spatial_lsc"]["applied"]
            or not calibrated_summary["candidate_isp"]["stage_outputs"]["raw_denoise"]["applied"]
            or calibrated_summary["calibration"]["provenance_verification"]
            != "declared_only_not_independently_verified"
        ):
            raise RuntimeError("Installed calibrated ISP stages/provenance are wrong")

        lens_template = client.lens_sensor_raw_template()
        ready = [
            row
            for row in lens_template["lens_catalog"]["revisions"]
            if row["preview_status"] == "ready"
        ]
        if not ready:
            raise RuntimeError("No ready Lens DB v2 PSF revision")
        dep_sensor = _sensor(lens_template, "DEP-2209-801")
        lens_spec = lens_template["specification"]
        lens_spec.update(
            name="installed geometric Lens x Sensor RAW",
            lens_revision_id=ready[0]["revision_id"],
            sensor_revision=dep_sensor,
            sensor_size=[8, 8],
            accept_approximate_lens_radiometry=True,
        )
        lens = _submitted(client, client.submit_lens_sensor_raw, lens_spec, "lens_sensor_raw")
        lens_summary = lens["result"]["summary"]
        lens_raw = np.load(
            io.BytesIO(_artifact(transport, lens_summary["preview"]["raw_artifact"])),
            allow_pickle=False,
        )
        if lens_raw.dtype != np.dtype("int32") or lens_raw.shape != (8, 8):
            raise RuntimeError("Installed Lens x Sensor RAW has the wrong dtype/shape")

        local_lens_spec = client.lens_sensor_raw_template(scope="local_field_psf")["specification"]
        local_lens_spec.update(
            name="installed local field PSF Lens x Sensor RAW",
            lens_revision_id=ready[0]["revision_id"],
            sensor_revision=dep_sensor,
            sensor_size=[8, 8],
            field_psf_index=2,
            field_azimuth_deg=90.0,
            accept_approximate_lens_radiometry=True,
        )
        local_lens = _submitted(
            client, client.submit_lens_sensor_raw, local_lens_spec, "lens_sensor_raw"
        )
        local_summary = local_lens["result"]["summary"]
        if local_summary["preview"]["scope"] != "approximate_geometric_local_field_psf_raw_preview":
            raise RuntimeError("Installed Lens x Sensor local field scope was not preserved")
        local_raw = np.load(
            io.BytesIO(_artifact(transport, local_summary["preview"]["raw_artifact"])),
            allow_pickle=False,
        )
        local_dataset_spec = client.retained_raw_dataset_template(source_kind="lens_sensor_raw")[
            "specification"
        ]
        local_dataset_spec.update(
            name="installed local Lens x Sensor retained RAW",
            source_job_id=local_lens["id"],
            acknowledge_simulation_limits=True,
        )
        local_dataset = _submitted(
            client,
            client.submit_retained_raw_dataset,
            local_dataset_spec,
            "retained_raw_dataset",
        )
        local_dataset_bytes = _artifact(transport, local_dataset["result"]["summary"]["bundle"])
        with ZipFile(io.BytesIO(local_dataset_bytes)) as archive:
            with np.load(
                io.BytesIO(archive.read("raw/retained_0000.npz")), allow_pickle=False
            ) as arrays:
                if not np.array_equal(arrays["raw"], local_raw):
                    raise RuntimeError("Installed local Lens dataset changed source RAW")

        hdr_spec = client.saved_sensor_hdr_template(query="DEF-1801-802")["specification"]
        hdr_spec.update(
            name="installed static saved HDR bracket",
            sensor_revision=def_sensor,
            acknowledge_simulation_limits=True,
        )
        hdr = _submitted(client, client.submit_saved_sensor_hdr, hdr_spec, "saved_sensor_hdr")
        hdr_summary = hdr["result"]["summary"]
        hdr_npz = _artifact(transport, hdr_summary["report"]["artifacts"]["npz"])
        with np.load(io.BytesIO(hdr_npz), allow_pickle=False) as arrays:
            hdr_raw_shape = list(arrays["original_raw_dn"].shape)
            if hdr_raw_shape[0] != 2 or arrays["original_raw_dn"].dtype != np.dtype("int32"):
                raise RuntimeError("Installed HDR did not retain both original integer RAW")

        native_hdr_spec = client.saved_sensor_hdr_template(query="DEF-1801-802")["specification"]
        native_hdr_spec.update(
            name="installed model-known native FPN HDR",
            sensor_revision=def_sensor,
            fixed_pattern_mode="model_known_native_fpn",
            acknowledge_simulation_limits=True,
        )
        native_hdr = _submitted(
            client, client.submit_saved_sensor_hdr, native_hdr_spec, "saved_sensor_hdr"
        )
        native_report = native_hdr["result"]["summary"]["report"]
        native_hdr_npz = _artifact(transport, native_report["artifacts"]["npz"])
        with np.load(io.BytesIO(native_hdr_npz), allow_pickle=False) as arrays:
            if arrays["original_raw_dn"].dtype != np.dtype("int32"):
                raise RuntimeError("Installed model-known FPN HDR did not retain RAW")
            if (
                "model_fpn_response_gain" not in arrays
                or "model_fpn_post_well_bias_e" not in arrays
            ):
                raise RuntimeError("Installed HDR did not retain model-known FPN maps")
        if native_report["controls"]["model_fpn_calibration"]["independent_calibration"]:
            raise RuntimeError("Model-known FPN was incorrectly called independent calibration")

        perception_template = client.saved_sensor_perception_template(
            quality_job_id=quality["id"], sensor_run_id=def_sensor["run_id"], scene="uniform"
        )
        image_binding = perception_template["image_binding"]
        labels = {"id": "installed-demo-objects", "version": "1", "labels": ["target"]}
        declared_model_sha = _sha(b"model-not-installed-in-this-proof")
        model_manifest = {
            "schema": "camerae2e_detection_model_manifest_v1",
            "id": "external-demo-detector",
            "version": "1",
            "backend": "external_predictions",
            "checkpoint_sha256": declared_model_sha,
            "label_set": labels,
            "preprocessing": {"rgb": "saved_fixed_linear"},
        }
        model_artifact = client.import_saved_sensor_perception_artifact(
            "model_manifest", model_manifest
        )["artifact"]
        annotations = {
            "schema": "camerae2e_detection_annotations_v1",
            "image_binding": image_binding,
            "annotation_set": {"id": "installed-demo-annotations", "version": "1"},
            "label_set": labels,
            "objects": [{"bbox": [2, 2, 6, 6], "label": "target"}],
        }
        annotation_artifact = client.import_saved_sensor_perception_artifact(
            "annotation", annotations
        )["artifact"]
        predictions = {
            "schema": "camerae2e_detection_predictions_v1",
            "image_binding": image_binding,
            "label_set": labels,
            "model_manifest_sha256": model_artifact["sha256"],
            "model_artifact_sha256": declared_model_sha,
            "inference_run_id": "external-demo-inference",
            "detections": [{"bbox": [2, 2, 6, 6], "label": "target", "score": 0.9}],
        }
        prediction_artifact = client.import_saved_sensor_perception_artifact(
            "prediction", predictions
        )["artifact"]
        perception_spec = perception_template["specification"]
        perception_spec.update(
            name="installed externally supplied box comparison",
            quality_job_id=quality["id"],
            sensor_run_id=def_sensor["run_id"],
            scene="uniform",
            annotation_artifact_sha256=annotation_artifact["sha256"],
            prediction_artifact_sha256=prediction_artifact["sha256"],
            model_manifest_sha256=model_artifact["sha256"],
            acknowledge_external_predictions=True,
        )
        perception = _submitted(
            client,
            client.submit_saved_sensor_perception,
            perception_spec,
            "saved_sensor_perception",
        )
        evaluated = perception["result"]["summary"]["evaluation"]
        if (
            evaluated["metrics"]["true_positive"] != 1
            or evaluated["prediction_provenance"]["model_execution_verified"]
        ):
            raise RuntimeError("Installed external-prediction metric scope was misreported")

        lfm_spec = client.saved_sensor_lfm_template(query="DEF-1801-802")["specification"]
        lfm_spec.update(
            name="installed saved PWM phase sweep",
            sensor_revision=def_sensor,
            acknowledge_simulation_limits=True,
        )
        lfm = _submitted(client, client.submit_saved_sensor_lfm, lfm_spec, "saved_sensor_lfm")
        lfm_summary = lfm["result"]["summary"]
        lfm_npz = _artifact(transport, lfm_summary["report"]["artifacts"]["npz"])
        with np.load(io.BytesIO(lfm_npz), allow_pickle=False) as arrays:
            phase_raw_shape = list(arrays["original_pwm_raw_dn"].shape)
            if phase_raw_shape[:2] != [2, 4] or arrays["original_pwm_raw_dn"].dtype != np.dtype(
                "int32"
            ):
                raise RuntimeError("Installed LFM did not retain 2x4 integer PWM RAW")

        if worker.poll() is not None:
            raise RuntimeError("Installed Worker exited before proof completed")
        records = {
            name: {
                "id": job["id"],
                "status": job["status"],
                "artifact_sha256": [item["sha256"] for item in job["artifacts"]],
            }
            for name, job in (
                ("saved_sensor_quality", quality),
                ("retained_raw_dataset", dataset),
                ("saved_sensor_isp_replay", isp),
                ("saved_sensor_calibrated_isp", calibrated_isp),
                ("lens_sensor_raw", lens),
                ("lens_sensor_local_raw", local_lens),
                ("lens_sensor_local_dataset", local_dataset),
                ("saved_sensor_hdr", hdr),
                ("saved_sensor_native_fpn_hdr", native_hdr),
                ("saved_sensor_lfm", lfm),
                ("saved_sensor_perception", perception),
            )
        }
        return {
            "schema": "camera_platform_integrated_installed_proof_v1",
            "version": camera_platform.__version__,
            "package_root": str(installed_root),
            "package_paths": [camera_platform.__file__, pyisetcam.__file__],
            "workspace": str(workspace),
            "state_dir": str(state_dir),
            "worker_child_pid": worker.pid,
            "records": records,
            "raw_evidence": {
                "quality_shape": list(source_raw.shape),
                "quality_dtype": str(source_raw.dtype),
                "dataset_raw_exact": True,
                "isp_input_raw_exact": True,
                "isp_candidate_differs": True,
                "calibrated_isp_input_raw_exact": True,
                "calibrated_isp_lsc_and_denoise_applied": True,
                "lens_raw_shape": list(lens_raw.shape),
                "lens_raw_dtype": str(lens_raw.dtype),
                "lens_local_raw_shape": list(local_raw.shape),
                "lens_local_dataset_raw_exact": True,
                "hdr_original_raw_shape": hdr_raw_shape,
                "hdr_model_known_fpn_maps_retained": True,
                "lfm_original_pwm_raw_shape": phase_raw_shape,
                "perception_supplied_box_true_positive": 1,
            },
            "boundaries": {
                "lens": (
                    "geometric_central_or_local_field_psf_patch_unity_radiometry_not_product_rank"
                ),
                "hdr": (
                    "static_two_exposure_zero_or_model_known_native_fpn_not_independent_calibration"
                ),
                "lfm": "authored_pwm_and_timing_phase_sensitivity_not_measured_lfm",
                "isp": (
                    "native_linear_rggb_with_declared_hash_bound_lsc_denoise_"
                    "no_physical_calibration_validation"
                ),
                "dataset": (
                    "one_exact_retained_quality_or_lens_raw_unlabeled_no_cross_export_holdout"
                ),
                "perception": "supplied_single_image_boxes_no_detector_inference_or_dataset_map",
                "physical_validation": "not_established",
                "product_approval": "not_granted",
            },
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    state = Path(tempfile.mkdtemp(prefix="camera-integrated-installed-"))
    proof = verify(args.workspace.resolve(), args.installed_root.resolve(), state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "output": str(args.output), "version": proof["version"]}))


if __name__ == "__main__":
    main()
