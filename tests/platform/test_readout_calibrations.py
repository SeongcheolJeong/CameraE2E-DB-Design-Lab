"""Imported expected-dark and absolute-scale evidence are distinct from RAW acquisition."""

import hashlib
import io
import json
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from camera_platform import nonlinear_dcg, nonlinear_readout
from camera_platform import readout_calibrations as calibrations
from camera_platform.readout_calibration_contracts import validate_metadata
from camera_platform.registry import IntegrityError, SQLiteRegistry, canonical


@pytest.fixture
def library(tmp_path):
    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path / "readout"))
    sensor = service.registry.put(
        "models",
        {
            "kind": "sensor",
            "payload": {
                "pixel_pitch_um": 2.0,
                "readout": {
                    "full_well_e": 2000.0,
                    "read_noise_e": 2.0,
                    "dark_current_e_per_s": 1.0,
                    "gain": 1.0,
                    "bit_depth": 12,
                    "black_level_dn": 64.0,
                },
            },
            "properties": {
                "active_height": {"value": 0.016, "unit": "mm"},
                "active_width": {"value": 0.016, "unit": "mm"},
            },
            "raw_format": {"cfa": "RGGB"},
        },
    )
    return service, sensor


def npy(array):
    stream = io.BytesIO()
    np.save(stream, array, allow_pickle=False)
    return stream.getvalue()


def metadata(library, mode="sequential_frames"):
    service, sensor = library
    return calibrations.template(service, sensor["id"], mode)


def imported(library, data=None, rate=None, *, evidence=None, metadata_bytes=None):
    data = metadata(library) if data is None else data
    rate = np.full(data["dark_rate_shape"], 3.0) if rate is None else rate
    evidence = calibrations.evidence_template(data) if evidence is None else evidence
    return calibrations.import_calibration(
        library[0],
        data,
        npy(rate),
        json.dumps(evidence, indent=2).encode(),
        metadata_bytes=metadata_bytes,
    )


def target(library, calibration, *, origin=(0, 0), shape=(4, 4), count=2):
    """Valid frozen capture stand-in isolates calibration math from capture import tests."""
    shared = calibration["readout_mode"] == "shared_charge_paths"
    return {
        "id": "capture-original",
        "source_kind": "simulated",
        "physical_instance": None,
        "readout_mode": calibration["readout_mode"],
        "model_snapshots": [library[1]],
        "pixel_pitch_um": calibration["pixel_pitch_um"],
        "geometry": {
            "kind": "native_sensor",
            "full_sensor_shape": [8, 8],
            "crop_origin": list(origin),
        },
        "raw_shape": [count, *([2] if shared else []), *shape],
        "raw_formats": deepcopy(calibration["raw_formats"]),
        "dataset_ids": ["target-data"],
        "acquisition_group_id": "target-acquisition",
        "exposure_tokens": [
            "capture-id:capture-original",
            "dataset-id:target-data",
            "acquisition-group-id:target-acquisition",
            "raw-frame-sha256:abc",
        ],
        "artifacts": [],
        "frames": [
            {
                "frame_id": f"f-{i}",
                "raw_index": i,
                "settings_evidence": {"status": "recorded_applied"},
                "applied": {
                    "exposure_ns": 4_000_000 * (i + 1),
                    "temperature_c": 25.0,
                    "gain": 1.0,
                    "readout_settings": [
                        {
                            "analog_gain": domain["analog_gain"][0] if shared else 1.0,
                            "digital_gain": domain["digital_gain"],
                        }
                        for domain in calibration["applicability"]["readouts"]
                    ],
                },
            }
            for i in range(count)
        ],
    }


def allow_target(monkeypatch, capture):
    monkeypatch.setattr(calibrations, "_get_capture", lambda service, identity: deepcopy(capture))
    monkeypatch.setattr(calibrations, "_capture_closure", lambda service, value: [deepcopy(value)])


@pytest.mark.parametrize("mode", ["sequential_frames", "shared_charge_paths"])
def test_original_bytes_normalized_model_and_authority_roundtrip(library, mode):
    data = metadata(library, mode)
    before = deepcopy(data)
    original = ("\n" + json.dumps(data, indent=3) + "\n").encode()
    record = imported(library, data, metadata_bytes=original)
    assert data == before
    assert calibrations.get(library[0], record["id"]) == record
    artifacts = {a["name"]: a for a in record["artifacts"]}
    assert (
        library[0]
        .registry.blob_path(artifacts["readout-calibration-metadata.json"]["sha256"])
        .read_bytes()
        == original
    )
    assert record["authority"] == {"physical_validation": False, "release": False}
    assert record["physical_validation"] == "not_established"
    assert record["electrical_model"]["schema"] in {nonlinear_readout.SCHEMA, nonlinear_dcg.SCHEMA}
    assert record["dark_rate"]["unit"] == "electron/pixel/s"
    assert record["dark_rate"]["reference_temperature_c"] == 25.0
    assert record["evidence_sha256"] == artifacts["readout-evidence.json"]["sha256"]


def test_temperature_scaled_spatial_expected_dark_never_randomized(library, monkeypatch):
    data = metadata(library)
    data["electrical_model"]["dark_current_doubling_c"] = 5.0
    data["electrical_model"]["charge_voltage_response"]["valid_domain"]["temperature_c"] = [
        20.0,
        30.0,
    ]
    data["electrical_model"]["read_noise"]["temperature_range_c"] = [20.0, 30.0]
    data["applicability"]["temperature_c"] = [20.0, 30.0]
    rate = np.arange(64, dtype=float).reshape(8, 8)
    record = imported(library, data, rate)
    capture = target(library, record, origin=(1, 3))
    capture["frames"][0]["applied"]["temperature_c"] = 20.0
    capture["frames"][1]["applied"]["temperature_c"] = 30.0
    allow_target(monkeypatch, capture)
    monkeypatch.setattr(np.random, "default_rng", lambda *a, **k: pytest.fail("No random dark"))
    result = calibrations.prepare_for_capture(library[0], record["id"], capture)
    expected = np.stack([rate[1:5, 3:7] * 0.5 * 0.004, rate[1:5, 3:7] * 2 * 0.008])
    np.testing.assert_allclose(result["dark_e"], expected, rtol=1e-15)
    for i, cal in enumerate(result["calibrations"]):
        nonlinear_readout.validate_calibration(cal, (4, 4))
        np.testing.assert_array_equal(cal["dark_e"], result["dark_e"][i])
        assert cal["acquisition"]["geometry"]["crop_origin"] == [1, 3]
    result["dark_e"][0, 0, 0] = -100
    assert result["calibrations"][0]["dark_e"][0, 0] >= 0


def test_paired_covariance_and_shared_normalization_are_preserved(library, monkeypatch):
    data = metadata(library, "shared_charge_paths")
    covariance = data["electrical_model"]["read_noise"]["covariance_v2"]
    covariance[0][1] = covariance[1][0] = 0.25 * np.sqrt(covariance[0][0] * covariance[1][1])
    record = imported(library, data)
    capture = target(library, record)
    allow_target(monkeypatch, capture)
    prepared = calibrations.prepare_for_capture(library[0], record["id"], capture)
    assert prepared["normalization_e"] == data["electrical_model"]["shared_charge_capacity_e"]
    for cal in prepared["calibrations"]:
        assert (
            nonlinear_dcg.validate_calibration(cal, (4, 4))["model"]["read_noise"]["covariance_v2"]
            == covariance
        )
        hcg = nonlinear_dcg.calibration_for_path(cal, 0)
        assert hcg["model"]["full_well_e"] < prepared["normalization_e"]


def test_two_interpretations_preserve_one_raw_acquisition(library, monkeypatch):
    data = metadata(library)
    first = imported(library, data)
    second_data = deepcopy(data)
    second_data["revision"] = "2"
    second_data["parents"] = [first["id"]]
    curve = second_data["electrical_model"]["charge_voltage_response"]
    curve["signal_voltage_v"] = [v * 0.5 for v in curve["signal_voltage_v"]]
    second = imported(library, second_data)
    capture = target(library, first, count=1)
    original = deepcopy(capture)
    allow_target(monkeypatch, capture)
    outputs = [
        calibrations.prepare_for_capture(library[0], c["id"], capture) for c in (first, second)
    ]
    raw = np.full((4, 4), 800, dtype=np.int32)
    decoded = [
        nonlinear_readout.decode_raw(raw, p["calibrations"][0])["arrays"]["total_charge_e"]
        for p in outputs
    ]
    np.testing.assert_allclose(decoded[1], 2 * decoded[0])
    assert capture == original
    assert first["id"] != second["id"]


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.update(dark_rate_unit="DN"),
        lambda m: m.update(dark_rate_shape=[True, 8]),
        lambda m: m.update(dark_reference_temperature_c=26.0),
        lambda m: m.update(pixel_pitch_um=float("nan")),
        lambda m: m.update(unknown=1),
        lambda m: m["electrical_model"]["fixed_pattern"].update(dsnu_e_per_s=1.0),
        lambda m: m["electrical_model"]["charge_voltage_response"].update(
            charge_e=[0.0, 500.0, 400.0]
        ),
        lambda m: m["charge_reference"]["uncertainty"].update(value=True),
        lambda m: m["charge_reference"]["uncertainty"].update(unit="DN"),
        lambda m: m["charge_reference"].update(evidence_document_ids=[]),
        lambda m: m["applicability"].update(exposure_s=[0.1, 0.01]),
        lambda m: m["applicability"]["readouts"][0].update(digital_gain=2.0),
        lambda m: m.update(source_kind="measured", physical_instance="device-1"),
    ],
)
def test_invalid_metadata_refused_without_publication(library, change):
    data = metadata(library)
    change(data)
    with pytest.raises(ValueError):
        validate_metadata(data)
    assert library[0].registry.list("readout_calibrations") == []


@pytest.mark.parametrize(
    "array",
    [
        np.ones((8, 8), dtype=bool),
        np.ones((4, 8)),
        np.ones((8, 8), dtype=complex),
        np.full((8, 8), np.nan),
        np.full((8, 8), -1.0),
        np.full((8, 8), 1e14),
        np.asfortranarray(np.ones((8, 8))),
    ],
)
def test_invalid_dark_npy_refused(library, array):
    with pytest.raises(ValueError):
        imported(library, rate=array)
    assert library[0].registry.list("readout_calibrations") == []


def test_npy_shape_budget_checked_before_np_load(library, monkeypatch):
    data = metadata(library)
    content = io.BytesIO()
    np.lib.format.write_array_header_1_0(
        content,
        {
            "descr": "<f8",
            "fortran_order": False,
            "shape": (1_000_000_000, 8),
        },
    )
    monkeypatch.setattr(np, "load", lambda *a, **k: pytest.fail("Do not allocate forged shape"))
    with pytest.raises(ValueError, match="shape"):
        calibrations._dark_rate(content.getvalue(), data)


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["frames"][0]["settings_evidence"].update(status="command_only"),
        lambda c: c["frames"][0]["applied"].update(temperature_c=None),
        lambda c: c["frames"][0]["applied"].update(temperature_c=35.0),
        lambda c: c["frames"][0]["applied"].update(exposure_ns=10**16),
        lambda c: c["frames"][0]["applied"]["readout_settings"][0].update(analog_gain=100.0),
        lambda c: c["frames"][0]["applied"]["readout_settings"][0].update(digital_gain=2.0),
        lambda c: c.update(pixel_pitch_um=3.0),
        lambda c: c["geometry"].update(crop_origin=[7, 7]),
        lambda c: c["raw_formats"][0].update(black_level_dn=65.0),
        lambda c: c.update(source_kind="measured", physical_instance="device-1"),
    ],
)
def test_capture_must_fit_recorded_calibration(library, monkeypatch, change):
    record = imported(library)
    capture = target(library, record)
    change(capture)
    allow_target(monkeypatch, capture)
    with pytest.raises(ValueError):
        calibrations.prepare_for_capture(library[0], record["id"], capture)


def test_external_anchor_must_supply_own_original_evidence_and_physical_binding(
    library, monkeypatch
):
    data = metadata(library)
    evidence = calibrations.evidence_template(data)
    data.update(source_kind="measured", physical_instance="device-1")
    data["charge_reference"].update(kind="external_charge", source_uri="lab://charge-standard")
    evidence["documents"][0]["source_uri"] = "lab://charge-standard"
    evidence["documents"][0]["description"] = "Supplied instrument reference; not authenticated."
    record = imported(library, data, evidence=evidence)
    capture = target(library, record)
    capture.update(source_kind="measured", physical_instance="device-1")
    allow_target(monkeypatch, capture)
    assert calibrations.prepare_for_capture(library[0], record["id"], capture)["record"] == record
    assert record["physical_validation"] == "not_established"
    capture["physical_instance"] = "device-2"
    with pytest.raises(ValueError, match="physical instance"):
        calibrations.prepare_for_capture(library[0], record["id"], capture)
    with pytest.raises(ValueError, match="original"):
        calibrations.evidence_template(data)


def test_lineage_separates_fitting_exposures_from_shared_dark_and_reference_blobs(
    library, monkeypatch
):
    data = metadata(library)
    first = imported(library, data)
    capture = target(library, first)
    capture = library[0].registry.put(
        "captures", {k: v for k, v in capture.items() if k != "id"}, identity=capture["id"]
    )
    allow_target(monkeypatch, capture)
    child_data = deepcopy(data)
    child_data.update(
        parents=[first["id"]],
        reference_capture_ids=[capture["id"]],
        dataset_ids=["fit-dataset-2"],
        revision="2",
    )
    child = imported(library, child_data)
    closure = calibrations.lineage(library[0], child)
    assert {c["id"] for c in closure["calibrations"]} == {first["id"], child["id"]}
    assert closure["reference_captures"] == [capture]
    assert set(closure["decoder_fitting_tokens"]) == {
        *capture["exposure_tokens"],
        "dataset-id:authored-readout-response-1",
        "dataset-id:fit-dataset-2",
    }
    assert not any(
        a["sha256"] in closure["decoder_fitting_tokens"] for a in closure["evidence_artifacts"]
    )
    # An alternate library with the exact same dark/evidence blobs is not a reused exposure.
    independent = deepcopy(data)
    independent.update(dataset_ids=["independent-response-dataset"], revision="independent")
    alternate = imported(library, independent)
    assert set(calibrations.lineage(library[0], alternate)["decoder_fitting_tokens"]).isdisjoint(
        closure["decoder_fitting_tokens"]
    )


def test_original_metadata_and_evidence_drift_refused(library):
    data = metadata(library)
    wrong = deepcopy(data)
    wrong["name"] = "different"
    with pytest.raises(ValueError, match="metadata"):
        imported(library, data, metadata_bytes=canonical(wrong).encode())
    evidence = calibrations.evidence_template(data)
    evidence["documents"][0]["role"] = "other"
    with pytest.raises(ValueError, match="charge-reference"):
        imported(library, data, evidence=evidence)
    record = imported(library)
    blob = library[0].registry.blob_path(record["artifacts"][2]["sha256"])
    blob.write_bytes(blob.read_bytes() + b" ")
    with pytest.raises(IntegrityError):
        calibrations.get(library[0], record["id"])


def test_parent_snapshot_or_derived_normalization_tamper_is_not_trusted(library, monkeypatch):
    record = imported(library)
    forged = deepcopy(record)
    forged["normalization_e"] *= 2
    original = library[0].registry.get
    monkeypatch.setattr(
        library[0].registry,
        "get",
        lambda kind, identity: (
            forged if kind == "readout_calibrations" else original(kind, identity)
        ),
    )
    with pytest.raises(IntegrityError, match="binding"):
        calibrations.get(library[0], record["id"])


def test_duplicate_evidence_json_keys_and_combined_upload_budget_refused(library, monkeypatch):
    data = metadata(library)
    evidence = json.dumps(calibrations.evidence_template(data)).encode()
    evidence = evidence.replace(b'{"schema":', b'{"schema":"duplicate","schema":', 1)
    with pytest.raises(ValueError, match="Duplicate"):
        calibrations.import_calibration(library[0], data, npy(np.zeros((8, 8))), evidence)
    monkeypatch.setattr(calibrations, "MAX_IMPORT_BYTES", 1024)
    with pytest.raises(ValueError, match="64 MiB"):
        imported(library)
    assert library[0].registry.list("readout_calibrations") == []


def test_original_dark_bytes_identity_survives_numeric_conversion(library):
    data = metadata(library)
    rate = np.arange(64, dtype=">f4").reshape(8, 8)
    record = imported(library, data, rate)
    original = npy(rate)
    artifact = next(a for a in record["artifacts"] if a["name"] == "expected-dark-rate.npy")
    assert artifact["sha256"] == hashlib.sha256(original).hexdigest()
    assert library[0].registry.blob_path(artifact["sha256"]).read_bytes() == original
    assert calibrations.get(library[0], record["id"])["dark_rate"]["dtype"] == ">f4"


def test_publication_refuses_missing_sensor_readset_after_preflight(library, monkeypatch):
    """Inject a missing publication snapshot, without bypassing real immutability triggers."""
    registry = library[0].registry
    original = registry._read_rows

    def missing_snapshot(queries):
        rows = original(queries)
        return [
            ([] if values == ("models", library[1]["id"]) else result)
            for (_, values), result in zip(queries, rows, strict=True)
        ]

    monkeypatch.setattr(registry, "_read_rows", missing_snapshot)
    with pytest.raises(IntegrityError, match="reference changed"):
        imported(library)
    assert registry.list("readout_calibrations") == []


def test_fitting_capture_transitive_artifacts_and_reference_rows_retained(library, monkeypatch):
    record = imported(library)
    registry = library[0].registry
    parent_artifact = registry.store_blob(
        b"original ancestor evidence", "ancestor.raw", "application/octet-stream"
    )
    parent = registry.put(
        "captures",
        {
            "dataset_ids": ["ancestor-data"],
            "raw_frame_hashes": ["ancestor-pixels"],
            "artifacts": [parent_artifact],
            "model_snapshots": [library[1]],
        },
    )
    capture = target(library, record)
    capture["exposure_tokens"].extend(
        [
            "capture-id:" + parent["id"],
            "dataset-id:ancestor-data",
            "raw-frame-sha256:ancestor-pixels",
        ]
    )
    capture = registry.put(
        "captures", {k: v for k, v in capture.items() if k != "id"}, identity=capture["id"]
    )
    monkeypatch.setattr(calibrations, "_get_capture", lambda *args: deepcopy(capture))
    monkeypatch.setattr(
        calibrations, "_capture_closure", lambda *args: [deepcopy(parent), deepcopy(capture)]
    )
    observed = []
    original = registry.put_verified_record

    def publish(kind, document, references):
        observed.extend((kind, item["id"]) for kind, item in references)
        return original(kind, document, references)

    monkeypatch.setattr(registry, "put_verified_record", publish)
    data = metadata(library)
    data["reference_capture_ids"] = [capture["id"]]
    child = imported(library, data)
    closure = calibrations.lineage(library[0], child)
    assert {c["id"] for c in closure["reference_captures"]} == {capture["id"], parent["id"]}
    assert parent_artifact in closure["evidence_artifacts"]
    assert ("captures", parent["id"]) in observed
    assert ("captures", capture["id"]) in observed
    assert ("models", library[1]["id"]) in observed
    assert "dataset-id:ancestor-data" in closure["decoder_fitting_tokens"]
