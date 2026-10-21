"""New HDR/LFM packets must be internally consistent, without physical claims."""

from __future__ import annotations

import copy
import hashlib
import io
import json

import numpy as np
import pytest

from camera_platform.hdr_lfm_reference_job import _packet_schema, _qualification_schema
from camera_platform.hdr_lfm_reference_packet import (
    MODE_SCHEMA,
    REFERENCE_SCHEMA,
    WAVEFORM_SCHEMA,
    validate_packet,
)
from camera_platform.hdr_lfm_registration_qualification import qualify
from camera_platform.registry import IntegrityError


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bytes(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _npy(array: np.ndarray) -> bytes:
    output = io.BytesIO()
    np.save(output, array, allow_pickle=False)
    return output.getvalue()


def _array_sha(array: np.ndarray) -> str:
    return _sha(np.ascontiguousarray(array).tobytes(order="C"))


def _packet():
    raw = np.arange(2 * 4 * 8 * 8, dtype=np.int32).reshape(2, 4, 8, 8)
    reference = np.ones((4, 8, 8), dtype=np.float64)
    metadata = {
        "source_hdr_job_id": "run-hdr-fixture",
        "source_hdr_report_sha256": "a" * 64,
        "sensor_revision": {"report_code": "fixture", "run_id": "fixture-run"},
        "physical_instance": "synthetic-test-sensor",
        "instrument": {"id": "synthetic-raw-reader", "calibration_revision": "r1"},
        "mode": {
            "mode_id": "fixture-mode", "timing_mode_sha256": "b" * 64,
            "operating_point": "fixture", "shutter": "rolling",
            "line_time_s": 4e-5, "native_crop_origin_row_col": [0, 0],
            "phase_alignment": "declared_phase_index_only_not_verified",
        },
        "stimulus": {
            "pwm_period_s": 0.01, "duty_cycle": 0.5,
            "off_to_on_ratio": 0.1, "on_luminance_cd_m2": 100,
            "phase_fractions_of_period": [0.0, 0.25, 0.5, 0.75],
        },
        "raw_acquisition": {
            "exposure_s": [0.001, 0.004], "shape": [2, 4, 8, 8],
            "dtype": "int32", "unit": "DN", "bit_depth": 12,
            "black_level_dn": 64, "source_uri": "fixture://raw",
        },
        "reference_rate": {
            "shape": [4, 8, 8], "dtype": "float64",
            "unit": "electron/pixel/s at PWM on luminance",
            "method": "independent_calibrated_reference_declared",
            "source_uri": "fixture://rate",
        },
        "sources": {
            key: {"uri": f"fixture://{key}", "sha256": ""}
            for key in ("mode_log", "waveform_log", "reference_log")
        },
    }
    controls = {"exposure_s": [0.001, 0.004]}
    source_report = {
        "controls": controls,
        "geometry_provenance": {
            "configured_sensor_scope": "virtual_simulation_window_only",
            "saved_native_array_crop_mapping": "not_established",
            "saved_native_array_crop_origin": None,
        },
    }
    native_cfa = {"pattern": [[1, 2], [2, 3]]}
    mode = {
        "schema": MODE_SCHEMA,
        "source_uri": metadata["sources"]["mode_log"]["uri"],
        "source_hdr_job_id": metadata["source_hdr_job_id"],
        "source_hdr_report_sha256": metadata["source_hdr_report_sha256"],
        "sensor_revision": copy.deepcopy(metadata["sensor_revision"]),
        "physical_instance": metadata["physical_instance"],
        "instrument": copy.deepcopy(metadata["instrument"]),
        "mode": copy.deepcopy(metadata["mode"]),
        "raw_acquisition": copy.deepcopy(metadata["raw_acquisition"]),
        "raw_file_sha256": _sha(_npy(raw)),
        "native_cfa_pattern": native_cfa["pattern"],
        "native_cfa_tile_origin_row_col": [0, 0],
        "frame_order": [
            {
                "array_index": index,
                "frame_id": f"raw-frame-{index}",
                "acquisition_order": index,
                "row_zero_timestamp_ns": index * 1_000_000,
                "exposure_index": index // 4,
                "phase_index": index % 4,
                "exposure_s": controls["exposure_s"][index // 4],
                "phase_fraction_of_period": metadata["stimulus"][
                    "phase_fractions_of_period"
                ][index % 4],
                "row_zero_phase_s": metadata["stimulus"]["phase_fractions_of_period"][
                    index % 4
                ] * metadata["stimulus"]["pwm_period_s"],
                "raw_frame_sha256": _array_sha(raw[index // 4, index % 4]),
            }
            for index in range(8)
        ],
    }
    waveform = {
        "schema": WAVEFORM_SCHEMA,
        "source_uri": metadata["sources"]["waveform_log"]["uri"],
        "source_hdr_job_id": metadata["source_hdr_job_id"],
        "source_hdr_report_sha256": metadata["source_hdr_report_sha256"],
        "mode_id": metadata["mode"]["mode_id"],
        "timing_mode_sha256": metadata["mode"]["timing_mode_sha256"],
        "mode_log_sha256": "",
        "raw_file_sha256": _sha(_npy(raw)),
        "line_time_s": metadata["mode"]["line_time_s"],
        "phase_alignment": metadata["mode"]["phase_alignment"],
        "stimulus": copy.deepcopy(metadata["stimulus"]),
    }
    rate_log = {
        "schema": REFERENCE_SCHEMA,
        "source_uri": metadata["sources"]["reference_log"]["uri"],
        "source_hdr_job_id": metadata["source_hdr_job_id"],
        "source_hdr_report_sha256": metadata["source_hdr_report_sha256"],
        "mode_log_sha256": "", "waveform_log_sha256": "",
        "raw_file_sha256": _sha(_npy(raw)),
        "reference_rate_file_sha256": _sha(_npy(reference)),
        "reference_rate": copy.deepcopy(metadata["reference_rate"]),
        "source_acquisition_id": "separate-reference-capture",
        "phase_order": [
            {
                "phase_index": phase,
                "phase_fraction_of_period": metadata["stimulus"][
                    "phase_fractions_of_period"
                ][phase],
                "reference_phase_sha256": _array_sha(reference[phase]),
            }
            for phase in range(4)
        ],
        "calibration": {
            "reference_instrument_id": "synthetic-reference-meter",
            "calibration_revision": "fixture-cal-1",
            "method_and_version": "synthetic-fixture-v1",
            "calibration_document_sha256": _sha(b"independent synthetic calibration file"),
            "relative_standard_uncertainty": 0.1,
            "applied_to_reference_array": True,
            "uses_uploaded_raw": False,
            "uses_model_output": False,
        },
    }
    return metadata, raw, reference, source_report, native_cfa, mode, waveform, rate_log


def _render(metadata, raw, reference, mode, waveform, rate_log):
    mode_bytes = _bytes(mode)
    waveform["mode_log_sha256"] = _sha(mode_bytes)
    waveform_bytes = _bytes(waveform)
    rate_log["mode_log_sha256"] = _sha(mode_bytes)
    rate_log["waveform_log_sha256"] = _sha(waveform_bytes)
    files = {
        "measured_raw": _npy(raw),
        "measured_reference_rate": _npy(reference),
        "mode_log": mode_bytes,
        "waveform_log": waveform_bytes,
        "reference_log": _bytes(rate_log),
    }
    for key in ("mode_log", "waveform_log", "reference_log"):
        metadata["sources"][key]["sha256"] = _sha(files[key])
    return files


def _validate(fixture):
    metadata, raw, reference, source_report, cfa, mode, waveform, rate_log = fixture
    files = _render(metadata, raw, reference, mode, waveform, rate_log)
    return validate_packet(
        metadata, files, raw=raw, reference_rate=reference,
        source_lfm_report=source_report, native_cfa=cfa,
    )


def test_synthetic_packet_is_consistent_but_not_physical_evidence():
    receipt = _validate(_packet())
    assert receipt["frame_count"] == 8
    assert receipt["internal_consistency"] == (
        "verified_against_uploaded_arrays_and_frozen_job"
    )
    assert receipt["spatial_registration"] == "not_established"
    assert receipt["physical_validation"] == "not_established"
    assert receipt["product_hdr_lfm_decision"] == "inconclusive"


def test_registration_qualification_is_source_bound_and_blocks_pixelwise_claim():
    fixture = _packet()
    metadata, raw, reference, source_report, cfa, mode, waveform, rate_log = fixture
    files = _render(metadata, raw, reference, mode, waveform, rate_log)
    packet_receipt = validate_packet(
        metadata, files, raw=raw, reference_rate=reference,
        source_lfm_report=source_report, native_cfa=cfa,
    )
    qualification = qualify(
        metadata, files, packet_receipt=packet_receipt,
        hdr_report={"schema": "fixture-hdr"}, source_lfm_report=source_report,
    )
    assert qualification["status"] == "blocked_pending_independent_registration_evidence"
    assert qualification["verified_packet_bindings"]["model_native_crop_origin"] is None
    assert qualification["declared_timing_window"]["first_start_ns"] == 0
    assert qualification["declared_timing_window"]["last_end_ns"] == 11_280_000
    assert qualification["declared_timing_window"]["waveform_trace_verified"] is False
    required = {item["code"] for item in qualification["requirements"]["required_inputs"]}
    assert required == {
        "virtual_window_to_native_mapping", "reference_to_raw_spatial_registration",
        "raw_waveform_clock_registration", "reference_phase_time_registration",
        "measured_readout_calibration", "independent_source_review",
    }
    assert qualification["requirements"]["pixelwise_error_available"] is False
    assert qualification["requirements"]["measured_fusion_error_available"] is False


def test_registration_qualification_rejects_unverified_packet_receipt():
    fixture = _packet()
    metadata, raw, reference, source_report, _, mode, waveform, rate_log = fixture
    files = _render(metadata, raw, reference, mode, waveform, rate_log)
    with pytest.raises(ValueError, match="verified structured packet"):
        qualify(
            metadata, files, packet_receipt={"internal_consistency": "not_assessed"},
            hdr_report={}, source_lfm_report=source_report,
        )


@pytest.mark.parametrize(
    ("change", "message"), [
        (lambda item: item[5]["frame_order"][1].update(phase_index=2),
         "exposure/phase labels"),
        (lambda item: item[5]["frame_order"][0].update(raw_frame_sha256="0" * 64),
         "raw_frame_sha256"),
        (lambda item: item[5]["frame_order"][1].update(row_zero_timestamp_ns=0),
         "timestamps"),
        (lambda item: item[6].update(line_time_s=1e-5), "line_time_s"),
        (lambda item: item[6]["stimulus"].update(duty_cycle=0.8), "stimulus"),
        (lambda item: item[7].update(reference_rate_file_sha256="0" * 64),
         "reference_rate_file_sha256"),
        (lambda item: item[7]["phase_order"][1].update(phase_index=2),
         "Reference phase array order"),
        (lambda item: item[7]["calibration"].update(uses_model_output=True),
         "uses_model_output"),
        (lambda item: item[7]["calibration"].update(
            reference_instrument_id="synthetic-raw-reader"), "instrument must differ"),
    ],
)
def test_packet_rejects_internal_inconsistency(change, message):
    fixture = _packet()
    change(fixture)
    with pytest.raises(ValueError, match=message):
        _validate(fixture)


def test_cfa_phase_mismatch_is_rejected_even_when_crop_is_in_bounds():
    fixture = _packet()
    fixture[0]["mode"]["native_crop_origin_row_col"] = [1, 0]
    with pytest.raises(ValueError, match="Native crop CFA phase"):
        _validate(fixture)


def test_duplicate_log_keys_are_rejected():
    fixture = _packet()
    metadata, raw, reference, source_report, cfa, mode, waveform, rate_log = fixture
    files = _render(metadata, raw, reference, mode, waveform, rate_log)
    files["mode_log"] = files["mode_log"].replace(
        b'"schema":"camerae2e_hdr_lfm_acquisition_mode_log_v1",',
        b'"schema":"camerae2e_hdr_lfm_acquisition_mode_log_v1",'
        b'"schema":"camerae2e_hdr_lfm_acquisition_mode_log_v1",',
    )
    with pytest.raises(ValueError, match="Duplicate HDR/LFM log key"):
        validate_packet(
            metadata, files, raw=raw, reference_rate=reference,
            source_lfm_report=source_report, native_cfa=cfa,
        )


def test_input_documents_are_not_mutated_by_validator():
    fixture = _packet()
    original = copy.deepcopy(fixture[0])
    _validate(fixture)
    assert fixture[0]["mode"] == original["mode"]


def test_structured_job_cannot_be_downgraded_to_legacy_opaque_logs():
    with pytest.raises(IntegrityError, match="marker was removed"):
        _packet_schema({"packet_consistency_receipt": {}})
    with pytest.raises(IntegrityError, match="marker was removed"):
        _packet_schema({}, {
            "packet_consistency": {
                "internal_consistency": "verified_against_uploaded_arrays_and_frozen_job"
            }
        })
    assert _packet_schema({}, {}) is None
    with pytest.raises(IntegrityError, match="qualification marker was removed"):
        _qualification_schema({"registration_qualification_receipt": {}})
    with pytest.raises(IntegrityError, match="qualification marker was removed"):
        _qualification_schema({}, {"registration_qualification": {}})
    assert _qualification_schema({}, {}) is None
