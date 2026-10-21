"""Independent contract checks for source-bound relative rolling-row timing."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from camera_platform.lens_sensor_lfm_job import import_timing_mode
from camera_platform.lens_sensor_timing_mode import (
    CAPTURE_PROGRAM,
    PHASE_ORIGIN,
    SCHEMA,
    mode_binding,
    timing_provenance,
    validate_timing_mode,
    validate_timing_mode_bytes,
)
from camera_platform.service import PlatformService

WORKSPACE = Path(__file__).resolve().parents[2]


def _mode(*, source_kind="authored_assumption", source_sha=None, line_time_s=40e-6):
    binding = mode_binding(
        {"report_code": "sensor-1", "run_id": "saved-1", "result_sha256": "a" * 64},
        {"bit_depth": 12, "black_level_dn": 64.0},
        {"profile": "generic_assumption", "classification": "unmeasured_assumption"},
        [8, 8], [0.001, 0.004],
    )
    return {
        "schema": SCHEMA,
        "mode_id": "bounded-relative-row-timing",
        "binding": binding,
        "timing": {
            "shutter": "rolling", "line_time_s": line_time_s, "frame_start_s": 0.0,
            "phase_origin": PHASE_ORIGIN, "capture_program": CAPTURE_PROGRAM,
        },
        "provenance": {
            "source_kind": source_kind,
            "source_uri": (
                "authored://timing-sensitivity" if source_kind == "authored_assumption"
                else "https://example.invalid/source-document"
            ),
            "source_document_sha256": source_sha,
            "method_and_version": "supplied_line_time_v1",
            "value_verification": "declared_not_independently_verified",
        },
    }


def _bytes(mode):
    return json.dumps(mode, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def test_mode_is_versioned_and_bound_to_exact_sensor_readout_size_and_exposures():
    mode = _mode()
    content = _bytes(mode)
    sha = hashlib.sha256(content).hexdigest()
    assert validate_timing_mode_bytes(content, sha) == mode
    assert timing_provenance(mode).startswith("authored_assumption_document_pinned")
    changed = copy.deepcopy(mode["binding"])
    changed["exposure_s"][1] = 0.008
    with pytest.raises(ValueError, match="differs from the selected"):
        validate_timing_mode(mode, expected_binding=changed)
    with pytest.raises(ValueError, match="submitted SHA-256"):
        validate_timing_mode_bytes(content, "0" * 64)


@pytest.mark.parametrize("change", [
    lambda m: m["timing"].update(shutter="global"),
    lambda m: m["timing"].update(capture_program="native_vendor_hdr_mode"),
    lambda m: m["timing"].update(phase_origin="known_die_row_zero"),
    lambda m: m["timing"].update(line_time_s=0),
    lambda m: m["timing"].update(frame_start_s=True),
    lambda m: m["binding"].update(geometry_scope="located_die_crop"),
    lambda m: m["provenance"].update(source_kind="measured"),
    lambda m: m["provenance"].update(value_verification="vendor_verified"),
])
def test_unsupported_sensor_modes_and_unverified_claims_are_refused(change):
    mode = _mode()
    change(mode)
    with pytest.raises(ValueError):
        validate_timing_mode(mode)


def test_vendor_or_measurement_claim_requires_pinned_source_bytes(tmp_path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    source = b"independent timing evidence fixture\n"
    source_sha = hashlib.sha256(source).hexdigest()
    for kind in ("vendor_document_declared", "measurement_declared"):
        mode = _mode(source_kind=kind, source_sha=source_sha)
        content = _bytes(mode)
        sha = hashlib.sha256(content).hexdigest()
        with pytest.raises(ValueError, match="source evidence bytes differ"):
            import_timing_mode(service, content, sha)
        imported = import_timing_mode(service, content, sha, source)
        assert imported["source_kind"] == kind
        assert imported["source_evidence"]["sha256"] == source_sha
        assert imported["verification"]["timing_value"] == (
            "declared_not_independently_verified"
        )
        assert service.registry.blob_path(sha).read_bytes() == content
    authored = _mode()
    authored_content = _bytes(authored)
    with pytest.raises(ValueError, match="cannot carry vendor"):
        import_timing_mode(
            service, authored_content, hashlib.sha256(authored_content).hexdigest(), source
        )
