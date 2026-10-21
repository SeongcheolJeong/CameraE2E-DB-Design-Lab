"""One pinned Lens DB × Sensor DB static RAW bracket, with honest CFA limits."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform import lens_sensor_hdr_bridge as bridge
from camera_platform.lens_catalog_bridge import lens_revision_catalog
from camera_platform.sensor_db_bridge import sensor_revision_catalog

WORKSPACE = Path(__file__).resolve().parents[2]
ROOT = WORKSPACE / "camerae2e_db"


def _pair(report_code: str) -> tuple[str, str]:
    if not (ROOT / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    lenses = [
        row for row in lens_revision_catalog(catalog_root=ROOT)["revisions"]
        if row["preview_status"] == "ready"
    ]
    sensors = [
        row for row in sensor_revision_catalog(root=ROOT)
        if row["report_code"] == report_code and row["model"] == "db_estimate_v1"
    ]
    if not lenses or not sensors:
        pytest.skip(f"Pinned geometric lens and {report_code} sensor are required")
    return lenses[0]["revision_id"], sensors[0]["run_id"]


def test_static_joint_hdr_preserves_original_native_raw_and_pinned_sources(monkeypatch):
    lens_id, sensor_run_id = _pair("DEF-2512-801")
    prepared: dict = {}
    original_prepare = bridge._prepare_lens_sensor_capture

    def record_prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        prepared.update(result)
        return result

    monkeypatch.setattr(bridge, "_prepare_lens_sensor_capture", record_prepare)
    result = bridge.evaluate_lens_sensor_hdr_bracket(
        lens_id, "DEF-2512-801", sensor_run_id=sensor_run_id,
        root=ROOT, workspace=WORKSPACE, exposure_s=(0.0005, 0.008),
        sensor_size=(8, 8), seed=17,
        accept_approximate_lens_radiometry=True,
    )
    report, arrays = result["report"], result["arrays"]
    raw = arrays["original_raw_dn"]
    assert report["schema"] == bridge.SCHEMA
    assert report["status"] == "completed"
    assert raw.shape == (2, 8, 8)
    assert raw.dtype == np.dtype("int32")
    raw_sha = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    assert raw_sha == report["arrays"]["original_raw_dn"]["sha256_c_order_bytes"]
    assert raw_sha == report["fusion"]["input_array_bytes_sha256"]
    assert report["fusion"]["valid_pixels"] > 0
    assert np.isfinite(arrays["hdr_merged_rate"][arrays["hdr_valid_mask"]]).all()
    assert report["sensor_revision"]["run_id"] == sensor_run_id
    assert report["lens_identity"] == prepared["lens_identity"]
    assert report["sensor_revision"] == prepared["sensor_revision"]
    provenance = report["geometry_provenance"]
    assert provenance["merger_geometry_scope"] == "virtual_configured_sensor_only"
    assert provenance["merger_configured_sensor_geometry"] == report["fusion"]["geometry"]
    assert provenance["configured_sensor_size"] == [8, 8]
    assert provenance["saved_native_array_size"] == (
        prepared["saved_metadata"]["native_cfa"]["native_size"]
    )
    assert provenance["saved_native_array_crop_origin"] is None
    assert provenance["saved_native_array_crop_mapping"] == "not_established"
    assert report["controls"]["readout"] == prepared["saved_readout"]
    assert report["controls"]["photoelectron_rate_linearity_relative_error"] < 1e-8
    assert report["controls"]["saved_fixed_pattern"] == prepared["saved_fixed_pattern"]
    assert set(report["controls"]["executed_fixed_pattern"].values()) == {0.0}
    assert report["captures"][0]["noise_seed"] != report["captures"][1]["noise_seed"]
    assert [item["sensor_capture"]["exposure_time_s"] for item in report["captures"]] == [
        0.0005, 0.008,
    ]
    assert all(
        item["sensor_capture"]["readout"] == prepared["saved_readout"]
        for item in report["captures"]
    )
    np.testing.assert_allclose(
        prepared["mean_electrons"],
        arrays["model_photoelectron_rate"] * 0.0005,
    )
    assert prepared["sensor"].fields["sensor_readout"] == prepared["saved_readout"]
    assert prepared["optical_image"].fields["optics"]["raytrace"]["asset_sha256"] == (
        prepared["lens_identity"]["asset_sha256"]
    )
    assert report["source_boundary"]["lens_absolute_radiometry"].startswith("unknown_")
    assert report["source_boundary"]["lens_cra_to_sensor"] == "not_connected"
    assert report["verification"]["physical_validation"] == "not_established"
    assert report["verification"]["lfm_evaluated"] is False
    json.dumps(report, allow_nan=False)


def test_quad_bayer_saved_sensor_is_rejected_before_optics(monkeypatch):
    lens_id, sensor_run_id = _pair("DEP-2209-801")

    def no_optics(*_args, **_kwargs):
        raise AssertionError("optics should not load before native CFA preflight")

    monkeypatch.setattr(bridge, "geometric_lens_preview", no_optics)
    with pytest.raises(ValueError, match="native 2x2 RGGB.*Quad Bayer"):
        bridge.evaluate_lens_sensor_hdr_bracket(
            lens_id, "DEP-2209-801", sensor_run_id=sensor_run_id,
            root=ROOT, workspace=WORKSPACE, sensor_size=(8, 8),
            accept_approximate_lens_radiometry=True,
        )


def test_joint_hdr_requires_explicit_approximation_and_bounded_inputs():
    kwargs = {
        "sensor_run_id": "run",
        "root": ROOT,
        "workspace": WORKSPACE,
    }
    with pytest.raises(ValueError, match="exactly two exposures"):
        bridge.evaluate_lens_sensor_hdr_bracket(
            "lens", "sensor", exposure_s=(0.001, 0.002, 0.004), **kwargs
        )
    with pytest.raises(ValueError, match="Explicit acceptance"):
        bridge.evaluate_lens_sensor_hdr_bracket(
            "lens", "sensor", **kwargs
        )
    with pytest.raises(ValueError, match="even integer dimensions"):
        bridge.evaluate_lens_sensor_hdr_bracket(
            "lens", "sensor", sensor_size=(9, 8),
            accept_approximate_lens_radiometry=True, **kwargs
        )
