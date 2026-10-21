"""Fitting, fixed-profile replay and independent-holdout invariants."""

import hashlib
import json

import numpy as np
import pytest
import tifffile
from camera_platform.esfr_engine import bilinear_isp, run, simulate_edge


def make_fixture(
    tmp_path,
    validation_sigma=1.25,
    transfer="linear",
    registered=True,
    angles=(5, 85),
    ellipse=None,
):
    shape = (176, 16 + 160 * len(angles))
    levels = {
        channel: {"low_dn": 600.0, "high_dn": 2800.0, "black_dn": 64.0, "white_dn": 4095.0}
        for channel in ("R", "Gr", "Gb", "B")
    }
    rois = [
        {
            "id": f"edge-{i}",
            "kind": "edge",
            "field_id": "center",
            "box": [24, 16 + i * 160, 128, 128],
        }
        for i in range(len(angles))
    ]
    captures = []
    for index in range(4):
        role = "development" if index < 2 else "validation"
        sigma = 1.25 if role == "development" else validation_sigma
        profile = {"fields": {"center": {"sigma_x_px": sigma, "sigma_y_px": sigma}}}
        if ellipse is not None:
            profile["fields"]["center"] = {
                "sigma_x_px": ellipse[0],
                "sigma_y_px": ellipse[1],
                "theta_deg": ellipse[2],
            }
        raw = np.full(shape, 1500, dtype=np.uint16)
        for angle, roi in zip(angles, rois, strict=True):
            normal = [-np.sin(np.deg2rad(angle)), np.cos(np.deg2rad(angle))]
            geometry = {
                "field_id": "center",
                "normal_xy": normal,
                "offset_px": normal[0] * 63.3 + normal[1] * 63.7,
            }
            row, col, height, width = roi["box"]
            frame = simulate_edge(profile, geometry, levels, (height, width), "RGGB", (row, col))
            raw[row : row + height, col : col + width] = frame["raw"]
        rng = np.random.default_rng(300 + index)
        raw = np.rint(np.clip(raw.astype(float) + rng.normal(0, 1, shape), 0, 4095)).astype(
            np.uint16
        )
        rgb = bilinear_isp(raw, "RGGB", (0, 0), levels)
        raw_path, isp_path = tmp_path / f"capture-{index}.bin", tmp_path / f"capture-{index}.tiff"
        raw.astype("<u2").tofile(raw_path)
        tifffile.imwrite(isp_path, np.uint16(np.clip(rgb, 0, 1) * 65535), photometric="rgb")
        metadata = {
            "camera_id": "synthetic-camera",
            "source_kind": "synthetic",
            "role": role,
            "conditions": {"light": "fixed", "exposure_s": 0.01},
            "raw_format": {
                "packing": "unpacked16",
                "width": shape[1],
                "height": shape[0],
                "stride_bytes": shape[1] * 2,
                "header_bytes": 0,
                "byte_order": "little",
                "bit_depth": 12,
                "bit_shift": 0,
                "cfa": "RGGB",
                "black_level_dn": 64,
                "white_level_dn": 4095,
            },
            "geometry": {"full_sensor_shape": list(shape), "crop_origin": [0, 0]},
            "isp": {
                "transfer": transfer,
                "coordinate_mapping": "same_native_raster" if registered else "unregistered",
            },
        }
        captures.append(({"id": f"capture-{index}", "metadata": metadata}, raw_path, isp_path))
    job = {
        "development_ids": ["capture-0", "capture-1"],
        "validation_ids": ["capture-2", "capture-3"],
        "specification": {
            "rois": rois,
            "fit": True,
            "baseline_sigma_native_px": 2.4,
            "thresholds": {"raw_sfr_rmse": 0.02, "isp_sfr_rmse": 0.02},
        },
    }
    return job, captures


def test_known_blur_recovery_independent_curves_and_replay(tmp_path):
    job, captures = make_fixture(tmp_path)
    result = run(job, captures, tmp_path / "report")
    assert result["fit"]["status"] == "fitted"
    assert abs(result["candidate_profile"]["fields"]["center"]["sigma_x_px"] - 1.25) < 0.06
    assert result["fit"]["candidate_frozen_before_holdout"] is True
    assert result["physical_validation"] == "not_established"
    for stage in ("raw", "isp"):
        metrics = result["metrics"][f"{stage}_sfr_rmse"]
        assert metrics["after"] < 0.02
        assert metrics["after"] < metrics["before"] * 0.15
        assert metrics["count"] == 4
    assert result["validation"]["status"] == "pass"
    profile = result["candidate_profile"]
    frozen = (tmp_path / "report/frozen-candidate-before-validation.json").read_bytes()
    assert hashlib.sha256(frozen).hexdigest() == result["candidate_profile_sha256"]
    assert json.loads(frozen) == profile
    nuisance = profile["geometry"]["roi_nuisance"]["edge-0"]
    replay = simulate_edge(
        profile,
        nuisance["geometry"],
        nuisance["levels"],
        nuisance["shape"],
        "RGGB",
        nuisance["geometry"]["crop_origin"],
    )
    comparison = next(
        x
        for x in result["comparisons"]
        if x["capture_id"] == "capture-2" and x["roi_id"] == "edge-0"
    )
    saved = np.load(tmp_path / "report" / comparison["images"]["after"]["raw_array"])
    assert np.array_equal(replay["raw"], saved)
    assert replay["raw"].dtype == np.uint16
    assert result["observations"][0]["rois"][0]["raw"]["Gr"]["frequency_cy_native_px"][-1] == 0.25
    json.dumps(result, allow_nan=False)


def test_holdout_changed_blur_cannot_change_candidate_and_failure_is_visible(tmp_path):
    base = tmp_path / "base"
    changed = tmp_path / "changed"
    base.mkdir()
    changed.mkdir()
    first_job, first_captures = make_fixture(base)
    other_job, other_captures = make_fixture(changed, validation_sigma=2.7)
    first = run(first_job, first_captures, base / "report")
    other = run(other_job, other_captures, changed / "report")
    assert first["candidate_profile"] == other["candidate_profile"]
    assert first["candidate_profile_sha256"] == other["candidate_profile_sha256"]
    assert first["metrics"]["raw_sfr_rmse"]["after"] < other["metrics"]["raw_sfr_rmse"]["after"]
    assert other["validation"]["status"] == "fail"
    assert any(x["status"] == "fail" for x in other["validation"]["checks"])


def test_unknown_isp_transfer_blocks_linear_comparison(tmp_path):
    job, captures = make_fixture(tmp_path, transfer="unknown")
    result = run(job, captures, tmp_path / "report")
    assert result["fit"]["status"] == "fitted"
    assert result["metrics"]["isp_sfr_rmse"]["after"] is None
    assert result["metrics"]["isp_sfr_rmse"]["unavailable_count"] == 4
    assert result["validation"]["status"] == "inconclusive"
    assert (
        result["observations"][0]["rois"][0]["isp_interpretation"]
        == "nonlinear_code_value_sfr_only"
    )


def test_unregistered_isp_does_not_get_implicitly_cropped(tmp_path):
    job, captures = make_fixture(tmp_path, registered=False)
    result = run(job, captures, tmp_path / "report")
    assert result["metrics"]["raw_sfr_rmse"]["after"] < 0.02
    assert result["metrics"]["isp_sfr_rmse"]["after"] is None
    assert (
        result["observations"][0]["rois"][0]["isp"]["Y"]["reason"] == "isp_coordinates_unregistered"
    )


def test_no_thresholds_are_not_an_implicit_camera_pass(tmp_path):
    job, captures = make_fixture(tmp_path)
    job["specification"]["thresholds"] = {}
    job["specification"]["fit"] = False
    result = run(job, captures, tmp_path / "report")
    assert result["fit"]["status"] == "no_fit"
    assert result["candidate_profile"]["fields"] == {}
    assert result["validation"]["status"] == "inconclusive"


def test_three_independent_directions_support_positive_definite_covariance(tmp_path):
    job, captures = make_fixture(tmp_path, angles=(5, 40, 85), ellipse=(1.9, 0.8, 26))
    result = run(job, captures, tmp_path / "report")
    field = result["candidate_profile"]["fields"]["center"]
    assert field["model"] == "elliptical_effective_gaussian"
    assert result["fit"]["fields"][0]["covariance_orientation_supported"] is True

    def covariance(sx, sy, angle):
        angle = np.deg2rad(angle)
        rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        return rotation @ np.diag([sx * sx, sy * sy]) @ rotation.T

    assert (
        np.max(
            np.abs(
                covariance(field["sigma_x_px"], field["sigma_y_px"], field["theta_deg"])
                - covariance(1.9, 0.8, 26)
            )
        )
        < 0.15
    )
    assert result["metrics"]["raw_sfr_rmse"]["after"] < 0.012
    assert result["metrics"]["isp_sfr_rmse"]["after"] < 0.012


def test_temporal_patch_variance_is_separate_from_spatial_variation(tmp_path):
    job, captures = make_fixture(tmp_path)
    job["specification"]["rois"].append(
        {"id": "gray-0", "kind": "gray", "field_id": "patch", "box": [0, 0, 16, 16]}
    )
    result = run(job, captures, tmp_path / "report")
    temporal = result["temporal_patch_noise"]
    assert len(temporal) == 8
    assert all(x["frame_count"] == 2 and x["status"] == "ok" for x in temporal)
    assert all(0.5 < x["temporal_variance_dn2"] < 1.8 for x in temporal)
    assert all(x["mean_signal_dn"] > 1400 and x["temporal_snr"] > 1000 for x in temporal)
    assert all(x["temporal_snr_db"] > 60 for x in temporal)
    patch = result["observations"][0]["rois"][-1]
    assert "spatial_variance" in patch["raw"]["Gr"]
    assert patch["absolute_reference_status"] == "not_established"


def test_frozen_profile_replay_cannot_refit_geometry_levels_or_blur(tmp_path, monkeypatch):
    job, captures = make_fixture(tmp_path)
    fitted = run(job, captures, tmp_path / "fit")
    job["frozen_profile_snapshot"] = fitted["candidate_profile"]
    import camera_platform.esfr_engine as engine

    def forbidden(*args, **kwargs):
        raise AssertionError("A frozen replay must never fit or derive nuisance geometry")

    monkeypatch.setattr(engine, "minimize_scalar", forbidden)
    monkeypatch.setattr(engine, "_geometry", forbidden)
    replay = run(job, captures, tmp_path / "replay")
    assert replay["candidate_profile"] == fitted["candidate_profile"]
    assert replay["candidate_profile_sha256"] == fitted["candidate_profile_sha256"]
    assert replay["fit"]["status"] == "frozen_replay"
    assert replay["fit"]["changed_parameters"] == []
    assert replay["metrics"] == fitted["metrics"]
    before = fitted["comparisons"][0]["images"]["after"]["raw_array"]
    after = replay["comparisons"][0]["images"]["after"]["raw_array"]
    assert np.array_equal(np.load(tmp_path / "fit" / before), np.load(tmp_path / "replay" / after))
    job["specification"]["rois"][0]["box"][0] += 1
    with pytest.raises(ValueError, match="ROI coordinates"):
        run(job, captures, tmp_path / "bad-replay")


def test_lateral_ca_uses_native_coordinates_and_common_green_normal():
    from camera_platform.esfr_analysis import edge_metrics
    from camera_platform.esfr_engine import _lateral_ca
    from scipy.special import ndtr

    shifts = {"R": 1.2, "Gr": 0.0, "Gb": 0.15, "B": -1.0}
    sites = {"R": (0, 0), "Gr": (0, 1), "Gb": (1, 0), "B": (1, 1)}
    normal = np.array([np.cos(np.deg2rad(175)), np.sin(np.deg2rad(175))])
    planes, sampling, metrics = {}, {}, {}
    for channel, shift in shifts.items():
        row, col = sites[channel]
        origin = [20 + row, 30 + col]
        yy, xx = np.indices((96, 96), dtype=float)
        signed = (
            normal[0] * (2 * xx + origin[1] - 125.3)
            + normal[1] * (2 * yy + origin[0] - 115.7)
            - shift
        )
        planes[channel] = 500 + 2500 * ndtr(signed / 2.0)
        sampling[channel] = {"native_origin": origin}
        metrics[channel] = edge_metrics(planes[channel], pitch_native=2)
        assert metrics[channel]["status"] == "ok"
    result = _lateral_ca(planes, sampling, metrics)
    assert result["status"] == "ok"
    for channel, expected in shifts.items():
        assert abs(result["relative_edge_offset_native_px"][channel] - expected) < 0.04
