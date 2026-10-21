"""Registered source-field RI mode survives HTTP, frozen job, RAW and ISP."""

import hashlib
import os
import time
from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_sequence_workflow import finish as finish_temporal
from test_sequence_workflow import workflow as _temporal_fixture
from test_spatial_workflow import array
from test_spatial_workflow import workflow as _spatial_fixture

from camera_platform import imaging
from camera_platform.api import create_app

MODE = "source_position_ri_v1"


@pytest.fixture
def workflow(tmp_path):
    return _spatial_fixture.__wrapped__(tmp_path)


@pytest.fixture
def temporal_workflow(tmp_path):
    return _temporal_fixture.__wrapped__(tmp_path)


def test_registered_api_source_ri_preserves_source_and_reaches_raw_isp(workflow):
    service, _, scene, _, request, _ = workflow
    request = deepcopy(request)
    request["parameters"].update(field_radiometry_mode=MODE, frames=1)
    source_artifact = next(a for a in scene["artifacts"]
                           if a["name"] == "spectral-image.npy")
    source_path = service.registry.blob_path(source_artifact["sha256"])
    original = source_path.read_bytes()
    app = create_app(service.registry.directory, start_worker=True, seed_demo=False)
    with TestClient(app) as http:
        assert app.state.worker.pid != os.getpid()
        assert http.post("/api/v1/runs", json=request).status_code == 403
        token = http.get("/api/v1/bootstrap").json()["token"]
        response = http.post("/api/v1/runs", json=request,
                             headers={"X-Platform-Token": token})
        assert response.status_code == 202, response.text
        queued = response.json()
        assert queued["parameters"]["field_radiometry_mode"] == MODE
        assert queued["request"]["field_radiometry_mode"] == MODE
        assert queued["resources"]["source_position_ri_extra_bytes"] > 0
        forged = deepcopy(queued)
        forged["request"]["field_radiometry_mode"] = "receiver_position_ri_v1"
        with pytest.raises(ValueError, match="Frozen spatial field radiometry mode"):
            imaging.execute_spatial(service, forged)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            reply = http.get(f"/api/v1/runs/{queued['id']}")
            assert reply.status_code == 200, reply.text
            run = reply.json()
            if run["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Actual Worker did not finish source-position RI within 30 seconds")
        assert run["status"] == "succeeded", run.get("error") or run
    summary = run["result"]["summary"]
    assert summary["field_radiometry_mode"] == MODE
    assert summary["relative_illumination_application"]["receiver_relative_illumination_map"] == (
        "diagnostic_only_not_multiplied"
    )
    assert summary["flux_diagnostics"]["post_ri_source_spectral_flux"]
    source_ri = array(service, run, "source_relative_illumination_map")
    raw = array(service, run, "raw_frames")
    rgb = array(service, run, "rgb_frames")
    assert source_ri.shape == tuple(scene["shape"])
    assert raw.shape == (1, *scene["shape"][:2])
    assert rgb.shape == (1, *scene["shape"][:2], 3)
    assert np.std(raw) > 0 and np.std(rgb) > 0
    assert source_path.read_bytes() == original
    assert hashlib.sha256(original).hexdigest() == source_artifact["sha256"]
    assert service.registry.audit()["integrity"] == "verified"


def test_temporal_registered_source_ri_freezes_mode_and_source_knot_evidence(temporal_workflow):
    service, _, scene, _, _, request, _ = temporal_workflow
    request = deepcopy(request)
    request["parameters"]["field_radiometry_mode"] = MODE
    original = {
        artifact["sha256"]: service.registry.blob_path(artifact["sha256"]).read_bytes()
        for image in scene["image_snapshots"] for artifact in image["artifacts"]
    }
    queued = service.submit_run(request)
    assert queued["parameters"]["field_radiometry_mode"] == MODE
    assert queued["request"]["field_radiometry_mode"] == MODE
    assert queued["resources"]["source_position_ri_map_bytes"] > 0
    forged = deepcopy(queued)
    forged["request"]["field_radiometry_mode"] = "receiver_position_ri_v1"
    from camera_platform import sequences

    with pytest.raises(ValueError, match="queued request differs"):
        sequences.execute_run(service, forged)
    run = finish_temporal(service, queued)
    assert run["status"] == "succeeded"
    summary = run["result"]["summary"]
    assert summary["field_radiometry_mode"] == MODE
    assert summary["source_relative_illumination_map_scope"] == (
        "same_fixed_source_grid_all_knots")
    assert len(summary["flux_diagnostics_per_scene"]) == len(scene["image_snapshots"])
    assert all(item["post_ri_source_spectral_flux"]
               for item in summary["flux_diagnostics_per_scene"])
    source_ri = array(service, run, "source_relative_illumination_map")
    rates = array(service, run, "channel_photoelectron_rate_scenes")
    raw = array(service, run, "raw_frames")
    assert source_ri.shape == tuple(scene["image_snapshots"][0]["shape"])
    assert rates.shape[0] > 1 and raw.shape[0] == 4
    assert not np.array_equal(rates[0], rates[-1])
    for identity, content in original.items():
        assert service.registry.blob_path(identity).read_bytes() == content
    assert service.registry.audit()["integrity"] == "verified"


def test_registered_hdr_source_ri_requires_separate_acquisition_contract(
        temporal_workflow):
    service, _, _, _, _, request, _ = temporal_workflow
    request = deepcopy(request)
    request.update(plugin_id="hdr_bracket_v1")
    request["parameters"]["field_radiometry_mode"] = MODE
    with pytest.raises(ValueError, match="fixed capture program"):
        service.submit_run(request)
