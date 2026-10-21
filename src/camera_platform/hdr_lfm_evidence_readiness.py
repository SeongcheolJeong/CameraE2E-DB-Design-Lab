"""Read-only evidence assessment for a saved Lens x Sensor PWM HDR report.

The existing HDR ``status`` describes model-internal pixel coverage.  A
completed reconstruction is not evidence that a vendor HDR/LFM acquisition
program, optical response, sensor readout, or physical camera was validated.
This module never changes a saved report or promotes its product status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from .lens_sensor_lfm import (
    BOUNDED_SCHEMA as BOUNDED_LFM_SCHEMA,
)
from .lens_sensor_lfm import (
    SCHEMA as LFM_SCHEMA,
)
from .lens_sensor_lfm import (
    TIMED_BOUNDED_SCHEMA as TIMED_LFM_SCHEMA,
)
from .lens_sensor_pwm_hdr import SCHEMA as HDR_SCHEMA
from .lens_sensor_timing_mode import validate_timing_mode
from .registry import canonical, digest

SCHEMA = "camerae2e_hdr_lfm_evidence_readiness_v1"
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_NPZ_BYTES = 64 * 1024 * 1024
MAX_NPZ_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{name} requires a lowercase SHA-256 digest.")
    return value


def _count(report: Mapping[str, Any], name: str) -> int:
    value = report.get(name)
    if type(value) is not int or value < 0:
        raise ValueError(f"HDR report {name} must be a nonnegative integer.")
    return value


def _block(code: str, required: str) -> dict[str, str]:
    return {"code": code, "required_evidence": required}


def assess_hdr_lfm_evidence(
    report: Mapping[str, Any], *, original_raw_dn: np.ndarray | None = None,
    source_lfm_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assess coverage and missing evidence without altering the HDR report.

    ``original_raw_dn`` is optional.  When supplied, its shape, dtype and
    C-order bytes are checked against the report.  Without it, the stored hash
    is retained as a claim but the RAW bytes are not marked reverified.
    """
    if not isinstance(report, Mapping) or report.get("schema") != HDR_SCHEMA:
        raise ValueError("Select one saved Lens x Sensor PWM HDR report.")
    report = dict(report)
    shape = report.get("original_pwm_raw_shape")
    if (
        not isinstance(shape, list) or len(shape) != 4
        or shape[:2] != [2, 4]
        or any(type(value) is not int or not 8 <= value <= 32 or value % 2
               for value in shape[2:])
    ):
        raise ValueError("HDR report needs the original [2,4,even H,even W] RAW shape.")
    # A phase pixel has two bracket observations; coverage is over the four
    # reconstructed phase images, not over all eight source RAW frames.
    total = math.prod(shape[1:])
    fused = _count(report, "fused_hdr_pixels")
    fallback = _count(report, "single_bracket_fallback_pixels")
    reconstructed = _count(report, "reconstructed_pixels")
    invalid = _count(report, "invalid_pixels")
    if fused + fallback != reconstructed or reconstructed + invalid != total:
        raise ValueError("HDR coverage counts do not agree with the original RAW shape.")
    complete = fused > 0 and reconstructed == total
    status = "completed" if complete else "inconclusive"
    expected_scope = (
        "all_pixels_estimable_with_fallback"
        if reconstructed == total and fallback > 0 and fused > 0
        else "all_pixels_two_bracket_fused"
        if fused == total
        else "partial_estimability_or_no_two_bracket_fusion"
    )
    if report.get("status") != status or report.get("completion_scope") != expected_scope:
        raise ValueError("HDR coverage status differs from its saved counts.")
    for name, count in (
        ("coverage_fraction", reconstructed),
        ("two_bracket_fused_fraction", fused),
        ("single_bracket_fallback_fraction", fallback),
    ):
        value = report.get(name)
        if (
            type(value) not in (int, float) or not math.isfinite(value)
            or not math.isclose(float(value), count / total, rel_tol=0, abs_tol=1e-12)
        ):
            raise ValueError(f"HDR report {name} differs from its saved counts.")
    raw_sha = _sha(report.get("original_pwm_raw_sha256"), "original_pwm_raw_sha256")
    raw_verified = False
    if original_raw_dn is not None:
        if (
            not isinstance(original_raw_dn, np.ndarray)
            or original_raw_dn.dtype != np.dtype("int32")
            or list(original_raw_dn.shape) != shape
            or hashlib.sha256(
                np.ascontiguousarray(original_raw_dn).tobytes(order="C")
            ).hexdigest() != raw_sha
        ):
            raise ValueError("Original PWM RAW differs from the saved HDR report.")
        raw_verified = True
    source_schema = report.get("source_lfm_schema")
    if source_schema not in {LFM_SCHEMA, BOUNDED_LFM_SCHEMA, TIMED_LFM_SCHEMA}:
        raise ValueError("HDR report refers to an unsupported PWM/LFM source schema.")
    waveform = report.get("waveform")
    geometry = report.get("geometry_provenance")
    verification = report.get("verification")
    if not all(isinstance(item, Mapping) for item in (waveform, geometry, verification)):
        raise ValueError("HDR report lacks waveform, geometry or verification evidence.")
    if verification.get("physical_validation") != "not_established":
        raise ValueError("Current HDR schema cannot claim physical validation.")
    readout_profile = None
    readout_classification = None
    source_reverified = False
    if source_lfm_summary is not None:
        if not isinstance(source_lfm_summary, Mapping):
            raise ValueError("Source PWM/LFM result must be a mapping.")
        expected_result = _sha(
            report.get("source_lfm_result_sha256"), "source_lfm_result_sha256"
        )
        expected_report = _sha(
            report.get("source_lfm_report_sha256"), "source_lfm_report_sha256"
        )
        source_lfm_summary = dict(source_lfm_summary)
        source_report = source_lfm_summary.get("report")
        if (
            digest(source_lfm_summary) != expected_result
            or not isinstance(source_report, Mapping)
            or digest(source_report) != expected_report
            or source_report.get("schema") != source_schema
            or source_report.get("lens_identity") != report.get("lens_identity")
            or source_report.get("sensor_revision") != report.get("sensor_revision")
            or source_report.get("geometry_provenance") != geometry
        ):
            raise ValueError("Source PWM/LFM result differs from the saved HDR binding.")
        boundary = source_report.get("source_boundary")
        if not isinstance(boundary, Mapping):
            raise ValueError("Source PWM/LFM result lacks Sensor readout provenance.")
        readout_profile = boundary.get("sensor_readout_profile")
        readout_classification = boundary.get("sensor_readout_classification")
        if not all(isinstance(value, str) and value for value in (
            readout_profile, readout_classification
        )):
            raise ValueError("Source PWM/LFM readout provenance is incomplete.")
        source_reverified = True
    timing_kind = "authored_assumption"
    if source_schema == TIMED_LFM_SCHEMA:
        mode = validate_timing_mode(waveform.get("timing_mode"))
        _sha(waveform.get("timing_mode_sha256"), "timing_mode_sha256")
        if (
            waveform.get("timing_mode_canonical_sha256") != digest(mode)
            or waveform.get("line_time_s") != mode["timing"]["line_time_s"]
        ):
            raise ValueError("HDR report timing mode differs from the saved waveform.")
        timing_kind = mode["provenance"]["source_kind"]
    elif "timing_mode" in waveform or "timing_mode_sha256" in waveform:
        raise ValueError("Legacy HDR report cannot claim an imported timing mode.")

    mode_blockers = []
    if not complete:
        mode_blockers.append(_block(
            "model_coverage_incomplete",
            "A declared use-case acceptance criterion and adequate valid RAW coverage; "
            "the present model has invalid or no two-bracket pixels.",
        ))
    if timing_kind == "authored_assumption":
        mode_blockers.append(_block(
            "authored_timing_assumption",
            "A sensor-mode timing source bound to the selected operating point; the "
            "authored line time is only a sensitivity input.",
        ))
    mode_blockers.extend([
        _block(
            "timing_value_not_independently_verified",
            "Mode-specific line/frame timing verified against its pinned source or measurement.",
        ),
        _block(
            "native_crop_and_absolute_phase_unknown",
            "Native die crop origin, CFA phase, row-zero timing and absolute PWM phase binding.",
        ),
        _block(
            "vendor_capture_program_not_executed",
            "A mode-specific exposure/readout/fusion schedule and an executor for that "
            "program. The current two-exposure four-phase repeated static-scene "
            "program is a sensitivity model.",
        ),
        _block(
            "illumination_and_motion_domain_unvalidated",
            "Measured or independently specified PWM waveform/spectrum and representative "
            "moving or changing scenes with phase coverage.",
        ),
    ])
    product_blockers = [
        _block(
            "lens_radiometry_diffraction_cra_unvalidated",
            "Bound lens transmission, relative illumination, diffraction/PSF and chief-ray "
            "angle to the sensor's pixel angular response.",
        ),
        _block(
            "sensor_readout_and_fpn_unvalidated",
            "Mode-specific readout, noise and FPN calibration with stated uncertainty; "
            "the current maps are generated from model assumptions.",
        ),
        _block(
            "independent_product_validation_missing",
            "Independent cross-tool and physical-camera reference over stated HDR/LFM "
            "acceptance criteria and operating conditions.",
        ),
    ]
    if not source_reverified:
        product_blockers.append(_block(
            "source_lfm_result_not_reverified",
            "Provide the saved source PWM/LFM result JSON and verify its SHA-256 "
            "against the HDR report before interpreting Sensor provenance.",
        ))
    elif (
        readout_profile == "generic_assumption"
        or readout_classification == "unmeasured_assumption"
    ):
        product_blockers.append(_block(
            "unmeasured_generic_readout",
            "Mode-specific vendor or calibrated electron-domain readout profile; the "
            "selected run uses generic unmeasured assumptions.",
        ))
    if not raw_verified:
        product_blockers.append(_block(
            "original_raw_bytes_not_reverified",
            "Provide the saved HDR evidence NPZ and verify its SHA-256 plus original "
            "int32 RAW array bytes.",
        ))
    return {
        "schema": SCHEMA,
        "assessment_scope": "read_only_saved_model_evidence; no product endorsement",
        "source": {
            "hdr_report_canonical_sha256": digest(report),
            "original_pwm_raw_sha256": raw_sha,
            "original_pwm_raw_shape": shape.copy(),
            "original_raw_bytes_reverified": raw_verified,
            "source_lfm_schema": source_schema,
            "source_lfm_result_reverified": source_reverified,
            "timing_source_kind": timing_kind,
            "sensor_readout_profile": readout_profile,
            "sensor_readout_classification": readout_classification,
        },
        "model_internal_coverage": {
            "status": status,
            "completion_scope": expected_scope,
            "total_phase_pixels": total,
            "two_bracket_fused_pixels": fused,
            "single_bracket_fallback_pixels": fallback,
            "invalid_pixels": invalid,
            "coverage_fraction": reconstructed / total,
        },
        "vendor_mode_simulation": {
            "status": "not_established",
            "blockers": mode_blockers,
        },
        "product_hdr_lfm_decision": {
            "status": "not_established",
            "blockers": mode_blockers + product_blockers,
        },
    }


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate HDR report JSON key: {key}.")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"HDR report contains nonfinite number {value}.")


def _bounded_bytes(path: Path, limit: int) -> bytes:
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError(f"Evidence file missing, empty or above {limit} bytes: {path}")
    return path.read_bytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_json", type=Path, help="saved lens-sensor-pwm-hdr-evidence.json")
    parser.add_argument("--sha256", required=True, help="expected SHA-256 of report JSON bytes")
    parser.add_argument("--npz", type=Path, help="optional saved HDR evidence NPZ")
    parser.add_argument(
        "--source-lfm-result", type=Path,
        help="optional saved lens-sensor-lfm-result.json; SHA bound by HDR report",
    )
    parser.add_argument("--output", type=Path, help="new readiness JSON path; default stdout")
    args = parser.parse_args(argv)
    expected = _sha(args.sha256, "report SHA-256")
    content = _bounded_bytes(args.report_json, MAX_REPORT_BYTES)
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("HDR report bytes differ from the expected SHA-256.")
    try:
        report = json.loads(content, object_pairs_hook=_unique_pairs,
                            parse_constant=_reject_nonfinite)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("HDR report is not valid UTF-8 JSON.") from exc
    raw = None
    if args.npz is not None:
        descriptor = report.get("evidence_npz") if isinstance(report, dict) else None
        if not isinstance(descriptor, dict):
            raise ValueError("HDR report lacks the saved evidence NPZ descriptor.")
        expected_npz = _sha(descriptor.get("sha256"), "evidence NPZ SHA-256")
        npz_content = _bounded_bytes(args.npz, MAX_NPZ_BYTES)
        if hashlib.sha256(npz_content).hexdigest() != expected_npz:
            raise ValueError("HDR evidence NPZ differs from its saved descriptor.")
        with zipfile.ZipFile(args.npz) as package:
            members = package.infolist()
            if (
                not 0 < len(members) <= 64
                or len({item.filename for item in members}) != len(members)
                or sum(item.file_size for item in members) > MAX_NPZ_UNCOMPRESSED_BYTES
            ):
                raise ValueError("HDR evidence NPZ exceeds its bounded array contract.")
        with np.load(args.npz, allow_pickle=False) as archive:
            if "original_pwm_raw_dn" not in archive.files:
                raise ValueError("HDR evidence NPZ lacks original_pwm_raw_dn.")
            raw = np.asarray(archive["original_pwm_raw_dn"]).copy()
    source_summary = None
    if args.source_lfm_result is not None:
        expected_source = _sha(
            report.get("source_lfm_result_sha256"), "source_lfm_result_sha256"
        )
        source_content = _bounded_bytes(args.source_lfm_result, MAX_REPORT_BYTES)
        if hashlib.sha256(source_content).hexdigest() != expected_source:
            raise ValueError("Source PWM/LFM result bytes differ from the HDR binding.")
        try:
            source_summary = json.loads(
                source_content, object_pairs_hook=_unique_pairs,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Source PWM/LFM result is not valid UTF-8 JSON.") from exc
    assessment = assess_hdr_lfm_evidence(
        report, original_raw_dn=raw, source_lfm_summary=source_summary
    )
    assessment["source"]["report_artifact_sha256"] = expected
    assessment["source"]["evidence_npz_reverified"] = args.npz is not None
    encoded = canonical(assessment) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
