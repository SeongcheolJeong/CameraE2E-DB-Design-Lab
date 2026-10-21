"""Field PSF sensitivity must not masquerade as measured lens radiometry."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from camera_platform.lens_catalog_bridge import geometric_lens_preview, lens_revision_catalog
from camera_platform.lens_sensor_raw_bridge import preview_lens_sensor_raw
from camera_platform.sensor_db_bridge import sensor_revision_catalog

WORKSPACE = Path(__file__).resolve().parents[2]


def _saved_pair() -> tuple[str, str]:
    root = WORKSPACE / "camerae2e_db"
    if not (root / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    ready = [
        row for row in lens_revision_catalog(catalog_root=root)["revisions"]
        if row["preview_status"] == "ready"
    ]
    sensors = [
        row for row in sensor_revision_catalog(root=root)
        if row["report_code"] == "DEP-2209-801" and row["model"] == "db_estimate_v1"
    ]
    if not ready or not sensors:
        pytest.skip("A pinned geometric lens and saved DB-estimate sensor are required")
    # Use an actual field-varying asset rather than assuming catalog order.
    lens = max(
        ready,
        key=lambda row: abs(
            geometric_lens_preview(row["revision_id"], catalog_root=root)["fields"][0][
                "rms_radius_um"
            ]
            - geometric_lens_preview(row["revision_id"], catalog_root=root)["fields"][1][
                "rms_radius_um"
            ]
        ),
    )
    return lens["revision_id"], sensors[0]["run_id"]


def _capture(lens_id: str, run_id: str, scene_kind: str, index: int) -> dict:
    return preview_lens_sensor_raw(
        lens_id,
        "DEP-2209-801",
        sensor_run_id=run_id,
        root=WORKSPACE / "camerae2e_db",
        workspace=WORKSPACE,
        scene_kind=scene_kind,
        sensor_size=(8, 8),
        noise_flag=0,
        seed=7,
        field_psf_index=index,
        accept_approximate_lens_radiometry=True,
    )


def test_stored_field_psf_changes_pattern_raw_without_inventing_vignetting():
    lens_id, run_id = _saved_pair()
    center_uniform = _capture(lens_id, run_id, "uniform", 0)
    edge_uniform = _capture(lens_id, run_id, "uniform", 4)
    assert center_uniform["lens_identity"] == edge_uniform["lens_identity"]
    assert center_uniform["sensor_revision"] == edge_uniform["sensor_revision"]
    assert center_uniform["mean_electrons"]["mean"] == pytest.approx(
        edge_uniform["mean_electrons"]["mean"], rel=1e-10
    )
    assert center_uniform["raw"]["values"] == edge_uniform["raw"]["values"]

    center_edge = _capture(lens_id, run_id, "slanted_edge", 0)
    edge_edge = _capture(lens_id, run_id, "slanted_edge", 4)
    center_raw = np.asarray(center_edge["raw"]["values"], dtype=np.int32)
    edge_raw = np.asarray(edge_edge["raw"]["values"], dtype=np.int32)
    assert center_raw.shape == edge_raw.shape == (8, 8)
    assert not np.array_equal(center_raw, edge_raw)
    assert center_edge["scope"] == edge_edge["scope"] == (
        "approximate_geometric_local_field_psf_raw_preview"
    )
    selection = edge_edge["lens_model"]["field_psf_selection"]
    assert selection["stored_field_index"] == 4
    assert selection["ideal_image_height_mm"] > 0
    assert selection["distortion_at_selected_field"] == "not_applied"
    assert selection["relative_illumination_at_selected_field"] == (
        "unknown_unity_assumption"
    )
    assert edge_edge["lens_model"]["absolute_radiometry"].startswith("unknown_")
    assert "not a calibrated RAW prediction" in edge_edge["limitation"]


def test_field_selection_requires_exact_stored_index_and_explicit_azimuth():
    with pytest.raises(ValueError, match="field_psf_index"):
        preview_lens_sensor_raw(
            "lens", "sensor", sensor_run_id="run", root=WORKSPACE,
            workspace=WORKSPACE, field_psf_index=True,
            accept_approximate_lens_radiometry=True,
        )
    with pytest.raises(ValueError, match="field_azimuth_deg requires"):
        preview_lens_sensor_raw(
            "lens", "sensor", sensor_run_id="run", root=WORKSPACE,
            workspace=WORKSPACE, field_azimuth_deg=90.0,
            accept_approximate_lens_radiometry=True,
        )
    lens_id, run_id = _saved_pair()
    with pytest.raises(ValueError, match="stored lens PSF field samples"):
        _capture(lens_id, run_id, "uniform", 999)
