"""Ghost job API, actual integer acquisition and immutable RAW replay."""

import copy
import io
import json
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import electrical
from camera_platform import reference_ghost_engine as engine
from camera_platform import reference_ghost_job as job
from camera_platform import reference_workshop_optics as optics
from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMERA_PLATFORM_WORKSHOP_ROOT", str(tmp_path / "external"))
    source = tmp_path / "source"
    source.mkdir()
    readout = {
        "full_well_e": 10000.0,
        "read_noise_e": 3.0,
        "dark_current_e_per_s": 0.1,
        "gain": 1.0,
        "bit_depth": 12,
        "black_level_dn": 64.0,
    }
    model = electrical.default_electrical_model(readout)
    (source / "calibration.json").write_text(
        json.dumps({"color": {"matrix": (np.eye(3) / 1000).tolist()}, "lsc": {}})
    )
    (source / "pixel-cell.json").write_text(json.dumps({"electrical_model": model}))
    (source / "pixel-response.json").write_text("{}")
    capture = {"fields": {"center": {"readout": readout}}, "configuration": {}}
    monkeypatch.setattr(job, "_load_source", lambda *a, **k: (source, capture))
    monkeypatch.setattr(
        optics,
        "load_solution",
        lambda *a: {"result": {"fields": [{"id": "center", "image_xy_mm": [0, 0]}]}},
    )
    monkeypatch.setattr(engine, "load_prescription", lambda *a: {"surfaces": []})
    monkeypatch.setattr(engine, "spectral_power", lambda *a: (np.ones(21) / 21, {}))
    monkeypatch.setattr(
        engine, "background", lambda *a: (np.full((256, 256), 25000.0), np.ones(21) * 0.9)
    )
    maps = tmp_path / "maps"
    maps.mkdir()
    np.save(maps / "overview.npy", np.zeros((2, 512, 512, 3), np.float32))
    rates = np.zeros((2, 256, 256, 3, 4))
    rates[1] = 100.0
    np.savez(maps / "rates.npz", center=rates)
    (maps / "maps.json").write_text(
        json.dumps(
            {
                "paths": [],
                "overview_bounds_mm": [-1, 1, -1, 1],
                "diagnostics": {},
                "unsupported_angle_weight_diagnostic": 0,
            }
        )
    )
    monkeypatch.setattr(engine, "map_angle", lambda *a: maps)
    app = create_app(
        tmp_path / "state", workspace=Path(__file__).parents[2], start_worker=False, seed_demo=False
    )
    spec = job.template(job.LEGACY_SCHEMA)
    spec.update(angles_deg=[15.0], fields=["center"], charts=["uniform"], coatings=["r005"])
    spec["isp"]["lsc_enabled"] = False
    return app, spec


def execute(app, client, spec):
    r = client.submit_reference_ghost_study(spec)
    claimed = app.state.service.registry.claim()
    assert claimed["id"] == r["id"]
    app.state.service.registry.finish(r["id"], result=app.state.service.execute_run(claimed))
    return client.wait_reference_workshop(r["id"], timeout=1, poll_interval=0)


def test_job_sdk_raw_replay_cancel_files_and_binding(fixture):
    app, spec = fixture
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        client = object.__new__(PlatformClient)
        client.http = http
        done = execute(app, client, spec)
        result = done["result"]["summary"]
        assert result["new_readout_frames"] == 16
        assert result["refinement"]["status"] == "within_demo_tolerance"
        case = result["cases"][0]
        assert case["ghost_mean_e"] == pytest.approx(40.0)
        assert http.get(result["report_url"]).status_code == 200
        bundle = http.get(result["bundle_url"], follow_redirects=True)
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            assert any(n.endswith("with_ghost.npz") for n in archive.namelist())
            assert archive.testzip() is None
        assert http.get(result["files_base"] + case["modes"][0]["image"]).status_code == 200
        replay = copy.deepcopy(spec)
        replay.update(name="replay", reprocess_of=done["id"])
        replay["isp"]["display_white_level"] = 2.0
        replay_result = execute(app, client, replay)["result"]["summary"]
        assert replay_result["new_readout_frames"] == 0
        assert replay_result["cases"][0]["raw_hashes"] == case["raw_hashes"]
        invalid = copy.deepcopy(replay)
        invalid["irradiance_w_m2"] = [200.0]
        with pytest.raises(httpx.HTTPStatusError):
            client.submit_reference_ghost_study(invalid)
        queued = copy.deepcopy(spec)
        queued["name"] = "cancel this"
        r = client.submit_reference_ghost_study(queued)
        assert client.cancel_reference_workshop(r["id"])["status"] == "cancelled"
        assert len(client.reference_ghost_studies()["studies"]) == 3
        assert client.reference_ghost_study(done["id"])["status"] == "succeeded"


def test_v2_probe_chart_three_way_capture_and_replay(fixture, monkeypatch):
    from camera_platform import reference_ghost_charts as charts

    app, legacy = fixture
    spec = job.template()
    spec.update(
        source=legacy["source"],
        fields=[],
        probes=[{"id": "probe-test", "xy_mm": [1.0, 2.0]}],
        angles_deg=[15.0],
        coatings=["r005"],
        charts=["black"],
        irradiance_w_m2=[10.0],
    )
    original_map = engine.map_angle

    def mapped(*args):
        path = original_map(*args)
        with np.load(path / "rates.npz") as data:
            rate = data["center"].copy()
        np.savez(path / "rates.npz", center=rate, **{"probe-test": rate})
        return path

    monkeypatch.setattr(engine, "map_angle", mapped)
    geometry = {
        "native_shape_row_col": [256, 256],
        "input_shape_row_col": [256, 256],
        "crop_bounds_row_col": [0, 256, 0, 256],
    }
    _, definition = charts.chart_scene("black", geometry, {"white": np.ones(21)})
    calls = []

    def background(*args):
        field = args[5]
        calls.append(field)
        return np.zeros((256, 256)), np.ones(21), definition, {"method": "zero_radiance_background"}

    monkeypatch.setattr(job, "_background", background)
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        client = object.__new__(PlatformClient)
        client.http = http
        done = execute(app, client, spec)
        summary = done["result"]["summary"]
        case = summary["cases"][0]
        assert summary["new_readout_frames"] == 24
        assert case["probe_xy_mm"] == [1.0, 2.0]
        assert calls[0]["id"] == "probe-test" and calls[0]["custom_probe"]
        assert [m["id"] for m in case["modes"]] == ["source_off", "direct_only", "with_ghost"]
        assert case["raw_hashes"]["source_off"] == case["raw_hashes"]["direct_only"]
        assert case["raw_hashes"]["source_off"] != case["raw_hashes"]["with_ghost"]
        assert case["modes"][2]["chart_metrics"]["black_level"]["mean_excess_e"] > 0
        replay = copy.deepcopy(spec)
        replay.update(name="v2 same RAW", reprocess_of=done["id"])
        replay["isp"]["display_white_level"] = 0.1
        repeated = execute(app, client, replay)["result"]["summary"]
        assert repeated["new_readout_frames"] == 0
        assert repeated["cases"][0]["raw_hashes"] == case["raw_hashes"]
        moved = copy.deepcopy(replay)
        moved["probes"][0]["xy_mm"][0] = 3
        with pytest.raises(httpx.HTTPStatusError):
            client.submit_reference_ghost_study(moved)


@pytest.mark.parametrize(
    "change",
    [
        {"probes": [{"id": "bad", "xy_mm": [0, 0]}]},
        {"probes": [{"id": "probe-out", "xy_mm": [13.5, 0]}]},
        {"probes": [{"id": "probe-nan", "xy_mm": [float("nan"), 0]}]},
        {"probes": [{"id": "probe-dup", "xy_mm": [0, 0]}, {"id": "probe-dup", "xy_mm": [1, 0]}]},
        {"fields": [], "probes": []},
    ],
)
def test_probe_configuration_refuses_invalid_geometry(change):
    spec = job.template()
    spec.update(change)
    with pytest.raises(ValueError):
        job.specification(spec)


def test_v2_numeric_spellings_share_cache_inputs_without_mutating_request():
    from camera_platform.registry import digest

    integer = job.template()
    integer.update(angles_deg=[15], azimuth_deg=45, irradiance_w_m2=[10],
                   probes=[{"id": "probe-one", "xy_mm": [0, 1]}])
    decimal = copy.deepcopy(integer)
    decimal.update(angles_deg=[15.0], azimuth_deg=45.0, irradiance_w_m2=[10.0],
                   probes=[{"id": "probe-one", "xy_mm": [-0.0, 1.0]}])
    assert digest(job.specification(integer)) == digest(job.specification(decimal))
    assert type(integer["angles_deg"][0]) is int
    legacy = job.template(job.LEGACY_SCHEMA)
    legacy["angles_deg"] = [15]
    assert type(job.specification(legacy)["angles_deg"][0]) is int
