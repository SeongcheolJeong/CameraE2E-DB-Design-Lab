"""Fresh DB optics/QE capture and exact electron-domain CCM composition."""

import hashlib
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import calibration_ccm as ccm
from camera_platform import calibration_db_color_engine as engine
from camera_platform import calibration_spectral as spectral
from camera_platform import electrical, reference_isp
from camera_platform.registry import canonical


@pytest.fixture
def prepared():
    readout = {
        "gain": 1,
        "full_well_e": 10000,
        "read_noise_e": 3,
        "dark_current_e_per_s": 1,
        "bit_depth": 12,
        "black_level_dn": 64,
    }
    cell = {
        "pixel_pitch_um": 2.9,
        "cfa_pattern": "RGGB",
        "readout": readout,
        "electrical_model": electrical.default_electrical_model(readout),
    }
    tuning = reference_isp.default_profile()
    tuning["demosaic_method"] = "bilinear"
    spec = {
        "frames": 8,
        "gain": 1,
        "seed": 7,
        "exposure_ms": 10,
        "luminance_cd_m2": 1000,
        "chart_suite": "classic_v1",
        "isp": tuning,
    }
    geometry = {
        "native_shape_row_col": [256, 256],
        "input_shape_row_col": [256, 256],
        "crop_bounds_row_col": [0, 256, 0, 256],
        "pixel_pitch_um": 2.9,
        "cfa_origin": {"pattern": "RGGB", "local_row": 0, "local_col": 0},
    }
    fields = [
        {
            "id": fid,
            "status": "succeeded",
            "geometry": deepcopy(geometry),
            "pupil_node_index": i,
            "azimuth_deg": 0,
            "normalized_radius": i * 0.85,
        }
        for i, fid in enumerate(engine.FIELDS)
    ]
    calibration = {"matrix": (np.eye(3) / 2000).tolist(), "method": "authored_baseline"}
    patch_values = np.random.default_rng(31).uniform(1e15, 1e16, (24, 21))
    source_spectra = {
        "patches": patch_values,
        "white": np.full(21, 1e16),
        "asset": "independent numerical fixture",
    }
    source_qe = np.column_stack(
        [np.linspace(0.15, 0.4, 21), np.full(21, 0.3), np.full(21, 0.3), np.linspace(0.4, 0.15, 21)]
    )
    candidate = {"id": "candidate-03", "source_identities": {}, "result": {"configuration": spec}}
    p = {
        "cell": cell,
        "spec": spec,
        "candidate": candidate,
        "calibration": calibration,
        "electrical_model": cell["electrical_model"],
        "optical": {
            "schema": "camera_reference_workshop_optics_v1",
            "revision": "test",
            "fields": fields,
        },
        "pixel": {
            "schema": "camera_design_workshop_absolute_qe_v1",
            "wavelength_nm": engine.WAVE.tolist(),
            "theta_deg": [0, 60],
            "eqe": np.stack([source_qe, source_qe]).tolist(),
        },
        "arrays": {
            "psf": np.ones((21, 2, 1, 1)),
            "conditional_weights": np.ones((21, 2, 1)),
            "directions_xyz": np.array([[[0, 0, 1]], [[0, 0, 1]]]),
            "irradiance_per_radiance_sr": np.full((21, 2), 0.02),
        },
        "dc": {
            fid: {
                "capture": {
                    "geometry": deepcopy(geometry),
                    "configuration": spec,
                    "calibration": calibration,
                }
            }
            for fid in engine.FIELDS
        },
        "spectral": source_spectra,
        "derived_files": {engine.SPECTRAL_FILE: b"{}"},
        "spectral_sha256": hashlib.sha256(b"{}").hexdigest(),
        "source_identity": {
            "run_id": "run-fixture",
            "candidate_id": "candidate-03",
            "condition_id": "condition-006",
            "manifest_sha256": "a" * 64,
        },
        "frozen_files": {"manifest.json": b"{}"},
        "manifest_bindings": {"": "a" * 64},
    }
    p["projection"] = engine.projection(p)
    return p


@pytest.fixture
def color_record():
    wave = engine.WAVE
    values = np.column_stack(
        [np.linspace(0.2, 0.65, 21), np.full(21, 0.5), np.linspace(0.55, 0.2, 21)]
    )
    conditions = {
        "camera_id": "different-synthetic-camera",
        "mode_id": "linear",
        "gain": 1,
        "temperature_c": 25,
    }
    scan = {
        "schema": "camera_qe_scan_v1",
        "wavelength_nm": wave.tolist(),
        "channels": ["R", "G", "B"],
        "mean_dn": (64 + 500 * values).tolist(),
        "dark_dn": np.full((21, 3), 64).tolist(),
        "dn_per_e": 0.5,
        "exposure_s": [0.01] * 21,
        "bandwidth_nm": 5,
        "adc_white_dn": 4095,
        "clipped_fraction": np.zeros((21, 3)).tolist(),
        "dark_clipped_fraction": np.zeros((21, 3)).tolist(),
        "reference_plane": "sensor_surface",
        "conditions": conditions,
        "source_kind": "synthetic",
        "provenance": {
            "calibration_id": "fixture",
            "session_id": "scan",
            "acquisition_id": "scan",
            "instrument_id": "synthetic",
        },
        "included_effects": ["cfa", "photodiode"],
        "incident_photons_per_pixel": [1000] * 21,
        "radiometry": {
            "kind": "absolute_calibrated",
            "calibration_id": "fixture",
            "unit": "photons/pixel",
        },
    }
    qe = spectral.derive_qe(scan)
    matrix = np.array([[1.2, -0.1, -0.05], [0.02, 0.9, 0.04], [0.03, 0.07, 1.1]])
    rows = []
    for seed in (1, 2):
        camera = np.random.default_rng(seed).uniform(0.05, 0.6, (24, 3))
        wb, scale = [1.1, 1, 1.2], 0.8
        rows.append(
            {
                "camera_rgb": camera.tolist(),
                "target_rgb": ((camera * wb * scale) @ matrix).tolist(),
                "target_space": "camera_linear_rgb",
                "white_balance": wb,
                "exposure_scale": scale,
                "session_id": str(seed),
                "acquisition_id": str(seed),
                "source_kind": "synthetic",
                "illuminant_id": "D65",
                "illuminant_spd_sha256": "a" * 64,
                "source_artifact_sha256": str(seed) * 64,
                "conditions": conditions,
                "whitepoint": "D65",
                "normalization": "fixed_reference_electrons",
                "input_domain": "camera_linear_rgb",
                "clipped_patch_indices": [],
                "normalization_metadata": {
                    "reference_white_e": 2000,
                    "dn_per_e": 0.5,
                    "black_level_dn": 64,
                    "formula": "(native_mean_dn-black_level_dn)/dn_per_e/reference_white_e",
                },
            }
        )
    fitted = ccm.fit_ccm(rows[0])
    validation = ccm.validate_ccm(fitted["profile"], {**rows[1], "thresholds": {"max_rmse": 1e-10}})
    return {
        "schema": "camera_spectral_color_record_v1",
        "id": "color-calibration-fixture",
        "content_hash": "b" * 64,
        "status": "pass",
        "source_kind": "synthetic",
        "qe": {"profile": qe},
        "ccm": {"profile": fitted["profile"], "validation": validation},
        "raw_validation": {"candidate": {"status": "pass"}},
    }


def test_binding_replaces_absolute_qe_and_composes_exact_row_matrix(prepared, color_record):
    result = engine.binding(prepared, color_record)
    applied = np.asarray(result["applied_response"]["qe_rggb"])
    np.testing.assert_array_equal(applied[:, 1], applied[:, 2])
    assert result["applied_response"]["application"] == "absolute_replacement"
    profile = color_record["ccm"]["profile"]
    electrons = np.array([[100, 500, 800], [900, 400, 200]])
    expected = (
        electrons / 2000 * profile["white_balance"] * profile["exposure_scale"]
    ) @ np.asarray(profile["matrix"])
    actual = electrons @ np.asarray(result["isp_adapter"]["electron_to_output_matrix"])
    np.testing.assert_allclose(actual, expected, atol=1e-15)
    assert result["db_readout"]["dn_per_e"] != profile["normalization_metadata"]["dn_per_e"]
    assert result["independent_db_validation"] == "not_established"


@pytest.mark.parametrize(
    "mutation", ["hash", "measured", "adc", "gain", "narrow_band", "normalization"]
)
def test_incompatible_profiles_refused(prepared, color_record, mutation):
    record = deepcopy(color_record)
    if mutation == "hash":
        record["ccm"]["profile"]["matrix"][0][0] += 0.1
    elif mutation == "measured":
        record["source_kind"] = "measured"
    elif mutation == "adc":
        prepared["cell"]["electrical_model"]["adc"]["bit_depth"] = 10
    elif mutation == "gain":
        prepared["spec"]["gain"] = 2
    elif mutation == "narrow_band":
        profile = record["qe"]["profile"]
        profile["wavelength_nm"] = profile["wavelength_nm"][1:]
        profile["qe"] = profile["qe"][1:]
        profile["bandwidth_nm"] = profile["bandwidth_nm"][1:]
        profile["profile_sha256"] = spectral.profile_sha256(profile)
    else:
        profile = record["ccm"]["profile"]
        profile["normalization_metadata"]["formula"] = "arbitrary scale"
        profile["profile_sha256"] = ccm._hash(
            {k: v for k, v in profile.items() if k != "profile_sha256"}
        )
        record["ccm"]["validation"]["profile_sha256"] = profile["profile_sha256"]
    with pytest.raises(ValueError):
        engine.binding(prepared, record)


def test_prepare_pins_generated_spectra_and_rejects_drift(prepared, monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "prepare_optical_source", lambda *a: deepcopy(prepared))
    monkeypatch.setattr(engine, "_spectra", lambda *a: deepcopy(prepared["spectral"]))
    result = engine.prepare(
        tmp_path, tmp_path, None, "candidate-03", expected_manifest_sha256="a" * 64
    )
    encoded = result["derived_files"][engine.SPECTRAL_FILE]
    assert hashlib.sha256(encoded).hexdigest() == result["spectral_sha256"]
    path = tmp_path / engine.SPECTRAL_FILE
    path.parent.mkdir()
    path.write_bytes(encoded + b" ")
    with pytest.raises(ValueError, match="Frozen DB chart spectra differ"):
        engine.prepare(tmp_path, tmp_path, None, "candidate-03", expected_manifest_sha256="a" * 64)


def test_real_optical_capture_keeps_raw_shared_and_ccm_spaces_separate(
    prepared, color_record, tmp_path
):
    progress = []
    before = canonical(color_record)
    result = engine.execute(prepared, color_record, tmp_path / "run", progress.append)
    assert result["fresh_raw_frames"] == 32 and result["saved_electron_rates_reused"] is False
    assert len(result["images"]) == 8 and len(result["comparisons"]) == 6
    assert result["physical_validation"] == result["independent_db_validation"] == "not_established"
    assert progress[-1]["fraction"] == 1
    assert canonical(color_record) == before
    for field in result["fields"]:
        variants = {v["variant"]: v for v in field["variants"]}
        assert field["raw_identity"]["qe_variants_identical"] is True
        assert (
            variants["db_baseline"]["raw"]["frames"][0]["sha256"]
            != variants["qe_only_db_isp"]["raw"]["frames"][0]["sha256"]
        )
        assert variants["qe_ccm_baseline"]["raw"] == variants["qe_ccm_fitted"]["raw"]
        assert variants["qe_ccm_baseline"]["applied_isp"]["tuning"]["wb"] == [1, 1, 1]
        assert (
            variants["qe_ccm_fitted"]["output_space"] == "imported_camera_linear_rgb_pre_clipping"
        )
        raw = np.load(tmp_path / "run" / variants["qe_ccm_fitted"]["raw"]["frames"][0]["path"])
        profile = color_record["ccm"]["profile"]
        tuning = deepcopy(prepared["spec"]["isp"])
        tuning["wb"] = (np.asarray(profile["white_balance"]) * profile["exposure_scale"]).tolist()
        tuning["ccm"] = np.asarray(profile["matrix"]).T.tolist()
        expected = reference_isp.process(
            raw, result["binding"]["db_readout"], np.eye(3) / 2000, tuning
        )[0]
        actual = np.load(tmp_path / "run" / variants["qe_ccm_fitted"]["images"]["linear"])
        np.testing.assert_allclose(actual, expected, atol=1e-14)
    for name, checksum in result["files"].items():
        assert hashlib.sha256((tmp_path / "run" / name).read_bytes()).hexdigest() == checksum
    assert all(
        c["comparison_type"] == "change_only_no_reference_observation"
        for c in result["comparisons"]
    )


def test_cancellation_interrupts_before_optical_work(prepared, color_record, tmp_path):
    def cancel(_):
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        engine.execute(prepared, color_record, tmp_path / "cancelled", cancel)
    assert not list((tmp_path / "cancelled").rglob("raw-*.npy"))
