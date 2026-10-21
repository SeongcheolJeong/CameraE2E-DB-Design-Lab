"""A pinned sensor model produces bounded, attributable quality evidence."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

import camera_platform.saved_sensor_quality as quality_module
import pyisetcam.system_faca as system_faca
from camera_platform.saved_sensor_quality import evaluate_saved_sensor_quality
from pyisetcam.image_sensor_db import image_sensor_db_config, image_sensor_db_get
from pyisetcam.physical_estimate import image_sensor_physical_model
from pyisetcam.sensor_estimate import image_sensor_db_model
from pyisetcam.sensor_fdtd_comparison import sensor_pupil_directions
from pyisetcam.sensor_readout_profiles import resolve_sensor_readout
from pyisetcam.sensor_simulation_db import (
    _connect,
    _digest,
    _store_result,
    sensor_simulation_db_path,
)

CODE = "DEF-1801-802"
WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.fixture
def saved_run(tmp_path):
    record = image_sensor_db_get(CODE)
    model = image_sensor_physical_model(CODE)
    profile = resolve_sensor_readout(CODE)
    configuration = image_sensor_db_config(CODE)
    configuration["scenario"]["sensor_illumination"] = sensor_pupil_directions(
        0.0, 0.0, 4.0, radial_samples=6, azimuth_samples=16
    )
    configuration["scenario"]["image_sensor_db"]["illumination_assumption"] = "explicit"
    configuration["scenario"].update(sensor_resize=False, sensor_output_mode="digital_raw")
    configuration["scenario"]["sensor"].update(
        integration_time=0.004, noise_flag=2, fixed_pattern_seed=17
    )
    result = {
        "metadata": {
            "status": "simulated",
            "model_kind": model["kind"],
            "config": configuration,
            "model_snapshot": model,
            "readout_profile": profile,
            "checks": {"numerical_passed": True},
            "source_scope_policy": {"action": "include"},
            "simulation_settings": {"fnumber": 4.0, "focal_length_m": 0.004},
            "isp": {"available": True},
            "native_cfa": {
                "filter_names": model["filter_names"],
                "pattern": [[1, 2], [3, 4]],
                "pattern_index_base": 1,
                "pixel_pitch_um": model["pixel_pitch_um"],
            },
        },
        "arrays": {"spectral_eqe": np.asarray(model["eqe"])},
    }
    db = sensor_simulation_db_path(tmp_path)
    manifest = {"source": "saved quality test", "files": {}, "settings": {}}
    fingerprint = _digest(manifest)
    (db.parent / "inputs").mkdir(parents=True)
    (db.parent / "inputs" / f"{fingerprint}.json").write_text(json.dumps(manifest))
    with _connect(db, write=True) as conn:
        _store_result(conn, db, record, result, run_id="fixed_run", input_digest=fingerprint)
    return tmp_path, db, record, result, fingerprint


def test_saved_run_quality_uses_pinned_model_and_retains_raw_metrics(
    saved_run, tmp_path, monkeypatch
):
    root, _, _, _, fingerprint = saved_run

    def forbid_rebuild(*_args, **_kwargs):
        raise AssertionError("Catalog model was rebuilt instead of using the saved snapshot")

    monkeypatch.setattr(system_faca, "image_sensor_db_model", forbid_rebuild)
    result = evaluate_saved_sensor_quality(
        CODE,
        run_id="fixed_run",
        root=root,
        workspace=WORKSPACE,
        output_dir=tmp_path / "evidence",
        frames=2,
        output_size=(32, 32),
        luminance_cd_m2=1000.0,
        seed=3,
    )
    assert result["schema"] == "camerae2e_saved_sensor_quality_v1"
    assert result["revision"]["run_id"] == "fixed_run"
    assert result["source_boundary"]["saved_sensor_simulation_run_used"] is True
    assert result["source_boundary"]["sensor_model_kind"] == "physical_estimate_v1"
    assert result["source_boundary"]["input_manifest_sha256"] == fingerprint
    assert len(result["source_boundary"]["saved_arrays_sha256"]) == 64
    assert result["controls"]["saved_fixed_pattern_seed"] == 17
    assert result["controls"]["readout_overrides"] == {}
    assert result["controls"]["fnumber"] == 4.0
    assert result["controls"]["focal_length_m"] == 0.004
    assert [case["scene"] for case in result["cases"]] == [
        "uniform", "slanted_edge", "macbeth_d65"
    ]
    assert all(case["metrics"]["native_raw_snr_db"]["available"] for case in result["cases"])
    assert result["cases"][1]["metrics"]["edge_mtf50"]["available"]
    color = result["cases"][2]["metrics"]["macbeth_cie76_mean"]
    assert color["available"]
    assert color["independent_color_accuracy"] is False
    assert color["evaluation_contract"] == "in_sample_calibration_fit_residual"
    assert result["method"]["color_evaluation"]["held_out_surfaces"] is False
    assert result["method"]["color_evaluation"]["calibration"]["sensor_response"] == (
        "saved physical_estimate_v1 spectral QE from pinned sensor-simulation run"
    )
    assert result["method"]["color_evaluation"]["calibration"]["saved_run_id"] == "fixed_run"
    assert result["sample_count"] == 6
    assert result["optical_solve_count"] == 3
    assert result["cases"][0]["frame_seeds"] != result["cases"][1]["frame_seeds"]
    report_path = Path(result["artifacts"]["json"])
    array_path = Path(result["artifacts"]["npz"])
    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == result["artifact_sha256"]["json"]
    assert hashlib.sha256(array_path.read_bytes()).hexdigest() == result["artifact_sha256"]["npz"]
    source = json.loads(report_path.read_text())
    assert source["revision"] == result["revision"]
    assert source["artifacts"]["npz_sha256"] == result["artifact_sha256"]["npz"]
    assert str(tmp_path) not in json.dumps(source["artifacts"])
    with np.load(array_path, allow_pickle=False) as arrays:
        for case in result["cases"]:
            prefix = case["array_prefix"]
            assert arrays[f"{prefix}_raw_dn_frame"].dtype == np.int32
            assert arrays[f"{prefix}_raw_dn_frame"].shape == (36, 36)
            assert float(np.mean(arrays[f"{prefix}_raw_dn_variance"])) > 0
            assert arrays[f"{prefix}_rgb_mean"].shape == (32, 32, 3)


def test_saved_run_quality_rejects_changed_config_hash_and_existing_evidence(saved_run, tmp_path):
    root, db, _, _, _ = saved_run
    destination = tmp_path / "keep"
    destination.mkdir()
    (destination / "evidence.txt").write_text("retain")
    with pytest.raises(FileExistsError, match="not empty"):
        evaluate_saved_sensor_quality(
            CODE, run_id="fixed_run", root=root, output_dir=destination
        )
    assert (destination / "evidence.txt").read_text() == "retain"
    with _connect(db, write=True) as conn:
        conn.execute("UPDATE runs SET config_sha256=? WHERE run_id=?", ("0" * 64, "fixed_run"))
    with pytest.raises(ValueError, match="config hash mismatched"):
        evaluate_saved_sensor_quality(
            CODE, run_id="fixed_run", root=root, output_dir=tmp_path / "untouched"
        )
    assert not (tmp_path / "untouched").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": ""},
        {"frames": 1},
        {"frames": 9},
        {"scenes": ("moving",)},
        {"output_size": (8, 8)},
        {"luminance_cd_m2": float("nan")},
        {"luminance_cd_m2": 10**1000},
    ],
)
def test_saved_run_quality_rejects_unbounded_inputs(saved_run, tmp_path, change):
    root, _, _, _, _ = saved_run
    options = {"run_id": "fixed_run", "root": root, "output_dir": tmp_path / "evidence"}
    options.update(change)
    with pytest.raises(ValueError):
        evaluate_saved_sensor_quality(CODE, **options)


def test_saved_run_quality_requires_matching_fixed_optics(saved_run, tmp_path):
    root, db, record, result, fingerprint = saved_run
    changed = copy.deepcopy(result)
    changed["metadata"]["simulation_settings"]["fnumber"] = 8.0
    with _connect(db, write=True) as conn:
        _store_result(conn, db, record, changed, run_id="wrong_optics", input_digest=fingerprint)
    with pytest.raises(ValueError, match="fixed F/4, 4 mm optics"):
        evaluate_saved_sensor_quality(
            CODE, run_id="wrong_optics", root=root, output_dir=tmp_path / "wrong"
        )


def test_saved_run_quality_marks_clipped_snr_diagnostic(saved_run, tmp_path):
    root, _, _, _, _ = saved_run
    result = evaluate_saved_sensor_quality(
        CODE,
        run_id="fixed_run",
        root=root,
        workspace=WORKSPACE,
        output_dir=tmp_path / "bright",
        scenes=("uniform",),
        frames=2,
        output_size=(16, 16),
        luminance_cd_m2=10000.0,
        exposure_s=1.0,
    )
    metrics = result["cases"][0]["metrics"]
    assert metrics["saturation_fractions"]["well_fraction"] > 0.001
    assert metrics["native_raw_snr_db"]["ranking_eligible"] is False
    assert metrics["native_raw_snr_db"]["interpretation"] == "Clipped capture: diagnostic SNR only."


def test_saved_db_estimate_is_used_without_catalog_rebuild(saved_run, tmp_path, monkeypatch):
    root, db, record, result, fingerprint = saved_run
    changed = copy.deepcopy(result)
    model = image_sensor_db_model(CODE)
    configuration = image_sensor_db_config(CODE, model="db_estimate_v1")
    configuration["scenario"].update(sensor_resize=False, sensor_output_mode="digital_raw")
    configuration["scenario"]["sensor"].update(
        integration_time=0.004, noise_flag=2, fixed_pattern_seed=17
    )
    changed["metadata"].update(
        model_kind="db_estimate_v1",
        model_snapshot=model,
        config=configuration,
    )
    changed["metadata"]["native_cfa"]["filter_names"] = model["filter_names"]
    with _connect(db, write=True) as conn:
        _store_result(
            conn, db, record, changed, run_id="saved_db_estimate", input_digest=fingerprint
        )

    def forbid_rebuild(*_args, **_kwargs):
        raise AssertionError("Fresh catalog estimate was used during saved-run evaluation")

    monkeypatch.setattr(system_faca, "image_sensor_db_model", forbid_rebuild)
    evaluated = evaluate_saved_sensor_quality(
        CODE,
        run_id="saved_db_estimate",
        root=root,
        workspace=WORKSPACE,
        output_dir=tmp_path / "saved-db-quality",
        scenes=("uniform",),
        frames=2,
        output_size=(16, 16),
    )
    assert evaluated["source_boundary"]["sensor_model_kind"] == "db_estimate_v1"
    assert evaluated["method"]["color_evaluation"]["calibration"]["sensor_response"] == (
        "saved db_estimate_v1 spectral QE from pinned sensor-simulation run"
    )
    assert evaluated["cases"][0]["metrics"]["native_raw_snr_db"]["available"]


def test_saved_run_hdr_chart_reports_single_exposure_native_strip_response(
    saved_run, tmp_path, monkeypatch
):
    root, _, _, _, _ = saved_run

    def forbid_rebuild(*_args, **_kwargs):
        raise AssertionError("HDR chart evaluation rebuilt the saved sensor model")

    monkeypatch.setattr(system_faca, "image_sensor_db_model", forbid_rebuild)
    evaluated = evaluate_saved_sensor_quality(
        CODE,
        run_id="fixed_run",
        root=root,
        workspace=WORKSPACE,
        output_dir=tmp_path / "hdr-evidence",
        scenes=("hdr_chart_d65",),
        frames=2,
        output_size=(32, 32),
        luminance_cd_m2=1000.0,
        exposure_s=0.004,
        seed=11,
    )
    assert evaluated["source_boundary"]["saved_sensor_simulation_run_used"] is True
    assert evaluated["optical_solve_count"] == 1
    case = evaluated["cases"][0]
    contract = case["scene_contract"]
    assert contract["generator"] == {
        "api": "pyisetcam.scene_create",
        "name": "hdr chart",
        "args": [1000.0, 6, 8],
        "illuminant": "D65",
        "background": "chart_darkest_strip_mean_spectrum",
        "scene_to_crop_scale": 1.2,
    }
    assert contract["scene_horizontal_fov_deg"] == evaluated["geometry"][
        "scene_horizontal_fov_deg"
    ]
    assert contract["chart_size"] == [48, 48]
    response = case["metrics"]["hdr_chart_response"]
    assert response["contract"] == "single_exposure_static_d65_strip_chart_response"
    assert response["hdr_fusion_performed"] is False
    assert response["lfm_evaluated"] is False
    assert response["sampled_well_clip_fraction"]["available"] is False
    levels = response["levels_bright_to_dark"]
    assert len(levels) == 6
    luminance_ratio = levels[0]["scene_luminance_cd_m2"] / levels[-1]["scene_luminance_cd_m2"]
    assert luminance_ratio == pytest.approx(
        1000.0, rel=0.01
    )
    assert all(
        first["mean_photoelectrons_per_pixel"] > second["mean_photoelectrons_per_pixel"]
        for first, second in zip(levels, levels[1:], strict=False)
    )
    for source, level in zip(contract["levels"], levels, strict=True):
        assert source["native_roi_half_open"] == level["native_roi_half_open"]
        y0, x0, y1, x1 = level["native_roi_half_open"]
        assert 0 <= y0 < y1 <= case["native_size"][0]
        assert 0 <= x0 < x1 <= case["native_size"][1]
        assert level["native_pixel_count"] == (y1 - y0) * (x1 - x0)
        assert 0 <= level["sampled_adc_white_fraction"] <= 1
        assert 0 <= level["sampled_adc_zero_fraction"] <= 1
    with np.load(evaluated["artifacts"]["npz"], allow_pickle=False) as arrays:
        frames = arrays["case_000_hdr_chart_raw_dn_frames"]
        assert frames.shape == (2, 36, 36)
        assert frames.dtype == np.int32
        assert arrays["case_000_hdr_chart_mean_photoelectrons_per_pixel"].shape == (6,)
        assert arrays["case_000_hdr_chart_mean_reference_dn"].shape == (6,)
        assert arrays["case_000_hdr_chart_mean_raw_dn"].shape == (6,)
        assert arrays["case_000_hdr_chart_sampled_adc_white_fraction"].shape == (6,)
        white_dn = 2 ** case["readout"]["bit_depth"] - 1
        for level in levels:
            y0, x0, y1, x1 = level["native_roi_half_open"]
            region = frames[:, y0:y1, x0:x1]
            assert level["mean_raw_dn"] == pytest.approx(float(np.mean(region)))
            assert level["sampled_adc_white_fraction"] == pytest.approx(
                float(np.mean(region == white_dn))
            )


def test_saved_run_hdr_chart_rejects_unresolved_strip_geometry(saved_run, tmp_path):
    root, _, _, _, _ = saved_run
    with pytest.raises(ValueError, match="at least 32 output pixels"):
        evaluate_saved_sensor_quality(
            CODE,
            run_id="fixed_run",
            root=root,
            output_dir=tmp_path / "too-small",
            scenes=("hdr_chart_d65",),
            output_size=(16, 32),
        )
    assert not (tmp_path / "too-small").exists()


def test_saved_quality_rejects_asset_change_before_writing_artifacts(
    saved_run, tmp_path, monkeypatch
):
    root, _, _, _, _ = saved_run
    real_identity = quality_module.offline_asset_identity
    calls = 0

    def changed_identity(*args, **kwargs):
        nonlocal calls
        calls += 1
        identity = real_identity(*args, **kwargs)
        if calls == 2:
            return {**identity, "upstream_revision": "changed"}
        return identity

    monkeypatch.setattr(quality_module, "offline_asset_identity", changed_identity)
    destination = tmp_path / "asset-changed"
    with pytest.raises(ValueError, match="upstream assets changed"):
        evaluate_saved_sensor_quality(
            CODE,
            run_id="fixed_run",
            root=root,
            workspace=WORKSPACE,
            output_dir=destination,
            scenes=("uniform",),
            frames=2,
            output_size=(16, 16),
        )
    assert calls == 2
    assert not destination.exists()
