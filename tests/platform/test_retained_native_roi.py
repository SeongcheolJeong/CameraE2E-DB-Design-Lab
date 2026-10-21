"""A fixed receiver ROI retains the native charge history and row/site identity."""

from copy import deepcopy

import numpy as np
import pytest
from httpx import HTTPStatusError
from test_hdr_runtime import request_data as request_data
from test_hdr_workflow import finish, load_array
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow
from test_retained_isp import inputs as code_isp_inputs
from test_retained_two_component import _waveform

from camera_platform import (
    capture_programs,
    isp,
    retained_charge,
    retained_isp,
    retained_isp_source,
    sequences,
)
from camera_platform.registry import IntegrityError, canonical, digest
from camera_platform.retained_charge_runtime import (
    RetainedChargeRuntime,
    default_capture_program,
    default_gain_merge_capture_program,
    default_trapped_capture_program,
)


@pytest.mark.parametrize("family", ["ordinary", "trapped", "paired", "trapped_paired"])
def test_receiver_roi_matches_full_native_deterministic_history(request_data, family):
    data = deepcopy(request_data)
    yy, xx = np.indices((12, 14))
    light = 1e-4 * (1 + yy / 24 + xx / 28)[..., None] * np.ones((1, 1, 5))
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["spectral_irradiance"] = light.copy()
        knot["scene"]["sampling"].update(
            full_sensor_shape=[12, 14], crop_origin=[0, 0]
        )
    data["scene_sequence"]["knots"][-1]["time_ns"] = 30_000_000
    frame_ids = [frame["frame_id"] for frame in data["control_sequence"]["frames"]]
    data["capture_program"] = {
        "ordinary": lambda: default_capture_program(data["readout"], frame_ids),
        "trapped": lambda: default_trapped_capture_program(data["readout"], frame_ids),
        "paired": lambda: default_gain_merge_capture_program(data["readout"], frame_ids),
        "trapped_paired": lambda: default_gain_merge_capture_program(
            data["readout"], frame_ids, trapped=True
        ),
    }[family]()
    model = data["capture_program"]["electrical_model"]
    model["dark_current_e_per_s"] = 23
    model["fixed_pattern"].update(dsnu_e_per_s=7, seed=391)
    data["capture_program"]["retained_charge_model"]["transfer_efficiency"] = [
        0.2, 0.4, 0.6, 0.8
    ]
    data["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=250_000)
    runtime = RetainedChargeRuntime()
    full = runtime.execute(data)
    crop = {"crop_origin": [3, 5], "shape": [8, 8]}
    cropped_request = deepcopy(data)
    for knot in cropped_request["scene_sequence"]["knots"]:
        knot["scene"]["sampling"]["capture_crop"] = deepcopy(crop)
    cropped = runtime.execute(cropped_request)
    a, b = full["arrays"], cropped["arrays"]
    for name in (
        "mean_electrons", "retained_delivered_e", "retained_residual_e",
        "retained_dark_rate_e_s", "raw_groups", "cfa_channel_index",
        "retained_native_site_index",
    ):
        np.testing.assert_allclose(b[name], a[name][..., 3:11, 5:13], rtol=1e-12, atol=1e-10)
    assert cropped["summary"]["geometry"] == {
        "kind": "native_sensor", "full_sensor_shape": [12, 14], "crop_origin": [3, 5]
    }
    assert cropped["summary"]["retained_charge"]["receiver_state_geometry"] == {
        "full_sensor_shape": [12, 14], "crop_origin": [3, 5]
    }
    expected_rows = full["summary"]["retained_charge"]["events"]["frames"][0]
    actual_rows = cropped["summary"]["retained_charge"]["events"]["frames"][0]
    assert actual_rows["row_open_ns"] == expected_rows["row_open_ns"][3:11]
    assert actual_rows["row_transfer_ns"] == expected_rows["row_transfer_ns"][3:11]
    assert cropped["summary"]["retained_charge"]["events"]["capture_crop"] == crop
    assert runtime.estimate_resources(cropped_request)["output_array_bytes_estimate"] >= sum(
        value.nbytes for value in b.values()
    )


def test_roi_state_hash_binds_native_origin_and_rejects_forgery():
    model = {"schema": retained_charge.SCHEMA, "capacity_e": 100,
             "transfer_efficiency": [0.1, 0.2, 0.3, 0.4]}
    geometry = {"crop_origin": [3, 5], "full_sensor_shape": [12, 14]}
    state = retained_charge.initial_state([8, 8], model, noise_enabled=False, geometry=geometry)
    result = retained_charge.step(
        state, np.ones((8, 8)), 0.001, 0.0, model,
        np.ones((8, 8)), np.zeros((8, 8)), False, False, None,
    )
    assert result["state"]["schema"] == retained_charge.ROI_STATE_SCHEMA
    assert result["arrays"]["native_site_index"][0, 0] == 3
    np.testing.assert_allclose(result["arrays"]["delivered_e"][0:2, 0:2],
                               [[0.4, 0.3], [0.2, 0.1]])
    forged = deepcopy(state)
    forged["native_origin"] = [0, 0]
    with pytest.raises(ValueError, match="hash"):
        retained_charge.step(
            forged, np.ones((8, 8)), 0.001, 0.0, model,
            np.ones((8, 8)), np.zeros((8, 8)), False, False, None,
        )


def test_product_native_geometry_uses_bounded_receiver_state(request_data):
    data = deepcopy(request_data)
    data["capture_program"] = default_capture_program(
        data["readout"], [frame["frame_id"] for frame in data["control_sequence"]["frames"]]
    )
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].update(
            full_sensor_shape=[1440, 1920], crop_origin=[3, 5]
        )
    runtime = RetainedChargeRuntime()
    resources = runtime.estimate_resources(data)
    assert resources["full_native_pattern_bytes_estimate"] == 1440 * 1920 * 48
    result = runtime.execute(data)
    assert result["arrays"]["raw_groups"].shape == (2, 1, 8, 10)
    assert result["summary"]["geometry"]["crop_origin"] == [3, 5]
    assert result["arrays"]["retained_native_site_index"][0, 0, 0] == 3


def test_cropped_code_isp_matches_independent_native_phase_demosaic():
    raw = np.tile(np.array([[500, 300], [200, 100]], dtype=np.uint16), (2, 4, 4))
    data = code_isp_inputs(raw)
    data["geometry"] = {"kind": "native_sensor", "full_sensor_shape": [12, 14],
                        "crop_origin": [3, 5]}
    data["events"].update(
        schema="camera_retained_row_events_v3", full_sensor_shape=[12, 14],
        capture_crop={"crop_origin": [3, 5], "shape": [8, 8]},
    )
    data["events"]["schedule_sha256"] = digest({
        key: value for key, value in data["events"].items() if key != "schedule_sha256"
    })
    result = retained_isp.process_raw(raw, profile=retained_isp.default_profile(), **data)
    reference = isp.process_raw(
        raw, data["raw_format"], isp.default_profile(), geometry=data["geometry"]
    )
    np.testing.assert_allclose(result["arrays"]["rgb_frames"],
                               reference["arrays"]["rgb_frames"], rtol=0, atol=1e-12)
    np.testing.assert_array_equal(
        result["arrays"]["native_site_index"],
        np.tile(np.array([[3, 2], [1, 0]], dtype=np.uint8), (4, 4)),
    )
    assert result["summary"]["geometry"]["crop_origin"] == [3, 5]
    forged = deepcopy(data)
    forged["events"]["capture_crop"]["crop_origin"] = [1, 1]
    forged["events"]["schedule_sha256"] = digest({
        key: value for key, value in forged["events"].items() if key != "schedule_sha256"
    })
    with pytest.raises(isp.ISPError, match="native receiver geometry"):
        retained_isp.process_raw(raw, profile=retained_isp.default_profile(), **forged)


def test_registered_roi_raw_and_cold_isp_source_use_frozen_receiver(workflow, monkeypatch):
    service, sdk, assembly, scene, control, program, _, full_request = workflow
    crop = {"crop_origin": [3, 5], "shape": [8, 8]}
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
        capture_row=3, capture_col=5, capture_height=8, capture_width=8,
    ))
    queued = sdk.submit_retained_charge(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        seed=591, noise_enabled=False, capture_crop=crop,
    )
    assert queued["request"]["retained_charge_events"]["capture_crop"] == crop
    run = finish(service, queued)
    full = finish(service, service.submit_run(full_request))
    np.testing.assert_array_equal(
        load_array(service, run, "raw_groups"),
        load_array(service, full, "raw_groups")[..., 3:11, 5:13],
    )
    source = retained_isp_source.resolve(
        service, run["id"],
        {"schema": retained_isp_source.SELECTION_SCHEMA, "readout_id": "single"},
    )
    assert source["geometry"]["crop_origin"] == [3, 5]
    np.testing.assert_array_equal(
        retained_isp_source.load_inputs(service, source),
        load_array(service, run, "raw_groups")[:, 0],
    )
    isp_model = sdk.post("models", sdk.retained_isp_template(run["id"], "single"))
    try:
        replay_job = sdk.submit_retained_isp(run["id"], "single", isp_model["id"])
    except HTTPStatusError as exc:
        pytest.fail(exc.response.text)
    isp_run = finish(service, replay_job)
    np.testing.assert_array_equal(
        load_array(service, isp_run, "raw_frames"),
        load_array(service, run, "raw_groups")[:, 0],
    )
    np.testing.assert_array_equal(
        load_array(service, isp_run, "native_site_index"),
        np.tile(np.array([[3, 2], [1, 0]], dtype=np.uint8), (4, 4)),
    )
    assert isp_run["result"]["summary"]["geometry"]["crop_origin"] == [3, 5]
    original_run = service.registry.get_run
    original_bytes = retained_isp_source.artifact_bytes
    for field in ("receiver_state_geometry", "state_scope"):
        forged = deepcopy(run)
        nested = forged["result"]["summary"]["retained_charge"]
        if field == "receiver_state_geometry":
            nested[field]["crop_origin"] = [0, 0]
        else:
            nested[field] = "one_immutable_full_sequence_job"
        with monkeypatch.context() as patch:
            patch.setattr(service.registry, "get_run", lambda ident, forged=forged: (
                forged if ident == run["id"] else original_run(ident)
            ))
            patch.setattr(
                retained_isp_source, "artifact_bytes",
                lambda svc, artifact, forged=forged: (
                    canonical(forged["result"]["summary"]).encode()
                    if artifact["name"] == "summary.json"
                    else original_bytes(svc, artifact)
                ),
            )
            with pytest.raises(IntegrityError, match="native geometry"):
                retained_isp_source.resolve(
                    service, run["id"],
                    {"schema": retained_isp_source.SELECTION_SCHEMA,
                     "readout_id": "single"},
                )
    with pytest.raises(HTTPStatusError) as exc_info:
        sdk.submit_retained_charge(
            assembly["id"], test["id"], scene["id"], control["id"], program["id"],
            seed=591, noise_enabled=False, capture_crop={"crop_origin": [1, 1], "shape": [8, 8]},
        )
    assert exc_info.value.response.status_code == 422


@pytest.mark.parametrize("mode,readout_id", [
    ("retained_charge", "single"),
    ("trapped_charge", "single"),
    ("retained_gain_merge", "HCG"),
    ("trapped_gain_merge", "HCG"),
])
def test_registered_two_component_roi_replays_frozen_optical_knots_and_isp(
    workflow, mode, readout_id,
):
    service, sdk, assembly, scene, control, parent, _, _ = workflow
    ambient = sequences.create_fixture(
        service, sequences.fixture_template(service, assembly["id"])
        | {"name": "Retained ambient ROI", "revision": "ambient-roi-1",
           "shape": [16, 16], "knot_count": 2, "pattern": "static",
           "irradiance_scale": .25},
    )
    draft = capture_programs.template(
        service, assembly["id"], mode=mode, control_sequence_id=control["id"]
    )
    draft.update(name=f"Retained {mode} ROI", revision="components-roi-1",
                 parents=[parent["id"]])
    draft["payload"]["waveform"] = _waveform(control["payload"]["clock"]["clock_id"])
    program = capture_programs.create(service, draft)
    crop = {"crop_origin": [3, 5], "shape": [8, 8]}
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        unmodulated_scene_sequence_id=ambient["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
        capture_row=3, capture_col=5, capture_height=8, capture_width=8,
    ))
    queued = sdk.submit_retained_charge(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        seed=591, noise_enabled=False, capture_crop=crop,
        unmodulated_scene_sequence_id=ambient["id"],
    )
    run = finish(service, queued)
    assert load_array(service, run, "raw_groups").shape[-2:] == (8, 8)
    source = retained_isp_source.resolve(
        service, run["id"],
        {"schema": retained_isp_source.SELECTION_SCHEMA, "readout_id": readout_id},
    )
    assert source["geometry"]["crop_origin"] == [3, 5]
    isp_model = sdk.post("models", sdk.retained_isp_template(run["id"], readout_id))
    replay = finish(service, sdk.submit_retained_isp(run["id"], readout_id, isp_model["id"]))
    assert replay["result"]["summary"]["geometry"]["crop_origin"] == [3, 5]
    assert load_array(service, replay, "raw_frames").shape[-2:] == (8, 8)


def test_photo_hdr_inverse_still_refuses_receiver_roi(request_data):
    from camera_platform.retained_charge_runtime import default_photo_hdr_capture_program

    data = deepcopy(request_data)
    data["capture_program"] = default_photo_hdr_capture_program(
        data["readout"], [frame["frame_id"] for frame in data["control_sequence"]["frames"]]
    )
    for knot in data["scene_sequence"]["knots"]:
        knot["scene"]["sampling"].update(full_sensor_shape=[12, 14], crop_origin=[0, 0])
    with pytest.raises(ValueError, match="full-native"):
        RetainedChargeRuntime().estimate_resources(data)
