"""Fixed AE meter path and independently selected shared-charge code ISP input."""

import io
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_electrical_control_workflow import workflow as _workflow
from test_electrical_isp_source import return_record, rewrite_summary
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as _hdr_workflow
from test_retained_charge_workflow import workflow as _retained_workflow

from camera_platform import electrical_control, electrical_isp_source, retained_isp
from camera_platform import retained_isp_source as source
from camera_platform.registry import IntegrityError, canonical

hdr_workflow = _hdr_workflow
retained_workflow = _retained_workflow
workflow = _workflow


def selector(path):
    return {"schema": source.SELECTION_SCHEMA, "readout_id": path}


def acquisition(workflow, meter="HCG", *, nonlinear=False):
    service, sdk, assembly, scene, control, parent, _, request = workflow
    mode = "electrical_control_" + meter.lower() + ("_nonlinear" if nonlinear else "")
    draft = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode=mode, control_sequence_id=control["id"]
    )
    draft.update(revision="paired-isp-" + meter.lower(), parents=[parent["id"]])
    draft["payload"]["electrical_control_policy"]["target_black_relative_dn"] = {
        "minimum": 0.5,
        "maximum": 1.0,
    }
    # Distinct declared path gains/black levels must survive display selection;
    # the shared acquisition gain is one and is not either readout gain.
    for index, path in enumerate(draft["payload"]["electrical_model"]["paths"]):
        path["analog_gain"] = 1.25 if index == 0 else 2.0
        path["digital_gain"] = 1.0 if index == 0 else 1.5
        path["adc"]["black_level_dn"] = 64.0 if index == 0 else 96.0
    program = sdk.create_capture_program(draft)
    test = service.create_test(
        sdk.template(
            "temporal-test",
            assembly_id=assembly["id"],
            scene_sequence_id=scene["id"],
            control_sequence_id=control["id"],
            capture_program_id=program["id"],
        )
    )
    run = finish(
        service,
        service.submit_run({**request, "capture_program_id": program["id"], "test_id": test["id"]}),
    )
    return service, sdk, run


def replay_request(service, sdk, run, path):
    draft = sdk.retained_isp_template(run["id"], path)
    draft["payload"]["temporal_filter"].update(
        mode="causal_difference_gate_v1",
        threshold_dn=1_000_000,
        previous_weight=0.5,
    )
    model = service.create_model(draft)
    return {
        "schema": "camera_retained_code_isp_replay_v1",
        "name": "Independent display path",
        "source_kind": "run",
        "source_id": run["id"],
        "selector": selector(path),
        "isp_model_id": model["id"],
    }


@pytest.mark.parametrize(
    "nonlinear,meter", [(False, "HCG"), (False, "LCG"), (True, "HCG"), (True, "LCG")]
)
def test_opposite_path_isp_uses_original_codes_while_feedback_stays_on_authored_meter(
    workflow,
    nonlinear,
    meter,
    monkeypatch,
):
    service, sdk, original = acquisition(workflow, meter, nonlinear=nonlinear)
    before = deepcopy(original)
    summary = original["result"]["summary"]
    raw = load_array(service, original, "raw_groups")
    assert raw.shape == (4, 2, 16, 16)
    assert not np.array_equal(raw[:, 0], raw[:, 1])
    meter_index = ["HCG", "LCG"].index(meter)
    display_index = 1 - meter_index
    display = ["HCG", "LCG"][display_index]
    selected = [source.resolve(service, original["id"], selector(path)) for path in ("HCG", "LCG")]
    paths = original["request"]["capture_program"]["electrical_model"]["paths"]
    for index, value in enumerate(selected):
        np.testing.assert_array_equal(source.load_inputs(service, value), raw[:, index])
        assert value["readout_index"] == index
        assert value["raw_format"] == summary["raw_formats"][index]
        assert value["raw_format"]["response_type"] == (
            "nonlinear_charge_voltage" if nonlinear else "linear"
        )
        assert value["control_trace"] == summary["electrical_control"]
        assert value["control_trace"]["readout_id"] == meter
        assert value["control_trace"]["observation_readout_index"] == meter_index
        assert value["planning_contract"]["observation_readout_id"] == meter
        assert value["planning_contract"]["readout_index"] == meter_index
        assert all(c["gain"] == 1 for c in value["frame_conditions"])
        assert all(
            c["readout_analog_gain"] == paths[index]["analog_gain"]
            for c in value["frame_conditions"]
        )
        assert all(
            c["readout_digital_gain"] == paths[index]["digital_gain"]
            for c in value["frame_conditions"]
        )
        assert [i["shared_charge_group_id"] for i in value["acquisition_identity"]] == summary[
            "frame_ids"
        ]
        for frame_index, frame in enumerate(value["control_trace"]["frames"]):
            expected = electrical_control.raw_statistics(
                raw[frame_index, meter_index],
                summary["raw_formats"][meter_index],
                value["control_trace"]["policy"],
            )
            assert frame["statistics"] == expected
    assert selected[0]["raw_sha256"] == selected[1]["raw_sha256"]
    assert selected[0]["selected_array_sha256"] != selected[1]["selected_array_sha256"]
    choices = [c for c in sdk.retained_isp_sources() if c["source_id"] == original["id"]]
    assert [c["readout_id"] for c in choices] == ["HCG", "LCG"]
    assert all(
        c["observation_readout_id"] == meter and c["observation_readout_index"] == meter_index
        for c in choices
    )

    process, calls = retained_isp.process_raw, []

    def only_selected_codes(codes, raw_format, profile, **kwargs):
        np.testing.assert_array_equal(codes, raw[:, display_index])
        assert set(kwargs) == {"geometry", "frame_records", "events"}
        assert "control_trace" not in kwargs and "retained_residual_e" not in canonical(kwargs)
        assert raw_format == summary["raw_formats"][display_index]
        calls.append(codes.copy())
        return process(codes, raw_format, profile, **kwargs)

    monkeypatch.setattr(retained_isp, "process_raw", only_selected_codes)
    request = replay_request(service, sdk, original, display)
    result = finish(service, sdk.post("replays", request))
    assert len(calls) == 1
    np.testing.assert_array_equal(load_array(service, result, "raw_frames"), raw[:, display_index])
    np.testing.assert_array_equal(
        load_array(service, result, "black_relative_dn_frames"),
        raw[:, display_index].astype(float)
        - summary["raw_formats"][display_index]["black_level_dn"],
    )
    replay_summary = result["result"]["summary"]
    assert replay_summary["readout_id"] == display
    assert replay_summary["control_trace"]["readout_id"] == meter
    assert replay_summary["raw_format"] == summary["raw_formats"][display_index]
    assert replay_summary["control_trace"]["raw_format"] == summary["raw_formats"][meter_index]
    assert not replay_summary["source_truth_arrays_used"] and not replay_summary["charge_inversion"]
    assert sdk.post("replays", request)["id"] == result["id"]
    assert service.registry.get_run(original["id"]) == before
    if nonlinear and meter == "HCG":
        forced = finish(service, sdk.post("replays", {**request, "force": True}))
        assert forced["id"] != result["id"] and len(calls) == 2
        np.testing.assert_array_equal(
            load_array(service, forced, "rgb_frames"), load_array(service, result, "rgb_frames")
        )
        with zipfile.ZipFile(io.BytesIO(sdk.replay_bundle(result["id"]))) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["source"] == original
            original_raw = next(a for a in original["artifacts"] if a["name"] == "raw_groups.npy")
            np.testing.assert_array_equal(
                np.load(
                    io.BytesIO(
                        archive.read("artifacts/" + original_raw["sha256"] + "/raw_groups.npy")
                    ),
                    allow_pickle=False,
                ),
                raw,
            )


def test_controller_reconstruction_cannot_use_display_path_or_relabel_meter(workflow, monkeypatch):
    service, _, original = acquisition(workflow, "HCG")
    summary = original["result"]["summary"]
    raw = load_array(service, original, "raw_groups")
    program = original["request"]["capture_program"]
    # The observed statistics distinguish paths even when both commands happen
    # to quantize to the same bounded exposure.
    _, wrong = electrical_isp_source._replay_controller(
        raw[:, 1],
        summary["raw_formats"][0],
        original["request"]["control_sequence"],
        program,
        summary["geometry"],
    )
    assert (
        wrong["frames"][0]["statistics"] != summary["electrical_control"]["frames"][0]["statistics"]
    )
    for field in (
        "meter_id",
        "meter_index",
        "meter_format",
        "statistics",
        "path_format",
        "descriptor",
    ):
        altered = deepcopy(original)
        trace = altered["result"]["summary"]["electrical_control"]
        if field == "meter_id":
            trace["readout_id"] = "LCG"
        elif field == "meter_index":
            trace["observation_readout_index"] = 1
        elif field == "meter_format":
            trace["raw_format"] = summary["raw_formats"][1]
        elif field == "statistics":
            trace["frames"][0]["statistics"] = wrong["frames"][0]["statistics"]
        elif field == "path_format":
            altered["result"]["summary"]["raw_formats"][1]["black_level_dn"] += 1
        else:
            altered["result"]["summary"]["readout_descriptors"][0]["input_quantity"] = (
                "current_photocharge"
            )
        rewrite_summary(service, altered)
        with monkeypatch.context() as patch:
            return_record(patch, service, altered)
            with pytest.raises(IntegrityError, match="Electrical"):
                source.resolve(service, altered["id"], selector("LCG"))
    with pytest.raises(ValueError, match="original acquired readout"):
        source.resolve(service, original["id"], selector("single"))


def test_unselected_path_blob_integrity_is_still_required(workflow):
    service, _, original = acquisition(workflow, "HCG")
    selected = source.resolve(service, original["id"], selector("HCG"))
    artifact = next(a for a in original["artifacts"] if a["name"] == "raw_groups.npy")
    path = service.registry.blob_path(artifact["sha256"])
    content = path.read_bytes()
    # Last byte belongs to LCG, not either the HCG meter or selected ISP path.
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    with pytest.raises(IntegrityError):
        source.load_inputs(service, selected)


def test_non_meter_codes_do_not_drive_feedback_but_all_path_ranges_are_checked(
    workflow, monkeypatch
):
    service, _, original = acquisition(workflow, "HCG")
    raw = load_array(service, original, "raw_groups")
    actual, trace = electrical_isp_source._replay_controller(
        raw[:, 0],
        original["result"]["summary"]["raw_formats"][0],
        original["request"]["control_sequence"],
        original["request"]["capture_program"],
        original["result"]["summary"]["geometry"],
    )
    assert actual == original["result"]["summary"]["control_sequence"]
    assert trace == original["result"]["summary"]["electrical_control"]
    altered = deepcopy(original)
    changed = raw.copy()
    changed[0, 1, 0, 0] = 2 ** original["result"]["summary"]["raw_formats"][1]["bit_depth"]
    stream = io.BytesIO()
    np.save(stream, changed, allow_pickle=False)
    old = next(a for a in altered["artifacts"] if a["name"] == "raw_groups.npy")
    artifact = service.registry.store_blob(stream.getvalue(), old["name"], old["media_type"])
    artifact.update({k: v for k, v in old.items() if k not in artifact})
    altered["artifacts"] = [
        artifact if a["name"] == old["name"] else a for a in altered["artifacts"]
    ]
    return_record(monkeypatch, service, altered)
    with pytest.raises(IntegrityError, match="code/path range"):
        source.resolve(service, altered["id"], selector("HCG"))
