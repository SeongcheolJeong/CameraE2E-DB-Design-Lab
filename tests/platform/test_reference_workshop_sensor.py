"""Original reference geometry, absolute QE and immutable RAW-domain LSC."""

import copy
import hashlib
from pathlib import Path

import numpy as np
import pytest

from camera_platform.reference_workshop_sensor import (
    apply_lsc,
    build_cell,
    build_lsc,
    build_response,
    interpolate_qe,
    lsc_gain_map,
)

WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cells():
    if not (WORKSPACE / "camerae2e_db/fdtd_tcad/image_sensor_db/sensor_catalog.json").exists():
        pytest.skip("Local canonical sensor material DB is required")
    return {scale: build_cell(WORKSPACE, scale) for scale in (0.9, 1.0, 1.1)}


def test_reference_is_original_pitch_and_zero_change_preserves_reviewed_geometry(cells):
    cell = cells[1.0]
    assert cell["pixel_pitch_um"] == cell["physical_model"]["pixel_pitch_um"] == 2.9
    assert cell["geometry_um"] == cell["provenance"]["source_geometry_um"]
    assert not cell["product_model"]
    assert cell["field_classification"]["user_overrides"] == {}
    assert cell["provenance"]["structural_evidence"]["available"]
    assert (
        cell["physical_model"]["optical_assumptions"]["geometry_source"] == "reviewed_techinsights"
    )
    pitch = cell["field_classification"]["source_derived"]["pixel_pitch_um"]
    assert pitch["pdf_page_1_based"] == 7
    assert pitch["source_id"] == "DEF-2011-804"
    assert pitch["value"] == 2.9
    assert cell["field_classification"]["common_assumptions"]["collection_efficiency"] == 0.75
    assert cell["field_classification"]["common_assumptions"]["readout"] == cell["readout"]


def test_cfa_overrides_conserve_each_channel_height_and_share_electrical_model(cells):
    baseline = cells[1.0]
    for scale, cell in cells.items():
        geometry = cell["geometry_um"]
        assert cell["cross_section"]["top_stack_height_um"] == pytest.approx(2.5)
        assert geometry["cfa_thickness"] == pytest.approx(0.5 * scale)
        for channel in ("red", "green", "blue"):
            assert geometry["cfa_thickness_by_channel"][channel] == pytest.approx(0.5 * scale)
            assert geometry["cfa_thickness_by_channel"][channel] + geometry[
                "passivation_thickness_by_channel"
            ][channel] == pytest.approx(1.65)
        for key, value in baseline["geometry_um"].items():
            if not key.startswith(("cfa_thickness", "passivation_thickness")):
                assert geometry[key] == value
        assert cell["readout"] == baseline["readout"]
        assert cell["electrical_model"] == baseline["electrical_model"]
        assert (
            cell["provenance"]["source_model_sha256"]
            == baseline["provenance"]["source_model_sha256"]
        )
    assert len({cell["cell_sha256"] for cell in cells.values()}) == 3


def test_cell_rebuild_preserves_db_and_source_identity(cells):
    paths = cells[1.0]["provenance"]["source_files"]
    before = {
        key: hashlib.sha256(Path(path).read_bytes()).hexdigest() for key, path in paths.items()
    }
    assert build_cell(WORKSPACE) == cells[1.0]
    assert before == {
        key: hashlib.sha256(Path(path).read_bytes()).hexdigest() for key, path in paths.items()
    }
    assert not cells[1.0]["provenance"]["database_modified"]


def test_absolute_response_changes_with_cfa_but_has_no_second_area_or_qe(cells):
    responses = {scale: build_response(cell) for scale, cell in cells.items()}
    thin = interpolate_qe(responses[0.9], 0, 550)
    thick = interpolate_qe(responses[1.1], 0, 550)
    assert np.all(thin > thick)
    assert thick[0] / thick[1] < thin[0] / thin[1]
    baseline = responses[1.0]
    assert np.asarray(baseline["eqe"]) == pytest.approx(
        np.asarray(baseline["layer_absorption"]["silicon"]) * 0.75
    )
    assert "absolute replacement" in baseline["provenance"]["qe_application"]
    mutated = copy.deepcopy(cells[1.0])
    mutated["pixel_pitch_um"] = 3.45
    with pytest.raises(ValueError, match="hash mismatch"):
        build_response(mutated)


def _uniform(values=(100, 200, 220, 80), shape=(8, 10), origin=(0, 0)):
    row, col = np.indices(shape)
    return np.asarray(values, float)[((row + origin[0]) % 2) * 2 + ((col + origin[1]) % 2)]


def test_lsc_retains_cfa_signal_differences_caps_and_never_refits_to_candidate():
    center = _uniform()
    field = _uniform((50, 100, 110, 8))
    reference = build_lsc({"center": center, "edge": field}, provenance={"baseline": "frozen-v1"})
    assert np.asarray(reference["gain_maps"]["center"]) == pytest.approx(np.ones_like(center))
    gains = np.asarray(reference["gain_maps"]["edge"])
    assert gains == pytest.approx(_uniform((2, 2, 2, 4)))
    assert reference["field_statistics"]["edge"]["capped_fraction"] == 0.25
    assert reference["center_site_mean_rate"] == {"R": 100, "Gr": 200, "Gb": 220, "B": 80}
    assert reference["policy"]["candidate_refit"] is False
    assert reference["provenance"]["baseline"] == "frozen-v1"
    # Improved candidate keeps the original baseline correction, including its
    # overcorrection. That behavior must not be hidden by re-fitting its map.
    candidate = field * 1.5 + 64
    assert apply_lsc(candidate, reference, "edge", black_level_dn=64) == pytest.approx(
        field * 1.5 * gains
    )


def test_lsc_is_signed_preserves_raw_and_propagates_noise_gain():
    center = _uniform()
    reference = build_lsc({"center": center, "edge": center / 2})
    raw = np.array([[60, 70], [80, 64]], np.uint16)
    small = build_lsc({"center": center[:2, :2], "edge": center[:2, :2] / 2})
    original = raw.copy()
    corrected = apply_lsc(raw, small, "edge", black_level_dn=64)
    assert np.array_equal(raw, original)
    assert corrected == pytest.approx(np.array([[-8, 12], [32, 0]]))
    rng = np.random.default_rng(120)
    independent_noise = rng.normal(size=(200, *center.shape))
    gains = lsc_gain_map(reference, "edge")
    assert np.var(independent_noise * gains, axis=0) == pytest.approx(
        np.var(independent_noise, axis=0) * gains**2
    )


def test_lsc_explicit_native_origin_and_hash_prevent_misapplication():
    origin = (3, -2)
    center = _uniform()
    shifted = _uniform((50, 100, 110, 40), origin=origin)
    reference = build_lsc({"center": center, "edge": shifted}, cfa_origins={"edge": origin})
    assert apply_lsc(
        shifted + 64, reference, "edge", black_level_dn=64, crop_origin=origin
    ) == pytest.approx(_uniform(origin=origin))
    with pytest.raises(ValueError, match="origin"):
        apply_lsc(shifted, reference, "edge", black_level_dn=0)
    with pytest.raises(ValueError, match="shape"):
        apply_lsc(shifted[:4], reference, "edge", black_level_dn=0, crop_origin=origin)
    altered = copy.deepcopy(reference)
    altered["gain_maps"]["edge"][0][0] = 1
    with pytest.raises(ValueError, match="hash mismatch"):
        apply_lsc(shifted, altered, "edge", black_level_dn=0, crop_origin=origin)


def test_lsc_invalid_calibration_pixels_are_explicit_and_bad_inputs_rejected():
    center = _uniform()
    edge = center.copy()
    edge[0, 0] = 0
    reference = build_lsc({"center": center, "edge": edge})
    assert reference["valid_masks"]["edge"][0][0] is False
    assert reference["gain_maps"]["edge"][0][0] == 1
    for bad in ({"edge": center}, {"center": np.zeros_like(center)}, {"center": center * np.nan}):
        with pytest.raises(ValueError):
            build_lsc(bad)
    with pytest.raises(ValueError, match="maximum gain"):
        build_lsc({"center": center}, max_gain=5)
    with pytest.raises(ValueError, match="integer"):
        build_lsc({"center": center}, cfa_origins={"center": (0.5, 0)})
