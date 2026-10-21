"""Legacy temporal acquisitions must hash numeric subcells, even without source RI."""

from copy import deepcopy

import numpy as np
from test_dcg_source_position_ri import _case

from camera_platform.dcg_runtime import SharedChargeDCGRuntime
from camera_platform.temporal import _scene_hash


def test_legacy_json_subcell_content_identity_is_unchanged():
    scene = {
        "spectral_irradiance": [[[1.0, 2.0]]],
        "source_subcell_irradiance": [[[
            [[1.0, 2.0], [1.0, 2.0]],
            [[1.0, 2.0], [1.0, 2.0]],
        ]]],
        "source_subcell_evidence": {
            "model": "bilinear_receiver_integrated_impulse_v1", "grid_side": 2,
        },
    }
    image = np.array(scene["spectral_irradiance"], dtype=float)
    assert _scene_hash(scene, image) == (
        "768ea3dc1b434002a626028cfe9c3447ec5843b854ce22c3a6beb10c3710a71d"
    )


def test_legacy_dcg_accepts_unchanged_subcells_and_rejects_hidden_motion():
    request, image, _ = _case()
    del request["field_radiometry_mode"]
    subcells = np.repeat(np.repeat(image[:, :, None, None, :], 2, axis=2), 2, axis=3)
    for index, knot in enumerate(request["scene_sequence"]["knots"]):
        knot["scene"]["source_subcell_irradiance"] = subcells.copy()
        knot["scene"]["source_subcell_evidence"] = {
            "model": "bilinear_receiver_integrated_impulse_v1", "grid_side": 2,
            "radiance_sha256": "a" * 64,
            "source_render_id": f"authored-legacy-subcells-{index}",
            "projection_job_id": None,
        }
    runtime = SharedChargeDCGRuntime()
    result = runtime.execute(request)
    assert result["arrays"]["raw_groups"].shape == (2, 2, 8, 8)
    assert "field_radiometry_mode" not in result["summary"]

    moved = deepcopy(request)
    changed = moved["scene_sequence"]["knots"][1]["scene"]["source_subcell_irradiance"]
    changed[4, 4, 0, 0] += 1e-5
    changed[4, 4, 0, 1] -= 1e-5
    np.testing.assert_allclose(changed.mean(axis=(2, 3)), image, rtol=0, atol=1e-20)
    issues = runtime.validate_request(moved)
    assert len(issues) == 1
    assert "one static unique optical image" in issues[0]["message"]


def test_source_ri_dcg_static_gate_ignores_only_subcell_provenance_labels():
    request, image, _ = _case()
    subcells = np.repeat(np.repeat(image[:, :, None, None, :], 2, axis=2), 2, axis=3)
    for index, knot in enumerate(request["scene_sequence"]["knots"]):
        knot["scene"]["source_subcell_irradiance"] = subcells.copy()
        knot["scene"]["source_subcell_evidence"] = {
            "model": "bilinear_receiver_integrated_impulse_v1", "grid_side": 2,
            "radiance_sha256": "a" * 64,
            "source_render_id": f"independent-source-{index}",
            "projection_job_id": None,
        }
    runtime = SharedChargeDCGRuntime()
    assert runtime.estimate_resources(request)["unique_scenes"] == 2
    result = runtime.execute(request)
    assert result["arrays"]["raw_groups"].shape == (2, 2, 8, 8)
    assert result["summary"]["field_radiometry_mode"] == "source_position_ri_v1"
