"""Public Lab lifecycle and RAW replay integrity; numerical metrics tested separately."""

import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient
from camera_platform.virtual_lab_job import default_specification, specification

CATALOG = {"lenses": [{"id": "L1", "label": "Lens"}], "sensors": [{"id": "S1", "label": "Sensor"}]}


def test_reject_nonfinite_controls_and_unsupported_fields():
    spec = default_specification(CATALOG)
    for payload in (
        {**spec, "gain": float("nan")},
        {**spec, "frames": True},
        {**spec, "lens_id": "other"},
        {**spec, "unmodeled": 1},
    ):
        with pytest.raises(ValueError):
            specification(payload, CATALOG)
    with pytest.raises(ValueError):
        specification({**spec, "isp": {"ccm": [[1]]}}, CATALOG)


def test_field_cra_contract_preserves_legacy_and_rejects_ambiguous_controls():
    spec = default_specification(CATALOG)
    assert specification(spec, CATALOG)["angular_response"]["mode"] == "physical_relative"
    legacy = {k: v for k, v in spec.items() if k not in {"field_model", "angular_response"}}
    normalized = specification(legacy, CATALOG)
    assert normalized["field_model"]["mode"] == "center_local"
    assert normalized["angular_response"]["mode"] == "off"
    for change in (
        {"field_model": {"mode": "full_field", "horizontal_fov_deg": float("nan")}},
        {"field_model": {"mode": "full_field", "horizontal_fov_deg": 60}},
        {"field_model": {"mode": "center_local", "horizontal_fov_deg": None}},
        {"angular_response": {"mode": "physical_relative", "exponents_r_gr_gb_b": [2, 2, 2]}},
        {"angular_response": {"mode": "cosine_power", "exponents_r_gr_gb_b": [2, True, 2, 2]}},
        {"angular_response": {"mode": "cosine_power", "exponents_r_gr_gb_b": [2, -1, 2, 2]}},
        {"angular_response": {"mode": "cosine_power", "exponents_r_gr_gb_b": [2, 2, 2, 17]}},
    ):
        with pytest.raises(ValueError):
            specification({**spec, **change}, CATALOG)


def test_capture_replay_sdk_wait_file_integrity_and_cancellation(tmp_path, monkeypatch):
    calls = []

    def run(workspace, output, spec, on_progress=None):
        calls.append("capture")
        (output / "raw.bin").write_bytes(b"untouched-native-RAW")
        on_progress("RAW acquired")
        return {
            "schema": "camera_virtual_lab_result_v1",
            "images": {},
            "tests": {"noise": {"snr": 12}},
        }

    def replay(workspace, source, output, spec, on_progress=None):
        calls.append("replay")
        assert (source / "raw.bin").read_bytes() == b"untouched-native-RAW"
        (output / "raw.bin").write_bytes((source / "raw.bin").read_bytes())
        return {"images": {}, "tests": {"noise": {"snr": 14}}}

    import camera_platform

    fake = SimpleNamespace(catalog=lambda w: CATALOG, run=run, replay=replay)
    monkeypatch.setitem(sys.modules, "camera_platform.virtual_lab_engine", fake)
    monkeypatch.setattr(camera_platform, "virtual_lab_engine", fake, raising=False)
    app = create_app(tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = http
        spec = sdk.virtual_lab_template()["specification"]
        job = sdk.submit_virtual_lab(spec)
        claimed = app.state.service.registry.claim()
        app.state.service.registry.finish(job["id"], result=app.state.service.execute_run(claimed))
        result = sdk.wait_virtual_lab(job["id"], timeout=1, poll_interval=0)
        assert result["status"] == "succeeded"
        summary = result["result"]["summary"]
        assert http.get(summary["report_url"]).status_code == 200
        assert http.get(summary["files_base"] + "../not-bound").status_code == 404
        changed = {**spec["isp"], "sharpen": 1}
        replayed = sdk.replay_virtual_lab(job["id"], changed)
        claimed = app.state.service.registry.claim()
        app.state.service.registry.finish(
            replayed["id"], result=app.state.service.execute_run(claimed)
        )
        assert (
            sdk.wait_virtual_lab(replayed["id"], timeout=1, poll_interval=0)["status"]
            == "succeeded"
        )
        assert calls == ["capture", "replay"]
        pending = sdk.submit_virtual_lab({**spec, "name": "Cancel me"})
        assert sdk.cancel_virtual_lab(pending["id"])["status"] == "cancelled"
        source = app.state.service.registry.directory / "virtual-lab" / job["id"]
        (source / "raw.bin").write_bytes(b"tampered")
        assert http.get(summary["files_base"] + "raw.bin").status_code == 409
        rejected = http.post(
            "/api/v1/virtual-lab/replays",
            json={"source_run_id": job["id"], "name": "reject", "isp": changed},
        )
        assert rejected.status_code == 409


def test_pupil_mode_is_explicit_and_rejects_lenses_without_directions():
    spec = default_specification(CATALOG)
    spec["angular_response"]["mode"] = "physical_pupil"
    assert specification(spec, CATALOG)["angular_response"]["mode"] == "physical_pupil"
    analytic = {
        "lenses": [{"id": "analytic", "supports": {"pupil_directions": False}}],
        "sensors": CATALOG["sensors"],
    }
    with pytest.raises(ValueError, match="stored lens pupil"):
        specification({**spec, "lens_id": "analytic"}, analytic)
    spec["field_model"]["mode"] = "center_local"
    with pytest.raises(ValueError, match="full_field"):
        specification(spec, CATALOG)
