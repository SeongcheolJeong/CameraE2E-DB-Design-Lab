"""Withheld color fitting, fixed electron units and preserved-source identity."""

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform import reference_isp_color as color

WORKSPACE = Path(__file__).resolve().parents[2]


def test_white_constraint_units_and_unconstrained_residual_are_explicit():
    x = np.array([[100, 30, 20], [30, 120, 40], [20, 30, 150], [60, 40, 55.0]])
    true = np.array([[0.008, 0.001, -0.001], [-0.002, 0.009, 0], [0.001, 0, 0.007]])
    y = x @ true
    ws = np.array([200.0, 200.0, 200.0])
    result = color.fit_white_constrained(x, y, ws, ws @ true)
    np.testing.assert_allclose(result, true, atol=1e-15)
    np.testing.assert_allclose(ws @ result, ws @ true, atol=1e-14)
    scaled = color.fit_white_constrained(x * 1000, y, ws * 1000, ws @ true)
    np.testing.assert_allclose(scaled * 1000, result, atol=1e-15)
    with pytest.raises(ValueError, match="full-rank"):
        color.fit_white_constrained(np.ones((4, 3)), y, ws, ws @ true)


def test_actual_pinned_training_and_holdout_are_disjoint():
    sets = color.reflectance_sets(WORKSPACE)
    assert sets["training"]["values"].shape == (21, 64)
    assert sets["holdout"]["values"].shape == (21, 24)
    assert (
        sets["training"]["provenance"]["sha256"]
        == "dd6ecf4cdd04f97ca2c057737b30cd20007e8a365237b51b628b42b2f9787d8d"
    )
    assert (
        sets["holdout"]["provenance"]["sha256"]
        == "a6e8e69885f9992005d4d9649acd21f39175ea82e0485298e1a95ff390a04540"
    )


@pytest.fixture
def preserved(tmp_path, monkeypatch):
    root = tmp_path / "source"
    (root / "optics").mkdir(parents=True)
    cell = {"readout": {"full_well_e": 10000}, "cfa_thickness_scale": 1.1}
    cell["cell_sha256"] = color._model_digest(cell)
    response = {"cell_sha256": cell["cell_sha256"], "wavelength_nm": color.WAVE.tolist()}
    response["table_sha256"] = color._model_digest(response)
    np.savez(root / "optics/optics.npz", fixture=np.array([1.0]))
    field = {"id": "center", "status": "succeeded", "geometry": {"input_shape_row_col": [8, 8]}}
    optical = {
        "revision": "saved-pupil-fixture",
        "fields": [field],
        "arrays_file": "optics.npz",
        "arrays_sha256": color._sha(root / "optics/optics.npz"),
    }
    capture = {
        "configuration": {"gain": 1},
        "fields": {
            "center": {
                "readout": {**cell["readout"], "gain": 1},
                "calibration": {"matrix": (np.eye(3) * 0.001).tolist()},
            }
        },
    }
    for name, data in (
        ("capture.json", capture),
        ("pixel-cell.json", cell),
        ("pixel-response.json", response),
        ("optical-response.json", optical),
        ("optics/result.json", optical),
    ):
        (root / name).write_text(json.dumps(data))
    centers = np.array([610, 545, 545, 465])
    rates = (np.exp(-(((color.WAVE[:, None] - centers) / 35) ** 2)) + 0.04) * 50000
    calls = []

    def apply(solution, field_id, photons, angular_response):
        calls.append((solution["result"]["revision"], field_id, photons.shape))
        return {"center_electron_rate_per_band": rates}

    monkeypatch.setattr(color.optics, "apply_field", apply)
    return root, calls


def _files(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_fit_binds_actual_pupil_and_never_mutates_source(preserved):
    root, calls = preserved
    before = _files(root)
    result = color.fit_calibration(WORKSPACE, root)
    assert calls == [("saved-pupil-fixture", "center", (8, 8, 21))]
    assert _files(root) == before
    assert result["input_unit"] == "electrons"
    assert result["reference_exposure_s"] == 0.004
    assert result["source_binding"]["cfa_thickness_scale"] == 1.1
    assert result["holdout"]["used_in_fit"] is False
    white = np.asarray(result["basis"]["white_electrons_rgb"])
    np.testing.assert_allclose(white @ np.asarray(result["matrix"]), np.ones(3), atol=2e-7)
    assert color.verify_calibration(result, root) is result
    assert color.validate_binding(result, root) is result
    changed = copy.deepcopy(result)
    changed["matrix"][0][0] += 0.1
    with pytest.raises(ValueError, match="content changed"):
        color.verify_calibration(changed)


def test_heldout_changes_do_not_change_the_fit(preserved, monkeypatch):
    root, _ = preserved
    original = color.fit_calibration(WORKSPACE, root)
    loader = color.reflectance_sets

    def changed(workspace):
        sets = loader(workspace)
        sets["holdout"]["values"] = sets["holdout"]["values"] * 0.7
        return sets

    monkeypatch.setattr(color, "reflectance_sets", changed)
    altered = color.fit_calibration(WORKSPACE, root)
    assert altered["matrix"] == original["matrix"]
    assert altered["training"] == original["training"]
    assert altered["holdout"]["reference_xyz"] != original["holdout"]["reference_xyz"]


def test_binding_allows_other_raw_but_not_other_model(preserved):
    root, _ = preserved
    result = color.fit_calibration(WORKSPACE, root)
    capture = json.loads((root / "capture.json").read_text())
    capture["configuration"]["seed"] = 123
    (root / "capture.json").write_text(json.dumps(capture))
    assert color.validate_binding(result, root) is result
    with pytest.raises(ValueError, match="source changed"):
        color.verify_calibration(result, root)
    cell = json.loads((root / "pixel-cell.json").read_text())
    cell["cfa_thickness_scale"] = 0.9
    (root / "pixel-cell.json").write_text(json.dumps(cell))
    with pytest.raises(ValueError, match="hash changed"):
        color.validate_binding(result, root)
