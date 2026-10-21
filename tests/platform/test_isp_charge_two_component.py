"""A retained nonlinear RAW view preserves both authored illumination sources."""

from copy import deepcopy

import pytest
from test_hdr_workflow import workflow as workflow
from test_nonlinear_workflow import worker_finish

from camera_platform import capture_programs, isp_charge_source, sequences
from camera_platform.registry import IntegrityError, canonical


def _dual_run(workflow):
    service, _, assembly, primary, control, _, _, request = workflow
    secondary = sequences.create_fixture(
        service,
        sequences.fixture_template(service, assembly["id"])
        | {"name": "Separate unmodulated source", "revision": "isp-source-1",
           "shape": [16, 16], "knot_count": 2, "pattern": "static",
           "irradiance_scale": .25},
    )
    draft = capture_programs.template(
        service, assembly["id"], mode="independent_bracket_nonlinear"
    )
    draft.update(name="Two-component ISP acquisition", revision="isp-source-1")
    draft["payload"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": control["payload"]["clock"]["clock_id"],
        "period_ns": 1_000_000, "on_time_ns": 500_000,
        "phase_origin_ns": 0, "low": 0, "high": 1,
    }
    program = capture_programs.create(service, draft)
    test = service.create_test(sequences.test_template(
        service, assembly["id"], primary["id"], control["id"],
        program["id"], secondary["id"],
    ))
    run = worker_finish(service, {
        **request, "test_id": test["id"], "capture_program_id": program["id"],
        "parameters": {"seed": 927, "unmodulated_scene_sequence_id": secondary["id"]},
    })
    return service, run, secondary


def _selector():
    return {"schema": isp_charge_source.SELECTION_SCHEMA, "mode": "sequential_frames"}


def test_dual_component_nonlinear_raw_source_retains_second_input(workflow):
    service, run, secondary = _dual_run(workflow)
    source = isp_charge_source.resolve(service, "run", run["id"], _selector())
    assert source["source_snapshot"] == run
    assert source["geometry"] == run["result"]["summary"]["geometry"]
    assert source["source_snapshot"]["unmodulated_scene_sequence_snapshot"] == secondary


@pytest.mark.parametrize("mutation", [
    "snapshot", "request", "parameter", "source_artifact", "summary_hash", "summary_stage",
])
def test_dual_component_source_refuses_secondary_substitution(workflow, monkeypatch, mutation):
    service, run, secondary = _dual_run(workflow)
    forged = deepcopy(run)
    if mutation == "snapshot":
        forged["unmodulated_scene_sequence_snapshot"]["content_hash"] = "0" * 64
    elif mutation == "request":
        forged["request"]["unmodulated_scene_sequence"]["knots"][0]["time_ns"] += 1
    elif mutation == "parameter":
        forged["parameters"]["unmodulated_scene_sequence_id"] = "another-scene"
    elif mutation == "source_artifact":
        artifact = secondary["artifacts"][0]
        forged["input_artifacts"] = [
            item for item in forged["input_artifacts"]
            if (item["sha256"], item["name"]) != (artifact["sha256"], artifact["name"])
        ]
    else:
        summary = forged["result"]["summary"]
        if mutation == "summary_hash":
            summary["unmodulated_scene_sequence_hash"] = "0" * 64
        else:
            summary["illumination_components"]["reference_plane"] = "sensor_raw"
        artifact = service.registry.store_blob(
            canonical(summary).encode(), "summary.json", "application/json"
        )
        forged["artifacts"] = [
            artifact if item["name"] == "summary.json" else item
            for item in forged["artifacts"]
        ]
    original = service.registry.get_run
    monkeypatch.setattr(
        service.registry, "get_run",
        lambda identity: deepcopy(forged) if identity == run["id"] else original(identity),
    )
    with pytest.raises(IntegrityError, match="illumination|component|source artifact"):
        isp_charge_source.resolve(service, "run", run["id"], _selector())


def test_receiver_geometry_uses_frozen_crop_not_larger_source(workflow, monkeypatch):
    service, run, _ = _dual_run(workflow)
    changed = deepcopy(run)
    scene = changed["scene_sequence_snapshot"]
    source_image = scene["image_snapshots"][0]
    source_origin = source_image["sampling"]["crop_origin"]
    capture = {"crop_origin": [source_origin[0] + 4, source_origin[1] + 4],
               "shape": [8, 8]}
    for image in scene["image_snapshots"]:
        image["sampling"]["capture_crop"] = capture
    original = service.registry.get
    monkeypatch.setattr(
        service.registry, "get",
        lambda kind, identity: deepcopy(source_image)
        if kind == "spectral_images" and identity == source_image["id"]
        else original(kind, identity),
    )
    geometry, pitch = isp_charge_source._source_geometry(service, changed, (8, 8))
    assert geometry["crop_origin"] == capture["crop_origin"]
    assert pitch == source_image["sampling"]["pixel_pitch_um"]
    with pytest.raises(IntegrityError, match="receiver crop"):
        isp_charge_source._source_geometry(service, changed, (8, 10))

