"""Registered source-position optics remain pinned through HDR/DCG acquisition."""

import os
import time
from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_dcg_api import dcg_workflow as _dcg_workflow
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as _hdr_workflow

from camera_platform import capture_programs, sequences
from camera_platform.api import create_app

MODE = "source_position_ri_v1"


@pytest.fixture
def hdr_workflow(tmp_path):
    yield from _hdr_workflow.__wrapped__(tmp_path)


@pytest.fixture
def dcg_workflow(hdr_workflow):
    return _dcg_workflow.__wrapped__(hdr_workflow)


@pytest.mark.parametrize("kind", ["hdr", "dcg"])
def test_registered_source_ri_reaches_raw_and_rejects_frozen_mode_change(
    hdr_workflow, dcg_workflow, kind,
):
    if kind == "hdr":
        service, sdk, assembly, scene, control, program, test, request = hdr_workflow
        submit = sdk.submit_hdr
        raw_name = "raw_frames"
    else:
        service, sdk, assembly, scene, control, program, test, request = dcg_workflow
        submit = sdk.submit_dcg
        raw_name = "raw_groups"
    queued = submit(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        seed=request["parameters"]["seed"], field_radiometry_mode=MODE,
    )
    assert queued["parameters"]["field_radiometry_mode"] == MODE
    assert queued["request"]["field_radiometry_mode"] == MODE
    assert queued["resources"]["source_position_ri_map_bytes"] > 0
    altered = deepcopy(queued)
    altered["request"].pop("field_radiometry_mode")
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(altered)
    selected = finish(service, queued)
    assert selected["status"] == "succeeded"
    summary = selected["result"]["summary"]
    assert summary["field_radiometry_mode"] == MODE
    assert summary["physical_validation"] == "not_established"
    source_map = load_array(service, selected, "source_relative_illumination_map")
    raw = load_array(service, selected, raw_name)
    assert source_map.shape[:2] == tuple(scene["image_snapshots"][0]["shape"][:2])
    assert raw.dtype.kind in "iu" and raw.size > 0
    default = finish(service, service.submit_run(request))
    assert default["status"] == "succeeded"
    assert "field_radiometry_mode" not in default["result"]["summary"]
    assert not np.array_equal(raw, load_array(service, default, raw_name))
    assert service.registry.audit()["integrity"] == "verified"


def test_registered_two_component_hdr_pins_source_ri_on_both_scenes(hdr_workflow):
    service, sdk, assembly, scene, control, _, _, _ = hdr_workflow
    second = sequences.create_fixture(
        service,
        sequences.fixture_template(service, assembly["id"])
        | {"name": "Independent unmodulated ideal image", "revision": "ambient-1",
           "shape": [16, 16], "knot_count": 2, "pattern": "static", "irradiance_scale": .25},
    )
    draft = capture_programs.template(service, assembly["id"], mode="independent_bracket")
    draft.update(name="Source RI two-component HDR", revision="source-ri-hdr-1")
    draft["payload"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": control["payload"]["clock"]["clock_id"],
        "period_ns": 1_000_000, "on_time_ns": 500_000,
        "phase_origin_ns": 0, "low": 0, "high": 1,
    }
    program = capture_programs.create(service, draft)
    test = service.create_test(sequences.test_template(
        service, assembly["id"], scene["id"], control["id"], program["id"],
        unmodulated_scene_sequence_id=second["id"],
    ))
    queued = sdk.submit_hdr(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        unmodulated_scene_sequence_id=second["id"], field_radiometry_mode=MODE,
    )
    assert queued["request"]["field_radiometry_mode"] == MODE
    assert queued["unmodulated_scene_sequence_snapshot"] == second
    run = finish(service, queued)
    assert run["status"] == "succeeded"
    summary = run["result"]["summary"]
    assert summary["field_radiometry_mode"] == MODE
    assert summary["unmodulated_scene_sequence_id"] == second["id"]
    assert load_array(service, run, "source_relative_illumination_map").size > 0
    assert load_array(service, run, "unmodulated_source_relative_illumination_map").size > 0
    np.testing.assert_allclose(
        load_array(service, run, "channel_electrons"),
        load_array(service, run, "modulated_channel_electrons")
        + load_array(service, run, "unmodulated_channel_electrons"),
        rtol=0, atol=0,
    )
    assert service.registry.audit()["integrity"] == "verified"


def test_selected_hdr_runs_in_separate_registered_worker(hdr_workflow):
    service, sdk, assembly, scene, control, program, test, _ = hdr_workflow
    queued = sdk.submit_hdr(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        field_radiometry_mode=MODE,
    )
    app = create_app(service.registry.directory, start_worker=True, seed_demo=False)
    with TestClient(app) as http:
        assert app.state.worker.pid != os.getpid()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = http.get(f"/api/v1/runs/{queued['id']}").json()
            if run["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Separate HDR Worker did not finish within 30 seconds")
        assert run["status"] == "succeeded", run.get("error") or run
    assert run["result"]["summary"]["field_radiometry_mode"] == MODE
    assert load_array(service, run, "raw_frames").dtype.kind in "iu"
    assert service.registry.audit()["integrity"] == "verified"
