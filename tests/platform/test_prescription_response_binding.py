"""Cold archive checks for one prescription's radiometry, direction and PSF."""

import hashlib
import io
import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import lens_factory, prescription_psf, spatial
from camera_platform.finite_conjugate import application_conditions
from camera_platform.prescription_identity import sampling_independent_binding
from camera_platform.prescription_response import ArchivedPrescription
from camera_platform.registry import digest


def _archive(tmp_path):
    """Make a small, self-contained original-byte archive; no solver is invoked."""
    state = tmp_path / "state"
    blobs = state / "blobs"
    blobs.mkdir(parents=True)

    def blob(name, value, *, array=False):
        if array:
            stream = io.BytesIO()
            np.save(stream, np.asarray(value), allow_pickle=False)
            content = stream.getvalue()
        else:
            content = json.dumps(value, sort_keys=True).encode()
        checksum = hashlib.sha256(content).hexdigest()
        (blobs / checksum).write_bytes(content)
        return {"name": name, "sha256": checksum, "bytes": len(content)}

    psf_input = prescription_psf.validate_request(
        prescription_psf.request_template(version=5)
    )
    prescription = lens_factory.validate_request(psf_input["prescription"])
    wave = prescription["wavelength_nm"]
    knots = len(prescription["image_height_mm"])
    irradiance = [[0.003 / (1 + i), 0.002 / (1 + i), 0.001 / (1 + i)]
                  for i in range(len(wave))]
    radiometry = {
        "schema": "camera_lens_radiometry_v2",
        "wavelength_nm": wave,
        "image_height_mm": prescription["image_height_mm"],
        "irradiance_per_radiance_sr": irradiance,
        "prescription_sha256": digest(prescription),
        "stop_transmission_sha256": digest(prescription["stop_transmission"]),
        "stop_transmission": prescription["stop_transmission"],
        "object_conjugate": prescription["object_conjugate"],
    }
    ray_candidate = {
        "wavelength_nm": wave,
        "field_radius": prescription["field_radius"],
        "theta_deg": [0, 5],
        "angular_weights": [[[0.8, 0.2] for _ in range(knots)] for _ in wave],
        "relative_illumination": [[1, 0.8, 0.5] for _ in wave],
        "field_reference_radius_mm": prescription["image_height_mm"][-1],
        "radiometry": radiometry,
    }
    kernel = np.zeros((len(wave), 3, 3, 3, 3))
    kernel[..., 1, 1] = 1
    psf = deepcopy(spatial.request_template()["psf"])
    psf.update(
        schema="field_intensity_psf_v1",
        wavelength_nm=wave,
        values=kernel.tolist(),
        sample_pitch_um=3.0,
        spatial_model="field_grid",
        field_y_mm=psf_input["field_y_mm"],
        field_x_mm=psf_input["field_x_mm"],
        field_coordinates="sensor_center_yx",
        interpolation="bilinear_source_position",
    )
    psf = spatial.validate_psf(psf)
    f_number = {"value": 19.812067271756955, "unit": "1"}
    conditions = {
        "f_number": f_number,
        **application_conditions(prescription["object_conjugate"]),
        "exposure_s": {"value": 0.004, "unit": "s"},
        "gain": {"value": 1, "unit": "1"},
    }
    lens = {
        "id": "lens-1",
        "kind": "lens",
        "psf_factory_job_id": "psf-job-1",
        "factory_condition_scope": "optical_response",
        "factory_application_conditions": deepcopy(conditions),
        "properties": {
            "f_number": deepcopy(f_number),
            "psf_pupil_f_number": deepcopy(f_number),
            "focal_length": {"value": psf_input["reference_focal_length_mm"], "unit": "mm"},
            "psf_object_conjugate": psf_input["object_conjugate"],
            "object_conjugate": prescription["object_conjugate"],
        },
        "payload": {**ray_candidate, "psf": psf},
    }
    sensor = {
        "id": "sensor-1",
        "kind": "sensor",
        "payload": {
            "readout": spatial.request_template()["readout"],
            "pixel_pitch_um": 3.0,
            "wavelength_nm": wave,
            "qe": [[0.5, 0.5] for _ in range(3)],
            "sensor_angular_response": {
                "theta_deg": [0, 5],
                "relative_response": [[[1, 1], [1, 1]] for _ in range(3)],
            },
        },
    }
    image = np.full((8, 8, len(wave)), 1e-4)
    scene_image = blob("spectral-image.npy", image, array=True)
    record = {
        "id": "run-1",
        "plugin_id": "spatial_scene_v1",
        "status": "succeeded",
        "assembly_id": "assembly-1",
        "assembly_snapshot": {
            "id": "assembly-1",
            "conditions": deepcopy(conditions),
            "model_ids": ["lens-1", "sensor-1"],
        },
        "model_snapshots": [lens, sensor],
        "scene_snapshot": {
            "projection_lens_model_id": "lens-1",
            "shape": list(image.shape),
            "artifacts": [scene_image],
        },
        "request": {
            "lens": ray_candidate,
            "psf": psf,
            "readout": sensor["payload"]["readout"],
            "wavelength_nm": wave,
            "scene": {"spectral_irradiance": image.tolist()},
        },
        "artifacts": [
            blob("ideal_spectral_irradiance.npy", image, array=True),
            blob("psf_intensity.npy", kernel, array=True),
        ],
        "input_artifacts": [
            blob("factory-input.json", {
                "plugin_id": "rayoptics_lens_v1", "request": prescription, "asset_id": "asset-1"
            }),
            blob("factory-input.json", {
                "plugin_id": "prescription_psf_v1", "request": psf_input, "asset_id": "asset-1"
            }),
            blob("characterization.json", {
                "summary": {
                    "normalized_request": prescription,
                    "request_sha256": digest(prescription),
                    "status": "PROPOSED",
                    "irradiance_per_radiance_sr": irradiance,
                },
                "model_candidate": ray_candidate,
            }),
            blob("characterization.json", {
                "summary": {
                    "normalized_request": psf_input,
                    "request_sha256": digest(psf_input),
                    "status": "PROPOSED",
                    "prescription_sha256": digest(prescription),
                    "geometric_reference_f_number": f_number["value"],
                },
                "model_candidate": psf,
            }),
        ],
    }
    return record, state, blob


def _load(tmp_path, record, state):
    path = tmp_path / "run.json"
    content = json.dumps(record, sort_keys=True).encode()
    path.write_bytes(content)
    return ArchivedPrescription(
        path, state, expected_sha256=hashlib.sha256(content).hexdigest()
    )


def test_cold_archive_accepts_one_frozen_finite_optical_response(tmp_path):
    record, state, _ = _archive(tmp_path)
    archive = _load(tmp_path, record, state)
    assert archive.prescription["object_conjugate"]["distance_mm"] == 500
    archive.assert_unchanged()


def test_cold_archive_accepts_separately_sampled_finite_psf_with_original_hashes(tmp_path):
    record, state, blob = _archive(tmp_path)
    ray_input = json.loads(
        (state / "blobs" / record["input_artifacts"][0]["sha256"]).read_bytes()
    )
    psf_input = json.loads(
        (state / "blobs" / record["input_artifacts"][1]["sha256"]).read_bytes()
    )
    psf_input["request"]["prescription"]["sampling"].update(
        polar_rings=20, azimuth_samples=20
    )
    normalized = prescription_psf.validate_request(psf_input["request"])
    record["input_artifacts"][1] = blob("factory-input.json", psf_input)
    summary_artifact = record["input_artifacts"][3]
    characterization = json.loads(
        (state / "blobs" / summary_artifact["sha256"]).read_bytes()
    )
    characterization["summary"].update(
        normalized_request=normalized,
        request_sha256=digest(normalized),
        prescription_sha256=digest(normalized["prescription"]),
    )
    record["input_artifacts"][3] = blob("characterization.json", characterization)
    binding = sampling_independent_binding(
        ray_input["request"], normalized["prescription"]
    )
    record["model_snapshots"][0]["properties"]["physical_prescription_binding"] = binding
    archive = _load(tmp_path, record, state)
    assert archive.prescription_binding == binding
    archive.assert_unchanged()

    forged = deepcopy(record)
    forged["model_snapshots"][0]["properties"]["physical_prescription_binding"][
        "physical_prescription_sha256"
    ] = "0" * 64
    with pytest.raises(ValueError, match="physical-prescription binding"):
        _load(tmp_path, forged, state)


def test_optical_response_scope_keeps_readout_controls_independent(tmp_path):
    record, state, _ = _archive(tmp_path)
    record["assembly_snapshot"]["conditions"]["exposure_s"]["value"] = 0.008
    record["assembly_snapshot"]["conditions"]["gain"]["value"] = 2
    assert _load(tmp_path, record, state).prescription["schema"] == "lens_prescription_v5"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("throughput", "throughput"),
        ("relative_illumination", "RI"),
        ("angular_weights", "angle"),
        ("psf", "PSF"),
        ("object_distance", "conditions"),
        ("f_number", "aperture"),
    ],
)
def test_cold_archive_rejects_stale_optical_response_or_condition(
    tmp_path, change, message
):
    record, state, blob = _archive(tmp_path)
    lens = record["model_snapshots"][0]
    if change == "throughput":
        lens["payload"]["radiometry"]["irradiance_per_radiance_sr"][0][0] *= 0.5
        record["request"]["lens"] = deepcopy({k: v for k, v in lens["payload"].items()
                                               if k != "psf"})
    elif change in {"relative_illumination", "angular_weights"}:
        if change == "relative_illumination":
            lens["payload"][change][0][-1] *= 0.5
        else:
            lens["payload"][change][0][0][-1] *= 0.5
        record["request"]["lens"] = deepcopy({k: v for k, v in lens["payload"].items()
                                               if k != "psf"})
    elif change == "psf":
        values = lens["payload"]["psf"]["values"]
        values[0][1][1][1][1] = 0.9
        values[0][1][1][1][2] = 0.1
        record["request"]["psf"] = deepcopy(lens["payload"]["psf"])
        record["artifacts"][1] = blob("psf_intensity.npy", values, array=True)
    elif change == "object_distance":
        record["assembly_snapshot"]["conditions"]["object_distance_mm"]["value"] = 600
    elif change == "f_number":
        record["assembly_snapshot"]["conditions"]["f_number"]["value"] = 8
    with pytest.raises(ValueError, match=message):
        _load(tmp_path, record, state)
