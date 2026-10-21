"""Charge replay retains original acquisition and verifies cache/Worker evidence."""

import io
import warnings
from copy import deepcopy

import numpy as np
import pytest
from PIL import Image
from test_hdr_workflow import workflow as workflow
from test_isp_charge_source import acquisition, selector

from camera_platform import isp_charge_calibration, isp_replay_v3
from camera_platform.contracts import ReplayCreate
from camera_platform.registry import IntegrityError


def request(service, run, selected, *, lsc=False):
    calibration = None
    if lsc:
        calibration = isp_charge_calibration.create(
            service, isp_charge_calibration.template(service, "run", run["id"], selected)
        )
    document = isp_replay_v3.template(
        service,
        source_kind="run",
        source_id=run["id"],
        selector=selected,
        calibration_id=calibration["id"] if calibration else None,
    )
    model = service.create_model(document)
    return {
        "schema": isp_replay_v3.SCHEMA,
        "name": "Charge replay",
        "source_kind": "run",
        "source_id": run["id"],
        "selector": selected,
        "isp_model_id": model["id"],
    }


def finish(service, payload):
    job = isp_replay_v3.submit(service, payload)
    claimed = service.registry.claim()
    assert claimed["id"] == job["id"]
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        output = isp_replay_v3.execute(service, claimed)
    return service.registry.finish(job["id"], result=output)


@pytest.mark.parametrize(
    "mode,selected",
    [
        ("independent_bracket_nonlinear", selector()),
        ("reference_guarded_nonlinear", selector()),
        ("shared_charge_nonlinear", selector("hcg")),
        ("shared_charge_nonlinear", selector("lcg")),
    ],
)
def test_charge_replay_real_source_worker_artifacts_and_cached_readback(workflow, mode, selected):
    service, _, run = acquisition(workflow, mode)
    payload = request(service, run, selected, lsc=True)
    replay = finish(service, payload)
    summary = replay["result"]["summary"]
    assert summary["selector"] == selected
    assert summary["raw_sha256"] == replay["source"]["raw_sha256"]
    assert summary["frame_count"] == 4 and len(summary["acquisition_identity"]) == 4
    assert summary["physical_validation"] == "not_established"
    assert len(summary["stage_previews"]) <= 9 * 4
    assert isp_replay_v3.submit(service, payload)["id"] == replay["id"]
    assert isp_replay_v3.verify(service, replay) == replay
    artifacts = replay["result"]["artifacts"]
    preview = next(a for a in artifacts if a["name"] == "sensor-rgb.png")
    with Image.open(
        io.BytesIO(service.registry.blob_path(preview["sha256"]).read_bytes())
    ) as image:
        assert image.size == (16, 16)
    source_before = deepcopy(run)
    assert service.registry.get_run(run["id"]) == source_before
    frozen = isp_replay_v3.prepare(service, payload)
    forecast = isp_replay_v3.summary_forecast(frozen)
    saved = next(a for a in artifacts if a["name"] == "summary.json")
    assert saved["bytes"] <= forecast["summary_artifact_bytes_limit"]


def test_new_request_does_not_add_legacy_fields():
    old = {"name": "linear", "source_kind": "run", "source_id": "a", "isp_model_id": "b"}
    assert ReplayCreate.model_validate(old).model_dump() == {**old, "force": False}
    with pytest.raises(ValueError):
        isp_replay_v3.ReplayCreateV3.model_validate(old)


def test_queued_historical_verify_and_worker_environment_refusal(workflow):
    service, _, run = acquisition(workflow)
    payload = request(service, run, selector())
    frozen = isp_replay_v3.prepare(service, payload)
    historical = {**frozen, "environment": {"historical": "different"}, "status": "queued"}
    assert isp_replay_v3.verify(service, historical) == historical
    with pytest.raises(IntegrityError, match="environment"):
        isp_replay_v3.execute(service, historical)


def test_cached_result_blob_corruption_refused(workflow):
    service, _, run = acquisition(workflow)
    payload = request(service, run, selector())
    replay = finish(service, payload)
    artifact = next(
        a for a in replay["result"]["artifacts"] if a["name"] == "sharpened_rgb_frames.npy"
    )
    path = service.registry.blob_path(artifact["sha256"])
    content = path.read_bytes()
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    with pytest.raises(IntegrityError):
        isp_replay_v3.submit(service, payload)


def test_preview_sanitizes_invalid_display_without_mutating_physical_arrays(tmp_path):
    from types import SimpleNamespace

    from camera_platform.registry import SQLiteRegistry

    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path))
    values = np.full((1, 2, 2), np.nan)
    output = {
        "arrays": {
            "physical": values,
            "baseline_rgb_frames": np.zeros((1, 2, 2, 3)),
            "raw_valid": np.zeros((1, 2, 2), dtype=bool),
            "baseline_rgb_valid_mask_frames": np.ones((1, 2, 2), dtype=bool),
        },
        "summary": {"stage_outputs": {"decode": {"array": "physical", "validity": "raw_valid"}}},
    }
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        artifacts = isp_replay_v3._stage_previews(service, output)
    assert np.isnan(values).all() and len(artifacts) == 2


def test_stage_preview_masks_finite_but_invalid_physical_values(tmp_path):
    from types import SimpleNamespace

    from camera_platform.registry import SQLiteRegistry

    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path))
    values = np.ones((1, 2, 2, 3))
    output = {
        "arrays": {
            "physical": values,
            "valid": np.zeros((1, 2, 2), dtype=bool),
            "baseline_rgb_frames": np.zeros((1, 2, 2, 3)),
            "baseline_rgb_valid_mask_frames": np.zeros((1, 2, 2), dtype=bool),
        },
        "summary": {"stage_outputs": {"decode": {"array": "physical", "validity": "valid"}}},
    }
    artifacts = isp_replay_v3._stage_previews(service, output)
    artifact = next(a for a in artifacts if a["stage_array"] == "physical")
    with Image.open(service.registry.blob_path(artifact["sha256"])) as image:
        assert not np.asarray(image).any()
    assert (values == 1).all()


def test_template_preserves_original_fixed_color_without_enabling_new_stages(monkeypatch):
    from types import SimpleNamespace

    from camera_platform.isp import default_profile

    profile = default_profile()
    profile["white_balance"]["gains_rgb"] = [1.25, 1.0, 0.8]
    profile["ccm"]["matrix_rgb"] = [[0.9, 0.1, 0.0], [0.0, 1.0, 0.0], [0.05, 0.0, 0.95]]
    profile["tone"]["curve"] = "linear"
    model = {"id": "fixed-color", "kind": "isp", "payload": profile}
    records = {
        ("assemblies", "camera"): {"model_ids": [model["id"]]},
        ("models", model["id"]): model,
    }
    service = SimpleNamespace(
        registry=SimpleNamespace(get=lambda kind, identity: records[kind, identity])
    )
    monkeypatch.setattr(
        isp_replay_v3.sources,
        "resolve",
        lambda *a: {
            "source_snapshot": {"assembly_id": "camera"},
            "raw_format": {"response_type": "nonlinear_charge_voltage"},
        },
    )
    document = isp_replay_v3.template(
        service, source_kind="run", source_id="source", selector=selector()
    )
    for key in ("white_balance", "ccm", "tone"):
        assert document["payload"][key] == profile[key]
    for key in ("spatial_lsc", "raw_denoise", "linear_sharpen"):
        assert document["payload"][key]["mode"] == "bypass"
    assert document["parents"] == ["fixed-color"]
