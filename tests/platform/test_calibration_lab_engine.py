"""Measured-chart recovery, refusal boundaries and fresh native RAW evidence."""

import hashlib
import io
import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import calibration_lab_engine as engine
from camera_platform.calibration_lab_raw import analyze_npy


def record(chart, observations, *, profile=None, role="training", suffix="one"):
    profile = profile or engine.default_profile()
    return {
        "id": f"{chart}-{suffix}",
        "chart": chart,
        "observations": observations,
        "role": role,
        "conditions": deepcopy(profile["conditions"]),
        "source_kind": "synthetic",
        "measurement_domain": "camera_isp" if chart in {"color", "tone"} else "camera_raw",
    }


def attach_raw(meta, raw):
    stream = io.BytesIO()
    np.save(stream, raw.astype(np.int32), allow_pickle=False)
    observations, quality, _ = analyze_npy(meta, stream.getvalue())
    return {**meta, "observations": observations, "quality": quality}


def raw_metadata(chart, profile, *, analysis=None):
    return {
        **record(chart, {}, profile=profile),
        "measurement_domain": "camera_raw",
        "raw_format": {
            "cfa": "RGGB",
            "bit_depth": 12,
            "black_level_dn": 72.0,
            "response_type": "linear",
        },
        "geometry": {"full_sensor_shape": profile["native_shape"], "crop_origin": [0, 0]},
        "analysis": analysis or {},
    }


def test_full_chart_analytic_recovery_and_holdout_is_not_fitted():
    fixture = engine.synthetic_fixture()
    original = deepcopy(fixture)
    fit = engine.fit_profile(fixture["profile"], fixture["measurements"])
    truth, p = fixture["truth_profile"], fit["profile"]
    for key in ("black_level_dn", "dn_per_e", "read_noise_e", "dark_current_e_per_s"):
        assert p["electronics"][key] == pytest.approx(truth["electronics"][key], abs=1e-8)
    assert p["electronics"]["full_well_e"] == fixture["profile"]["electronics"]["full_well_e"]
    assert p["geometry"]["radial_coefficients"] == pytest.approx([0.04, -0.012])
    assert p["shading"]["radial_coefficients"] == pytest.approx([-0.25, 0.05])
    np.testing.assert_allclose(p["isp"]["color_matrix"], truth["isp"]["color_matrix"], atol=1e-10)
    assert p["blur"]["field_nodes"][0]["sigma_x_px"] == pytest.approx(1.5, abs=1e-8)
    assert fixture == original
    poison = deepcopy(fixture["measurements"])
    for row in poison:
        if row["role"] == "final_validation" and row["chart"] == "tone":
            row["observations"]["output_level"] = [1.0] * len(row["observations"]["input_level"])
    assert engine.fit_profile(fixture["profile"], poison)["profile"] == p
    assert fit["physical_validation"] == "not_established"
    assert any(
        x["chart"] == "texture" and x["status"] == "validation_only" for x in fit["diagnostics"]
    )


def test_synthetic_demo_has_distinct_heldout_coordinates_and_fresh_lineage():
    a, b = engine.synthetic_fixture(), engine.synthetic_fixture()
    assert a["campaign"]["camera_id"] == "synthetic-camera"
    assert a["measurements"][0]["acquisition_id"] != b["measurements"][0]["acquisition_id"]
    for chart in engine.CHARTS:
        rows = [r for r in a["measurements"] if r["chart"] == chart]
        assert rows[0]["observations"] != rows[1]["observations"]
        other_final = next(
            r for r in b["measurements"] if r["chart"] == chart and r["role"] == "final_validation"
        )
        assert rows[1]["observations"] != other_final["observations"]
        assert str(a["generation_seed"]) in rows[1]["reference"]["source_uri"]
    replay = engine.synthetic_fixture(seed=a["generation_seed"])
    assert [r["observations"] for r in a["measurements"]] == [
        r["observations"] for r in replay["measurements"]
    ]
    assert a["truth_profile"] == b["truth_profile"] == replay["truth_profile"]


def test_parameter_changes_preserve_electrical_units():
    fixture = engine.synthetic_fixture(seed=21)
    fitted = engine.fit_profile(fixture["profile"], fixture["measurements"])
    units = {change["parameter"]: change["unit"] for change in fitted["changed_parameters"]}
    assert units["electronics.dn_per_e"] == "DN/e"
    assert units["electronics.dark_current_e_per_s"] == "e/s"
    assert units["electronics.read_noise_e"] == "e"
    assert units["electronics.black_level_dn"] == "DN"


def test_raw_poisson_ptc_recovers_gain_without_quantization_double_count():
    p = engine.default_profile()
    rng = np.random.default_rng(8813)
    # An independently authored scalar Poisson/electron noise/ADC implementation.
    true_gain, read_e, black = 0.65, 2.5, 72.0
    means = [20.0, 70.0, 160.0, 350.0, 700.0]
    frames = np.stack(
        [
            np.rint(
                black
                + true_gain
                * (rng.poisson(mean, (32, 128, 128)) + rng.normal(0, read_e, (32, 128, 128)))
            )
            for mean in means
        ]
    )
    p["electronics"]["black_level_dn"] = black
    measured = attach_raw(raw_metadata("ptc", p), frames)
    fitted = engine.fit_profile(p, [measured])
    assert fitted["profile"]["electronics"]["dn_per_e"] == pytest.approx(true_gain, rel=0.015)
    assert fitted["profile"]["electronics"]["read_noise_e"] == pytest.approx(read_e, abs=0.5)
    diag = next(x for x in fitted["diagnostics"] if x["chart"] == "ptc")
    assert diag["quantization_variance_assumption_dn2"] == 1 / 12
    # A noise-free analog model with only rounding does not acquire read noise.
    signal = np.array([20.0, 80.0, 200.0])
    row = record(
        "ptc",
        {
            "mean_dn": (black + signal).tolist(),
            "variance_dn2": (true_gain * signal + 1 / 12).tolist(),
        },
        profile=p,
    )
    result = engine.fit_profile(p, [row])
    assert result["profile"]["electronics"]["read_noise_e"] < 1e-6


def test_same_raw_observer_is_headline_after_fresh_dark_capture(tmp_path):
    p = engine.default_profile()
    p["native_shape"] = p["render_shape"] = [64, 64]
    rng = np.random.default_rng(91)
    times = [0.001, 0.1, 0.5, 1.0]
    raw = np.stack(
        [
            np.rint(72 + 0.5 * (rng.poisson(12 * t, (16, 64, 64)) + rng.normal(0, 3, (16, 64, 64))))
            for t in times
        ]
    )
    meta = raw_metadata("dark", p, analysis={"exposure_s": times})
    measured = attach_raw(meta, raw)
    candidate = engine.fit_profile(p, [measured])["profile"]
    summary = engine.run_comparison(p, candidate, [measured], tmp_path, seed=17)
    row = summary["comparisons"][0]
    assert row["operator_parity"] == "same_native_raw_observer"
    assert "analytic_prediction" in row
    assert row["candidate"] != row["analytic_prediction"]["candidate"]
    assert row["residuals"]["candidate_rmse"] < row["residuals"]["baseline_rmse"]
    for image in summary["images"]:
        for artifact in image["artifacts"]:
            assert (
                hashlib.sha256((tmp_path / artifact["path"]).read_bytes()).hexdigest()
                == artifact["sha256"]
            )
        fresh = np.load(tmp_path / image["raw"])
        assert fresh.shape == (4, 8, 64, 64)
        assert image["observed_metrics"]["shared_observer"].endswith("analyze_npy")
    assert summary["physical_validation"] == "not_established"


@pytest.mark.parametrize("chart", ["color", "tone"])
def test_raw_color_and_tone_fit_before_cfa_and_remeasure_same_raw(tmp_path, chart):
    p = engine.default_profile()
    p["electronics"]["black_level_dn"] = 72.0
    references = np.array(
        [
            [0.65, 0.08, 0.08],
            [0.08, 0.65, 0.08],
            [0.08, 0.08, 0.65],
            [0.25, 0.25, 0.25],
            [0.5, 0.4, 0.1],
            [0.15, 0.45, 0.6],
        ]
    )
    if chart == "tone":
        references = np.repeat(np.array([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])[:, None], 3, axis=1)
        target = np.repeat((0.52 * references[:, 0] ** 0.8)[:, None], 3, axis=1)
    else:
        target = references @ np.array([[0.45, 0.02, 0], [0.01, 0.5, 0.01], [0, 0.01, 0.4]])
    rois = []
    rgb = np.zeros((128, 128, 3))
    for i, color in enumerate(target):
        r, c = divmod(i, 3)
        row, col = 16 + r * 64, 12 + c * 40
        rois.append([row, col, 16, 16])
        rgb[row - 8 : row + 24, col - 8 : col + 24] = color
    yy, xx = np.indices((128, 128))
    channels = np.where(yy % 2 == 0, np.where(xx % 2 == 0, 0, 1), np.where(xx % 2 == 0, 1, 2))
    signal = np.take_along_axis(rgb, channels[..., None], axis=2)[..., 0]
    raw = np.stack([np.rint(72 + signal * (4095 - 72))] * 8)
    analysis = {"rois": rois}
    analysis["reference_rgb" if chart == "color" else "input_level"] = (
        references.tolist() if chart == "color" else references[:, 0].tolist()
    )
    measured = attach_raw(raw_metadata(chart, p, analysis=analysis), raw)
    fitted = engine.fit_profile(p, [measured])
    assert fitted["profile"]["isp"] == p["isp"]
    assert fitted["profile"]["raw_response"] != p["raw_response"]
    result = engine.run_comparison(p, fitted["profile"], [measured], tmp_path, seed=71)
    comparison = result["comparisons"][0]
    assert comparison["operator_parity"] == "same_native_raw_observer"
    assert comparison["residuals"]["candidate_rmse"] < comparison["residuals"]["baseline_rmse"]
    assert comparison["residuals"]["candidate_rmse"] < 0.01


@pytest.mark.parametrize("orientation", [0, 90])
def test_mtf_raw_forward_fit_and_remeasurement_use_same_cfa_operator(tmp_path, orientation):
    from scipy.ndimage import gaussian_filter

    p = engine.default_profile()
    p["native_shape"] = p["render_shape"] = [64, 64]
    p["electronics"].update(black_level_dn=72.0, dn_per_e=0.5)
    # Independently sampled supersampled ideal target and Gaussian blur.
    y, x = np.indices((256, 256), dtype=float)
    target = 300 + 1500 * (x > 128 + 0.07 * (y - 128))
    if orientation == 90:
        target = target.T
    mean_dn = gaussian_filter(target, 1.4 * 4, mode="nearest")[2::4, 2::4]
    raw = np.stack([np.rint(mean_dn + 72)] * 4)
    meta = raw_metadata("mtf", p, analysis={"orientation_deg": orientation})
    measured = attach_raw(meta, raw)
    measured["measurement_domain"] = "camera_raw"
    # ROI field is the same native center and the kernel is the whole system response.
    assert measured["observations"]
    fitted = engine.fit_profile(p, [measured], max_iterations=100)
    result = engine.run_comparison(p, fitted["profile"], [measured], tmp_path, seed=13)
    row = result["comparisons"][0]
    assert row["operator_parity"] == "same_native_raw_observer"
    assert row["residuals"]["candidate_rmse"] < row["residuals"]["baseline_rmse"]
    assert fitted["profile"]["blur"]["field_nodes"][0]["sigma_x_px"] == pytest.approx(1.4, abs=0.15)


def test_all_charts_produce_new_raw_isp_and_comparison_files(tmp_path):
    f = engine.synthetic_fixture()
    p = deepcopy(f["profile"])
    p["native_shape"] = p["render_shape"] = [64, 64]
    measurements = [r for r in f["measurements"] if r["role"] == "training"]
    candidate = engine.fit_profile(p, measurements)["profile"]
    (tmp_path / "progress.json").write_text("{}")
    result = engine.run_comparison(p, candidate, measurements, tmp_path, seed=111)
    assert {r["chart"] for r in result["comparisons"]} == set(engine.CHARTS)
    assert len(result["images"]) == 16
    for row in result["comparisons"]:
        assert row["residuals"]["candidate_rmse"] <= row["residuals"]["baseline_rmse"] + 1e-10
    assert json.loads((tmp_path / "comparison.json").read_text())["seed"] == 111
    for item in result["images"]:
        assert (tmp_path / item["isp"]).is_file()
        assert (tmp_path / item["raw"]).is_file()
        assert "not a pixel-aligned replay" in item["source"]
    with pytest.raises(ValueError, match="new or empty"):
        engine.run_comparison(p, candidate, measurements, tmp_path, seed=111)


def test_full_native_geometry_is_preserved_for_a_bounded_crop(tmp_path):
    p = engine.default_profile()
    p["native_shape"] = [1080, 1920]
    p["render_shape"] = [64, 64]
    m = record("tone", {"input_level": [0, 0.5, 1], "output_level": [0, 0.5, 1]}, profile=p)
    m["render_crop_origin"] = [401, 701]
    result = engine.run_comparison(p, p, [m], tmp_path, seed=4)
    artifact = result["images"][0]
    assert artifact["geometry"] == {"full_sensor_shape": [1080, 1920], "crop_origin": [401, 701]}
    assert np.load(tmp_path / artifact["raw"]).shape[-2:] == (64, 64)


def test_bad_quality_is_excluded_and_noisy_aggregate_is_not_identified():
    f = engine.synthetic_fixture()
    rows = [r for r in f["measurements"] if r["role"] == "training"]
    for row in rows:
        if row["chart"] == "ptc":
            row["quality"] = {"not_fit_reasons": ["ADC clipping"]}
    result = engine.fit_profile(f["profile"], rows)
    assert result["profile"]["electronics"]["dn_per_e"] == f["profile"]["electronics"]["dn_per_e"]
    assert any(d["status"] == "excluded" for d in result["diagnostics"])


def test_fitted_center_does_not_extrapolate_to_an_unmeasured_field():
    f = engine.synthetic_fixture()
    center = next(r for r in f["measurements"] if r["chart"] == "mtf")
    p = engine.fit_profile(f["profile"], [center])["profile"]
    outer = deepcopy(center)
    outer["observations"]["field_xy"] = [0.7, 0.7]
    with pytest.raises(ValueError, match="No effective PSF"):
        engine.predict_observation(p, outer)


@pytest.mark.parametrize("domain", ["optical", "camera_isp", None])
def test_wrong_measured_mtf_domain_is_refused(domain):
    f = engine.synthetic_fixture()
    m = next(r for r in f["measurements"] if r["chart"] == "mtf")
    m.update(source_kind="measured", measurement_domain=domain)
    with pytest.raises(ValueError, match="camera_raw"):
        engine.fit_profile(f["profile"], [m])


def test_rank_deficient_color_and_geometry_are_refused():
    p = engine.default_profile()
    with pytest.raises(ValueError, match="rank three"):
        engine.fit_profile(
            p,
            [
                record(
                    "color",
                    {"reference_rgb": [[0.2, 0.2, 0.2]] * 6, "measured_rgb": [[0.2, 0.2, 0.2]] * 6},
                )
            ],
        )
    with pytest.raises(ValueError, match="identify"):
        engine.fit_profile(
            p, [record("geometry", {"object_xy": [[0.3, 0.4]] * 6, "image_xy": [[0.31, 0.42]] * 6})]
        )


def test_multiview_planar_intrinsics_and_distortion_recovery():
    cv2 = pytest.importorskip("cv2")
    p = engine.default_profile()
    p["native_shape"] = [1080, 1920]
    k = np.array([[1250.0, 0, 950.0], [0, 1220.0, 530.0], [0, 0, 1.0]])
    d = np.array([0.03, -0.01, 0.001, -0.0005, 0.0])
    y, x = np.indices((7, 9), dtype=float)
    points = np.c_[x.ravel() * 25, y.ravel() * 25, np.zeros(x.size)]
    objects, images, ids = [], [], []
    for i, (r, t) in enumerate(
        [
            ([0.12, 0.2, 0.01], [-110, -80, 650]),
            ([-0.2, 0.1, -0.1], [-90, -60, 700]),
            ([0.1, -0.25, 0.2], [-70, -100, 550]),
            ([-0.15, -0.1, 0.05], [-100, -70, 600]),
        ]
    ):
        image = cv2.projectPoints(points, np.array(r, float), np.array(t, float), k, d)[0].reshape(
            -1, 2
        )
        objects.extend(points[:, :2].tolist())
        images.extend(image.tolist())
        ids.extend([str(i)] * len(points))
    m = record(
        "geometry",
        {"mode": "multiview_planar", "object_xy": objects, "image_xy": images, "view_ids": ids},
        profile=p,
    )
    result = engine.fit_profile(p, [m])["profile"]
    np.testing.assert_allclose(result["geometry"]["camera_matrix_px"], k, atol=0.1)
    predicted = engine.predict_observation(result, m)
    np.testing.assert_allclose(predicted["prediction"], np.asarray(images).ravel(), atol=0.001)
    assert result["geometry"]["object_unit"] == "mm"


def test_resource_limits_and_profile_effect_repetition_refused(tmp_path):
    p = engine.default_profile()
    broken = deepcopy(p)
    broken["blur"]["application"] = "residual_blur"
    with pytest.raises(ValueError, match="replace"):
        engine.validate_profile(broken)
    m = record("dark", {"exposure_s": np.linspace(0.001, 1, 33).tolist(), "mean_dn": [64.0] * 33})
    with pytest.raises(ValueError, match="32 levels"):
        engine.run_comparison(p, p, [m], tmp_path, seed=0)
    assert not list(tmp_path.iterdir())


def test_raw_csv_domain_routes_to_pre_cfa_and_reports_coupled_residuals():
    p = engine.default_profile()
    p["electronics"]["black_level_dn"] = 72.0
    x = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
    curve = 0.8 * np.sqrt(x)
    scale = 4000 * 0.4 / (4095 - 72)
    tone = raw_metadata("tone", p)
    tone.update(
        source_kind="measured",
        observations={"input_level": x.tolist(), "output_level": (scale * curve).tolist()},
    )
    patches = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [0.3, 0.4, 0.5]])
    # Deliberately inconsistent neutral green gain must remain visible after fitting.
    matrix = np.diag([0.9, 1.3, 0.8])
    color = raw_metadata("color", p)
    color.update(
        source_kind="measured",
        observations={
            "reference_rgb": patches.tolist(),
            "measured_rgb": (scale * (np.interp(patches, x, curve) @ matrix)).tolist(),
        },
    )
    fitted = engine.fit_profile(p, [tone, color])
    assert fitted["profile"]["isp"] == p["isp"]
    joint = next(
        d for d in fitted["diagnostics"] if d.get("method") == "coupled_raw_color_tone_residual_v1"
    )
    tone_residual = next(r for r in joint["final_combined_residuals"] if r["chart"] == "tone")
    forward = engine.predict_observation(fitted["profile"], tone)
    error = np.asarray(forward["prediction"]) - forward["measured"]
    assert tone_residual["rmse"] == pytest.approx(np.sqrt(np.mean(error**2)))
    assert tone_residual["rmse"] > 0.02
    assert joint["status"] == "conditional_fit"


def test_measured_color_requires_explicit_response_domain():
    f = engine.synthetic_fixture()
    m = next(r for r in f["measurements"] if r["chart"] == "color")
    m.update(source_kind="measured")
    m.pop("measurement_domain")
    with pytest.raises(ValueError, match="measurement_domain"):
        engine.fit_profile(f["profile"], [m])


def test_geometry_native_shape_mismatch_is_refused():
    m = record(
        "geometry",
        {
            "mode": "multiview_planar",
            "native_shape": [1080, 1920],
            "object_xy": [[0, 0]],
            "image_xy": [[0, 0]],
            "view_ids": ["pose"],
        },
    )
    with pytest.raises(ValueError, match="native_shape"):
        engine.fit_profile(engine.default_profile(), [m])


def test_large_raw_patch_observation_needs_explicit_native_crop(tmp_path):
    p = engine.default_profile()
    p["native_shape"] = [1080, 1920]
    m = raw_metadata("tone", p)
    m["quality"] = {"shape": [1, 8, 1080, 1920], "method": "native_bayer_tone_v1"}
    m["observations"] = {"input_level": [0, 0.5, 1], "output_level": [0, 0.25, 0.5]}
    with pytest.raises(ValueError, match="explicitly supplied native crop"):
        engine.run_comparison(p, p, [m], tmp_path)
    assert not list(tmp_path.iterdir())
