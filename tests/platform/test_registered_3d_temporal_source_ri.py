"""A frozen moving 3D scene uses source RI before temporal RAW acquisition.

This verifies registration and execution of a discrete model, not measured
camera accuracy or continuous geometric motion between rendered knots.
"""

import hashlib
from copy import deepcopy

import numpy as np
import pytest
import test_registered_3d_pwm_trapped as moving_fixture
import test_scene_render

from camera_platform import scene_motion_sampling, sequences

MODE = "source_position_ri_v1"


def _array(service, run, name):
    artifact = next(item for item in run["artifacts"] if item["name"] == name + ".npy")
    return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)


def test_registered_moving_3d_source_ri_reaches_rolling_raw_and_immutable_bundle(
        tmp_path, monkeypatch):
    if not test_scene_render.PYTHON.exists():
        pytest.skip("Isolated Mitsuba environment is not installed")
    service, original_assembly, render_request = test_scene_render.scene_workflow.__wrapped__(
        tmp_path, monkeypatch)
    assembly = moving_fixture._small_native_camera(service, original_assembly)
    moving = moving_fixture._moving_scene(service, render_request["scene_asset_id"])
    clock = moving_fixture.CLOCK
    receiver = {"crop_origin": [10, 10], "shape": [12, 12]}
    render_request.update(
        assembly_id=assembly["id"], scene_asset_id=moving["id"],
        shape=receiver["shape"], crop_origin=receiver["crop_origin"],
        render_psf_halo=True, render_clock=clock, samples_per_pixel=2,
    )
    times = [0, 40_000_000, 80_000_000]
    renders = [
        test_scene_render.finish(service, queued)
        for queued in scene_motion_sampling.submit_renders(service, render_request, times)
    ]
    assert all(item["status"] == "succeeded" for item in renders)
    hits = [_array(service, run, "first-hit-object-index") for run in renders]
    assert any(not np.array_equal(hits[0], item) for item in hits[1:])
    scene = scene_motion_sampling.publish_sequence(
        service, [run["id"] for run in renders], name="Moving 3D source RI knots")
    control_data = sequences.control_template(service, assembly["id"], scene["id"])
    control_data["payload"]["mode"].update(
        shutter="rolling", line_time_ns=137_000, hidden_top_rows=1)
    control = sequences.create_sequence(service, "control_sequences", control_data)
    test = service.create_test(sequences.test_template(
        service, assembly["id"], scene["id"], control["id"], capture_crop=receiver))
    request = {
        "assembly_id": assembly["id"], "test_id": test["id"],
        "plugin_id": "temporal_scene_v1", "scene_sequence_id": scene["id"],
        "control_sequence_id": control["id"],
        "parameters": {"seed": 821, "capture_crop": receiver,
                       "field_radiometry_mode": MODE},
    }
    source_blobs = {
        artifact["sha256"]: service.registry.blob_path(artifact["sha256"]).read_bytes()
        for image in scene["image_snapshots"] for artifact in image["artifacts"]
    }
    queued = service.submit_run(request)
    assert queued["request"]["field_radiometry_mode"] == MODE
    assert queued["resources"]["source_position_ri_map_bytes"] > 0
    assert queued["request"]["scene_sequence"] == scene["payload"]
    forged = deepcopy(queued)
    forged["request"]["field_radiometry_mode"] = "receiver_position_ri_v1"
    with pytest.raises(ValueError, match="queued request differs"):
        sequences.execute_run(service, forged)
    run = test_scene_render.finish(service, queued)
    assert run["status"] == "succeeded", run.get("error")
    summary = run["result"]["summary"]
    assert summary["field_radiometry_mode"] == MODE
    assert summary["physical_validation"] == "not_established"
    assert summary["geometry"]["crop_origin"] == receiver["crop_origin"]
    assert len(summary["flux_diagnostics_per_scene"]) == len(times)
    for ledger in summary["flux_diagnostics_per_scene"]:
        assert ledger["capture_flux_scope"] == (
            "after_source_ri_and_psf_before_receiver_angular_qe")
        assert ledger["relative_illumination_application"] == MODE
    source_ri = _array(service, run, "source_relative_illumination_map")
    rates = _array(service, run, "channel_photoelectron_rate_scenes")
    raw = _array(service, run, "raw_frames")
    rgb = _array(service, run, "rgb_frames")
    assert source_ri.shape == tuple(scene["image_snapshots"][0]["shape"])
    assert rates.shape[0] == len(times)
    assert raw.shape[1:] == tuple(receiver["shape"])
    assert rgb.shape[1:] == (*receiver["shape"], 3)
    assert not np.array_equal(rates[0], rates[-1])
    for identity, content in source_blobs.items():
        assert hashlib.sha256(content).hexdigest() == identity
        assert service.registry.blob_path(identity).read_bytes() == content
    assert service.registry.audit()["integrity"] == "verified"

