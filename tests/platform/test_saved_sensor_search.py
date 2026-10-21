"""One saved sensor run is searched without rebuilding its optical response."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

import pyisetcam.system_faca as system_faca
from camera_platform.saved_sensor_search import search_saved_sensor_capture
from pyisetcam.image_sensor_db import image_sensor_db_config, image_sensor_db_get
from pyisetcam.sensor_estimate import image_sensor_db_model
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
def saved_run(tmp_path: Path) -> Path:
    record = image_sensor_db_get(CODE)
    model = image_sensor_db_model(CODE)
    profile = resolve_sensor_readout(CODE)
    configuration = image_sensor_db_config(CODE, model="db_estimate_v1")
    configuration["scenario"].update(sensor_resize=False, sensor_output_mode="digital_raw")
    configuration["scenario"]["sensor"].update(integration_time=0.004, noise_flag=0)
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
        },
        "arrays": {"spectral_eqe": np.asarray(model["eqe"])},
    }
    db = sensor_simulation_db_path(tmp_path)
    manifest = {"source": "saved search test", "files": {}, "settings": {}}
    fingerprint = _digest(manifest)
    (db.parent / "inputs").mkdir(parents=True)
    (db.parent / "inputs" / f"{fingerprint}.json").write_text(json.dumps(manifest))
    with _connect(db, write=True) as conn:
        _store_result(conn, db, record, result, run_id="fixed_run", input_digest=fingerprint)
    return tmp_path


def _search(root: Path, **overrides):
    options = {
        "run_id": "fixed_run",
        "root": root,
        "workspace": WORKSPACE,
        "exposure_s": [0.001, 0.004],
        "analog_gain": [1.0, 2.0],
        "seed": 7,
    }
    options.update(overrides)
    return search_saved_sensor_capture(CODE, **options)


def test_saved_run_search_pins_source_and_separates_electrons_from_gain(
    saved_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbid_rebuild(*_args, **_kwargs):
        raise AssertionError("Catalog response was rebuilt instead of using the saved run")

    monkeypatch.setattr(system_faca, "image_sensor_db_model", forbid_rebuild)
    report = _search(saved_run)
    assert report["schema"] == "camerae2e_saved_sensor_search_v1"
    assert (report["case_count"], report["feasible_count"]) == (4, 4)
    assert report["revision"]["run_id"] == "fixed_run"
    assert report["raw_arrays_exported"] is False
    assert report["best_case"]["parameters"] == {
        "sensor.integration_time": 0.004,
        "sensor.analog_gain": 1.0,
    }
    cases = report["cases"]
    assert [case["seed"] for case in cases] == [7, 8, 9, 10]
    assert all(case["source_binding"]["run_id"] == "fixed_run" for case in cases)
    assert all(
        case["source_binding"]["result_sha256"] == report["revision"]["result_sha256"]
        for case in cases
    )
    assert all(
        case["source_binding"]["config_sha256"] == report["revision"]["config_sha256"]
        for case in cases
    )
    assert math.isclose(
        cases[0]["mean_electrons_e_per_pixel"],
        cases[1]["mean_electrons_e_per_pixel"],
        rel_tol=1e-12,
    )
    assert cases[1]["mean_raw_dn"] > cases[0]["mean_raw_dn"]
    assert cases[2]["mean_electrons_e_per_pixel"] > cases[0]["mean_electrons_e_per_pixel"]
    assert [case["capture"]["readout"]["gain"] for case in cases] == [1.0, 2.0, 1.0, 2.0]
    assert all(case["capture"]["noise_flag"] == 0 for case in cases)
    assert report["verification"]["physical_validation"] == "not_established"


def test_saved_run_search_rejects_clipped_candidate(saved_run: Path) -> None:
    report = _search(
        saved_run,
        exposure_s=[0.001, 1.0],
        analog_gain=[1.0],
        luminance_cd_m2=10000.0,
        max_saturation_fraction=0.0,
    )
    assert report["case_count"] == 2
    assert report["cases"][1]["capture"]["saturation"]["well_fraction"] > 0.0
    assert report["cases"][1]["feasible"] is False
    assert report["cases"][1]["score"] is None
    json.dumps(report, allow_nan=False)
    assert all(item["metric"].startswith("sensor_capture.saturation.")
               for item in report["cases"][1]["constraint_results"])


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": ""},
        {"exposure_s": [0.001, 0.002, 0.003]},
        {"analog_gain": [0.5]},
        {"analog_gain": [1.0, 1.0]},
        {"luminance_cd_m2": float("nan")},
        {"luminance_cd_m2": 10**1000},
        {"sensor_size": [64, 64]},
        {"max_saturation_fraction": 1.1},
    ],
)
def test_saved_run_search_rejects_unbounded_request(saved_run: Path, changes: dict) -> None:
    with pytest.raises(ValueError):
        _search(saved_run, **changes)
