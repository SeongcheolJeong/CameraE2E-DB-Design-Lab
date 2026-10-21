#!/usr/bin/env python3
"""Cold dev25 wheel proof for object-radiance RAW, legacy ISP, and DB optics.

The two paths are deliberately distinct: arbitrary supplied spectral radiance
produces RAW, while the model-derived D65 color transform uses a separate
saved D65 RAW. Neither path establishes calibrated product performance.
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _artifact, _client, _verify_wheel

from camera_platform.api import create_app
from camera_platform.lens_db_wavefront_bridge import (
    APERTURE_HYPOTHESIS,
    db_wavefront_preflight,
)
from camera_platform.registry import canonical
from pyisetcam.dataset import _sha256_array

LENS_REVISION_ID = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
SENSOR_REPORT_CODE = "DEF-2512-801"


def _finished(client, submitted, kind):
    result = client.wait(submitted["id"], timeout=300, expected_kind=kind)
    if result.get("status") != "succeeded" or result.get("job_kind") != kind:
        raise RuntimeError(f"Installed {kind} failed: {result.get('error')}")
    return result


def _sensor(template):
    selected = next(
        row for row in template["sensor_catalog"]
        if row["report_code"] == SENSOR_REPORT_CODE and row["model"] == "db_estimate_v1"
    )
    return {"identifier": SENSOR_REPORT_CODE, "run_id": selected["run_id"]}


def _radiance(client, transport):
    metadata = client.lens_sensor_radiance_scene_template()
    metadata.update(
        name="dev25 authored object-side spectral radiance",
        source_uri="fixture://dev25-independent-radiance-array",
        license="test-authored synthetic fixture",
        method_and_version="explicit-two-region-spectral-radiance:1",
        horizontal_fov_deg=0.12,
        object_distance_m="infinity",
    )
    array = np.zeros((32, 32, 3), dtype=np.float32)
    array[..., :] = (0.02, 0.016, 0.012)
    array[:, 16:, :] *= 3.0
    with tempfile.TemporaryDirectory(prefix="camera-dev25-radiance-") as directory:
        path = Path(directory) / "object-radiance.npy"
        np.save(path, array, allow_pickle=False)
        original_bytes = path.read_bytes()
        scene = client.import_lens_sensor_radiance_scene(metadata, path)
    if (
        scene["unit"] != "W/m^2/sr/nm"
        or scene["reference_plane"] != "object_directional_radiance_before_lens"
        or scene["physical_validation"] != "not_established"
        or _artifact(transport, scene["artifacts"][0]) != original_bytes
        or client.lens_sensor_radiance_scene(scene["id"])["content_hash"]
        != scene["content_hash"]
    ):
        raise RuntimeError("Installed source radiance is not immutable and retrievable.")
    template = client.lens_sensor_raw_template(scope="central_bounded_radiance_scene")
    spec = template["specification"]
    if (
        spec["schema"] != "camerae2e_lens_sensor_raw_preview_study_v6"
        or "object-side spectral radiance" not in template["limitation"]
        or "D65 scene" in template["limitation"]
    ):
        raise RuntimeError("Installed Lens RAW lacks the v6 radiance-scene contract.")
    spec.update(
        name="dev25 registered object radiance to Lens x Sensor RAW",
        lens_revision_id=LENS_REVISION_ID,
        sensor_revision=_sensor(template),
        radiance_scene_id=scene["id"],
        sensor_size=[8, 8],
        exposure_s=0.004,
        noise_flag=0,
        seed=7,
        accept_approximate_lens_radiometry=True,
    )
    raw_job = _finished(client, client.submit_lens_sensor_raw(spec), "lens_sensor_raw")
    preview = raw_job["result"]["summary"]["preview"]
    raw = np.load(io.BytesIO(_artifact(transport, preview["raw_artifact"])), allow_pickle=False)
    if (
        raw.dtype != np.dtype("int32") or raw.shape != (8, 8)
        or int(raw.max()) <= int(raw.min())
        or preview["scene"]["kind"] != "registered_object_radiance"
        or preview["scene"]["source_asset"]["source_sha256"] != scene["source_sha256"]
        or preview["scene"]["luminance_cd_m2"] is not None
        or preview["scene"]["spectral_conversion"]
        != "linear_energy_density_then_energy_to_quanta_once"
        or preview["lens_model"]["psf_execution"]["mode"] != "strict_bounded_streaming"
        or preview["lens_model"]["diffraction_included"] is not False
        or raw.tolist() != preview["raw"]["values"]
    ):
        raise RuntimeError("Installed v6 RAW lost original scene, optics or readout evidence.")
    return {
        "scene_id": scene["id"],
        "scene_source_sha256": scene["source_sha256"],
        "raw_job_id": raw_job["id"],
        "raw_content_sha256": _sha256_array(raw),
        "raw_min_dn": int(raw.min()),
        "raw_max_dn": int(raw.max()),
        "lens_psf_kind": preview["lens_model"]["psf_kind"],
        "diffraction_included": preview["lens_model"]["diffraction_included"],
        "radiometry": preview["lens_model"]["absolute_radiometry"],
        "pixel_angular_coupling": "not_established",
    }


def _reject_overfield(client, transport):
    metadata = client.lens_sensor_radiance_scene_template()
    metadata.update(
        name="dev25 diagonal FOV preflight fixture",
        source_uri="fixture://dev25-square-scene-overfield",
        license="test-authored synthetic fixture",
        method_and_version="constant-object-radiance:1",
        horizontal_fov_deg=20.0,
        object_distance_m="infinity",
    )
    with tempfile.TemporaryDirectory(prefix="camera-dev25-overfield-") as directory:
        path = Path(directory) / "overfield.npy"
        np.save(path, np.ones((32, 32, 3), dtype=np.float32), allow_pickle=False)
        scene = client.import_lens_sensor_radiance_scene(metadata, path)
    template = client.lens_sensor_raw_template(scope="central_bounded_radiance_scene")
    spec = template["specification"]
    spec.update(
        lens_revision_id="444be61b-3bbb-51f9-938a-c4823e702d13",
        sensor_revision=_sensor(template),
        radiance_scene_id=scene["id"],
        sensor_size=[8, 8],
        accept_approximate_lens_radiometry=True,
    )
    before = [item["id"] for item in client.list_lens_sensor_raw()]
    rejected = transport.post("/api/v1/lens-sensor-raw", json=spec)
    after = [item["id"] for item in client.list_lens_sensor_raw()]
    if (
        rejected.status_code != 422
        or "diagonal FOV exceeds" not in rejected.text
        or after != before
    ):
        raise RuntimeError("Installed preflight queued a scene beyond the Lens diagonal FOV.")
    return {
        "scene_id": scene["id"],
        "horizontal_fov_deg": 20.0,
        "lens_field_limit_checked": "diagonal_fov",
        "job_queued": False,
    }


def _legacy_isp(client, transport):
    template = client.lens_sensor_raw_template(scope="central_bounded")
    spec = template["specification"]
    spec.update(
        name="dev25 D65 slanted edge for CameraE2E ISP",
        lens_revision_id=LENS_REVISION_ID,
        sensor_revision=_sensor(template),
        scene_kind="slanted_edge",
        sensor_size=[16, 16],
        exposure_s=0.004,
        noise_flag=0,
        seed=7,
        accept_approximate_lens_radiometry=True,
    )
    raw_job = _finished(client, client.submit_lens_sensor_raw(spec), "lens_sensor_raw")
    source = raw_job["result"]["summary"]["preview"]
    raw = np.load(io.BytesIO(_artifact(transport, source["raw_artifact"])), allow_pickle=False)
    isp_spec = client.lens_sensor_legacy_isp_template(
        source_lens_sensor_job_id=raw_job["id"]
    )["specification"]
    isp_spec.update(
        name="dev25 saved RAW CameraE2E spectral ISP",
        demosaic_method="adaptive_laplacian",
        acknowledge_model_derived_color_limits=True,
    )
    isp_job = _finished(
        client, client.submit_lens_sensor_legacy_isp(isp_spec), "lens_sensor_legacy_isp"
    )
    summary = client.lens_sensor_legacy_isp(isp_job["id"])["result"]["summary"]
    report = summary["report"]
    with np.load(io.BytesIO(_artifact(transport, summary["evidence"]["npz"])),
                 allow_pickle=False) as arrays:
        retained = np.asarray(arrays["source_raw_dn_frame"])
        rgb = np.asarray(arrays["srgb"])
    if (
        not np.array_equal(raw, retained)
        or summary["raw_content_sha256"] != _sha256_array(raw)
        or report["color_contract"]["measured_color_accuracy"] is not False
        or report["method_binding"]["demosaic_method"] != "adaptive_laplacian"
        or rgb.shape != (16, 16, 3) or not np.isfinite(rgb).all()
    ):
        raise RuntimeError("Installed CameraE2E ISP changed original RAW or color scope.")
    return {
        "source_raw_job_id": raw_job["id"],
        "isp_job_id": isp_job["id"],
        "original_raw_content_sha256": _sha256_array(raw),
        "isp_raw_content_sha256": summary["raw_content_sha256"],
        "demosaic_method": report["method_binding"]["demosaic_method"],
        "sensor_to_xyz": report["color_contract"]["sensor_to_xyz"],
        "measured_color_accuracy": False,
        "srgb_mean": np.mean(rgb, axis=(0, 1)).tolist(),
    }


def _db_wavefront(workspace):
    result = db_wavefront_preflight(
        "6c48e967-74b4-5901-8186-ba6fafe4797b",
        catalog_root=workspace / "camerae2e_db",
        reviewed_csv_sha256=(
            "f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd"
        ),
        aperture_hypothesis=APERTURE_HYPOTHESIS,
    )
    paraxial = result["paraxial_translation_diagnostic"]
    comparison = paraxial["comparison_to_saved_geometric_psf"]
    if (
        result["status"] != "blocked"
        or result["psf_request"] is not None
        or result["sequential_surface_count"] != 28
        or result["product_ranking_allowed"] is not False
        or len(result["blockers"]) != 1
        or result["blockers"][0]["code"] != "rear_angular_bound_exceeds_factory_domain"
        or paraxial["status"] != "computed_paraxial_only"
        or paraxial["not_diffraction"] is not True
        or abs(comparison["focal_length_delta_mm"]) > 1e-10
        or not 0.01 < comparison["entrance_pupil_radius_relative_delta"] < 0.02
    ):
        raise RuntimeError("Installed DB wavefront bridge overstated PSF readiness or lost parity.")
    return {
        "source": result["source"],
        "sequential_surface_count": result["sequential_surface_count"],
        "status": result["status"],
        "blocker": result["blockers"][0],
        "paraxial_translation_diagnostic": paraxial,
        "diffraction_psf_computed": False,
        "physical_validation": "not_established",
        "product_ranking_allowed": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package = _verify_wheel(args.installed_root, args.wheel)
    if package["version"] != "0.16.2.dev25" or args.state.exists():
        raise RuntimeError("A dev25 wheel and fresh isolated Registry are required.")
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start.")
        radiance = _radiance(client, transport)
        overfield = _reject_overfield(client, transport)
        color = _legacy_isp(client, transport)
        wavefront = _db_wavefront(args.workspace)
        if worker.poll() is not None:
            raise RuntimeError("Installed child Worker exited during the proof.")
        proof = {
            "schema": "camerae2e_installed_joint_dev25_radiance_isp_wavefront_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "registered_object_radiance_raw": radiance,
            "overfield_preflight": overfield,
            "saved_d65_raw_legacy_isp": color,
            "saved_lens_wavefront_preflight": wavefront,
            "physical_validation": "not_established",
            "product_ranking_allowed": False,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "version": package["version"],
        "wheel_sha256": package["wheel_sha256"],
        "radiance_raw_job_id": radiance["raw_job_id"],
        "legacy_isp_job_id": color["isp_job_id"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
