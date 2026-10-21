"""The HDR coverage result and product evidence gate must remain separate."""

from __future__ import annotations

import copy
import hashlib
import io
import json

import numpy as np
import pytest

from camera_platform.hdr_lfm_evidence_readiness import assess_hdr_lfm_evidence, main
from camera_platform.lens_sensor_lfm import SCHEMA as LFM_SCHEMA
from camera_platform.lens_sensor_lfm import TIMED_BOUNDED_SCHEMA as TIMED_LFM_SCHEMA
from camera_platform.lens_sensor_timing_mode import (
    CAPTURE_PROGRAM,
    GEOMETRY_SCOPE,
    PHASE_ORIGIN,
    mode_binding,
)
from camera_platform.lens_sensor_timing_mode import (
    SCHEMA as TIMING_SCHEMA,
)
from camera_platform.registry import canonical, digest


def _case(*, complete: bool = False, timed: bool = False):
    raw = np.arange(2 * 4 * 8 * 8, dtype=np.int32).reshape((2, 4, 8, 8))
    geometry = {
        "configured_sensor_size": [8, 8],
        "saved_native_array_crop_origin": None,
        "saved_native_array_crop_mapping": "not_established",
    }
    lens = {"revision_id": "lens-rev"}
    sensor = {"identifier": "sensor", "run_id": "saved-run"}
    source_report = {
        "schema": TIMED_LFM_SCHEMA if timed else LFM_SCHEMA,
        "lens_identity": lens,
        "sensor_revision": sensor,
        "geometry_provenance": geometry,
        "source_boundary": {
            "sensor_readout_profile": "generic_assumption",
            "sensor_readout_classification": "unmeasured_assumption",
        },
    }
    source_summary = {"report": source_report, "status": "completed"}
    waveform = {"line_time_s": 20e-6}
    if timed:
        binding = mode_binding(
            sensor, {"full_well_e": 10000.0},
            {"profile": "generic_assumption", "classification": "unmeasured_assumption"},
            [8, 8], [0.001, 0.004],
        )
        mode = {
            "schema": TIMING_SCHEMA,
            "mode_id": "source-declared-mode",
            "binding": binding,
            "timing": {
                "shutter": "rolling", "line_time_s": 20e-6,
                "frame_start_s": 0.0, "phase_origin": PHASE_ORIGIN,
                "capture_program": CAPTURE_PROGRAM,
            },
            "provenance": {
                "source_kind": "vendor_document_declared",
                "source_uri": "vendor://declared-test-document",
                "source_document_sha256": "b" * 64,
                "method_and_version": "manual_value_declaration_v1",
                "value_verification": "declared_not_independently_verified",
            },
        }
        assert binding["geometry_scope"] == GEOMETRY_SCOPE
        waveform.update(
            timing_mode=mode, timing_mode_sha256="a" * 64,
            timing_mode_canonical_sha256=digest(mode),
        )
    fused, fallback, invalid = (224, 32, 0) if complete else (8, 105, 143)
    reconstructed = fused + fallback
    report = {
        "schema": "camerae2e_lens_sensor_pwm_hdr_v1",
        "status": "completed" if complete else "inconclusive",
        "completion_scope": (
            "all_pixels_estimable_with_fallback" if complete
            else "partial_estimability_or_no_two_bracket_fusion"
        ),
        "source_lfm_schema": source_report["schema"],
        "source_lfm_result_sha256": digest(source_summary),
        "source_lfm_report_sha256": digest(source_report),
        "lens_identity": lens,
        "sensor_revision": sensor,
        "geometry_provenance": geometry,
        "original_pwm_raw_shape": [2, 4, 8, 8],
        "original_pwm_raw_sha256": hashlib.sha256(raw.tobytes()).hexdigest(),
        "fused_hdr_pixels": fused,
        "single_bracket_fallback_pixels": fallback,
        "reconstructed_pixels": reconstructed,
        "invalid_pixels": invalid,
        "coverage_fraction": reconstructed / 256,
        "two_bracket_fused_fraction": fused / 256,
        "single_bracket_fallback_fraction": fallback / 256,
        "waveform": waveform,
        "verification": {"physical_validation": "not_established"},
    }
    return raw, source_summary, report


def _codes(assessment, gate):
    return {item["code"] for item in assessment[gate]["blockers"]}


def test_inconclusive_coverage_and_assumptions_stay_separate_from_product_claim():
    raw, source_summary, report = _case()
    before = copy.deepcopy(report)
    assessed = assess_hdr_lfm_evidence(
        report, original_raw_dn=raw, source_lfm_summary=source_summary
    )
    assert report == before
    assert assessed["model_internal_coverage"] == {
        "status": "inconclusive",
        "completion_scope": "partial_estimability_or_no_two_bracket_fusion",
        "total_phase_pixels": 256,
        "two_bracket_fused_pixels": 8,
        "single_bracket_fallback_pixels": 105,
        "invalid_pixels": 143,
        "coverage_fraction": 113 / 256,
    }
    assert assessed["source"]["original_raw_bytes_reverified"] is True
    assert assessed["source"]["sensor_readout_profile"] == "generic_assumption"
    assert assessed["source"]["sensor_readout_classification"] == "unmeasured_assumption"
    assert assessed["vendor_mode_simulation"]["status"] == "not_established"
    assert {
        "model_coverage_incomplete", "authored_timing_assumption",
        "timing_value_not_independently_verified", "vendor_capture_program_not_executed",
    } <= _codes(assessed, "vendor_mode_simulation")
    assert "unmeasured_generic_readout" in _codes(assessed, "product_hdr_lfm_decision")
    assert assessed["product_hdr_lfm_decision"]["status"] == "not_established"


def test_completed_model_and_source_pinned_vendor_declaration_still_not_product_ready():
    raw, source_summary, report = _case(complete=True, timed=True)
    assessed = assess_hdr_lfm_evidence(
        report, original_raw_dn=raw, source_lfm_summary=source_summary
    )
    assert assessed["model_internal_coverage"]["status"] == "completed"
    assert assessed["source"]["timing_source_kind"] == "vendor_document_declared"
    assert "authored_timing_assumption" not in _codes(assessed, "vendor_mode_simulation")
    assert "timing_value_not_independently_verified" in _codes(
        assessed, "vendor_mode_simulation"
    )
    assert assessed["product_hdr_lfm_decision"]["status"] == "not_established"


def test_missing_companion_artifacts_are_explicitly_not_reverified():
    _, _, report = _case()
    assessed = assess_hdr_lfm_evidence(report)
    assert assessed["source"]["original_raw_bytes_reverified"] is False
    assert assessed["source"]["source_lfm_result_reverified"] is False
    assert {
        "original_raw_bytes_not_reverified", "source_lfm_result_not_reverified"
    } <= _codes(assessed, "product_hdr_lfm_decision")


def test_forged_counts_or_source_or_raw_are_rejected():
    raw, source_summary, report = _case()
    altered = copy.deepcopy(report)
    altered["invalid_pixels"] = 142
    with pytest.raises(ValueError, match="coverage counts"):
        assess_hdr_lfm_evidence(altered)
    altered = copy.deepcopy(source_summary)
    altered["report"]["source_boundary"]["sensor_readout_profile"] = "vendor"
    with pytest.raises(ValueError, match="source|Source"):
        assess_hdr_lfm_evidence(report, source_lfm_summary=altered)
    changed_raw = raw.copy()
    changed_raw[0, 0, 0, 0] += 1
    with pytest.raises(ValueError, match="Original PWM RAW"):
        assess_hdr_lfm_evidence(report, original_raw_dn=changed_raw)


def test_cli_checks_report_and_companion_artifact_hashes(tmp_path, capsys):
    raw, source_summary, report = _case()
    npz_stream = io.BytesIO()
    np.savez_compressed(npz_stream, original_pwm_raw_dn=raw)
    npz_bytes = npz_stream.getvalue()
    report["evidence_npz"] = {"sha256": hashlib.sha256(npz_bytes).hexdigest()}
    report_bytes = canonical(report).encode()
    source_bytes = canonical(source_summary).encode()
    report_path = tmp_path / "lens-sensor-pwm-hdr-evidence.json"
    npz_path = tmp_path / "lens-sensor-pwm-hdr-evidence.npz"
    source_path = tmp_path / "lens-sensor-lfm-result.json"
    report_path.write_bytes(report_bytes)
    npz_path.write_bytes(npz_bytes)
    source_path.write_bytes(source_bytes)
    sha = hashlib.sha256(report_bytes).hexdigest()
    assert main([
        str(report_path), "--sha256", sha, "--npz", str(npz_path),
        "--source-lfm-result", str(source_path),
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source"]["report_artifact_sha256"] == sha
    assert result["source"]["evidence_npz_reverified"] is True
    assert result["source"]["source_lfm_result_reverified"] is True
    assert result["product_hdr_lfm_decision"]["status"] == "not_established"
    with pytest.raises(ValueError, match="report bytes"):
        main([str(report_path), "--sha256", "0" * 64])
    source_path.write_bytes(source_bytes + b" ")
    with pytest.raises(ValueError, match="Source PWM/LFM result bytes"):
        main([
            str(report_path), "--sha256", sha,
            "--source-lfm-result", str(source_path),
        ])
