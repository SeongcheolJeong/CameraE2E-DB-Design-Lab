"""Whole-chart admission, coverage and holdout isolation without edge reduction."""

from copy import deepcopy

import numpy as np
import pytest
from camera_platform.esfr_chart import chart_rois

from camera_platform import calibration_lab_contracts as contracts
from camera_platform import calibration_lab_engine as engine


def full_chart_records():
    profile = engine.default_profile()
    profile["native_shape"] = [1440, 2560]
    profile["blur"]["field_nodes"][0].update(sigma_x_px=0.6, sigma_y_px=0.6)
    frequency = np.linspace(0, 0.25, 41)
    records = []
    for role in ("training", "final_validation"):
        for index, roi in enumerate(r for r in chart_rois() if r["kind"] == "edge"):
            r, c, h, w = roi["box"]
            sigma = 0.9 + index / 140
            # Authored closed-form transfer is independent of the fitter. Each
            # edge has its own known response; nearby arm centers must not merge.
            transfer = np.exp(-2 * np.pi**2 * sigma**2 * frequency**2)
            records.append(
                {
                    "id": f"{role}-{roi['id']}",
                    "chart": "mtf",
                    "role": role,
                    "source_kind": "synthetic",
                    "measurement_domain": "camera_raw",
                    "conditions": deepcopy(profile["conditions"]),
                    "geometry": {"full_sensor_shape": [1440, 2560], "crop_origin": [0, 0]},
                    "observations": {
                        "frequency_cy_px": frequency.tolist(),
                        "mtf": transfer.tolist(),
                        "field_xy": [
                            2 * (c + (w - 1) / 2) / 2559 - 1,
                            2 * (r + (h - 1) / 2) / 1439 - 1,
                        ],
                        "orientation_deg": (roi["edge_tangent_deg"] + 90) % 180,
                    },
                    "provenance": {
                        "native_roi": roi["box"],
                        "roi_id": roi["id"],
                        "import_classification": (
                            "external_camera_raw_curve_not_legacy_image_operator"
                        ),
                    },
                }
            )
    return profile, records


def test_all_112_edges_fit_without_merging_and_holdout_never_changes_candidate():
    profile, records = full_chart_records()
    original = deepcopy(records)
    fit = engine.fit_profile(profile, records)
    nodes = fit["profile"]["blur"]["field_nodes"]
    assert fit["field_coverage"] == {
        "fitted_field_count": 112,
        "profile_field_count": 113,
        "inherited_field_count": 1,
        "interpolation": "not_established; explicit measured positions only",
    }
    assert len(fit["training_measurement_ids"]) == 112
    assert len(fit["excluded_evaluation_measurement_ids"]) == 112
    assert fit["resource_budget"]["measurement_count"] == 224
    diagnostics = [d for d in fit["diagnostics"] if d["chart"] == "mtf"]
    assert len(diagnostics) == 112
    assert {tuple(n["field_xy"]) for n in nodes[:-1]} == {
        tuple(r["observations"]["field_xy"]) for r in records
    }
    for index, (node, row, diagnostic) in enumerate(
        zip(nodes[:-1], records[:112], diagnostics, strict=True)
    ):
        assert node["sigma_x_px"] == pytest.approx(0.9 + index / 140, abs=1e-6)
        assert node["training_measurement_ids"] == [row["id"]]
        assert node["measured_directions_deg"] == [row["observations"]["orientation_deg"]]
        assert diagnostic["orientation_deg"] == [row["observations"]["orientation_deg"]]
        assert diagnostic["directional_identification"] == "single_projection_isotropy_assumed"
        assert not diagnostic["physical_psf_recovered"]
    for row in records[112:]:
        prediction = engine.predict_observation(fit["profile"], row)
        assert np.max(np.abs(np.subtract(prediction["prediction"], prediction["measured"]))) < 1e-6
    poisoned = deepcopy(records)
    for row in poisoned[112:]:
        row["observations"]["mtf"] = [1.0] * 41
    assert engine.fit_profile(profile, poisoned)["profile"] == fit["profile"]
    assert records == original


def test_comparison_preserves_all_224_curves_and_admits_actual_native_crop_cost(
    tmp_path, monkeypatch
):
    profile, records = full_chart_records()
    candidate = engine.fit_profile(profile, records)["profile"]

    # Real fresh captures are checked by the live end-to-end suite. Here isolate
    # result coverage/admission from 448 image writes and expensive convolutions.
    def render(p, row, root, variant, index, seed):
        geometry, shape = engine._crop(p, row)
        return {
            "measurement_id": row["id"],
            "variant": variant,
            "geometry": geometry,
            "shape": list(shape),
            "observed_metrics": {},
        }

    monkeypatch.setattr(engine, "_render", render)
    comparison = engine.run_comparison(profile, candidate, records, tmp_path)
    assert len(comparison["comparisons"]) == 224
    assert len(comparison["images"]) == 448
    assert not comparison["issues"]
    assert {r["measurement_id"] for r in comparison["comparisons"]} == {r["id"] for r in records}
    budget = comparison["resource_budget"]
    assert budget["retained_raw_samples"] == sum(
        16 * r["provenance"]["native_roi"][2] * r["provenance"]["native_roi"][3] for r in records
    )
    assert budget["retained_raw_samples"] < engine.MAX_RAW_SAMPLES
    assert all(row["residuals"]["candidate_rmse"] < 1e-6 for row in comparison["comparisons"])


def test_native_crop_fallback_preserves_offset_phase_and_does_not_modify_record():
    profile, rows = full_chart_records()
    row = rows[0]
    row["geometry"]["crop_origin"] = [3, 5]
    before = deepcopy(row)
    r, c, h, w = row["provenance"]["native_roi"]
    geometry, shape = engine._crop(profile, row)
    assert geometry["crop_origin"] == [r + 3, c + 5]
    assert shape == (h, w)
    assert row == before
    row["provenance"]["native_roi"] = [0, 0, 258, 62]
    with pytest.raises(ValueError, match="bounded even native crop"):
        engine._crop(profile, row)


def test_resource_budgets_replace_counts_but_fail_before_expensive_work(monkeypatch):
    profile, records = full_chart_records()
    request = contracts.study_spec(
        {"campaign_id": "campaign", "measurement_ids": [r["id"] for r in records]}
    )
    assert len(request["measurement_ids"]) == 224
    assert contracts.measurement_resource_budget(records)["position_count_limit"] is None
    monkeypatch.setattr(contracts, "MAX_OBSERVATION_SCALARS", 10)
    with pytest.raises(ValueError, match="observation scalars"):
        engine.fit_profile(profile, records)
    monkeypatch.setattr(contracts, "MAX_OBSERVATION_SCALARS", 2_000_000)
    monkeypatch.setattr(contracts, "MAX_MEASUREMENT_BYTES", 100)
    with pytest.raises(ValueError, match="byte resource budget"):
        contracts.measurement_resource_budget(records)
    monkeypatch.setattr(contracts, "MAX_STUDY_IDS_BYTES", 10)
    with pytest.raises(ValueError, match="byte resource budget"):
        contracts.study_measurement_ids(["measurement-long-name"])


def test_duplicate_ids_cannot_double_weight_an_edge():
    profile, records = full_chart_records()
    with pytest.raises(ValueError, match="unique"):
        contracts.study_measurement_ids(["id", " id "])
    with pytest.raises(ValueError, match="unique"):
        engine.fit_profile(profile, [records[0], records[0]])


def test_nearly_collinear_three_directions_do_not_identify_elliptical_psf():
    profile, records = full_chart_records()
    rows = []
    for index, angle in enumerate((0, 0.00001, 0.00002)):
        row = deepcopy(records[0])
        row["id"] = f"near-collinear-{index}"
        row["observations"]["orientation_deg"] = angle
        rows.append(row)
    result = engine.fit_profile(profile, rows)
    diagnostic = next(d for d in result["diagnostics"] if d["chart"] == "mtf")
    assert diagnostic["direction_count"] == 3
    assert diagnostic["symmetry_assumption"] == "isotropic"
    assert (
        diagnostic["directional_identification"]
        == "isotropy_assumed_insufficient_direction_information"
    )


def test_profile_field_count_removed_but_profile_byte_budget_retained(monkeypatch):
    profile = engine.default_profile()
    node = profile["blur"]["field_nodes"][0]
    profile["blur"]["field_nodes"] = [{**node, "field_xy": [i / 200, 0]} for i in range(130)]
    assert len(engine.validate_profile(profile)["blur"]["field_nodes"]) == 130
    monkeypatch.setattr(engine, "MAX_MEASUREMENT_BYTES", 100)
    with pytest.raises(ValueError, match="byte resource budget"):
        engine.validate_profile(profile)
