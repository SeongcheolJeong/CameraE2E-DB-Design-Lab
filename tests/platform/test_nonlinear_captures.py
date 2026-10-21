"""Independent authored integer captures test import contracts, not physical accuracy."""

import io
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, local
from types import SimpleNamespace

import numpy as np
import pytest

from camera_platform import imaging, native_model_evidence
from camera_platform import nonlinear_captures as captures
from camera_platform.nonlinear_capture_contracts import NonlinearCaptureImport
from camera_platform.registry import (
    ConflictError,
    IntegrityError,
    SQLiteRegistry,
)


@pytest.fixture
def capture_service(tmp_path, monkeypatch):
    """No runtime/Factory or frozen environment; isolate the persistence boundary."""
    registry = SQLiteRegistry(tmp_path / "capture-qa")
    sensor = registry.put(
        "models",
        {
            "kind": "sensor",
            "payload": {"pixel_pitch_um": 2.0},
            "properties": {
                "active_width": {"value": 0.032, "unit": "mm"},
                "active_height": {"value": 0.032, "unit": "mm"},
            },
            "raw_format": {"cfa": "RGGB", "bit_depth": 12, "black_level": 64},
        },
    )
    lens = registry.put("models", {"kind": "lens", "payload": {}})
    isp = registry.put("models", {"kind": "isp", "payload": {}})
    conditions = {"temperature_c": {"value": 25.0, "unit": "degC"}}
    assembly = registry.put(
        "assemblies", {"model_ids": [lens["id"], sensor["id"], isp["id"]], "conditions": conditions}
    )
    test = registry.put("tests", {"conditions": conditions})
    service = SimpleNamespace(registry=registry)
    monkeypatch.setattr(imaging, "_current", lambda s: {"fixture": "independently-authored-import"})
    monkeypatch.setattr(native_model_evidence, "verify_models", lambda s, m: [])
    return service, assembly, test


def metadata(service, assembly, test, mode="sequential_frames", *, suffix="a", count=4):
    data = captures.template(service, assembly["id"], test["id"], mode)
    data.update(
        source_kind="simulated",
        source_uri="fixture://independent-integer-RAW/" + suffix,
        method_and_version="authored-import-contract-fixture:1",
        license="Authored test data",
        dataset_ids=["fixture-dataset-" + suffix],
        acquisition_group_id="fixture-group-" + suffix,
        name="Authored RAW fixture " + suffix,
        clock={
            "clock_id": "authored-clock",
            "unit": "ns",
            "reference": "relative",
            "origin": "authored zero",
        },
        timing_mode={
            "shutter": "global",
            "line_time_ns": 0,
            "hidden_top_rows": 0,
            "readout_basis": "full_sensor_rows",
        },
    )
    data["geometry"]["crop_origin"] = [2, 4]
    paths = 2 if mode == "shared_charge_paths" else 1
    data["frames"] = [
        {
            "frame_id": f"{suffix}-frame-{index}",
            "raw_index": index,
            "frame_start_ns": index * 10_000_000,
            "applied": {
                "exposure_ns": 4_000_000,
                "gain": 1.0 if paths == 2 else 2.0,
                "temperature_c": 25.0,
                "applied_at_ns": 0,
                "readout_settings": [
                    {"analog_gain": 2.0 if p == 0 else 1.0, "digital_gain": 1.0}
                    for p in range(paths)
                ],
            },
            "settings_evidence": {
                "status": "recorded_applied",
                "source_uri": "fixture://authored-settings",
                "method_and_version": "authored:1",
                "description": "Independent authored values, not camera measurements",
            },
        }
        for index in range(count)
    ]
    return data


def raw_bytes(mode="sequential_frames", *, offset=0, dtype="uint16", count=4):
    raw = (np.arange(count * 8 * 8).reshape(count, 8, 8) + 100 + offset).astype(dtype)
    if mode == "shared_charge_paths":
        raw = np.stack([raw, raw + 500], axis=1)
    stream = io.BytesIO()
    np.save(stream, raw, allow_pickle=False)
    return stream.getvalue(), raw


@pytest.mark.parametrize("mode", ["sequential_frames", "shared_charge_paths"])
def test_preserved_raw_calibration_independent_import_and_fresh_read(capture_service, mode):
    service, assembly, test = capture_service
    data = metadata(service, assembly, test, mode)
    content, raw = raw_bytes(mode)
    original = json.dumps(data, indent=4).encode()
    capture = captures.import_capture(service, data, content, metadata_bytes=original)
    assert captures.get(service, capture["id"]) == capture
    np.testing.assert_array_equal(captures.load_raw(service, capture), raw)
    assert not service.registry.list("measurements") and not service.registry.list(
        "capture_analyses"
    )
    assert "readout_calibration_id" not in capture and "capture_program_id" not in capture
    assert (
        capture["source_kind"] == "simulated"
        and capture["physical_validation"] == "not_established"
    )
    artifacts = {a["name"]: a for a in capture["artifacts"]}
    assert (
        service.registry.blob_path(artifacts["raw-capture.npy"]["sha256"]).read_bytes() == content
    )
    assert (
        service.registry.blob_path(artifacts["acquisition-metadata.json"]["sha256"]).read_bytes()
        == original
    )
    assert len(capture["raw_path_frame_hashes"]) == 4
    assert all(
        len(row) == (2 if mode == "shared_charge_paths" else 1)
        for row in capture["raw_path_frame_hashes"]
    )
    assert captures.lineage(service, capture) == capture["exposure_tokens"]
    assert all(not token.startswith("artifact") for token in capture["exposure_tokens"])
    assert (
        service.registry.artifact_owner(artifacts["raw-capture.npy"]["sha256"])["owner"]["id"]
        == capture["id"]
    )


@pytest.mark.parametrize("mode", ["sequential_frames", "shared_charge_paths"])
def test_reencoding_and_reordering_do_not_create_new_exposure(capture_service, mode):
    service, assembly, test = capture_service
    data = metadata(service, assembly, test, mode)
    original, raw = raw_bytes(mode)
    captures.import_capture(service, data, original)
    for altered in (raw.astype("int32"), raw[::-1].copy()):
        stream = io.BytesIO()
        np.save(stream, altered, allow_pickle=False)
        renamed = metadata(service, assembly, test, mode, suffix="renamed")
        with pytest.raises(ConflictError, match="pixels overlap"):
            captures.import_capture(service, renamed, stream.getvalue())
    assert len(service.registry.list("captures")) == 1


def test_extracted_hcg_cannot_become_independent_capture(capture_service):
    service, assembly, test = capture_service
    content, raw = raw_bytes("shared_charge_paths")
    captures.import_capture(
        service, metadata(service, assembly, test, "shared_charge_paths"), content
    )
    stream = io.BytesIO()
    np.save(stream, raw[:, 0], allow_pickle=False)
    with pytest.raises(ConflictError, match="pixels overlap"):
        captures.import_capture(
            service, metadata(service, assembly, test, suffix="extracted"), stream.getvalue()
        )


@pytest.mark.parametrize("field", ["dataset_ids", "acquisition_group_id"])
def test_duplicate_identity_refused_even_with_different_pixels(capture_service, field):
    service, assembly, test = capture_service
    data = metadata(service, assembly, test)
    captures.import_capture(service, data, raw_bytes()[0])
    other = metadata(service, assembly, test, suffix="different")
    other[field] = data[field]
    with pytest.raises(ConflictError):
        captures.import_capture(service, other, raw_bytes(offset=1)[0])


@pytest.mark.parametrize("status", ["unknown", "command_only"])
def test_unknown_or_commanded_settings_are_preserved_without_promotion(capture_service, status):
    service, assembly, test = capture_service
    data = captures.template(service, assembly["id"], test["id"])
    data["frames"][0]["settings_evidence"]["status"] = status
    capture = captures.import_capture(service, data, raw_bytes(count=1)[0])
    assert capture["clock"]["status"] == "unknown"
    assert capture["timing_mode"]["shutter"] == "unknown"
    assert capture["frames"][0]["applied"]["exposure_ns"] is None
    assert capture["frames"][0]["settings_evidence"]["status"] == status
    assert captures.get(service, capture["id"]) == capture


def test_measured_provenance_requires_physical_identity_and_instrument(capture_service):
    service, assembly, test = capture_service
    data = metadata(service, assembly, test)
    data["source_kind"] = "measured"
    with pytest.raises(ValueError, match="Measured RAW"):
        NonlinearCaptureImport.model_validate(data)
    data.update(
        physical_instance="test-serial",
        instrument={"id": "test-meter", "calibration_revision": "1"},
    )
    assert NonlinearCaptureImport.model_validate(data).source_kind == "measured"


@pytest.mark.parametrize(
    "field,value",
    [
        ("pixel_pitch_um", 4.0),
        ("raw_index", 1),
        ("cfa", "BGGR"),
        ("code", 5000),
        ("late_applied", 1),
    ],
)
def test_metadata_and_codes_bind_native_acquisition(capture_service, field, value):
    service, assembly, test = capture_service
    data = metadata(service, assembly, test)
    content, _ = raw_bytes()
    if field == "raw_index":
        data["frames"][0]["raw_index"] = value
    elif field == "cfa":
        data["raw_formats"][0]["cfa"] = value
    elif field == "code":
        content, _ = raw_bytes(offset=value)
    elif field == "late_applied":
        data["frames"][0]["applied"]["applied_at_ns"] = value
    else:
        data[field] = value
    with pytest.raises(ValueError):
        captures.import_capture(service, data, content)
    assert not service.registry.list("captures")


@pytest.mark.parametrize(
    "shape,dtype",
    [
        ((65, 2, 2), "uint16"),
        ((1, 2, 4098), "uint16"),
        ((1, 1024, 1026), "uint16"),
        ((1, 3, 2), "uint16"),
        ((1, 2, 2), "float64"),
    ],
)
def test_header_limits_before_array_allocation(monkeypatch, shape, dtype):
    stream = io.BytesIO()
    np.save(stream, np.zeros(shape, dtype=dtype), allow_pickle=False)
    monkeypatch.setattr(
        np, "load", lambda *a, **k: pytest.fail("NPY must be rejected before allocation")
    )
    with pytest.raises(ValueError):
        captures.parse_raw(stream.getvalue(), "sequential_frames")


def test_same_size_raw_corruption_is_detected_fresh(capture_service):
    service, assembly, test = capture_service
    capture = captures.import_capture(service, metadata(service, assembly, test), raw_bytes()[0])
    path = service.registry.blob_path(capture["raw_sha256"])
    content = path.read_bytes()
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    with pytest.raises(IntegrityError):
        captures.get(service, capture["id"])


def test_parent_exposure_lineage_is_transitive_without_merging_shared_assets(capture_service):
    service, assembly, test = capture_service
    first = captures.import_capture(service, metadata(service, assembly, test), raw_bytes()[0])
    child = metadata(service, assembly, test, suffix="child")
    child["parents"] = [first["id"]]
    second = captures.import_capture(service, child, raw_bytes(offset=1200)[0])
    assert set(first["exposure_tokens"]) < set(second["exposure_tokens"])
    assert second["parent_capture_bindings"] == [
        {"id": first["id"], "content_hash": first["content_hash"]}
    ]
    assert not set(a["sha256"] for a in first["artifacts"]) & set(second["exposure_tokens"])
    assert captures.get(service, second["id"]) == second
    assert {record["id"] for record in captures.evidence_closure(service, second)} == {
        first["id"],
        second["id"],
    }


def test_two_concurrent_imports_publish_only_one_acquisition(capture_service, monkeypatch):
    service, assembly, test = capture_service
    data = metadata(service, assembly, test)
    content, _ = raw_bytes()
    barrier, thread = Barrier(2), local()
    read_rows = service.registry._read_rows

    def simultaneous_preflight(queries):
        rows = read_rows(queries)
        if not getattr(thread, "prepared", False):
            thread.prepared = True
            barrier.wait(timeout=5)
        return rows

    monkeypatch.setattr(service.registry, "_read_rows", simultaneous_preflight)

    def attempt():
        try:
            return captures.import_capture(service, deepcopy(data), content)
        except ConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(service.registry.list("captures")) == 1
    assert service.registry.audit()["integrity"] == "verified"


def test_read_set_drift_rechecks_before_publication(capture_service, monkeypatch):
    service, assembly, test = capture_service
    registry = service.registry
    original = registry._same_read_rows
    calls = []

    def changed_once(conn, queries, before):
        calls.append(1)
        return False if len(calls) == 1 else original(conn, queries, before)

    monkeypatch.setattr(registry, "_same_read_rows", changed_once)
    capture = captures.import_capture(service, metadata(service, assembly, test), raw_bytes()[0])
    assert len(calls) == 2 and captures.get(service, capture["id"]) == capture


def test_calibration_owner_appends_without_reordering_old_owner_priority(capture_service):
    service, _, _ = capture_service
    registry = service.registry
    assert registry.ARTIFACT_RECORD_KINDS[-3:] == (
        "capture_programs", "readout_calibrations", "readout_observations"
    )
    blob = registry.store_blob(
        b"authored calibration evidence", "reference.json", "application/json"
    )
    record = registry.put("readout_calibrations", {"artifacts": [blob]})
    assert registry.artifact_owner(blob["sha256"])["owner"]["id"] == record["id"]


@pytest.mark.parametrize("cfa", ["RGGB", "BGGR", "GRBG", "GBRG"])
def test_every_existing_native_cfa_is_preserved(capture_service, cfa):
    service, assembly, test = capture_service
    sensor = next(
        service.registry.get("models", identity)
        for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    fields = {
        k: deepcopy(v) for k, v in sensor.items() if k not in {"id", "created_at", "content_hash"}
    }
    fields["raw_format"]["cfa"] = cfa
    selected = service.registry.put("models", fields)
    fields = {
        k: deepcopy(v) for k, v in assembly.items() if k not in {"id", "created_at", "content_hash"}
    }
    fields["model_ids"] = [
        selected["id"] if identity == sensor["id"] else identity for identity in fields["model_ids"]
    ]
    assembly = service.registry.put("assemblies", fields)
    data = metadata(service, assembly, test)
    record = captures.import_capture(service, data, raw_bytes()[0])
    assert captures.get(service, record["id"])["raw_formats"][0]["cfa"] == cfa


def test_failed_audit_rolls_back_capture_and_artifact_owners(capture_service, monkeypatch):
    service, assembly, test = capture_service
    original = service.registry._audit
    before = service.registry.audit()

    def fail(conn, action, subject, details):
        if action == "captures.created":
            raise RuntimeError("authored failure after capture/index insertion")
        return original(conn, action, subject, details)

    monkeypatch.setattr(service.registry, "_audit", fail)
    with pytest.raises(RuntimeError, match="authored failure"):
        captures.import_capture(service, metadata(service, assembly, test), raw_bytes()[0])
    assert not service.registry.list("captures")
    assert service.registry.audit() == before
    with service.registry.connection() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM artifact_owners WHERE owner_kind='captures'"
            ).fetchone()[0]
            == 0
        )


def test_original_json_duplicate_keys_are_not_silently_reinterpreted(capture_service):
    service, assembly, test = capture_service
    data = metadata(service, assembly, test)
    original = json.dumps(data).encode()
    original = b'{"source_kind":"measured",' + original[1:]
    with pytest.raises(ValueError, match="duplicate keys"):
        captures.import_capture(service, data, raw_bytes()[0], metadata_bytes=original)
    assert not service.registry.list("captures")


def test_known_timestamps_cannot_reverse_across_an_unknown_frame(capture_service):
    service, assembly, test = capture_service
    data = metadata(service, assembly, test)
    data["frames"][0]["frame_start_ns"] = 10_000_000
    data["frames"][1]["frame_start_ns"] = None
    data["frames"][2]["frame_start_ns"] = 0
    with pytest.raises(ValueError, match="strictly increase"):
        captures.import_capture(service, data, raw_bytes()[0])


def test_verified_record_publication_refuses_stale_or_missing_reference(capture_service):
    service, assembly, _ = capture_service
    registry = service.registry
    changed = deepcopy(assembly)
    changed["conditions"] = {}
    with pytest.raises(IntegrityError, match="reference changed"):
        registry.put_verified_record(
            "readout_calibrations", {"name": "never published"}, [("assemblies", changed)]
        )
    missing = deepcopy(assembly)
    missing["id"] = "missing-assembly"
    with pytest.raises(IntegrityError, match="reference changed"):
        registry.put_verified_record(
            "readout_calibrations", {"name": "never published"}, [("assemblies", missing)]
        )
    assert not registry.list("readout_calibrations")


def test_verified_record_read_set_retry_preserves_original_identity(capture_service, monkeypatch):
    service, assembly, _ = capture_service
    registry = service.registry
    original = registry._same_read_rows
    calls = []

    def changed_once(conn, queries, before):
        calls.append(1)
        return False if len(calls) == 1 else original(conn, queries, before)

    monkeypatch.setattr(registry, "_same_read_rows", changed_once)
    value = registry.put_verified_record(
        "readout_calibrations",
        {"name": "authored reference"},
        [("assemblies", assembly)],
        identity="stable-calibration",
    )
    assert len(calls) == 2 and value["id"] == "stable-calibration"
    assert registry.get("readout_calibrations", value["id"]) == value


def test_legacy_parent_closure_keeps_original_record_and_checks_every_artifact(capture_service):
    service, assembly, test = capture_service
    content, raw = raw_bytes()
    metadata_artifact = service.registry.store_blob(
        b'{"legacy":"retained"}', "acquisition-metadata.json", "application/json"
    )
    parent = service.registry.put(
        "captures",
        {
            "dataset_ids": ["legacy-acquisition"],
            "parents": [],
            "raw_content_hash": captures.pixel_hash(raw),
            "raw_frame_hashes": [captures.pixel_hash(frame) for frame in raw],
            "artifacts": [
                service.registry.store_blob(content, "raw-capture.npy", "application/octet-stream"),
                metadata_artifact,
            ],
        },
    )
    data = metadata(service, assembly, test, suffix="child")
    data["parents"] = [parent["id"]]
    child = captures.import_capture(service, data, raw_bytes(offset=1200)[0])
    closure = captures.evidence_closure(service, child)
    assert parent in closure and service.registry.get("captures", parent["id"]) == parent
    service.registry.blob_path(metadata_artifact["sha256"]).write_bytes(b'{"legacy":"altered!"}')
    with pytest.raises(IntegrityError):
        captures.evidence_closure(service, child)
