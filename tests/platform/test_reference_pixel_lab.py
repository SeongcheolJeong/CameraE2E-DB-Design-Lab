"""Preserved response budgets/pupil fractions, without optical or capture jobs."""

import copy
import hashlib
import json

import numpy as np
import pytest

from camera_platform import reference_pixel_lab as lab


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    wave = [500.0, 550.0]
    theta = [0.0, 30.0, 60.0]
    cfa = np.broadcast_to(0.1 + np.asarray(theta)[:, None, None] * 0.002, (3, 2, 4)).copy()
    silicon = 0.75 - cfa

    def constant(value):
        return np.full((3, 2, 4), value).tolist()

    cell = {
        "cfa_thickness_scale": 0.9,
        "cross_section": {},
        "geometry_um": {},
        "field_classification": {"source_derived": {}, "common_assumptions": {}},
    }
    cell["cell_sha256"] = lab._model_digest(cell)
    response = {
        "cell_sha256": cell["cell_sha256"],
        "wavelength_nm": wave,
        "theta_deg": theta,
        "sites": lab.SITES,
        "array_axes": ["theta_deg", "wavelength_nm", "site"],
        "reflection": constant(0.05),
        "uncollected_deep_transmission": constant(0.1),
        "eqe": (silicon * 0.75).tolist(),
        "normal_qe": (silicon[0] * 0.75).tolist(),
        "collection_loss": (silicon * 0.25).tolist(),
        "layer_absorption": {
            "lens": constant(0.05),
            "cfa": cfa.tolist(),
            "passivation": constant(0.05),
            "silicon": silicon.tolist(),
        },
        "provenance": {
            "angular_model": "planar unpolarized; no azimuth variation",
            "angular_weights": "conditional sensor-plane incident-photon fractions",
        },
    }
    response["table_sha256"] = lab._model_digest(response)
    fields = [
        {"id": "center", "normalized_radius": 0.0, "status": "succeeded"},
        {"id": "outer-ne", "normalized_radius": 0.85, "status": "succeeded"},
        {"id": "failed", "normalized_radius": 0.5, "status": "failed"},
    ]
    radians = np.deg2rad([10, 40])
    directions = np.column_stack((np.sin(radians), np.zeros(2), np.cos(radians)))
    weights = np.broadcast_to(np.array([[0.8, 0.2], [0.3, 0.7]])[:, None], (2, 3, 2)).copy()
    (root / "optics").mkdir()
    np.savez(
        root / "optics/optics.npz",
        wavelength_nm=wave,
        chief_ray_angle_deg=np.full((2, 3), 20),
        conditional_weights=weights,
        directions_xyz=np.broadcast_to(directions, (3, 2, 3)),
    )
    optical = {
        "revision": "optical-reference",
        "fields": fields,
        "arrays_sha256": hashlib.sha256((root / "optics/optics.npz").read_bytes()).hexdigest(),
    }
    config = {
        "name": "source",
        "isp": {},
        "pixel": {"cfa_thickness_scale": 0.9},
        "exposure_ms": 4,
        "gain": 1,
    }
    records = {
        f["id"]: {
            "geometry": {},
            "readout": {},
            "scene_reference": {},
            "captures": {
                "uniform": {
                    "rate_sha256": "rate",
                    "raw": [{"file": "raw-00.npy", "sha256": "raw", "seed": 1}],
                }
            },
        }
        for f in fields
        if f["status"] == "succeeded"
    }
    capture = {"configuration": config, "fields": records}
    summary = {
        "id": "candidate-00",
        "configuration": config,
        "optical_revision": optical["revision"],
        "pixel": {"cell": cell},
        "fields": [
            {"id": f["id"], "status": f["status"], "tests": {}, "images": {}} for f in fields
        ],
    }
    for name, doc in (
        ("capture.json", capture),
        ("pixel-cell.json", cell),
        ("pixel-response.json", response),
        ("optical-response.json", optical),
    ):
        _write(root / name, doc)
    return root, summary


def test_budget_closes_without_double_counting_silicon_and_collection(source):
    root, summary = source
    result = lab.build_candidate(root, summary)
    budget = result["photon_budget"]
    total = np.asarray(budget["reflection"]) + np.asarray(budget["deep_transmission"])
    total += sum(np.asarray(v) for v in budget["layer_absorption"].values())
    np.testing.assert_allclose(total, 1)
    np.testing.assert_allclose(
        np.asarray(budget["collected_electrons"]) + budget["collection_loss"],
        budget["silicon_absorption"],
    )
    assert budget["optical_closure_max_abs"] < 1e-12
    assert result["fields"][-1]["reason"] == "preserved_optical_field_unavailable"
    assert result["fields"][0]["color_comparison"]["status"] == "unavailable"
    assert len(result["dependencies"]) == 5
    json.dumps(result, allow_nan=False)


def test_full_pupil_uses_saved_conditional_fractions_without_cosine(source):
    result = lab.build_candidate(*source)
    field = result["fields"][0]
    q10, q40 = (0.75 - (0.1 + np.array([10, 40]) * 0.002)) * 0.75
    expected = np.array([0.8 * q10 + 0.2 * q40, 0.3 * q10 + 0.7 * q40])
    np.testing.assert_allclose(np.asarray(field["pupil_qe"])[:, 0], expected)
    np.testing.assert_allclose(field["chief_qe"], (0.75 - 0.14) * 0.75)
    np.testing.assert_allclose(np.sum(field["pupil_histogram"]["weights"], axis=1), 1)
    np.testing.assert_allclose(field["pupil_histogram"]["weights"], [[0.8, 0.2], [0.3, 0.7]])


def test_changed_budget_and_saved_hash_are_refused(source):
    root, summary = source
    response = json.loads((root / "pixel-response.json").read_text())
    response["reflection"][0][0][0] += 0.1
    _write(root / "pixel-response.json", response)
    with pytest.raises(ValueError, match="content hash"):
        lab.build_candidate(root, summary)
    response["table_sha256"] = lab._model_digest(
        {k: v for k, v in response.items() if k != "table_sha256"}
    )
    _write(root / "pixel-response.json", response)
    with pytest.raises(ValueError, match="budget does not close"):
        lab.build_candidate(root, summary)


def test_qe_delta_requires_same_optics_and_mutates_candidate(source):
    original = lab.build_candidate(*source)
    candidate = copy.deepcopy(original)
    candidate["qe"]["normal"][0][0] += 0.02
    assert lab.add_delta(candidate, original) is candidate
    assert candidate["qe"]["delta_normal"][0][0] == pytest.approx(0.02)
    candidate["source_binding"]["optical_revision"] = "different"
    with pytest.raises(ValueError, match="identical"):
        lab.add_delta(candidate, original)


def test_color_study_cannot_mix_a_different_retained_raw(source, tmp_path):
    root, summary = source
    color = tmp_path / "color"
    capture = json.loads((root / "capture.json").read_text())
    capture["fields"]["center"]["captures"]["uniform"]["raw"][0]["sha256"] = "another-raw"
    _write(color / "candidate-00/capture.json", capture)
    color_summary = {
        "study": {"recipe": "color_recalibration"},
        "candidates": [
            {
                "id": "candidate-00",
                "configuration": summary["configuration"],
                "status": "succeeded",
                "fields": [],
            }
        ],
    }
    _write(color / "result.json", color_summary)
    with pytest.raises(ValueError, match="same CFA/RAW"):
        lab.build_candidate(root, summary, color_directory=color, color_summary=color_summary)


def test_cancel_callback_is_used_before_reading(source):
    def cancelled():
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        lab.build_candidate(*source, cancel=cancelled)


def test_equivalent_raw_content_ignores_cache_history_and_numeric_json_spelling(source):
    root, _ = source
    first = json.loads((root / "capture.json").read_text())
    second = copy.deepcopy(first)
    first["fields"]["center"]["captures"]["uniform"]["response_cache_reused"] = False
    second["fields"]["center"]["captures"]["uniform"]["response_cache_reused"] = True
    second["configuration"]["gain"] = 1.0
    second["configuration"]["name"] = "another acquisition with identical stored content"
    assert lab._capture_identity(first) == lab._capture_identity(second)


def test_color_patch_noise_and_coefficient_deltas_keep_units_and_zero_guards():
    metadata = lab._color_metadata({"matrix": np.eye(3).tolist()}, "baseline-bundle")
    fitted = lab._color_metadata(
        {
            "matrix": (np.eye(3) * 2).tolist(),
            "id": "fitted-id",
            "training": {"file": "Munsell", "sha256": "train-hash", "role": "fit_only"},
            "holdout": {"file": "Macbeth", "sha256": "holdout-hash", "used_in_fit": False},
            "assets": {"snapshot": "asset-hash"},
        },
        "unused",
    )
    old = {
        "id": "center",
        "status": "succeeded",
        "tests": {
            "color": {
                "delta_e00_mean": 4,
                "delta_e00_max": 8,
                "patches": [{"patch": 1, "delta_e00": 5, "roi": [1, 2, 1, 2]}],
            },
            "isp_lab": {
                "color_noise_luma": 0.1,
                "temporal_noise_luma": 0,
                "color_noise_rgb": {"r": 0.2, "g": 0, "b": None},
            },
        },
    }
    new = copy.deepcopy(old)
    new["tests"]["color"]["delta_e00_mean"] = 3
    new["tests"]["color"]["patches"][0]["delta_e00"] = 2
    new["tests"]["isp_lab"]["color_noise_luma"] = 0.12
    new["tests"]["isp_lab"]["color_noise_rgb"]["r"] = 0.1
    result = lab._field_color(
        "center",
        {
            "frozen": {"center": ("candidate-00", old, metadata)},
            "munsell": {"center": ("candidate-01", new, fitted)},
        },
        None,
    )
    assert result["frozen"]["calibration"]["id"] == "baseline-bundle"
    assert result["munsell"]["calibration"]["training"]["sha256"] == "train-hash"
    assert result["munsell"]["calibration"]["holdout"]["used_in_fit"] is False
    assert result["munsell"]["noise_unit"] == "signed_linear_srgb RMS"
    delta = result["delta"]
    assert delta["delta_e00_mean"] == -1
    assert delta["patches"] == [{"patch": 1, "delta_e00": -3}]
    assert delta["color_noise_luma_percent"] == pytest.approx(20)
    assert delta["uniform_noise_luma_percent"] is None
    assert delta["color_noise_rgb_percent"] == {"r": -50, "g": None, "b": None}
    assert delta["ccm_column_norm_percent"] == [100, 100, 100]
