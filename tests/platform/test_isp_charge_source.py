"""Retained nonlinear views bind the original paired bytes and acquisition settings."""

import hashlib
import io
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import workflow as workflow
from test_nonlinear_workflow import nonlinear_workflow, worker_finish

from camera_platform import isp_charge_calibration as calibration
from camera_platform import isp_charge_source as sources
from camera_platform.registry import IntegrityError, canonical, digest


def selector(readout=None):
    return {
        "schema": sources.SELECTION_SCHEMA,
        "mode": "sequential_frames" if readout is None else "shared_charge_path",
        **({"readout_id": readout} if readout is not None else {}),
    }


def acquisition(workflow, mode="independent_bracket_nonlinear"):
    service, sdk, _, _, _, _, _, request = nonlinear_workflow(workflow, mode)
    return service, sdk, worker_finish(service, request)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"schema": sources.SELECTION_SCHEMA},
        {"schema": sources.SELECTION_SCHEMA, "mode": "shared_charge_path", "readout_id": "HCG"},
        {"schema": sources.SELECTION_SCHEMA, "mode": "sequential_frames", "readout_id": "hcg"},
        {"schema": sources.SELECTION_SCHEMA, "mode": "sequential_frames", "frame_indices": [0]},
    ],
)
def test_selection_requires_exact_explicit_topology(value):
    with pytest.raises(ValueError):
        sources.validate_selector(value)


@pytest.mark.parametrize("value", [[], {}, ["hcg"]])
def test_malformed_selection_is_a_request_error(value):
    with pytest.raises(ValueError):
        sources.validate_selector({**selector("hcg"), "readout_id": value})


@pytest.mark.parametrize("field", ["source_kind", "dataset_role"])
def test_malformed_calibration_enum_is_a_request_error(field):
    payload = {key: None for key in calibration.FIELDS}
    payload.update(
        schema=calibration.SCHEMA,
        source_kind="estimated",
        name="a",
        revision="1",
        source_uri="test://example",
        license="test",
        method_and_version="test:1",
        parents=[],
        dataset_ids=[],
        dataset_role="development",
    )
    payload[field] = []
    with pytest.raises(ValueError):
        calibration.validate_payload(payload)


@pytest.mark.parametrize(
    "mode,selected",
    [
        ("independent_bracket_nonlinear", selector()),
        ("reference_guarded_nonlinear", selector()),
        ("shared_charge_nonlinear", selector("hcg")),
    ],
)
def test_exact_source_and_decoder_inputs_from_real_acquisition(workflow, mode, selected):
    service, _, run = acquisition(workflow, mode)
    source = sources.resolve(service, "run", run["id"], selected)
    raw, calibrations = sources.load_inputs(service, source)
    assert raw.shape == (4, 16, 16)
    assert source["raw_sha256"] == source["raw_artifact"]["sha256"]
    assert source["selected_array_sha256"] == sources.array_sha256(raw)
    assert len(calibrations) == len(source["acquisition_identity"]) == 4
    for index, cal in enumerate(calibrations):
        assert (
            hashlib.sha256(np.asarray(cal["dark_e"], dtype="<f8").tobytes()).hexdigest()
            == (cal["dark_e_sha256"])
        )
        assert cal["acquisition"]["exposure_s"] == (0.004 if index % 2 == 0 else 0.008)
    assert source["raw_format"]["response_type"] == "nonlinear_charge_voltage"
    assert source["source_snapshot"] == run


def test_shared_views_keep_one_container_and_one_acquisition(workflow):
    service, _, run = acquisition(workflow, "shared_charge_nonlinear")
    hcg = sources.resolve(service, "run", run["id"], selector("hcg"))
    lcg = sources.resolve(service, "run", run["id"], selector("lcg"))
    assert hcg["raw_sha256"] == lcg["raw_sha256"]
    assert hcg["raw_container_shape"] == [4, 2, 16, 16]
    assert hcg["raw_shape"] == lcg["raw_shape"] == [4, 16, 16]
    assert (
        hcg["normalization_e"]
        == lcg["normalization_e"]
        == (
            run["capture_program_snapshot"]["payload"]["electrical_model"][
                "shared_charge_capacity_e"
            ]
        )
    )
    assert [r["shared_charge_group_id"] for r in hcg["acquisition_identity"]] == [
        r["shared_charge_group_id"] for r in lcg["acquisition_identity"]
    ]
    for source in [hcg, lcg]:
        raw, _ = sources.load_inputs(service, source)
        original = np.load(service.registry.blob_path(source["raw_sha256"]), allow_pickle=False)
        np.testing.assert_array_equal(raw, original[:, 0 if source is hcg else 1])
    with pytest.raises(ValueError):
        sources.resolve(service, "run", run["id"], selector())


def test_changed_blob_or_selected_identity_is_not_cached(workflow):
    service, _, run = acquisition(workflow)
    source = sources.resolve(service, "run", run["id"], selector())
    altered = deepcopy(source)
    altered["selected_array_sha256"] = "0" * 64
    with pytest.raises(IntegrityError):
        sources.load_inputs(service, altered)
    path = service.registry.blob_path(source["raw_sha256"])
    content = path.read_bytes()
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    with pytest.raises(IntegrityError):
        sources.load_inputs(service, source)


@pytest.mark.parametrize(
    "field,value",
    [("shape", [4, 16, 18]), ("unit", "electron"), ("dtype", "float64"), ("bytes", 1)],
)
def test_forged_raw_descriptor_rejected(workflow, monkeypatch, field, value):
    service, _, run = acquisition(workflow)
    changed = deepcopy(run)
    artifact = next(a for a in changed["artifacts"] if a["name"] == "raw_frames.npy")
    artifact[field] = value
    original = service.registry.get_run
    monkeypatch.setattr(
        service.registry, "get_run", lambda i: deepcopy(changed) if i == run["id"] else original(i)
    )
    with pytest.raises((ValueError, IntegrityError)):
        sources.resolve(service, "run", run["id"], selector())


def test_self_consistent_calibration_substitution_cannot_replace_program(workflow, monkeypatch):
    service, _, run = acquisition(workflow)
    changed = deepcopy(run)
    summary = changed["result"]["summary"]
    for cal in summary["calibrations"]:
        cal["model"]["digital_gain"] *= 0.9
        cal["model_sha256"] = digest(cal["model"])
    replacement = service.registry.store_blob(
        canonical(summary).encode(), "summary.json", "application/json"
    )
    changed["artifacts"] = [
        replacement if a["name"] == "summary.json" else a for a in changed["artifacts"]
    ]
    original = service.registry.get_run
    monkeypatch.setattr(
        service.registry, "get_run", lambda i: deepcopy(changed) if i == run["id"] else original(i)
    )
    with pytest.raises(IntegrityError, match="model differs"):
        sources.resolve(service, "run", run["id"], selector())


def test_charge_lsc_calibration_retains_path_and_byte_binding(workflow):
    service, _, run = acquisition(workflow, "shared_charge_nonlinear")
    document = calibration.template(service, "run", run["id"], selector("hcg"))
    record = calibration.create(service, document)
    assert record == calibration.get_calibration(service, record["id"])
    assert record["binding"]["signal_basis"] == "calibrated_photocharge_e_before_lsc"
    hcg = sources.resolve(service, "run", run["id"], selector("hcg"))
    assert calibration.resolve_for_source(service, record["id"], hcg) == record
    lcg = sources.resolve(service, "run", run["id"], selector("lcg"))
    with pytest.raises(ValueError, match="binding differs"):
        calibration.resolve_for_source(service, record["id"], lcg)
    invalid = deepcopy(document)
    invalid["payload"]["noise"] = None
    with pytest.raises(ValueError):
        calibration.create(service, invalid)
    path = service.registry.blob_path(record["artifacts"][0]["sha256"])
    path.write_bytes(b"{}")
    with pytest.raises(IntegrityError):
        calibration.get_calibration(service, record["id"])


def test_float_dark_header_rejects_object_or_size_before_load(tmp_path):
    from types import SimpleNamespace

    from camera_platform.registry import SQLiteRegistry

    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path))
    stream = io.BytesIO()
    np.save(stream, np.zeros((1, 2, 2), dtype=object), allow_pickle=True)
    artifact = service.registry.store_blob(
        stream.getvalue(), "dark.npy", "application/octet-stream"
    )
    artifact.update(
        shape=[1, 2, 2], dtype="object", unit="electron/pixel", spatial_sampling="native_sensor"
    )
    with pytest.raises(ValueError):
        sources._array(service, artifact, dimensions=3, kind="f", unit="electron/pixel")


@pytest.mark.parametrize(
    "shape", [(65, 2, 2), (1, 3, 2), (1, 2, 4098), (1, 1024, 1026), (1, 3, 2, 2)]
)
def test_native_limits_rejected_from_header_before_np_load(tmp_path, monkeypatch, shape):
    from types import SimpleNamespace

    from camera_platform.registry import SQLiteRegistry

    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path))
    stream = io.BytesIO()
    np.save(stream, np.zeros(shape, dtype=np.int32), allow_pickle=False)
    artifact = service.registry.store_blob(stream.getvalue(), "raw.npy", "application/octet-stream")
    artifact.update(shape=list(shape), dtype="int32", unit="DN", spatial_sampling="native_sensor")
    monkeypatch.setattr(
        np, "load", lambda *a, **k: pytest.fail("np.load must not allocate this input")
    )
    with pytest.raises(ValueError, match="bounds"):
        sources._array(service, artifact, dimensions=len(shape), kind="iu", unit="DN")


def test_dark_requires_retained_float64_before_np_load(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from camera_platform.registry import SQLiteRegistry

    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path))
    stream = io.BytesIO()
    np.save(stream, np.zeros((1, 2, 2), dtype=np.float32), allow_pickle=False)
    artifact = service.registry.store_blob(
        stream.getvalue(), "dark.npy", "application/octet-stream"
    )
    artifact.update(
        shape=[1, 2, 2], dtype="float32", unit="electron/pixel", spatial_sampling="native_sensor"
    )
    monkeypatch.setattr(
        np, "load", lambda *a, **k: pytest.fail("np.load must not allocate this input")
    )
    with pytest.raises(ValueError, match="dtype"):
        sources._array(service, artifact, dimensions=3, kind="f", unit="electron/pixel")
