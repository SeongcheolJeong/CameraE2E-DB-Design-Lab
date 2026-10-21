"""A source-pinned active mode can plan field tiles but cannot qualify a product."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from camera_platform.lens_sensor_fullfield_study_preflight import (
    DECLARATION_SCHEMA,
    MODE_SCHEMA,
    preflight_fullfield_study,
)

WORKSPACE = Path(__file__).resolve().parents[2]
LENS_REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
SENSOR_CODE = "DEF-2512-801"
SENSOR_RUN = "360dfe5370ada20b81c085d5859e9b0ae54be66df0a5e7e8eed0bffe7c6dc601"


@pytest.fixture(scope="module")
def base() -> dict:
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    result = preflight_fullfield_study(
        LENS_REVISION,
        SENSOR_CODE,
        sensor_run_id=SENSOR_RUN,
        active_mode=None,
        root=WORKSPACE / "camerae2e_db",
    )
    assert result["sensor_revision"]["report_code"] == SENSOR_CODE
    return result


def _mode(
    tmp_path: Path,
    revision: dict,
    *,
    axial_offset_um: float = 0.0,
    cfa_origin_row_col: list[int] | None = None,
) -> dict:
    # These dimensions are illustrative test numbers, not GC50F6 product data.
    core = {
        "schema": MODE_SCHEMA,
        "mode_id": "illustrative-test-declaration",
        "sensor_run_id": revision["run_id"],
        "sensor_report_code": revision["report_code"],
        "sensor_result_sha256": revision["result_sha256"],
        "active_rows": 1000,
        "active_cols": 1200,
        "pixel_pitch_um": 0.61,
        "sensor_center_offset_from_optical_axis_um_row_col": [0.0, 0.0],
        "sensor_plane": "perpendicular_to_optical_axis",
        "sensor_orientation_deg_clockwise": 0,
        "cfa_origin_row_col": [0, 0] if cfa_origin_row_col is None else cfa_origin_row_col,
        "image_plane_binding": {
            "reference_plane": "saved_prescription_image_plane",
            "axial_offset_um": axial_offset_um,
        },
    }
    path = tmp_path / "illustrative-mode.json"
    path.write_text(
        json.dumps({"schema": DECLARATION_SCHEMA, "active_mode": core}, sort_keys=True),
        encoding="utf-8",
    )
    return {
        **core,
        "source": {
            "kind": "engineering_declaration",
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        },
    }


def _check(active_mode: dict | None) -> dict:
    return preflight_fullfield_study(
        LENS_REVISION,
        SENSOR_CODE,
        sensor_run_id=SENSOR_RUN,
        active_mode=active_mode,
        root=WORKSPACE / "camerae2e_db",
    )


def test_saved_crop_and_nominal_megapixels_do_not_infer_active_mode(base):
    assert base["status"] == "not_evaluable"
    assert base["reason"] == "full_sensor_active_mode_geometry_missing"
    assert base["active_mode"] is None
    assert base["planning_ready"] is False
    assert base["execution_qualified"] is False
    assert base["product_qualification_allowed"] is False


def test_mode_source_digest_change_is_blocked(base, tmp_path):
    mode = _mode(tmp_path, base["sensor_revision"])
    path = Path(mode["source"]["path"])
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    result = _check(mode)
    assert result["status"] == "blocked"
    assert result["reason"] == "mode_source_digest_mismatch"
    assert result["source_validation"] is None
    assert result["execution_qualified"] is False


def test_matching_unrelated_source_values_are_blocked(base, tmp_path):
    mode = _mode(tmp_path, base["sensor_revision"])
    changed = deepcopy(mode)
    changed["active_cols"] = 1300
    result = _check(changed)
    assert result["status"] == "blocked"
    assert result["reason"] == "mode_source_values_mismatch"


def test_nonzero_axial_offset_is_blocked_even_with_pinned_source(base, tmp_path):
    mode = _mode(tmp_path, base["sensor_revision"], axial_offset_um=2.0)
    result = _check(mode)
    assert result["status"] == "blocked"
    assert result["reason"] == "axial_offset_unsupported"
    assert result["planning_ready"] is False
    assert result["physical_mtf_allowed"] is False


def test_orientation_is_required_and_cfa_origin_must_fit_saved_period(base, tmp_path):
    mode = _mode(tmp_path, base["sensor_revision"])
    del mode["sensor_orientation_deg_clockwise"]
    missing = _check(mode)
    assert missing["status"] == "blocked"
    assert missing["reason"] == "active_mode_fields_incomplete"

    invalid = _mode(
        tmp_path, base["sensor_revision"], cfa_origin_row_col=[2, 0]
    )
    out_of_period = _check(invalid)
    assert out_of_period["status"] == "blocked"
    assert out_of_period["reason"] == "cfa_origin_outside_period"


def test_declared_geometric_support_is_planning_only(base, tmp_path):
    mode = _mode(tmp_path, base["sensor_revision"])
    result = _check(mode)
    assert result["status"] == "planning_only"
    assert result["reason"] == "saved_geometric_field_support_only"
    assert result["geometric_fov"]["status"] == "within_saved_model_support"
    assert result["source_validation"]["file_hash_verified"] is True
    assert result["source_validation"]["declaration_values_match"] is True
    assert result["source_validation"]["physical_mode_verified"] is False
    assert result["active_mode"]["sensor_orientation_deg_clockwise"] == 0
    assert result["active_mode"]["cfa_origin_row_col"] == [0, 0]
    assert result["planning_ready"] is True
    assert result["execution_qualified"] is False
    assert result["full_frame_capture_qualified"] is False
    assert result["physical_mtf_allowed"] is False
    assert result["product_qualification_allowed"] is False


def test_quarter_turn_swaps_active_extents_on_optical_axes(base, tmp_path):
    mode = _mode(tmp_path, base["sensor_revision"])
    mode["sensor_orientation_deg_clockwise"] = 90
    path = Path(mode["source"]["path"])
    path.write_text(
        json.dumps({
            "schema": DECLARATION_SCHEMA,
            "active_mode": {key: value for key, value in mode.items() if key != "source"},
        }, sort_keys=True),
        encoding="utf-8",
    )
    mode["source"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = _check(mode)
    assert result["status"] == "planning_only"
    assert result["active_extent_on_optical_axes_um_row_col"] == [732.0, 610.0]
    assert result["geometric_fov"]["active_mode"]["active_rows"] == 1200
    assert result["geometric_fov"]["active_mode"]["active_cols"] == 1000


def test_integer_zero_declaration_remains_source_identical(base, tmp_path):
    mode = _mode(tmp_path, base["sensor_revision"])
    mode["sensor_center_offset_from_optical_axis_um_row_col"] = [0, 0]
    mode["image_plane_binding"]["axial_offset_um"] = 0
    path = Path(mode["source"]["path"])
    path.write_text(
        json.dumps({
            "schema": DECLARATION_SCHEMA,
            "active_mode": {key: value for key, value in mode.items() if key != "source"},
        }, sort_keys=True),
        encoding="utf-8",
    )
    mode["source"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert _check(mode)["status"] == "planning_only"


def test_oversized_integer_geometry_fails_closed(base, tmp_path):
    mode = _mode(tmp_path, base["sensor_revision"])
    mode["pixel_pitch_um"] = 10**1000
    result = _check(mode)
    assert result["status"] == "blocked"
    assert result["reason"] == "active_mode_geometry_invalid"


def test_unhashable_source_kind_fails_closed(base, tmp_path):
    mode = _mode(tmp_path, base["sensor_revision"])
    mode["source"]["kind"] = []
    result = _check(mode)
    assert result["status"] == "blocked"
    assert result["reason"] == "mode_source_kind_invalid"


def test_outer_field_exceedance_blocks_plan(base, tmp_path):
    mode = _mode(tmp_path, base["sensor_revision"])
    mode["active_rows"] = 45_000
    mode["active_cols"] = 45_000
    path = Path(mode["source"]["path"])
    path.write_text(
        json.dumps(
            {
                "schema": DECLARATION_SCHEMA,
                "active_mode": {key: value for key, value in mode.items() if key != "source"},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    mode["source"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = _check(mode)
    assert result["status"] == "blocked"
    assert result["reason"] == "active_boundary_exceeds_saved_lens_field_at_one_or_more_wavelengths"
    assert result["geometric_fov"]["status"] == "outside_saved_model_support"
    assert result["execution_qualified"] is False
