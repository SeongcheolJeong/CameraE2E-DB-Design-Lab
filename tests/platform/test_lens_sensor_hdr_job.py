"""Pinned joint HDR job retains original RAW and source-bound evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from camera_platform import lens_sensor_hdr_job as job
from camera_platform.lens_catalog_bridge import lens_revision_catalog
from camera_platform.sensor_db_bridge import sensor_revision_catalog
from camera_platform.service import PlatformService

WORKSPACE = Path(__file__).resolve().parents[2]
ROOT = WORKSPACE / "camerae2e_db"


def _spec(report_code: str) -> dict:
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
        pytest.skip("Pinned Lens DB or Sensor DB run is unavailable")
    spec = job.default_spec()
    spec.update(
        lens_revision_id=lenses[0]["revision_id"],
        sensor_revision={"identifier": report_code, "run_id": sensors[0]["run_id"]},
        sensor_size=[8, 8],
        accept_approximate_lens_radiometry=True,
    )
    return spec


def test_joint_hdr_job_spec_and_quad_bayer_preflight(tmp_path):
    spec = job.default_spec()
    spec.update(
        lens_revision_id="lens-id",
        sensor_revision={"identifier": "sensor-id", "run_id": "run-id"},
    )
    with pytest.raises(ValueError, match="acceptance"):
        job.specification(spec)
    spec["accept_approximate_lens_radiometry"] = True
    with pytest.raises(ValueError, match="exactly two exposures"):
        job.specification(dict(spec, exposure_s=[0.001, 0.002, 0.004]))
    with pytest.raises(ValueError, match="even integer dimensions"):
        job.specification(dict(spec, sensor_size=[9, 8]))
    service = PlatformService(tmp_path / "state", WORKSPACE)
    with pytest.raises(ValueError, match="Quad Bayer"):
        job.prepare(service, _spec("DEP-2209-801"))


def test_joint_hdr_real_8x8_job_and_retained_artifacts(tmp_path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    spec = _spec("DEF-2512-801")
    queued = job.submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    result = job.execute(service, queued)
    service.registry.finish(queued["id"], result=result, error=None)
    finished = job.get(service, queued["id"])
    assert finished["status"] == "succeeded", finished.get("error")
    summary = finished["result"]["summary"]
    report = summary["report"]
    assert summary["source_identities"]["lens"] == report["lens_identity"]
    assert summary["source_identities"]["sensor"] == report["sensor_revision"]
    assert summary["saved_readout"] == report["controls"]["readout"]
    assert report["controls"]["fixed_pattern_mode"] == "zero_fpn_override"
    assert report["controls"]["joint_lfm_readout_comparability"].startswith("different_")
    assert report["geometry_provenance"]["merger_geometry_scope"] == (
        "virtual_configured_sensor_only"
    )
    assert report["geometry_provenance"]["saved_native_array_crop_origin"] is None
    assert report["fusion"]["valid_pixels"] > 0
    assert report["verification"]["physical_validation"] == "not_established"
    with np.load(
        service.registry.blob_path(summary["evidence"]["npz"]["sha256"]),
        allow_pickle=False,
    ) as arrays:
        raw = arrays["original_raw_dn"]
        assert raw.shape == (2, 8, 8)
        assert raw.dtype == np.dtype("int32")
        assert arrays["hdr_merged_rate"].shape == (8, 8)
    assert hashlib.sha256(raw.tobytes(order="C")).hexdigest() == (
        report["fusion"]["input_array_bytes_sha256"]
    )
