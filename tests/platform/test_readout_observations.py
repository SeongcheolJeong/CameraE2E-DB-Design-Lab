"""Authored anchored-target import tests; no measured-camera accuracy claim."""

import io
import json
from copy import deepcopy

import numpy as np
import pytest
from test_nonlinear_captures import capture_service as capture_service
from test_nonlinear_captures import metadata as capture_metadata

from camera_platform import nonlinear_captures as captures
from camera_platform import readout_observations as observations
from camera_platform.readout_fit_contracts import (
    ObsROI,
    ReadoutFitObservationImport,
    ReadoutFitPublish,
    ReadoutFitRequest,
)
from camera_platform.registry import IntegrityError, canonical


def capture(
    library,
    mode="sequential_frames",
    suffix="original",
    *,
    count=4,
    origin=(2, 4),
    offset=0,
    change=None,
):
    service, assembly, test = library
    data = capture_metadata(service, assembly, test, mode, suffix=suffix, count=count)
    data["geometry"]["crop_origin"] = list(origin)
    if change:
        change(data)
        for frame in data["frames"]:
            if frame["applied"]["applied_at_ns"] is not None:
                frame["applied"]["applied_at_ns"] = frame["frame_start_ns"]
    raw = (np.arange(count * 64).reshape(count, 8, 8) % 1000 + 100 + offset).astype(np.uint16)
    if mode == "shared_charge_paths":
        raw = np.stack([raw, raw + 500], axis=1)
    stream = io.BytesIO()
    np.save(stream, raw, allow_pickle=False)
    return captures.import_capture(service, data, stream.getvalue()), raw


def metadata(service, record, *, name="authored targets", role="development"):
    data = observations.template(service, record["id"])
    data.update(
        name=name,
        source_kind="simulated",
        source_uri="fixture://independent-total-charge-targets",
        method_and_version="authored-targets:1",
        license="Authored test data",
        dataset_ids=["targets-" + name],
        dataset_role=role,
    )
    data["charge_reference"]["uncertainty"]["value"] = 0.0
    data["charge_reference"]["uncertainty"]["description"] = "Exact authored fixture targets"
    data["roi"] = {"row_range": [2, 6], "column_range": [4, 8]}
    for frame in data["frames"]:
        frame["mean_total_charge_e"] = [1.0, 2.0, 3.0, 4.0]
    return data


def imported(service, data, *, evidence=None, metadata_bytes=None):
    evidence = observations.evidence_template(data) if evidence is None else evidence
    return observations.import_observations(
        service, data, json.dumps(evidence, indent=2).encode(), metadata_bytes=metadata_bytes
    )


@pytest.mark.parametrize("mode", ["sequential_frames", "shared_charge_paths"])
def test_original_bytes_snapshot_roles_and_fresh_verified_roundtrip(capture_service, mode):
    service = capture_service[0]
    source, _ = capture(capture_service, mode)
    data = metadata(service, source)
    original = (json.dumps(data, indent=2).replace("\n", "\r\n") + "\r\n").encode()
    evidence = (json.dumps(observations.evidence_template(data), indent=3) + "\n").encode()
    before = deepcopy(data)
    record = observations.import_observations(service, data, evidence, metadata_bytes=original)
    assert data == before
    assert observations.get(service, record["id"]) == record
    assert record["capture_snapshot"] == source
    assert record["dataset_role"] == "development"
    artifacts = {a["name"]: a for a in record["artifacts"]}
    assert set(artifacts) == observations.NAMES
    for name, content in (
        (observations.METADATA_NAME, original),
        (observations.EVIDENCE_NAME, evidence),
    ):
        assert service.registry.blob_path(artifacts[name]["sha256"]).read_bytes() == content
        assert (
            service.registry.artifact_owner(artifacts[name]["sha256"])["owner"]["id"]
            == record["id"]
        )
    assert record["physical_validation"] == "not_established"
    assert record["authority"] == {"physical_validation": False, "release": False}
    assert record["assumptions"]["total_physical_uncertainty"] == "unknown"


@pytest.mark.parametrize("mode", ["sequential_frames", "shared_charge_paths"])
def test_exact_mean_groups_preserve_cfa_order_shared_paths_and_total_dark_mean(
    capture_service, mode
):
    service = capture_service[0]
    source, raw = capture(capture_service, mode)
    record = imported(service, metadata(service, source))
    result = observations.groups(service, record)
    assert result["capture"] == source
    assert result["sites"] == ["R", "Gr", "Gb", "B"]
    assert result["temperature_c"] == 25
    assert result["analog_gains"] == ([2.0, 1.0] if raw.ndim == 4 else [2.0])
    assert result["digital_gains"] == ([1.0, 1.0] if raw.ndim == 4 else [1.0])
    assert result["exposure_s"] == [0.004] * 4
    assert result["sample_rows"] == 64
    assert result["raw_code_samples"] == (128 if raw.ndim == 4 else 64)
    assert [g["mean_charge_e"] for g in result["groups"]] == [1.0, 2.0, 3.0, 4.0]
    for site, (dr, dc) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
        expected = (
            np.concatenate([raw[i, :, dr:4:2, dc:4:2].reshape(2, -1).T for i in range(4)])
            if raw.ndim == 4
            else raw[:, dr:4:2, dc:4:2].reshape(-1, 1)
        )
        np.testing.assert_array_equal(result["groups"][site]["raw_dn"], expected)
    # Targets are total pre-full-well charge, not inferred or dark-corrected values.
    assert record["frames"][0]["mean_total_charge_e"] == [1.0, 2.0, 3.0, 4.0]


def test_native_even_roi_uses_global_cfa_phase_with_an_odd_crop_origin(capture_service):
    service = capture_service[0]
    source, raw = capture(capture_service, origin=(1, 3))
    result = observations.groups(service, imported(service, metadata(service, source)))
    np.testing.assert_array_equal(
        result["groups"][0]["raw_dn"], raw[:, 1:5:2, 1:5:2].reshape(-1, 1)
    )


def test_grouping_merges_equal_declared_means_but_never_rounds_close_targets(capture_service):
    service = capture_service[0]
    source, _ = capture(capture_service)
    data = metadata(service, source)
    close = float(np.nextafter(1.0, 2.0))
    for frame in data["frames"]:
        frame["mean_total_charge_e"] = [1.0, close, 1.0, close]
    result = observations.groups(service, imported(service, data))
    assert [g["mean_charge_e"] for g in result["groups"]] == [1.0, close]
    assert [g["raw_dn"].shape for g in result["groups"]] == [(32, 1), (32, 1)]


def test_template_does_not_infer_targets_uncertainty_or_external_evidence(capture_service):
    service = capture_service[0]
    source, _ = capture(capture_service)
    template = observations.template(service, source["id"])
    assert [f["frame_id"] for f in template["frames"]] == [f["frame_id"] for f in source["frames"]]
    assert all(f["mean_total_charge_e"] == [None] * 4 for f in template["frames"])
    assert template["charge_reference"]["uncertainty"]["value"] is None
    with pytest.raises(ValueError):
        ReadoutFitObservationImport.model_validate(template)
    template["charge_reference"]["kind"] = "external_charge"
    with pytest.raises(ValueError, match="never generated"):
        observations.evidence_template(template)


@pytest.mark.parametrize(
    "target", [[0, 1, 2], [-1, 1, 2, 3], [np.nan, 1, 2, 3], [np.inf, 1, 2, 3], [True, 1, 2, 3]]
)
def test_charge_targets_require_four_finite_nonnegative_site_values(capture_service, target):
    service = capture_service[0]
    source, _ = capture(capture_service)
    data = metadata(service, source)
    data["frames"][0]["mean_total_charge_e"] = target
    with pytest.raises(ValueError):
        imported(service, data)
    assert not service.registry.list("readout_observations")


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["frames"].reverse(),
        lambda d: d["frames"].pop(),
        lambda d: d["frames"][0].update(frame_id="different-frame"),
        lambda d: d["frames"][1].update(frame_id=d["frames"][0]["frame_id"]),
    ],
)
def test_frame_targets_are_exact_complete_original_order(capture_service, change):
    service = capture_service[0]
    source, _ = capture(capture_service, "shared_charge_paths")
    data = metadata(service, source)
    change(data)
    with pytest.raises(ValueError):
        imported(service, data)


@pytest.mark.parametrize(
    "roi",
    [
        {"row_range": [1, 6], "column_range": [4, 8]},
        {"row_range": [2, 5], "column_range": [4, 8]},
        {"row_range": [2, 2], "column_range": [4, 8]},
        {"row_range": [0, 4], "column_range": [4, 8]},
        {"row_range": [2, 12], "column_range": [4, 8]},
    ],
)
def test_roi_is_even_full_native_and_inside_capture(capture_service, roi):
    service = capture_service[0]
    source, _ = capture(capture_service)
    data = metadata(service, source)
    data["roi"] = roi
    with pytest.raises(ValueError):
        imported(service, data)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["frames"][0]["settings_evidence"].update(status="command_only"),
        lambda d: d["frames"][0]["applied"].update(temperature_c=26.0),
        lambda d: d["frames"][0]["applied"]["readout_settings"][0].update(digital_gain=2.0),
        lambda d: (
            d["frames"][0]["applied"].update(gain=3.0),
            d["frames"][0]["applied"]["readout_settings"][0].update(analog_gain=3.0),
        ),
    ],
)
def test_unconfirmed_or_changing_settings_cannot_enter_fitting(capture_service, change):
    service = capture_service[0]
    source, _ = capture(capture_service, change=change)
    with pytest.raises(ValueError, match="recorded applied|remain fixed"):
        imported(service, metadata(service, source))


def test_unknown_time_is_retained_and_variable_exposure_is_not_changed(capture_service):
    service = capture_service[0]

    def change(data):
        data["clock"] = {"status": "unknown", "reason": "Not provided"}
        data["timing_mode"] = {"shutter": "unknown", "reason": "Not provided"}
        for i, frame in enumerate(data["frames"]):
            frame["frame_start_ns"] = None
            frame["applied"]["applied_at_ns"] = None
            frame["applied"]["exposure_ns"] = (i + 1) * 1_000_000

    source, _ = capture(capture_service, change=change)
    result = observations.groups(service, imported(service, metadata(service, source)))
    assert result["exposure_s"] == [0.001, 0.002, 0.003, 0.004]
    assert all(f["frame_start_ns"] is None for f in result["capture"]["frames"])


def test_measured_capture_requires_external_anchor_and_exact_physical_identity(capture_service):
    service = capture_service[0]

    def measured(data):
        data.update(source_kind="measured", physical_instance="declared-camera-serial")
        data["instrument"] = {"id": "declared-bench", "calibration_revision": "supplied-v1"}

    source, _ = capture(capture_service, change=measured)
    data = metadata(service, source)
    with pytest.raises(ValueError, match="authored charge anchor"):
        imported(service, data)
    evidence = observations.evidence_template(data)
    data["charge_reference"]["kind"] = "external_charge"
    data["physical_instance"] = "wrong-serial"
    with pytest.raises(ValueError, match="physical instance"):
        imported(service, data, evidence=evidence)
    data["physical_instance"] = source["physical_instance"]
    record = imported(service, data, evidence=evidence)
    assert record["physical_validation"] == "not_established"
    assert record["charge_reference"]["kind"] == "external_charge"


@pytest.mark.parametrize("which", ["metadata", "evidence"])
def test_original_json_duplicate_nonfinite_and_oversized_inputs_refused(capture_service, which):
    service = capture_service[0]
    source, _ = capture(capture_service)
    data = metadata(service, source)
    content = canonical(
        data if which == "metadata" else observations.evidence_template(data)
    ).encode()
    key = "name" if which == "metadata" else "schema"
    duplicate = ('{"' + key + '":"forged",').encode() + content[1:]
    values = [
        duplicate,
        b'{"n":NaN}',
        b'{"n":1e400}',
        b"\xff",
        b" "
        * (
            (
                observations.MAX_METADATA_BYTES
                if which == "metadata"
                else observations.MAX_EVIDENCE_BYTES
            )
            + 1
        ),
    ]
    for bad in values:
        with pytest.raises(ValueError):
            observations.import_observations(
                service,
                data,
                bad
                if which == "evidence"
                else canonical(observations.evidence_template(data)).encode(),
                metadata_bytes=bad if which == "metadata" else None,
            )
    assert not service.registry.list("readout_observations")


def test_reference_source_method_and_document_role_are_bound(capture_service):
    service = capture_service[0]
    source, _ = capture(capture_service)
    data = metadata(service, source)
    for key, value in (
        ("source_uri", "other://source"),
        ("method_and_version", "other:1"),
        ("role", "other"),
        ("id", "other-id"),
    ):
        evidence = observations.evidence_template(data)
        evidence["documents"][0][key] = value
        with pytest.raises(ValueError):
            imported(service, data, evidence=evidence)


def test_selected_sample_and_exact_group_budgets_precede_group_allocation(
    capture_service, monkeypatch
):
    service = capture_service[0]
    source, _ = capture(capture_service, "shared_charge_paths", count=64)
    data = metadata(service, source)
    data["roi"] = {"row_range": [2, 10], "column_range": [4, 12]}
    record = imported(service, data)
    assert record["binding"]["raw_code_samples"] == 8192
    monkeypatch.setattr(observations, "MAX_RAW_CODE_SAMPLES", 8191)
    monkeypatch.setattr(
        np, "concatenate", lambda *a, **kw: pytest.fail("group allocation before budget")
    )
    monkeypatch.setattr(np, "load", lambda *a, **kw: pytest.fail("RAW allocation before budget"))
    with pytest.raises(ValueError, match="8192"):
        observations.groups(service, record)
    monkeypatch.setattr(observations, "MAX_RAW_CODE_SAMPLES", 8192)
    for i, frame in enumerate(data["frames"]):
        frame["mean_total_charge_e"] = [float(i * 4 + site) for site in range(4)]
    with pytest.raises(ValueError, match="128"):
        imported(service, data)


def test_fresh_bytes_changed_or_deleted_cannot_be_hidden_by_prior_reads(capture_service):
    service = capture_service[0]
    source, _ = capture(capture_service)
    record = imported(service, metadata(service, source))
    observations.get(service, record["id"])
    artifact = next(a for a in record["artifacts"] if a["name"] == observations.EVIDENCE_NAME)
    path = service.registry.blob_path(artifact["sha256"])
    content = path.read_bytes()
    path.write_bytes(b"X" + content[1:])
    with pytest.raises(IntegrityError):
        observations.groups(service, record)
    path.unlink()
    with pytest.raises((IntegrityError, KeyError)):
        observations.get(service, record["id"])


def test_original_capture_bytes_are_reverified_before_observation_reads(capture_service):
    service = capture_service[0]
    source, _ = capture(capture_service)
    record = imported(service, metadata(service, source))
    artifact = next(a for a in source["artifacts"] if a["name"] == "raw-capture.npy")
    path = service.registry.blob_path(artifact["sha256"])
    content = path.read_bytes()
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    with pytest.raises(IntegrityError):
        observations.get(service, record["id"])


def test_original_metadata_and_non_rggb_capture_cannot_be_reinterpreted(capture_service):
    service, assembly, test = capture_service
    source, _ = capture(capture_service)
    data = metadata(service, source)
    with pytest.raises(ValueError, match="Original observation metadata differs"):
        imported(service, data, metadata_bytes=canonical({**data, "name": "other"}).encode())
    sensor = next(m for m in source["model_snapshots"] if m["kind"] == "sensor")
    payload = {
        k: deepcopy(v) for k, v in sensor.items() if k not in {"id", "created_at", "content_hash"}
    }
    payload["raw_format"]["cfa"] = "BGGR"
    other = service.registry.put("models", payload)
    payload = {
        k: deepcopy(v) for k, v in assembly.items() if k not in {"id", "created_at", "content_hash"}
    }
    payload["model_ids"] = [
        other["id"] if key == sensor["id"] else key for key in payload["model_ids"]
    ]
    other_assembly = service.registry.put("assemblies", payload)
    other_source, _ = capture((service, other_assembly, test), suffix="bggr", offset=1500)
    with pytest.raises(ValueError, match="native RGGB"):
        imported(service, metadata(service, other_source))


def test_capture_parent_closure_retains_legacy_pixels_and_reverifies_original_bytes(
    capture_service,
):
    service = capture_service[0]
    legacy_raw = np.arange(64, dtype=np.uint16).reshape(1, 8, 8) + 2500
    stream = io.BytesIO()
    np.save(stream, legacy_raw, allow_pickle=False)
    artifact = service.registry.store_blob(
        stream.getvalue(), "raw-capture.npy", "application/x-npy"
    )
    legacy = service.registry.put(
        "captures",
        {
            "schema": "camera_capture_v1",
            "parents": [],
            "dataset_ids": ["legacy-dataset"],
            "raw_content_hash": captures.pixel_hash(legacy_raw),
            "raw_frame_hashes": [captures.pixel_hash(legacy_raw[0])],
            "artifacts": [artifact],
        },
    )
    source, _ = capture(capture_service, change=lambda data: data.update(parents=[legacy["id"]]))
    record = imported(service, metadata(service, source))
    lineage = observations.lineage(service, record)
    assert {c["id"] for c in lineage["captures"]} == {legacy["id"], source["id"]}
    assert {
        "capture-id:" + legacy["id"],
        "dataset-id:legacy-dataset",
        "raw-frame-sha256:" + legacy["raw_frame_hashes"][0],
    } <= set(lineage["tokens"])
    assert set(record["capture_closure_sha256"]) == {legacy["id"], source["id"]}
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"X" * artifact["bytes"])
    with pytest.raises(IntegrityError):
        observations.get(service, record["id"])


def test_parent_observation_node_budget_does_not_silently_drop_ancestors(
    capture_service, monkeypatch
):
    service = capture_service[0]
    source, _ = capture(capture_service)
    parent = imported(service, metadata(service, source))
    data = metadata(service, source, name="child")
    data["parents"] = [parent["id"]]
    monkeypatch.setattr(observations, "MAX_ANCESTORS", 1)
    with pytest.raises(ValueError, match="ancestry budget"):
        imported(service, data)
    assert len(service.registry.list("readout_observations")) == 1


def test_parent_observation_and_capture_lineage_roles_and_cas_closure(capture_service, monkeypatch):
    service = capture_service[0]
    source, _ = capture(capture_service, "shared_charge_paths")
    parent = imported(service, metadata(service, source, name="parent", role="holdout"))
    data = metadata(service, source, name="child")
    data["parents"] = [parent["id"]]
    passed = []
    original = service.registry.put_verified_record

    def inspect(kind, document, refs, **kwargs):
        passed.extend(refs)
        return original(kind, document, refs, **kwargs)

    monkeypatch.setattr(service.registry, "put_verified_record", inspect)
    record = imported(service, data)
    lineage = observations.lineage(service, record)
    assert {r["id"] for r in lineage["observations"]} == {record["id"], parent["id"]}
    assert {r["dataset_role"] for r in lineage["observations"]} == {"development", "holdout"}
    assert set(captures.lineage(service, source)) <= set(lineage["tokens"])
    assert {"dataset-id:targets-parent", "dataset-id:targets-child"} <= set(lineage["tokens"])
    assert {"models", "assemblies", "tests", "captures", "readout_observations"} == {
        kind for kind, _ in passed
    }
    # The identical authored evidence document is shared and deduplicated by name/SHA.
    assert len(lineage["artifacts"]) == 6
    # Changing targets or dataset label cannot manufacture a new exposure identity.
    sibling = imported(service, metadata(service, source, name="new-target-file"))
    assert set(lineage["tokens"]) & set(observations.lineage(service, sibling)["tokens"])


def test_observation_ancestry_cannot_change_physical_instance_or_mode(capture_service):
    service = capture_service[0]
    source, _ = capture(capture_service)
    parent = imported(service, metadata(service, source))
    child_source, _ = capture(
        capture_service,
        "shared_charge_paths",
        suffix="child",
        offset=1500,
        change=lambda d: d.update(physical_instance="different-instance"),
    )
    data = metadata(service, child_source)
    data["parents"] = [parent["id"]]
    with pytest.raises(ValueError, match="ancestry changes"):
        imported(service, data)


def test_stale_record_arguments_and_changed_capture_reference_fail_closed(
    capture_service, monkeypatch
):
    service = capture_service[0]
    source, _ = capture(capture_service)
    data = metadata(service, source)
    record = imported(service, data)
    forged = deepcopy(record)
    forged["frames"][0]["mean_total_charge_e"][0] += 1
    for function in (observations.groups, observations.lineage):
        with pytest.raises(IntegrityError, match="immutable revision"):
            function(service, forged)
    original = service.registry.put_verified_record

    def deleted(kind, document, refs, **kwargs):
        with service.registry.connection() as conn:
            # Simulate external database corruption after validation; ordinary API
            # mutations are independently blocked by the immutable-record trigger.
            conn.execute("DROP TRIGGER immutable_records_delete")
            conn.execute("DELETE FROM records WHERE kind='captures' AND id=?", (source["id"],))
            conn.commit()
        return original(kind, document, refs, **kwargs)

    monkeypatch.setattr(service.registry, "put_verified_record", deleted)
    with pytest.raises(IntegrityError, match="Verified reference changed"):
        imported(service, data)
    assert len(service.registry.list("readout_observations")) == 1


def test_readset_retry_publishes_one_observation_and_one_audit(capture_service, monkeypatch):
    service = capture_service[0]
    source, _ = capture(capture_service)
    original = service.registry._same_read_rows
    calls = []

    def once(conn, queries, before):
        calls.append(1)
        return False if len(calls) == 1 else original(conn, queries, before)

    monkeypatch.setattr(service.registry, "_same_read_rows", once)
    record = imported(service, metadata(service, source))
    assert len(calls) == 2
    assert observations.get(service, record["id"]) == record
    with service.registry.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM audit WHERE action='readout_observations.created'"
            ).fetchone()[0]
            == 1
        )


def request():
    return {
        "schema": "camera_readout_fit_request_v1",
        "name": "bounded authored fit",
        "base_calibration_id": "base",
        "training_observation_ids": ["train"],
        "validation_observation_ids": ["validate"],
        "slope_scale_bounds": [0.01, 100.0],
        "sigma_v_bounds": [1e-8, 1.0],
        "max_iterations": 10,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("training_observation_ids", []),
        ("validation_observation_ids", ["v", "v"]),
        ("validation_observation_ids", ["train"]),
        ("training_observation_ids", [" "]),
        ("slope_scale_bounds", [0.0, 1.0]),
        ("slope_scale_bounds", [1.0, 1.0]),
        ("slope_scale_bounds", [1.0, 101.0]),
        ("sigma_v_bounds", [0.0, 1.0]),
        ("sigma_v_bounds", [0.1, 1.1]),
        ("max_iterations", 9),
        ("max_iterations", 201),
        ("max_iterations", True),
    ],
)
def test_fit_request_has_explicit_disjoint_identities_and_strict_bounds(field, value):
    data = request()
    data[field] = value
    with pytest.raises(ValueError):
        ReadoutFitRequest.model_validate(data)


def test_request_publish_and_roi_strict_valid_contracts():
    assert ReadoutFitRequest.model_validate(request()).max_iterations == 10
    assert (
        ReadoutFitPublish.model_validate(
            {
                "name": "frozen candidate",
                "revision": "2",
                "request_sha256": "a" * 64,
                "result_sha256": "b" * 64,
            }
        ).revision
        == "2"
    )
    with pytest.raises(ValueError):
        ReadoutFitPublish.model_validate(
            {
                "name": "missing identity",
                "revision": "2",
                "request_sha256": "a" * 63,
                "result_sha256": "b" * 64,
            }
        )
    with pytest.raises(ValueError):
        ObsROI.model_validate({"row_range": [False, 2], "column_range": [0, 2]})
