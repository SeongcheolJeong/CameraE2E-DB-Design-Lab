"""Numeric, temporal, lifecycle and provenance tests, not physical validation."""

import json

import numpy as np
import pytest

from camera_platform.pedestrian_braking_metrics import CausalTracker, evaluate_event
from camera_platform.road_hdr_evaluation import acceptance
from camera_platform.road_hdr_fusion import calibrated_rate, merge, warp_cfa_tile
from camera_platform.road_hdr_job import specification
from camera_platform.road_hdr_study import manifest, schedule, timing_record, verify_manifest


def frame(shape=(64, 64), value=100000, variance=100):
    return {
        "rate": np.full(shape, value, dtype=float),
        "variance": np.full(shape, variance, dtype=float),
        "valid": np.ones(shape, bool),
    }


def test_static_gain_and_reference_holes_are_different_contracts():
    a, b = frame(), frame(variance=10)
    a["valid"][20:24, 20:24] = False
    static = merge(a, b, profile="static", static_input=True)
    guarded = merge(a, b, profile="local")
    assert static["valid"][21, 21] and not guarded["valid"][21, 21]
    assert np.isnan(guarded["rate"][21, 21])
    assert np.max(static["variance"]) < np.max(a["variance"])
    with pytest.raises(ValueError):
        merge(a, b, profile="static")


def test_plane_warp_never_mixes_gr_gb_or_colors_and_propagates_noise():
    a = frame((16, 16))
    a["variance"][:] = 4
    for pr in (0, 1):
        for pc in (0, 1):
            a["rate"][pr::2, pc::2] = 10 + 10 * pr + pc
    motion = {"flow": np.full((8, 8, 2), 0.5, np.float32), "trusted": np.ones((8, 8), bool)}
    w = warp_cfa_tile(a, motion, (16, 16), (0, 16, 0, 16))
    for pr in (0, 1):
        for pc in (0, 1):
            assert np.all(w["rate"][pr:12:2, pc:12:2] == 10 + 10 * pr + pc)
    assert w["variance"][4, 4] == 1 and w["variance_bound"][4, 4] == 4
    assert not w["valid"][-1, -1]


def test_residual_guard_and_tiling_do_not_depend_on_tile_partition():
    a, b = frame((80, 80)), frame((80, 80), value=1000000)
    small = merge(a, b, profile="static", static_input=True, tile_side=16)
    large = merge(a, b, profile="static", static_input=True, tile_side=64)
    assert np.array_equal(small["rate"], large["rate"])
    a["rate"][20:60, 20:60] += np.indices((40, 40))[0] * 10000
    b["rate"] = a["rate"].copy()
    b["rate"][30:34, 30:34] += 1000000
    x = merge(a, b, profile="global", tile_side=16)
    y = merge(a, b, profile="global", tile_side=64)
    assert np.allclose(x["rate"], y["rate"], equal_nan=True)
    assert x["fallback"][32, 32]


def test_schedule_is_causal_irregular_and_single_cadence_is_preserved():
    timing = {"readout_s": 0.005, "common_processing_s": 0.1, "hdr_processing_s": 0.015}
    frames = [
        {"token": str(i), "timestamp_s": t}
        for i, t in enumerate([0, 0.003, 0.10, 0.18, 0.30, 0.39])
    ]
    out = schedule(frames, 0.008, timing)
    assert len(out) == 3 and out[0]["long"] is None
    assert out[1]["long"]["frame"]["token"] == "2"
    assert out[1]["result_available_ns"] == 301000000
    with pytest.raises(ValueError):
        timing_record(frames[1], 0.001, timing, long_record=out[1]["long"])
    assert len([timing_record(f, 0.004, timing) for f in frames]) == 6


def test_scheduled_six_hz_tracker_requires_three_real_observations():
    times = [0.0, 1 / 6, 2 / 6, 3 / 6, 4 / 6]
    tr = CausalTracker(expected_output_timestamps=times)
    detection = {"bbox": [10, 10, 30, 50], "score": 0.8}
    assert not tr.update(times[0], [detection])[0]["stable"]
    assert not tr.update(times[1], [detection])[0]["stable"]
    assert tr.update(times[2], [detection])[0]["stable"]
    assert not tr.update(times[4], [detection])[0]["stable"]
    with pytest.raises(ValueError):
        tr.update(0.9, [detection])


def test_available_time_uses_third_result_without_double_latency():
    from test_pedestrian_braking import event, sequence

    e = event()
    f = sequence()
    for r in f:
        r["result_available_ns"] = int(round((r["timestamp_s"] + 0.2) * 1e9))
    result = evaluate_event(e, f, processing_latency_s=10)
    assert result["availability_source"] == "per_frame"
    assert result["recognition_time_interval_s"][0] >= 2 / 12 + 0.2 - 1e-9
    assert result["recognition_time_interval_s"][1] < 2


def test_no_seed_inflation_or_incomplete_fp_recommendation():
    def result(seed, status, unknown=()):
        return {
            "event_id": "e",
            "condition": "low",
            "seed": seed,
            "risk": True,
            "status": status,
            "confirmed_false_track_ids": [],
            "uncertain_track_ids": list(unknown),
        }

    baseline = [result(i, "missed") for i in range(5)]
    candidate = [result(i, "success") for i in range(5)]
    assert acceptance(baseline, candidate)["accepted"]
    assert not acceptance(baseline, candidate[:-1])["accepted"]
    candidate[1]["uncertain_track_ids"] = [7]
    assert not acceptance(baseline, candidate)["accepted"]


def test_immutable_raw_manifest_rejects_changed_inputs(tmp_path):
    (tmp_path / "raw.npy").write_bytes(b"original")
    manifest(tmp_path, "manifest.json")
    verify_manifest(tmp_path, "manifest.json")
    (tmp_path / "raw.npy").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        verify_manifest(tmp_path, "manifest.json")


def test_calibration_tiled_matches_existing_fpn_inverse_and_raw_is_immutable():
    from camera_platform.saved_sensor_hdr import _merge_native_fpn

    shape = (64, 64)
    raw = np.full(shape, 500, np.uint16)
    before = raw.copy()
    cal = {
        "dn_per_e": 0.4,
        "black_level_dn": 64.0,
        "white_level_dn": 4095.0,
        "full_well_e": 10000.0,
        "dark_e": np.full(shape, 0.01),
        "read_noise_variance_e2": 100.0,
        "quantization_variance_e2": 1 / 12 / 0.4**2,
        "linear_ceiling_dn": 4064.0,
        "linear_floor_dn": 0.0,
        "response_gain": np.full(shape, 1.01),
        "post_well_bias_e": np.full(shape, 2.0),
    }
    geo = {"kind": "native_sensor", "full_sensor_shape": [64, 64], "crop_origin": [0, 0]}
    actual = calibrated_rate(raw, 0.001, cal, geo)
    reference = _merge_native_fpn(raw[None].astype(np.int32), [0.001], [cal], geometry=geo)[
        "arrays"
    ]
    np.testing.assert_array_equal(actual["rate"], reference["exposure_rate"][0])
    assert np.array_equal(raw, before)


def test_job_requires_bound_action_and_package():
    payload = {
        "schema": "camera_road_hdr_job_v1",
        "name": "HDR",
        "package_path": "outputs/hdr",
        "specification_sha256": "a" * 64,
        "action": "reprocess",
    }
    assert specification(payload) == payload
    with pytest.raises(ValueError):
        specification({**payload, "package_path": "../escape"})


def test_typed_api_sdk_wait_cancel_and_bound_reprocessing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from camera_platform.api import create_app
    from camera_platform.road_design_inputs import write_json
    from camera_platform.road_hdr_inputs import SCHEMA
    from camera_platform.road_rgb_source import json_sha256
    from camera_platform.sdk import PlatformClient

    package = tmp_path / "study"
    package.mkdir()
    frozen = {
        "schema": SCHEMA,
        "candidates": [{"id": "L1-S1"}],
        "observation": {"common_shape_row_col": [360, 640]},
        "first_photo": None,
        "events": [],
    }
    sha = json_sha256(frozen)
    write_json(package / "specification.json", frozen)
    write_json(package / "specification-binding.json", {"sha256": sha})

    def compute(workspace, target, action, on_progress):
        assert target == package and action == "reprocess"
        directory = package / "reprocessing" / "one"
        directory.mkdir(parents=True)
        (directory / "report.html").write_text("protocol test, not physical evidence")
        manifest(directory, "manifest.json")
        return {"status": "reprocessed", "result_directory": str(directory)}

    monkeypatch.setattr("camera_platform.road_hdr_job.run", compute)
    app = create_app(tmp_path / "registry", workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["x-platform-token"] = http.get("/api/v1/bootstrap").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = http
        assert http.get("/road-hdr.html").status_code == 200
        assert http.get("/road-hdr.js").status_code == 200
        payload = {
            "schema": "camera_road_hdr_job_v1",
            "name": "HDR",
            "package_path": "study",
            "specification_sha256": sha,
            "action": "reprocess",
        }
        queued = sdk.submit_road_hdr_reprocessing(payload)
        job = app.state.service.registry.claim()
        result = app.state.service.execute_run(job)
        app.state.service.registry.finish(queued["id"], result=result)
        completed = sdk.wait_road_hdr(queued["id"], timeout=1, poll_interval=0)
        url = completed["result"]["summary"]["viewer_path"]
        assert http.get(url).status_code == 200
        (package / "reprocessing/one/report.html").write_text("changed")
        assert http.get(url).status_code == 409
        pending = sdk.submit_road_hdr_acquisition({**payload, "action": "first-photo"})
        alias = http.post(
            "/api/v1/road-hdr-studies",
            json={**payload, "package_path": str(package)},
        )
        assert alias.status_code >= 400
        assert "already owns" in alias.text
        assert (
            http.post("/api/v1/road-hdr-studies/" + pending["id"] + "/cancel", json={}).json()[
                "status"
            ]
            == "cancelled"
        )


def test_local_motion_is_estimated_without_target_or_flow_truth():
    from scipy.ndimage import gaussian_filter, shift

    from camera_platform.road_hdr_fusion import estimate_motion

    rng = np.random.default_rng(7)
    a = frame((128, 192), variance=100000)
    a["rate"] = 500000 + 400000 * gaussian_filter(rng.uniform(size=(128, 192)), 1)
    b = {**a, "rate": shift(a["rate"], [1.5, 3.5], order=1, mode="nearest")}
    motion = estimate_motion(a, b, "local")
    center = motion["flow"][10:-10, 10:-10]
    # Guide has one sample per 2x2 Bayer cell.
    assert np.median(center[..., 0]) * 2 == pytest.approx(3.5, abs=1.0)
    assert np.median(center[..., 1]) * 2 == pytest.approx(1.5, abs=1.0)
    assert motion["summary"]["ground_truth_used"] is False


def test_moving_corridor_entry_is_eligible_without_recorded_collision(tmp_path):
    from camera_platform.pedestrian_braking_inputs import TABLES, inventory
    from camera_platform.road_design_inputs import write_json

    root = tmp_path / "v1.0-mini"
    root.mkdir()
    tables = {key: [] for key in TABLES}
    tables["scene"] = [
        {"token": "scene", "log_token": "log", "description": "controlled moving pedestrian"}
    ]
    tables["sensor"] = [{"token": "sensor", "channel": "CAM_FRONT"}]
    tables["calibrated_sensor"] = [
        {
            "token": "cal",
            "sensor_token": "sensor",
            "translation": [0, 0, 1.5],
            "rotation": [0.5, -0.5, 0.5, -0.5],
            "camera_intrinsic": [[1000, 0, 800], [0, 1000, 450], [0, 0, 1]],
        }
    ]
    tables["instance"] = [{"token": "person", "category_token": "pedestrian"}]
    tables["category"] = [{"token": "pedestrian", "name": "human.pedestrian.adult"}]
    for i in range(73):
        t = i / 12
        sample = str(i // 6)
        pose = {"token": str(i), "translation": [5 * t, 0, 0], "rotation": [1, 0, 0, 0]}
        tables["ego_pose"].append(pose)
        (tmp_path / (str(i) + ".jpg")).write_bytes(b"input inventory fixture only")
        tables["sample_data"].append(
            {
                "token": str(i),
                "calibrated_sensor_token": "cal",
                "ego_pose_token": str(i),
                "sample_token": sample,
                "timestamp": round(t * 1e6),
                "is_key_frame": i % 6 == 0,
                "filename": str(i) + ".jpg",
                "height": 900,
                "width": 1600,
            }
        )
        if i % 6 == 0:
            tables["sample"].append({"token": sample, "scene_token": "scene"})
            tables["sample_annotation"].append(
                {
                    "token": "ann" + sample,
                    "instance_token": "person",
                    "sample_token": sample,
                    "translation": [35, 3 - min(t, 2.5), 0.9],
                    "rotation": [1, 0, 0, 0],
                    "size": [0.6, 0.6, 1.8],
                    "visibility_token": "4",
                }
            )
    for name, rows in tables.items():
        write_json(root / (name + ".json"), rows)
    audit = inventory(tmp_path, straight_projection=True, output_shape=(360, 640))
    assert audit["risk_events"] == 1
    e = next(e for e in audit["events"] if e["risk"])
    assert e["geometry_assumptions"]["motion_bin"] == "moving"
    assert not e["geometry_assumptions"]["observed_braking_boundary_crossing_required"]
    assert not e["motion"][0]["collision_corridor_valid"]
    assert any(m["collision_corridor_valid"] for m in e["motion"])


def test_braking_margin_is_unknown_outside_collision_corridor():
    from test_pedestrian_braking import event, sequence

    e = event()
    for m in e["motion"]:
        m["collision_corridor_valid"] = False
    result = evaluate_event(e, sequence())
    assert result["status"] == "uncertain_corridor_entry_exit"
    assert result["braking_margin_interval_m"] is None


def test_sdk_freeze_returns_exact_unicode_spec_binding(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from camera_platform.api import create_app
    from camera_platform.road_design_inputs import write_json
    from camera_platform.road_hdr_inputs import SCHEMA
    from camera_platform.road_rgb_source import json_sha256
    from camera_platform.sdk import PlatformClient

    def freeze(workspace, package):
        package.mkdir()
        spec = {
            "schema": SCHEMA,
            "title": "도로 HDR 유니코드 명세",
            "candidates": [{"id": "L1-S1"}],
            "observation": {"common_shape_row_col": [360, 640]},
            "first_photo": None,
            "events": [],
        }
        write_json(package / "specification.json", spec)
        write_json(package / "specification-binding.json", {"sha256": json_sha256(spec)})
        return spec

    monkeypatch.setattr("camera_platform.road_hdr_job.freeze_first", freeze)
    app = create_app(tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["x-platform-token"] = http.get("/api/v1/bootstrap").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = http
        frozen = sdk.freeze_road_hdr("package")
        sha = json.loads((tmp_path / "package/specification-binding.json").read_text())["sha256"]
        assert frozen["specification_sha256"] == sha
        job = sdk.submit_road_hdr_acquisition(
            {"schema": "camera_road_hdr_job_v1", "name": "HDR", **frozen, "action": "first-photo"}
        )
        assert job["status"] == "queued"
        assert (
            http.post("/api/v1/road-hdr-studies/" + job["id"] + "/cancel", json={}).json()["status"]
            == "cancelled"
        )
