import json
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from camera_platform import reference_asset_trial as trial
from camera_platform.electrical import default_electrical_model
from camera_platform.registry import IntegrityError, digest
from camera_platform.virtual_lab_job import sha256

READOUT = {
    "full_well_e": 10000,
    "read_noise_e": 10,
    "dark_current_e_per_s": 2,
    "gain": 1,
    "bit_depth": 12,
    "black_level_dn": 64,
}


def model_for(cell, **values):
    baseline = deepcopy(cell["electrical_model"])
    applied = {k: baseline[k] for k in ("read_noise_e", "dark_current_e_per_s")}
    applied.update(values)
    return {
        "id": "design_asset_model-test",
        "content_hash": "a" * 64,
        "execution_supported": True,
        "applied_inputs": applied,
        "baseline_electrical_model": baseline,
        "source_binding": {},
    }


def test_unchanged_readout_preserves_seeded_integer_raw_and_input():
    electrical = default_electrical_model(READOUT)
    model = model_for({"electrical_model": electrical})
    rate = np.full((128, 128), 123456.0)
    before = rate.copy()
    config = {"exposure_ms": 4, "gain": 1}
    from camera_platform.design_workshop_engine import _capture

    expected = _capture(rate, electrical, config, 42)
    actual = trial.recapture(rate, trial.applied_model(model, electrical), config, 42)
    np.testing.assert_array_equal(actual["arrays"]["raw_dn"], expected["arrays"]["raw_dn"])
    np.testing.assert_array_equal(rate, before)
    assert np.issubdtype(actual["arrays"]["raw_dn"].dtype, np.integer)


def test_read_noise_changes_temporal_variance_without_changing_mean_rate():
    baseline = default_electrical_model(READOUT)
    rate = np.full((128, 128), 100.0)
    low = {**baseline, "read_noise_e": 5.0}
    high = {**baseline, "read_noise_e": 20.0}
    config = {"exposure_ms": 4, "gain": 1}
    a = np.stack([trial.recapture(rate, low, config, s)["arrays"]["raw_dn"] for s in range(8)])
    b = np.stack([trial.recapture(rate, high, config, s)["arrays"]["raw_dn"] for s in range(8)])
    assert (
        np.var(b.astype(float), axis=0, ddof=1).mean()
        > 10 * np.var(a.astype(float), axis=0, ddof=1).mean()
    )
    assert abs(a.mean() - b.mean()) < 0.2
    assert np.all(rate == 100)


def test_dark_current_scales_with_actual_exposure_time():
    baseline = default_electrical_model(READOUT)
    model = {**baseline, "read_noise_e": 0.0, "dark_current_e_per_s": 10000.0}
    rate = np.zeros((256, 256))
    a = trial.recapture(rate, model, {"exposure_ms": 4, "gain": 1}, 23)["arrays"]["raw_dn"]
    b = trial.recapture(rate, model, {"exposure_ms": 8, "gain": 1}, 23)["arrays"]["raw_dn"]
    assert (b.mean() - 64) / (a.mean() - 64) == pytest.approx(2, rel=0.02)
    np.testing.assert_array_equal(rate, np.zeros_like(rate))


def test_adopted_baseline_and_supported_inputs_are_enforced():
    baseline = default_electrical_model(READOUT)
    model = model_for({"electrical_model": baseline}, read_noise_e=5)
    assert trial.applied_model(model, baseline)["read_noise_e"] == 5
    with pytest.raises(IntegrityError):
        trial.applied_model(model, {**baseline, "read_noise_e": 11})
    model["applied_inputs"]["gain"] = 2
    with pytest.raises(ValueError):
        trial.applied_model(model, baseline)


def test_cell_snapshot_updates_all_readout_metadata():
    baseline = default_electrical_model(READOUT)
    cell = {
        "electrical_model": baseline,
        "readout": deepcopy(READOUT),
        "physical_model": {"readout": deepcopy(READOUT)},
        "provenance": {},
        "field_classification": {"user_overrides": {}},
        "cell_sha256": "b" * 64,
    }
    model = model_for(cell, read_noise_e=5, dark_current_e_per_s=6)
    updated = trial._updated_cell(cell, trial.applied_model(model, baseline), model)
    assert cell["readout"]["read_noise_e"] == 10
    assert updated["readout"]["read_noise_e"] == 5
    assert updated["physical_model"]["readout"]["dark_current_e_per_s"] == 6
    assert updated["electrical_model"]["dark_current_e_per_s"] == 6
    assert updated["cell_sha256"] == digest(
        {k: v for k, v in updated.items() if k != "cell_sha256"}
    )


@pytest.fixture
def source(tmp_path, monkeypatch):
    from camera_platform import reference_workshop_job as workshop

    config = workshop.defaults()
    geometry = {
        "native_shape_row_col": [256, 256],
        "cfa_origin": {"pattern": "RGGB", "local_row": 0, "local_col": 0},
    }
    baseline = default_electrical_model(READOUT)
    cell = {"cell_sha256": "b" * 64, "source_report": "DEF-test", "electrical_model": baseline}
    (tmp_path / "pixel-cell.json").write_text(json.dumps(cell))
    (tmp_path / "calibration.json").write_text("{}")
    fieldcaptures = {}
    for field in trial.FIELDS:
        charts = {}
        for chart in trial.CHARTS:
            folder = tmp_path / "fields" / field / chart
            folder.mkdir(parents=True)
            np.save(folder / "mean-electron-rate.npy", np.full((256, 256), 100.0))
            charts[chart] = {
                "rate_sha256": sha256(folder / "mean-electron-rate.npy"),
                "optical_pixel_contract": {"unit": "electrons/s", "geometry": deepcopy(geometry)},
                "raw": [{"seed": i} for i in range(8)],
            }
        fieldcaptures[field] = {"geometry": deepcopy(geometry), "captures": charts}
    capture = {
        "configuration": config,
        "fields": fieldcaptures,
        "calibration_sha256": sha256(tmp_path / "calibration.json"),
    }
    (tmp_path / "capture.json").write_text(json.dumps(capture))
    record = {
        "id": "run-" + "1" * 32,
        "status": "succeeded",
        "plugin_id": workshop.SCHEMA,
        "result": {"summary": {"manifest_sha256": "c" * 64, "configuration": config}},
    }
    monkeypatch.setattr(workshop, "get", lambda *args: record)
    monkeypatch.setattr(workshop, "_directory", lambda *args: tmp_path)
    monkeypatch.setattr(workshop, "verify_files", lambda *args: None)
    model = model_for(cell, read_noise_e=5)
    model["source_binding"] = {
        "run_id": record["id"],
        "candidate_id": None,
        "manifest_sha256": "c" * 64,
        "cell_sha256": cell["cell_sha256"],
        "source_report": cell["source_report"],
    }
    return SimpleNamespace(), model, record, capture, tmp_path


def test_exact_source_and_cfa_are_required(source):
    service, model, record, capture, path = source
    loaded = trial._load_source(service, model, record["id"], verify=True)
    assert loaded[-1]["read_noise_e"] == 5
    assert loaded[2]["fields"]["center"]["geometry"] == capture["fields"]["center"]["geometry"]
    assert digest(loaded[2]["fields"]["center"]["geometry"]) == digest(
        capture["fields"]["center"]["geometry"]
    )
    changed = deepcopy(model)
    changed["source_binding"]["manifest_sha256"] = "0" * 64
    with pytest.raises(IntegrityError, match="exact acquisition"):
        trial._load_source(service, changed, record["id"])
    capture["fields"]["center"]["geometry"]["cfa_origin"]["pattern"] = "BGGR"
    (path / "capture.json").write_text(json.dumps(capture))
    with pytest.raises(ValueError, match="RGGB"):
        trial._load_source(service, model, record["id"])


def test_changed_cell_or_rate_refused(source):
    service, model, record, _, path = source
    changed = deepcopy(model)
    changed["source_binding"]["cell_sha256"] = "0" * 64
    with pytest.raises(IntegrityError, match="cell hash"):
        trial._load_source(service, changed, record["id"])
    np.save(path / "fields/center/uniform/mean-electron-rate.npy", np.zeros((256, 256)))
    with pytest.raises(IntegrityError, match="rate changed"):
        trial._load_source(service, model, record["id"])


def test_invalid_rate_never_silently_clipped():
    baseline = default_electrical_model(READOUT)
    with pytest.raises(ValueError):
        trial.recapture(np.array([[-1.0, 1.0]]), baseline, {"exposure_ms": 4, "gain": 1}, 0)


def test_trial_writes_new_raw_and_keeps_source_bytes_and_cfa(source, monkeypatch):
    from camera_platform import reference_workshop_engine as engine

    service, model, record, capture, path = source
    baseline = model["baseline_electrical_model"]
    cell = json.loads((path / "pixel-cell.json").read_text())
    cell.update(
        readout=deepcopy(READOUT), provenance={}, field_classification={"user_overrides": {}}
    )
    (path / "calibration.json").write_text(json.dumps({"color": {}, "identity": "fixed"}))
    (path / "pixel-response.json").write_text("{}")
    optical = {
        "revision": "fixed-optics",
        "fields": [{"id": f, "status": "succeeded", "images": {}} for f in trial.FIELDS],
    }
    (path / "optical-response.json").write_text(json.dumps(optical))
    for field in trial.FIELDS:
        saved = capture["fields"][field]
        saved.update(readout=deepcopy(READOUT))
        for chart in trial.CHARTS:
            folder = path / "fields" / field / chart
            np.save(
                folder / "source-reference-linear.npy", np.zeros((256, 256, 3), dtype=np.float32)
            )
            rate = np.load(folder / "mean-electron-rate.npy")
            info = saved["captures"][chart]
            info["source_reference_sha256"] = sha256(folder / "source-reference-linear.npy")
            for i, entry in enumerate(info["raw"]):
                filename = f"raw-{i:02d}.npy"
                np.save(
                    folder / filename,
                    trial.recapture(rate, baseline, capture["configuration"], entry["seed"])[
                        "arrays"
                    ]["raw_dn"],
                )
                entry.update(file=filename, sha256=sha256(folder / filename))
    before = {str(p.relative_to(path)): sha256(p) for p in path.rglob("*") if p.is_file()}
    monkeypatch.setattr(
        engine, "_render_field", lambda *args: {"id": args[3]["id"], "status": "succeeded"}
    )
    monkeypatch.setattr(
        engine, "_summary", lambda root, spec, optics, cell, cal, fields: {"fields": fields}
    )
    output = path.parent / (path.name + "-trial")
    updated = trial.applied_model(model, baseline)
    result = trial.calculate(
        path, output, capture, capture["configuration"], cell, updated, model, lambda _: None
    )
    assert result["asset_trial"]["new_raw_frames"] == 64
    assert all(
        x["source_rate_sha256"] == x["trial_rate_sha256"]
        for x in result["asset_trial"]["comparisons"]
    )
    assert not any(x["raw_identical"] for x in result["asset_trial"]["comparisons"])
    assert all(sha256(path / name) == expected for name, expected in before.items())
    current = json.loads((output / "capture.json").read_text())
    for field in trial.FIELDS:
        assert current["fields"][field]["geometry"] == capture["fields"][field]["geometry"]
        assert current["fields"][field]["readout"]["read_noise_e"] == 5
    assert json.loads((output / "applied-electrical-model.json").read_text()) == updated


def test_trial_requires_current_successful_exact_model_readiness(monkeypatch):
    from camera_platform import design_asset_readiness

    model = {"id": "m1", "asset_id": "a1", "execution_supported": True}
    service = SimpleNamespace(registry=SimpleNamespace(get=lambda *args: model))
    state = {"latest_check": None}
    monkeypatch.setattr(
        design_asset_readiness, "_model_views", lambda *args: [{"id": "m1", **state}]
    )
    with pytest.raises(ValueError, match="readiness check"):
        trial._model(service, "m1")
    state["latest_check"] = {"status": "stale", "execution_ready": False}
    with pytest.raises(ValueError, match="readiness check"):
        trial._model(service, "m1")
    state["latest_check"] = {"status": "passed", "execution_ready": True}
    assert trial._model(service, "m1") is model
