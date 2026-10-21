"""Temporal camera orchestration preserves clocks, settings, RAW and source bytes."""

import copy
import json
import zipfile

import numpy as np
import pytest

from camera_platform import factory, imaging, sequences
from camera_platform.backup import backup_state, restore_state
from camera_platform.contracts import AssemblyCreate
from camera_platform.demo import seed
from camera_platform.evidence import build_bundle
from camera_platform.service import PlatformService, PreflightError


def finish(service, queued):
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    return service.registry.finish(job["id"], result=service.execute_run(job))


def array(service, record, name):
    artifact = next(a for a in record["artifacts"] if a["name"] == name + ".npy")
    return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)


@pytest.fixture
def workflow(tmp_path):
    service = PlatformService(tmp_path / "temporal")
    seed(service)
    original = service.registry.list("assemblies")[0]
    base = next(
        service.registry.get("models", i)
        for i in original["model_ids"]
        if service.registry.get("models", i)["kind"] == "lens"
    )
    job = finish(service, factory.submit(service, factory.template(service, "prysm_psf_v1")))
    lens = factory.publish(
        service,
        {
            "job_id": job["id"],
            "base_lens_model_id": base["id"],
            "name": "Temporal optical response",
            "revision": "1",
            "conditions": original["conditions"],
            "condition_scope": "optical_response",
        },
    )
    fields = {k: copy.deepcopy(v) for k, v in original.items() if k in AssemblyCreate.model_fields}
    fields.update(name="Temporal camera", revision="temporal-1")
    fields["model_ids"] = [lens["id"] if i == base["id"] else i for i in fields["model_ids"]]
    assembly = service.create_assembly(fields)
    scene = sequences.create_fixture(
        service,
        sequences.fixture_template(service, assembly["id"]) | {"shape": [16, 16], "knot_count": 17},
    )
    control_data = sequences.control_template(service, assembly["id"], scene["id"])
    frames = control_data["payload"]["frames"]
    for index, frame in enumerate(frames):
        frame["applied"]["exposure_ns"] = 4_000_000 if index % 2 == 0 else 8_000_000
    # A command on frame 0 takes effect on frame 1. An observed readout event is
    # retained; missing transport/application events must remain unknown.
    frames[0]["commanded"] = {
        "command_id": "next-exposure",
        "issued_at_ns": 0,
        "exposure_ns": 8_000_000,
        "gain": 1,
        "isp_model_id": frames[1]["applied"]["isp_model_id"],
    }
    frames[1]["applied"]["command_id"] = "next-exposure"
    frames[1]["sensor_readout_end_ns"] = 36_000_000
    original_text = "  \r\n" + json.dumps(control_data, ensure_ascii=False, indent=2) + "\r\n"
    control = sequences.import_sequence(service, "control_sequences", original_text)
    test = service.create_test(
        sequences.test_template(service, assembly["id"], scene["id"], control["id"])
    )
    request = {
        "assembly_id": assembly["id"],
        "test_id": test["id"],
        "plugin_id": "temporal_scene_v1",
        "scene_sequence_id": scene["id"],
        "control_sequence_id": control["id"],
        "parameters": {"seed": 471},
    }
    return service, assembly, scene, control, test, request, original_text


def test_temporal_run_replay_and_bundle_preserve_acquisition(workflow):
    service, assembly, scene, control, test, request, original_text = workflow
    run = finish(service, service.submit_run(request))
    summary = run["result"]["summary"]
    assert summary["frame_count"] == 4
    assert summary["clock"] == control["payload"]["clock"]
    assert summary["frame_conditions"] == test["frame_conditions"]
    assert summary["prediction"]["included_effects"] == test["included_effects"]
    assert summary["timing"]["frames"][1]["command_to_applied_ns"] == 20_000_000
    assert summary["timing"]["frames"][1]["transport_end_ns"] is None
    raw, rgb = array(service, run, "raw_frames"), array(service, run, "rgb_frames")
    assert raw.shape == (4, 16, 16) and rgb.shape == (4, 16, 16, 3)
    assert not np.array_equal(raw[0], raw[1])
    for index, frame in enumerate(summary["frames"]):
        assert frame["frame_index"] == index
        assert frame["applied"] == control["payload"]["frames"][index]["applied"]
        assert (
            service.registry.blob_path(frame["preview_artifact_id"])
            .read_bytes()
            .startswith(b"\x89PNG")
        )
    fields = imaging.isp_template(service, assembly["id"])
    fields["payload"]["white_balance"]["gains_rgb"] = [1.3, 1.0, 0.75]
    isp = service.create_model(fields)
    replay = finish(
        service,
        imaging.submit_replay(
            service,
            {
                "name": "Temporal preserved RAW",
                "source_kind": "run",
                "source_id": run["id"],
                "isp_model_id": isp["id"],
            },
        ),
    )
    np.testing.assert_array_equal(array(service, replay, "raw_frames"), raw)
    assert not np.array_equal(array(service, replay, "rgb_frames"), rgb)
    output = replay["result"]["summary"]
    assert output["clock"] == summary["clock"]
    assert output["frame_conditions"] == summary["frame_conditions"]
    assert output["timing"] == summary["timing"]
    for source_frame, replay_frame in zip(summary["frames"], output["frames"], strict=True):
        assert source_frame["applied"] == replay_frame["applied"]
        assert replay_frame["replay_isp_model_id"] == isp["id"]
    artifact = next(a for a in control["artifacts"] if a["name"].endswith("-source.json"))
    with zipfile.ZipFile(build_bundle(service, "replays", replay["id"])) as archive:
        assert archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}") == (
            original_text.encode()
        )
        for image in scene["image_snapshots"]:
            for item in image["artifacts"]:
                assert archive.read(f"artifacts/{item['sha256']}/{item['name']}") == (
                    service.registry.blob_path(item["sha256"]).read_bytes()
                )
    assert service.registry.audit()["integrity"] == "verified"
    assert service.registry.list("measurements") == []
    root = service.registry.directory.parent
    backup_state(service.registry.directory, root / "backup.zip")
    restore_state(root / "backup.zip", root / "restored")
    restored = PlatformService(root / "restored")
    assert restored.registry.get("control_sequences", control["id"]) == control
    assert restored.registry.get_run(run["id"]) == run
    np.testing.assert_array_equal(array(restored, replay, "raw_frames"), raw)


@pytest.mark.parametrize("field,value", [("bit_depth", 10), ("black_level_dn", 65)])
def test_declared_raw_must_match_actual_temporal_readout(workflow, field, value):
    service, assembly, scene, control, _, request, _ = workflow
    sensor = next(
        service.registry.get("models", i)
        for i in assembly["model_ids"]
        if service.registry.get("models", i)["kind"] == "sensor"
    )
    data = imaging._model_payload(sensor)
    data.update(name="Mismatched RAW readout", revision=field)
    data["payload"]["readout"][field] = value
    changed_sensor = service.create_model(data)
    data = {k: copy.deepcopy(v) for k, v in assembly.items() if k in AssemblyCreate.model_fields}
    data.update(name="Mismatched temporal sensor", revision=field)
    data["model_ids"] = [
        changed_sensor["id"] if i == sensor["id"] else i for i in data["model_ids"]
    ]
    changed_assembly = service.create_assembly(data)
    data = {
        k: copy.deepcopy(v)
        for k, v in control.items()
        if k in sequences.ControlSequenceCreate.model_fields
    }
    data.update(assembly_id=changed_assembly["id"], revision=field)
    changed_control = sequences.create_sequence(service, "control_sequences", data)
    test = service.create_test(
        sequences.test_template(service, changed_assembly["id"], scene["id"], changed_control["id"])
        | {"revision": field}
    )
    with pytest.raises(ValueError, match="RAW contract differs"):
        service.submit_run(
            request
            | {
                "assembly_id": changed_assembly["id"],
                "control_sequence_id": changed_control["id"],
                "test_id": test["id"],
            }
        )


def test_temporal_rejects_scalar_overrides_and_mismatched_test(workflow):
    service, _, _, _, test, request, _ = workflow
    for key, value in [("exposure_ms", 8), ("frames", 2), ("output_size", [8, 8])]:
        with pytest.raises(ValueError, match="Recorded settings"):
            service.submit_run(request | {"parameters": {key: value}})
    changed = copy.deepcopy(test)
    changed = {
        k: v for k, v in changed.items() if k not in {"id", "created_at", "content_hash", "kind"}
    }
    changed["revision"] = "wrong-clock"
    changed["clock"]["clock_id"] = "different-clock"
    other = service.create_test(changed)
    with pytest.raises(ValueError, match="exact sequence, clock"):
        service.submit_run(request | {"test_id": other["id"]})


def test_temporal_sensor_domain_and_replay_check_every_frame(workflow):
    service, assembly, scene, control, _, request, _ = workflow
    data = {
        k: copy.deepcopy(v)
        for k, v in control.items()
        if k in sequences.ControlSequenceCreate.model_fields
    }
    data["revision"] = "unsupported-gain"
    data["payload"]["frames"][-1]["applied"]["gain"] = 2
    changed = sequences.create_sequence(service, "control_sequences", data)
    test = service.create_test(
        sequences.test_template(service, assembly["id"], scene["id"], changed["id"])
        | {"revision": "gain-2"}
    )
    with pytest.raises(PreflightError):
        service.submit_run(request | {"control_sequence_id": changed["id"], "test_id": test["id"]})
    run = finish(service, service.submit_run(request))
    fields = imaging.isp_template(service, assembly["id"])
    fields["valid_domain"]["exposure_s"]["max"] = 0.006
    isp = service.create_model(fields)
    with pytest.raises(ValueError, match="original acquisition conditions"):
        imaging.submit_replay(
            service,
            {
                "name": "Too narrow",
                "source_kind": "run",
                "source_id": run["id"],
                "isp_model_id": isp["id"],
            },
        )


def test_flicker_fixture_rejects_aliasing_before_creating_images(tmp_path):
    service = PlatformService(tmp_path / "flicker")
    seed(service)
    payload = sequences.fixture_template(service) | {"pattern": "flicker", "frequency_hz": 10000}
    with pytest.raises(ValueError, match="8 samples per period"):
        sequences.create_fixture(service, payload)
    assert service.registry.list("spectral_images") == []
