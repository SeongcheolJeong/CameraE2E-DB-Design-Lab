"""A published halo render retains its receiver ROI when run parameters omit it."""

from copy import deepcopy

import pytest
from test_scene_render import finish
from test_scene_render import scene_workflow as scene_workflow

from camera_platform import imaging, scene_render


def test_published_halo_render_defaults_to_its_frozen_receiver_roi(scene_workflow):
    service, assembly, render_request = scene_workflow
    render_request.update(
        shape=[8, 8], crop_origin=[0, 1], render_psf_halo=True,
        samples_per_pixel=2,
    )
    render_job = finish(service, scene_render.submit(service, render_request))
    image = scene_render.publish(
        service, {"job_id": render_job["id"], "name": "Pinned receiver ROI"}
    )
    intended = render_job["request"]["capture_crop"]
    assert image["shape"][:2] != intended["shape"]
    test = service.create_test(
        imaging.spatial_test_template(service, assembly["id"], image["id"])
    )
    queued = service.submit_run({
        "assembly_id": assembly["id"], "test_id": test["id"],
        "plugin_id": "spatial_scene_v1", "scene_id": image["id"],
        "parameters": {"frames": 1, "seed": 5},
    })
    assert queued["parameters"]["capture_crop"] == intended
    assert queued["request"]["scene"]["sampling"]["capture_crop"] == intended
    completed = finish(service, queued)
    assert completed["result"]["summary"]["output_size"] == intended["shape"]

    forged = deepcopy(image)
    forged["render_job_snapshot"]["result"]["capture_crop"]["shape"] = [8, 10]
    with pytest.raises(ValueError, match="frozen source"):
        imaging._rendered_capture_crop(forged)
