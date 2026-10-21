"""Sensor Mode lifecycle on small real readout data; no external source assets."""

from __future__ import annotations

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
from camera_platform import reference_sensor_job as job
from camera_platform import reference_sensor_modes as core
from camera_platform import reference_workshop_job as workshop
from camera_platform.api import create_app
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient
from camera_platform.virtual_lab_job import sha256


def small_spec(recipe="binning"):
    spec = job.template()
    spec.update(recipe=recipe, fields=["center"], charts=["uniform"], light_scales=[1.0])
    spec["isp"]["lsc_enabled"] = False
    return spec


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(schema="unknown"),
        lambda s: s.update(recipe="product_imx462_binning"),
        lambda s: s.update(fields=["unavailable"]),
        lambda s: s.update(fields=["center", "center"]),
        lambda s: s.update(charts=["fabricated"]),
        lambda s: s.update(light_scales=[True]),
        lambda s: s.update(light_scales=[float("nan")]),
        lambda s: s.update(seed=True),
        lambda s: s.update(frames=True),
        lambda s: s.update(exposure_ms=float("nan")),
        lambda s: s["charge_binning"].update(capacity_multiplier=True),
        lambda s: s["charge_binning"].update(read_noise_multiplier=float("nan")),
        lambda s: s["isp"].update(lsc_enabled=1),
        lambda s: s["isp"].update(display_white_level=float("inf")),
        lambda s: s["source"].update(manifest_sha256="latest"),
        lambda s: s["source"].update(candidate_id="../../escape"),
        lambda s: s.update(unrecognized=True),
    ],
)
def test_specification_rejects_invalid_fields_nonfinite_and_bool(mutate):
    original = job.template()
    assert job.specification(original) == original
    changed = copy.deepcopy(original)
    mutate(changed)
    with pytest.raises(ValueError):
        job.specification(changed)


def test_source_manifest_raw_and_reference_conditions_are_bound(tmp_path, monkeypatch):
    spec = small_spec()
    source = tmp_path / "source"
    source.mkdir()
    (source / "raw.npy").write_bytes(b"original raw")
    config = workshop.defaults()
    config.update(exposure_ms=4, gain=1, frames=8, luminance_cd_m2=1000)
    capture = {
        "configuration": config,
        "fields": {
            "center": {
                "geometry": {
                    "native_shape_row_col": [256, 256],
                    "cfa_origin": {"pattern": "RGGB"},
                },
                "captures": {"uniform": {}},
            }
        },
    }
    capture_path = source / "capture.json"
    capture_path.write_text(json.dumps(capture))
    (source / "manifest.json").write_text(
        json.dumps({"files": {name: sha256(source / name) for name in ("capture.json", "raw.npy")}})
    )
    spec["source"]["manifest_sha256"] = sha256(source / "manifest.json")
    record = {
        "id": spec["source"]["run_id"],
        "status": "succeeded",
        "result": {"summary": {"manifest_sha256": spec["source"]["manifest_sha256"]}},
    }
    monkeypatch.setattr(workshop, "get", lambda *_: record)
    monkeypatch.setattr(workshop, "_directory", lambda *_: source)
    assert job._load_source(None, spec, verify=True)[1] == capture
    changed = copy.deepcopy(spec)
    changed["source"]["manifest_sha256"] = "0" * 64
    with pytest.raises(IntegrityError, match="pinned manifest"):
        job._load_source(None, changed)
    (source / "raw.npy").write_bytes(b"changed raw")
    with pytest.raises(IntegrityError, match="artifact changed"):
        job._load_source(None, spec, verify=True)
    config["gain"] = 2
    capture_path.write_text(json.dumps(capture))
    with pytest.raises(ValueError, match="gain1"):
        job._load_source(None, spec)


@pytest.fixture
def small_source(tmp_path, monkeypatch):
    """Replace only external source discovery; execute real core, ISP and metrics."""
    monkeypatch.setenv("CAMERA_PLATFORM_WORKSHOP_ROOT", str(tmp_path / "external"))
    source = tmp_path / "source"
    chart_dir = source / "fields" / "center" / "uniform"
    chart_dir.mkdir(parents=True)
    readout = {
        "full_well_e": 10000.0,
        "read_noise_e": 3.0,
        "dark_current_e_per_s": 0.1,
        "gain": 1.0,
        "bit_depth": 16,
        "black_level_dn": 64.0,
    }
    model = electrical.default_electrical_model(readout)
    rate = np.full((64, 64), 100 / 0.004)
    np.save(chart_dir / "mean-electron-rate.npy", rate)
    native = core.binning_modes(rate, readout, frames=8, seed=42)["modes"]["native"]["raw_frames"]
    records = []
    for i, raw in enumerate(native):
        path = chart_dir / f"raw-{i:03d}.npy"
        np.save(path, raw)
        records.append({"file": path.name, "sha256": sha256(path), "seed": 42 + i})
    config = workshop.defaults()
    config.update(exposure_ms=4, gain=1, frames=8, seed=42, luminance_cd_m2=1000)
    capture = {
        "configuration": config,
        "fields": {
            "center": {
                "geometry": {
                    "native_shape_row_col": [64, 64],
                    "cfa_origin": {"pattern": "RGGB"},
                    "horizontal_fov_deg": 0.64,
                },
                "readout": readout,
                "captures": {"uniform": {"raw": records}},
            }
        },
    }
    (source / "calibration.json").write_text(
        json.dumps({"color": {"matrix": (np.eye(3) / 1000).tolist()}, "lsc": {}})
    )
    (source / "pixel-cell.json").write_text(json.dumps({"electrical_model": model}))
    (source / "capture.json").write_text(json.dumps(capture))
    original_hashes = {str(path): sha256(path) for path in source.rglob("*") if path.is_file()}
    loads = []

    def load(_service, spec, verify=False):
        assert spec["source"] == job.template()["source"]
        if verify:
            assert all(sha256(Path(path)) == value for path, value in original_hashes.items())
        loads.append(verify)
        return source, capture

    monkeypatch.setattr(job, "_load_source", load)
    # Concurrent implementation edits elsewhere must not invalidate this bounded fixture.
    code = job.fingerprint()
    monkeypatch.setattr(job, "fingerprint", lambda: code)
    app = create_app(
        tmp_path / "state", workspace=Path(__file__).parents[2], start_worker=False, seed_demo=False
    )
    return app, source, loads, original_hashes


@pytest.mark.parametrize(
    "recipe", ["binning", "dual_gain", "gain_budget", "noise_structure", "adc_headroom"]
)
def test_real_core_common_sdk_lifecycle_case_profile_bundle_and_cancel(small_source, recipe):
    app, source, loads, original_hashes = small_source
    service = app.state.service
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        client = object.__new__(PlatformClient)
        client.http = http
        spec = small_spec(recipe)
        record = client.submit_reference_sensor_mode_study(spec)
        duplicate = client.submit_reference_sensor_mode_study(spec)
        assert duplicate["id"] == record["id"]
        assert len(client.reference_sensor_mode_studies()["studies"]) == 1
        claimed = service.registry.claim()
        assert claimed["id"] == record["id"]
        service.registry.finish(record["id"], result=service.execute_run(claimed))
        complete = client.wait(record["id"], timeout=1, poll_interval=0)
        assert complete["status"] == "succeeded"
        assert client.reference_sensor_mode_study(record["id"])["status"] == "succeeded"
        assert (
            client.wait_reference_workshop(record["id"], timeout=1, poll_interval=0)["id"]
            == record["id"]
        )
        summary = complete["result"]["summary"]
        assert summary["raw_preserved"]
        assert summary["reused_native_raw_frames"] == (8 if recipe == "binning" else 0)
        assert summary["new_readout_frames"] == (
            8 if recipe == "binning" else 16 if recipe == "dual_gain" else 24
        )
        case = client.reference_sensor_mode_case(record["id"], "case-000")
        expected = {
            "binning": ["native", "digital_binning", "charge_binning"],
            "dual_gain": ["hcg", "lcg", "dcg"],
            "gain_budget": ["analog_gain", "digital_gain", "balanced_gain"],
            "noise_structure": ["pixel_iid", "column_noise", "cds_correlated"],
            "adc_headroom": ["adc_10bit", "adc_12bit", "adc_14bit"],
        }[recipe]
        assert [mode["id"] for mode in case["modes"]] == expected
        for mode in case["modes"]:
            assert mode["metrics"]["mean_electrons"] == pytest.approx(100, rel=0.02)
            assert mode["metrics"]["temporal_noise_e"] > 0
            assert http.get(summary["files_base"] + mode["files"]["display"]).status_code == 200
            if recipe in job.READOUT_RECIPES:
                assert mode["readout_diagnostics"]
                assert mode["metrics"]["digital_saturation_fraction"] is not None
                assert mode["metadata_arrays_file"] == mode["files"]["arrays"]
                assert (
                    http.get(summary["files_base"] + mode["files"]["measured_noise"]).status_code
                    == 200
                )
                root = workshop._directory(service, record["id"])
                with np.load(root / mode["files"]["arrays"], allow_pickle=False) as arrays:
                    assert arrays["raw_frames"].dtype.kind in "iu"
                    assert arrays["adc_frames"].shape == (8, 64, 64)
                    assert arrays["digital_saturated_frames"].shape == (8, 64, 64)
                    noise = np.sqrt(np.var(arrays["electron_frames"], axis=0, ddof=1))
                    valid = arrays["measured_temporal_noise_valid"]
                    np.testing.assert_allclose(
                        arrays["measured_temporal_noise_e_per_contributor"][valid], noise[valid]
                    )
        profile = client.save_reference_sensor_mode_profile(
            record["id"],
            {
                "name": "Mode comparison",
                "case_id": "case-000",
                "mode": expected[-1],
                "baseline_mode": expected[0],
                "interpolation": "bilinear",
            },
        )
        assert http.get(profile["download_url"]).json()["selection"]["mode"] == expected[-1]
        invalid = http.post(
            job.ROUTE + f"/{record['id']}/profiles",
            json={
                **profile["selection"],
                "mode": "not_a_case_mode",
            },
        )
        assert invalid.status_code == 422 or invalid.status_code == 400
        assert http.get(summary["report_url"]).status_code == 200
        bundle = http.get(summary["bundle_url"])
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            assert "manifest.json" in archive.namelist()
            assert "cases/case-000/case.json" in archive.namelist()
            assert f"cases/case-000/{expected[-1]}/arrays.npz" in archive.namelist()
        assert client.submit_reference_sensor_mode_study(spec)["id"] == record["id"]
        assert len(client.reference_sensor_mode_studies()["studies"]) == 1
        pending = client.submit_reference_sensor_mode_study({**spec, "name": "Cancelled mode"})
        assert client.cancel_reference_workshop(pending["id"])["status"] == "cancelled"
        assert client.wait(pending["id"], timeout=1, poll_interval=0)["status"] == "cancelled"
        assert service.registry.claim() is None
        assert loads.count(True) >= 2
        assert all(sha256(Path(path)) == value for path, value in original_hashes.items())
        # Case/profile/report binding fails closed if a published case is edited.
        path = workshop._directory(service, record["id"]) / "cases/case-000/case.json"
        path.write_text(path.read_text() + " ")
        with pytest.raises(httpx.HTTPStatusError):
            client.reference_sensor_mode_case(record["id"], "case-000")
        assert http.get(profile["download_url"]).status_code >= 400


def test_changed_code_and_exhausted_storage_fail_before_execution(small_source, monkeypatch):
    app, _, _, _ = small_source
    service = app.state.service
    record = job.submit(service, small_spec())
    claimed = service.registry.claim()
    monkeypatch.setattr(job, "fingerprint", lambda: {"changed": "code"})
    with pytest.raises(IntegrityError, match="code or storage changed"):
        job.execute(service, claimed)
    service.registry.finish(record["id"], error="Source code changed")
    monkeypatch.setattr(
        job.storage,
        "status",
        lambda _: {
            "remaining_bytes": 1,
            "volume_free_bytes": 10**12,
        },
    )
    with pytest.raises(ValueError, match="SSD budget"):
        job.submit(service, small_spec())


def test_dcg_reprocessing_reuses_pair_and_refuses_acquisition_changes(small_source, monkeypatch):
    app, _, _, _ = small_source
    service = app.state.service

    def finish(record):
        claimed = service.registry.claim()
        assert claimed["id"] == record["id"]
        return service.registry.finish(record["id"], result=service.execute_run(claimed))

    source_spec = small_spec("dual_gain")
    first = finish(job.submit(service, source_spec))
    source_dir = workshop._directory(service, first["id"])
    before = {
        str(p.relative_to(source_dir)): sha256(p) for p in source_dir.rglob("*") if p.is_file()
    }

    def never_acquire(*_args, **_kwargs):
        raise AssertionError("RAW reprocessing must not acquire charge or readout again")

    monkeypatch.setattr(core, "dual_gain_modes", never_acquire)
    changed = copy.deepcopy(source_spec)
    changed.update(name="Preserved pair changed merge", reprocess_of=first["id"])
    changed["dcg_merge"]["gain_path_agreement_sigma"] = 4.0
    changed["isp"]["tone_gamma"] = 2.0
    replay = finish(job.submit(service, changed))
    summary = replay["result"]["summary"]
    assert summary["new_readout_frames"] == 0
    assert summary["reused_native_raw_frames"] == 16
    replay_dir = workshop._directory(service, replay["id"])
    for mode in ("hcg", "lcg"):
        relative = f"cases/case-000/{mode}/arrays.npz"
        with np.load(source_dir / relative) as original, np.load(replay_dir / relative) as output:
            np.testing.assert_array_equal(original["raw_frames"], output["raw_frames"])
            np.testing.assert_array_equal(
                original["well_saturated_frames"], output["well_saturated_frames"]
            )
    assert {
        str(p.relative_to(source_dir)): sha256(p) for p in source_dir.rglob("*") if p.is_file()
    } == before
    changed["seed"] += 1
    with pytest.raises(ValueError, match="acquisition is frozen"):
        job.submit(service, changed)


def test_readout_settings_are_optional_for_legacy_requests_and_explicit_for_new_recipes():
    from camera_platform.reference_readout_lab import default_settings

    old = job.template()
    old.pop("readout_lab")
    assert job.specification(old) == old
    new = {**old, "recipe": "gain_budget", "readout_lab": {"gain_total": 4}}
    assert job.specification(new)["readout_lab"] == {**default_settings(), "gain_total": 4}
    for bad in ({"gain_total": True}, {"post_gain_noise_e": float("nan")}, {"unsafe_units": 1}):
        with pytest.raises(ValueError):
            job.specification({**new, "readout_lab": bad})
    for recipe in ("binning", "dual_gain"):
        with pytest.raises(ValueError, match="dark chart"):
            job.specification({**old, "recipe": recipe, "charts": ["dark"]})
    with pytest.raises(ValueError, match="dual_gain"):
        job.specification({**new, "reprocess_of": "run-" + "1" * 32})


def test_readout_dark_chart_is_zero_input_with_fresh_noise_not_source_raw(
    small_source, monkeypatch
):
    app, source, _, original_hashes = small_source
    service = app.state.service
    spec = small_spec("noise_structure")
    spec["charts"] = ["dark"]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Readout dark chart must not rerun optics or reuse source RAW")

    monkeypatch.setattr(job, "_dynamic_rate", forbidden)
    monkeypatch.setattr(core, "binning_modes", forbidden)
    monkeypatch.setattr(core, "dual_gain_modes", forbidden)
    submitted = job.submit(service, spec)
    claimed = service.registry.claim()
    complete = service.registry.finish(submitted["id"], result=service.execute_run(claimed))
    result = complete["result"]["summary"]
    assert result["new_readout_frames"] == 24 and result["reused_native_raw_frames"] == 0
    assert result["readout_lab"]["original_raw_reused"] is False
    case = job.case_detail(service, submitted["id"], "case-000")
    assert case["source_refs"]["response"]["kind"] == "declared_zero_incident_photoelectron_rate"
    assert case["source_refs"]["native_raw"] == []
    root = workshop._directory(service, submitted["id"])
    assert not np.load(root / "cases/case-000/mean-electron-rate.npy").any()
    for mode in case["modes"]:
        assert mode["metrics"]["temporal_noise_e"] > 0
        with np.load(root / mode["files"]["arrays"], allow_pickle=False) as arrays:
            assert np.any(arrays["raw_frames"][0] != arrays["raw_frames"][1])
            # Calibration maps belong in the NPZ; case JSON carries references.
            calibration = mode["metadata"]["metadata"]["calibration"]
            assert calibration["dark_e"]["array"] == "dark_e"
            assert arrays["dark_e"].shape == (64, 64)
            noise_scale = mode["preview_scales"]["residual_limit_e"]
            assert noise_scale >= 4 * spec["readout_lab"]["cds_rms_e"]
            assert noise_scale == case["modes"][0]["preview_scales"]["residual_limit_e"]
            supported = arrays["measured_temporal_noise_valid"]
            residual = arrays["electron_frames"][0] - arrays["electron_frames"].mean(axis=0)
            np.testing.assert_allclose(
                arrays["first_frame_temporal_residual_e_per_contributor"][supported],
                residual[supported],
            )
    assert all(sha256(Path(path)) == value for path, value in original_hashes.items())


def test_dynamic_tone_curve_reports_digital_clipping_separately():
    shape = (8, 256, 256)
    mode = {
        "electron_frames": np.full(shape, 100.0),
        "valid_frames": np.ones(shape, bool),
        "well_saturated_frames": np.zeros(shape, bool),
        "adc_saturated_frames": np.zeros(shape, bool),
        "digital_saturated_frames": np.zeros(shape, bool),
    }
    mode["digital_saturated_frames"][:, :, 16:32] = True
    curve = job._tone_curve(mode, np.full(shape[1:], 100.0), 1)
    assert curve[0]["digital_saturation_fraction"] == 0
    assert curve[1]["digital_saturation_fraction"] == 1
    assert curve[1]["adc_saturation_fraction"] == 0


def test_new_readout_reservation_scales_with_mode_count(small_source):
    app, _, _, _ = small_source
    service = app.state.service
    old = job.submit(service, small_spec("binning"))
    single = small_spec("adc_headroom")
    single["readout_lab"]["adc_bits"] = [12]
    one = job.submit(service, single)
    three = job.submit(service, small_spec("adc_headroom"))
    assert one["estimated_bytes"] == old["estimated_bytes"]
    assert three["estimated_bytes"] > one["estimated_bytes"]
