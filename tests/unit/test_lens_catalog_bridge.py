"""Existing DB/PSF integration checks for the geometric-only lens bridge."""

from pathlib import Path

import pytest

from camera_platform import lens_catalog_bridge as bridge

ROOT = Path(__file__).resolve().parents[2] / "camerae2e_db"
CATALOG = ROOT / "catalog.sqlite"
PSF = (
    ROOT
    / "lens_db"
    / "CameraE2E_Lens_DB_v9_20260627"
    / "data"
    / "lens_patents"
    / "raytrace_psf_v2"
    / "p0014_intermediate.npz"
)
pytestmark = pytest.mark.skipif(
    not CATALOG.is_file() or not PSF.is_file(),
    reason="Local imported lens catalog and v2 PSF unavailable",
)

P0014_MID = "6c48e967-74b4-5901-8186-ba6fafe4797b"
P0019_MID = "fd959d9d-8503-5fe4-a582-4cc9074e6bfd"
P0014_WIDE = "a23607b1-3e9e-5bcd-9f4c-490c6443a2b2"


def test_two_pinned_db_revisions_yield_distinct_geometric_spot_evidence():
    result = bridge.compare_geometric_lenses([P0014_MID, P0019_MID], catalog_root=ROOT)
    first, second = result["lenses"]
    assert result["comparison_scope"].endswith("no_ranking")
    assert [item["identity"]["simulation_id"] for item in (first, second)] == [
        "p0014:intermediate",
        "p0019:intermediate",
    ]
    assert all(
        item["identity"]["asset_sha256"]
        and item["identity"]["source_db_sha256"]
        and item["identity"]["revision_sha256"]
        for item in (first, second)
    )
    assert first["model"]["response_kind"] == "geometric_ray_histogram_psf"
    assert first["model"]["radiometry_status"] == "unknown"
    assert first["model"]["sensor_sampling_status"] == "unvalidated"
    assert first["model"]["object_distance_m"] == second["model"]["object_distance_m"]
    assert first["fields"][0]["ideal_image_height_mm"] == 0
    assert (
        first["fields"][1]["ideal_image_height_mm"] != second["fields"][1]["ideal_image_height_mm"]
    )
    assert first["fields"][0]["ee80_radius_um"] != second["fields"][0]["ee80_radius_um"]
    assert all(item["r1_raw_eligibility"]["status"] == "blocked" for item in (first, second))
    assert all("request" not in item and "lens" not in item for item in (first, second))


def test_missing_v2_asset_and_unstored_wavelength_fail_closed():
    with pytest.raises(bridge.LensCatalogBridgeError) as missing:
        bridge.geometric_lens_preview(P0014_WIDE, catalog_root=ROOT)
    assert missing.value.code == "validated_psf_missing"
    with pytest.raises(bridge.LensCatalogBridgeError) as wave:
        bridge.geometric_lens_preview(P0014_MID, catalog_root=ROOT, wavelength_nm=575)
    assert wave.value.code == "wavelength_outside_asset"


def test_changed_source_and_psf_hashes_are_rejected(monkeypatch):
    original_hash = bridge.file_hash

    def changed_db(path):
        return "0" * 64 if Path(path).suffix == ".sqlite" else original_hash(path)

    monkeypatch.setattr(bridge, "file_hash", changed_db)
    with pytest.raises(bridge.LensCatalogBridgeError) as source:
        bridge.geometric_lens_preview(P0014_MID, catalog_root=ROOT)
    assert source.value.code == "source_hash_mismatch"

    def changed_psf(path):
        return "0" * 64 if Path(path).suffix == ".npz" else original_hash(path)

    monkeypatch.setattr(bridge, "file_hash", changed_psf)
    with pytest.raises(bridge.LensCatalogBridgeError) as asset:
        bridge.geometric_lens_preview(P0014_MID, catalog_root=ROOT)
    assert asset.value.code == "asset_hash_mismatch"


def test_revision_catalog_finds_four_v2_candidates_and_explains_blocked_rows():
    catalog = bridge.lens_revision_catalog(catalog_root=ROOT)
    ready = [row for row in catalog["revisions"] if row["preview_status"] == "ready"]
    assert catalog["preview_ready_count"] == len(ready) == 4
    assert {row["revision_id"] for row in ready} >= {P0014_MID, P0019_MID}
    assert all(row["reason_code"] is None and row["r1_raw_status"] == "blocked" for row in ready)
    filtered = bridge.lens_revision_catalog(catalog_root=ROOT, query="p0014")
    assert {row["simulation_id"] for row in filtered["revisions"]} == {
        "p0014:intermediate",
        "p0014:wide-angle",
        "p0014:telephoto",
    }
    wide = next(row for row in filtered["revisions"] if row["revision_id"] == P0014_WIDE)
    assert wide["reason_code"] == "validated_psf_missing"
    public = next(
        row for row in catalog["revisions"] if row["model_kind"] == "public_optical_prescription"
    )
    assert public["preview_status"] == "blocked"
    assert public["reason_code"] == "unsupported_response_contract"
