"""Retained RAW code replay: API, Worker, cache, restart and original evidence."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import finish, load_array
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow

from camera_platform import imaging, retained_isp
from camera_platform import retained_isp_replay as replay
from camera_platform.registry import IntegrityError
from camera_platform.service import PlatformService


def acquisition(workflow, family="single"):
    service, sdk, assembly, scene, control, parent, _, request = workflow
    if family != "single":
        from camera_platform import dcg_electrical, nonlinear_examples

        models = {
            "nonlinear": nonlinear_examples.single_model,
            "paired": dcg_electrical.default_model,
            "paired_nonlinear": nonlinear_examples.shared_model,
        }
        draft = sdk.template(
            "capture-program",
            assembly_id=assembly["id"],
            mode="retained_charge",
            control_sequence_id=control["id"],
        )
        draft.update(revision=family, parents=[parent["id"]])
        draft["payload"]["electrical_model"] = models[family](
            parent["sensor_model_snapshot"]["payload"]["readout"]
        )
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
        request = {**request, "capture_program_id": program["id"], "test_id": test["id"]}
    return service, sdk, finish(service, service.submit_run(request))


def payload(service, sdk, run, readout="single", *, enabled=False):
    draft = sdk.retained_isp_template(run["id"], readout)
    if enabled:
        draft["payload"]["spatial_filter"]["mode"] = "same_site_bilateral_code_v1"
        draft["payload"]["temporal_filter"]["mode"] = "causal_difference_gate_v1"
        draft["payload"]["detail"]["mode"] = "bounded_code_detail_v1"
    model = service.create_model(draft)
    return {
        "schema": replay.SCHEMA,
        "name": "Retained code replay",
        "source_kind": "run",
        "source_id": run["id"],
        "isp_model_id": model["id"],
        "selector": {"schema": "camera_retained_raw_selection_v1", "readout_id": readout},
    }


@pytest.mark.parametrize(
    "family,readout,index",
    [
        ("single", "single", 0),
        ("nonlinear", "single", 0),
        ("paired", "HCG", 0),
        ("paired_nonlinear", "LCG", 1),
    ],
)
def test_api_worker_selects_one_original_path_all_frames_and_preserves_format(
    workflow, family, readout, index
):
    service, sdk, run = acquisition(workflow, family)
    original = deepcopy(run)
    choices = [c for c in sdk.retained_isp_sources() if c["source_id"] == run["id"]]
    assert [c["readout_id"] for c in choices] == run["result"]["summary"]["readout_ids"]
    request = payload(service, sdk, run, readout)
    queued = sdk.post("replays", request)
    result = finish(service, queued)
    summary = result["result"]["summary"]
    expected = load_array(service, run, "raw_groups")[:, index]
    np.testing.assert_array_equal(load_array(service, result, "raw_frames"), expected)
    np.testing.assert_allclose(
        load_array(service, result, "rgb_frames"),
        load_array(service, run, "rgb_groups")[:, index],
        rtol=0,
        atol=1e-14,
    )
    assert summary["raw_format"] == run["result"]["summary"]["raw_formats"][index]
    assert summary["source_truth_arrays_used"] is False
    assert summary["charge_inversion"] is False and summary["dark_subtraction"] is False
    assert summary["physical_validation"] == "not_established"
    assert summary["frame_count"] == expected.shape[0]
    assert len(summary["stage_previews"]) == 7 * expected.shape[0]
    assert sdk.post("replays", request)["id"] == result["id"]
    assert sdk.get("replays/" + result["id"])["id"] == result["id"]
    assert any(r["id"] == result["id"] for r in sdk.get("replays"))
    assert not any(r["id"] == result["id"] for r in sdk.get("runs"))
    assert service.registry.get_run(run["id"]) == original
    assert not any(r["source_id"] == run["id"] for r in sdk.replay_sources())
    with pytest.raises(ValueError, match="[Rr]etained|[Dd]elivered"):
        imaging.replay_source(service, "run", run["id"])
    assert len([a for a in result["artifacts"] if a["name"] == "summary.json"]) == 1
    assert any(a["name"] == "acquisition-summary.json" for a in result["artifacts"])
    forecast = queued["evidence_forecast"]
    assert sum(a["bytes"] for a in result["artifacts"]) < forecast["archive_bytes_bound"]
    json.dumps(summary, allow_nan=False)


def test_bundle_cold_readback_and_only_declared_processor_inputs(workflow, monkeypatch):
    service, sdk, run = acquisition(workflow)
    request = payload(service, sdk, run, enabled=True)
    process = retained_isp.process_raw
    calls = []

    def isolated(raw, raw_format, profile, **kwargs):
        assert raw.ndim == 3 and raw.dtype.kind in "iu"
        assert set(kwargs) == {"geometry", "frame_records", "events"}
        assert all(
            "delivered_e" not in frame and "residual_e" not in frame
            for frame in kwargs["frame_records"]
        )
        calls.append(raw.copy())
        return process(raw, raw_format, profile, **kwargs)

    monkeypatch.setattr(retained_isp, "process_raw", isolated)
    result = finish(service, sdk.post("replays", request))
    assert len(calls) == 1
    cold = PlatformService(service.registry.directory)
    assert replay.verify(cold, cold.registry.get_run(result["id"]))["id"] == result["id"]
    bundle = sdk.replay_bundle(result["id"])
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["source"] == run
        assert manifest["factory_jobs"]
        for record in (run, result, *manifest["factory_jobs"]):
            for artifact in record["artifacts"]:
                content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
                assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
    assert len(bundle) <= result["evidence_forecast"]["archive_bytes_bound"]
    assert sdk.http.get(result["artifacts"][-1]["url"]).status_code == 200
    forced = finish(service, sdk.post("replays", {**request, "force": True}))
    assert forced["id"] != result["id"] and len(calls) == 2
    for name in ("temporal_dn_frames", "temporal_previous_state_dn_frames", "rgb_frames"):
        np.testing.assert_array_equal(
            load_array(service, forced, name), load_array(service, result, name)
        )
    assert service.registry.audit()["integrity"] == "verified"


@pytest.mark.parametrize("field", ["selector", "profile", "events", "source_hash", "budget"])
def test_frozen_replay_tampering_refused_before_processor(workflow, monkeypatch, field):
    service, sdk, run = acquisition(workflow)
    queued = replay.submit(service, payload(service, sdk, run))
    modified = deepcopy(queued)
    if field == "selector":
        modified["selector"]["readout_id"] = "LCG"
    elif field == "profile":
        modified["profile"]["temporal_filter"]["previous_weight"] = 0.1
    elif field == "events":
        modified["source"]["retained_charge_events"]["frames"][0]["row_transfer_ns"][0] += 1
    elif field == "source_hash":
        modified["source"]["raw_sha256"] = "0" * 64
    else:
        modified["evidence_forecast"]["archive_bytes_bound"] += 1
    monkeypatch.setattr(
        retained_isp, "process_raw", lambda *_a, **_kw: pytest.fail("processor called")
    )
    with pytest.raises((ValueError, IntegrityError)):
        service.execute_run(modified)


def test_historical_environment_is_readable_but_not_executable(workflow):
    service, sdk, run = acquisition(workflow)
    frozen = replay.prepare(service, payload(service, sdk, run))
    historical = {**frozen, "environment": {"historical": "short"}, "status": "queued"}
    assert replay.verify(service, historical) == historical
    with pytest.raises(IntegrityError, match="environment"):
        replay.execute(service, historical)


def test_budget_refusal_precedes_queue_and_blob_corruption_refuses_cache(workflow, monkeypatch):
    service, sdk, run = acquisition(workflow)
    request = payload(service, sdk, run)
    prior = len(service.registry.list_runs())
    with monkeypatch.context() as limited:
        limited.setattr(replay, "MAX_ARCHIVE_BYTES", 1)
        with pytest.raises(ValueError, match="budget"):
            replay.submit(service, request)
    assert len(service.registry.list_runs()) == prior
    result = finish(service, replay.submit(service, request))
    artifact = next(a for a in result["artifacts"] if a["name"] == "temporal_dn_frames.npy")
    path = service.registry.blob_path(artifact["sha256"])
    content = path.read_bytes()
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    for action in (
        lambda: replay.submit(service, request),
        lambda: service.export_replay(result["id"]),
        service.list_replays,
    ):
        with pytest.raises(IntegrityError):
            action()


def test_original_raw_corruption_refuses_queue_cache_and_export(workflow):
    service, sdk, run = acquisition(workflow)
    request = payload(service, sdk, run)
    result = finish(service, replay.submit(service, request))
    original = next(a for a in run["artifacts"] if a["name"] == "raw_groups.npy")
    path = service.registry.blob_path(original["sha256"])
    content = path.read_bytes()
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    for action in (
        lambda: replay.submit(service, request),
        lambda: replay.submit(service, {**request, "force": True}),
        lambda: service.export_replay(result["id"]),
        service.list_replays,
    ):
        with pytest.raises((ValueError, IntegrityError)):
            action()


@pytest.mark.parametrize("family", ["single", "nonlinear"])
def test_browser_json_number_roundtrip_registers_and_replays_same_raw(workflow, family):
    service, sdk, run = acquisition(workflow, family)
    draft = sdk.retained_isp_template(run["id"], "single")
    # JSON.stringify uses one Number type: integral 64.0 is serialized as 64.
    # This fixture contains only finite safe-range numbers; mirror that spelling.
    browser_draft = json.loads(
        json.dumps(draft),
        parse_float=lambda value: int(float(value)) if float(value).is_integer() else float(value),
    )
    assert type(draft["input_raw_format"]["black_level_dn"]) is float
    assert type(browser_draft["input_raw_format"]["black_level_dn"]) is int
    model = sdk.post("models", browser_draft)
    queued = sdk.submit_retained_isp(run["id"], "single", model["id"])
    result = finish(service, queued)
    assert result["isp_model_snapshot"]["input_raw_format"] == model["input_raw_format"]
    original_format = run["result"]["summary"]["raw_formats"][0]
    assert result["result"]["summary"]["raw_format"] == original_format
    assert type(result["result"]["summary"]["raw_format"]["black_level_dn"]) is float
    np.testing.assert_array_equal(
        load_array(service, result, "raw_frames"), load_array(service, run, "raw_groups")[:, 0]
    )
    assert sdk.submit_retained_isp(run["id"], "single", model["id"])["id"] == result["id"]


def test_browser_numeric_equivalence_does_not_relax_changed_formats(workflow):
    service, sdk, run = acquisition(workflow)
    base = sdk.retained_isp_template(run["id"], "single")
    count = len(service.registry.list_runs())
    for index, (field, value) in enumerate(
        [
            ("black_level_dn", 65),
            ("black_level_dn", True),
            ("response_type", "nonlinear_charge_voltage"),
            ("bit_depth", 12.0),
            ("hdr_streams", True),
        ]
    ):
        draft = deepcopy(base)
        draft.update(name="Invalid RAW comparison " + str(index), revision=str(index))
        draft["input_raw_format"][field] = value
        model = sdk.post("models", draft)
        with pytest.raises(ValueError, match="[Ff]ormat|black level"):
            replay.submit(
                service,
                {
                    "schema": replay.SCHEMA,
                    "name": "Refused format alias",
                    "source_kind": "run",
                    "source_id": run["id"],
                    "isp_model_id": model["id"],
                    "selector": {
                        "schema": "camera_retained_raw_selection_v1",
                        "readout_id": "single",
                    },
                },
            )
    assert len(service.registry.list_runs()) == count


@pytest.mark.parametrize("black", [True, False, "64", None, float("inf"), float("nan"), 10**1000])
def test_raw_format_numeric_guard_refuses_nonfinite_and_boolean_black_levels(black):
    with pytest.raises(ValueError, match="black level"):
        replay._format_comparison_key(
            {
                "cfa": "RGGB",
                "bit_depth": 12,
                "black_level_dn": black,
            }
        )
