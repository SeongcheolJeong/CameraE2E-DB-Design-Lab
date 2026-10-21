"""Executable A/B evidence and queued-source integrity for existing LUT adapters."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from camera_platform.physics_lut_bridge import (
    compare_fdtd_field_raw,
    default_spec,
    execute,
    get,
    inspect_fdtd_lut,
    install_routes,
    prepare,
    submit,
)
from camera_platform.registry import IntegrityError
from camera_platform.service import PlatformService
from pyisetcam.assets import DEFAULT_UPSTREAM_SHA


def _install_snapshot(tmp_path):
    from camera_platform.offline_assets import _REQUIRED

    origin = Path(__file__).resolve().parents[2] / ".cache/upstream/isetcam" / DEFAULT_UPSTREAM_SHA
    if not all((origin / relative).is_file() for relative in _REQUIRED):
        pytest.skip("Approved local upstream snapshot is unavailable")
    destination = tmp_path / ".cache/upstream/isetcam" / DEFAULT_UPSTREAM_SHA
    for relative in _REQUIRED:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin / relative, target)
    return destination


def _pin_environment(monkeypatch):
    # This test verifies asset/source drift while other development workers may
    # edit unrelated source files concurrently in the shared checkout.
    from camera_platform.service import environment_fingerprint

    identity = environment_fingerprint()
    monkeypatch.setattr("camera_platform.service.environment_fingerprint", lambda: identity)


def _lut(tmp_path, edge_response=0.5):
    path = tmp_path / "camera_lut.json"
    path.write_text(json.dumps({
        "schema": "camera_supercell_optical_lut_v2",
        "response_model": "synthetic_test_optical_response",
        "mode": "split-pd-1x1",
        "wavelengths_nm": [550.0],
        "cases": [
            {"name": "center", "field_x_norm": 0.0, "field_z_norm": 0.0,
             "cra_x_deg": 0.0, "cra_z_deg": 0.0},
            {"name": "edge20x", "field_x_norm": 1.0, "field_z_norm": 0.0,
             "cra_x_deg": 20.0, "cra_z_deg": 0.0},
        ],
        "summaries": [
            {"case": "center", "wavelength_nm": 550.0,
             "field_x_norm": 0.0, "field_z_norm": 0.0,
             "cra_x_deg": 0.0, "cra_z_deg": 0.0, "total_response": 0.8},
            {"case": "edge20x", "wavelength_nm": 550.0,
             "field_x_norm": 1.0, "field_z_norm": 0.0,
             "cra_x_deg": 20.0, "cra_z_deg": 0.0,
             "total_response": edge_response * 0.8},
        ],
    }), encoding="utf-8")
    return path


def test_fdtd_field_only_study_preserves_two_raws_and_meaning(tmp_path, monkeypatch):
    path = _lut(tmp_path)
    _install_snapshot(tmp_path)
    monkeypatch.setattr(
        "pyisetcam.assets.ensure_upstream_snapshot",
        lambda **_kwargs: pytest.fail("Network-capable snapshot resolution was used"),
    )
    inspection = inspect_fdtd_lut(path)
    assert inspection["available_effects"]["center_relative_field_at_selected_wavelength"]
    assert not inspection["available_effects"]["crosstalk_kernel"]

    report = compare_fdtd_field_raw(path, size=(8, 8), wavelength_nm=550)
    base = np.asarray(report["baseline"]["raw"]["values"], dtype=np.int32)
    changed = np.asarray(report["with_lut"]["raw"]["values"], dtype=np.int32)
    np.testing.assert_array_equal(
        changed.astype(np.int64) - base.astype(np.int64),
        np.asarray(report["difference"]["raw_dn"]["values"]),
    )
    assert report["effect"]["absolute_qe_applied"] is False
    assert report["effect"]["crosstalk_applied"] is False
    assert report["effect"]["field_factor"]["min"] < 1
    assert report["upstream_assets"]["upstream_revision"] == DEFAULT_UPSTREAM_SHA
    assert report["with_lut"]["photoelectrons"]["mean"] < report["baseline"][
        "photoelectrons"
    ]["mean"]
    with pytest.raises(ValueError, match="exactly match"):
        compare_fdtd_field_raw(path, wavelength_nm=560)


def test_physics_lut_job_rechecks_source_and_retains_original_npy(tmp_path, monkeypatch):
    path = _lut(tmp_path)
    _install_snapshot(tmp_path)
    _pin_environment(monkeypatch)
    service = PlatformService(tmp_path / "state", workspace=tmp_path)
    spec = default_spec(source_path=path)
    spec["acknowledge_proxy_limits"] = True
    queued = submit(service, spec)
    running = service.registry.claim()
    assert running["id"] == queued["id"]
    result = execute(service, running)
    service.registry.finish(queued["id"], result=result)
    saved = get(service, queued["id"])
    assert saved["status"] == "succeeded"
    assert len(saved["artifacts"]) == 3
    for key in ("baseline", "with_lut"):
        original = saved["result"]["summary"]["comparison"][key]
        content = service.registry.blob_path(original["raw_artifact"]["sha256"]).read_bytes()
        raw = np.load(io.BytesIO(content), allow_pickle=False)
        np.testing.assert_array_equal(raw, np.asarray(original["raw"]["values"]))

    # A second queued job freezes the old source hash. Source drift must fail
    # before a Worker can silently evaluate a different response.
    alternate = default_spec(source_path=path)
    alternate["name"] = "queued before source change"
    alternate["acknowledge_proxy_limits"] = True
    pending = submit(service, alternate)
    _lut(tmp_path, edge_response=0.75)
    claimed = service.registry.claim()
    assert claimed["id"] == pending["id"]
    with pytest.raises(IntegrityError, match="Frozen physics LUT source"):
        execute(service, claimed)


def test_job_rejects_changed_upstream_asset_without_capture(tmp_path, monkeypatch):
    path = _lut(tmp_path)
    snapshot = _install_snapshot(tmp_path)
    _pin_environment(monkeypatch)
    service = PlatformService(tmp_path / "state", workspace=tmp_path)
    spec = default_spec(source_path=path)
    spec["acknowledge_proxy_limits"] = True
    queued = submit(service, spec)
    assert queued["upstream_assets"]["required_files_sha256"]

    with (snapshot / "data/lights/D65.mat").open("ab") as handle:
        handle.write(b"changed-after-submit")
    running = service.registry.claim()
    with pytest.raises(IntegrityError, match="Frozen physics LUT source"):
        execute(service, running)


def test_api_template_does_not_offer_outside_workspace_or_missing_source(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    service = PlatformService(tmp_path / "state", workspace=tmp_path)
    spec = default_spec(source_path=tmp_path / "missing.json")
    spec["acknowledge_proxy_limits"] = True
    with pytest.raises(ValueError, match="source is missing"):
        prepare(service, spec)

    path = _lut(tmp_path)
    spec["source_path"] = str(path)
    monkeypatch.setattr(
        "pyisetcam.assets.ensure_upstream_snapshot",
        lambda **_kwargs: pytest.fail("Missing workspace attempted a download"),
    )
    with pytest.raises(ValueError, match="Pinned upstream ISETCam assets are unavailable"):
        prepare(service, spec)

    app = FastAPI()
    install_routes(app, service)
    template = TestClient(app).get("/api/v1/physics-lut/template").json()
    assert "workspace" in template["options"]["fdtd"]["error"]
    assert "workspace" in template["options"]["tcad"]["error"]
