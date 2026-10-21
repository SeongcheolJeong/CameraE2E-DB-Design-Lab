from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load(
    Path(__file__).resolve().parents[2] / "tools/validate_lens_raytrace_assets.py",
    "lens_raytrace_asset_audit",
)
fixture_module = _load(
    Path(__file__).with_name("test_lens_asset_contract.py"), "lens_asset_contract_fixtures"
)


@pytest.fixture
def assets(tmp_path):
    # Reuse the four-surface synthetic prescription and 4x4 PSF fixture.
    db, root, arrays = fixture_module.checked_asset.__wrapped__(tmp_path)
    modern = root / "raytrace_psf_v2"
    modern.mkdir()
    shutil.move(root / "demo.npz", modern / "demo.npz")
    shutil.move(root / "manifest.json", modern / "manifest.json")
    for directory in ("raytrace_psf", "raytrace_psf_highres"):
        folder = root / directory
        folder.mkdir()
        legacy = dict(arrays)
        metadata = json.loads(str(legacy["optics_json"]))
        metadata["raytrace"].pop("contract_version")
        legacy["optics_json"] = np.array(json.dumps(metadata))
        np.savez_compressed(folder / "demo.npz", **legacy)
        (folder / "manifest.json").write_text(
            json.dumps(
                {
                    "rows": [
                        {
                            "simulation_id": "demo:base",
                            "status": "generated",
                            "file": "demo.npz",
                        }
                    ]
                }
            )
        )
    with sqlite3.connect(db) as con:
        con.executescript(
            "CREATE TABLE camerae2e_quality(simulation_id TEXT PRIMARY KEY,"
            "recommended_for_camerae2e INTEGER,use_tier TEXT,psf_debug_status TEXT,"
            "psf_highres_status TEXT);"
            "INSERT INTO camerae2e_quality "
            "VALUES('demo:base',1,'highres_psf','generated','generated');"
            "INSERT INTO camerae2e_quality "
            "VALUES('stale:base',1,'debug_psf','generated','missing');"
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
            "INSERT INTO metadata VALUES('untouched','original');"
        )
    return db, root, arrays


def test_readonly_audit_checks_every_asset_and_never_approves_legacy(assets, tmp_path):
    db, root, _ = assets
    before = db.read_bytes()
    output = tmp_path / "report.json"
    report = audit.audit_database(db, output)
    assert db.read_bytes() == before
    assert report["summary"]["asset_rows"] == 3
    assert report["summary"]["valid_contract_assets"] == 1
    assert report["summary"]["reason_counts"]["legacy_contract"] == 2
    assert report["image_quality_validated"] is False
    assert report["asset_root"] == str(root)
    assert json.loads(output.read_text())["summary"] == report["summary"]
    current = next(row for row in report["results"] if row["valid_contract"])
    assert current["radiometry_status"] == "unknown"
    assert current["sampling_status"] == "unvalidated"
    assert len(current["asset_sha256"]) == 64
    assert len(current["source_prescription_sha256"]) == 64
    assert len(current["contract_sha256"]) == 64


def test_write_resets_stale_recommendations_preserving_availability_tiers(assets):
    db, _, _ = assets
    report = audit.audit_database(db, write_db=True)
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT * FROM camerae2e_quality ORDER BY simulation_id").fetchall()
        assert rows == [
            ("demo:base", 1, "highres_psf", "generated", "generated"),
            ("stale:base", 0, "debug_psf", "generated", "missing"),
        ]
        assert con.execute("SELECT count(*) FROM raytrace_asset_validation").fetchone()[0] == 3
        assert (
            con.execute("SELECT sum(valid_contract) FROM raytrace_asset_validation").fetchone()[0]
            == 1
        )
        assert (
            con.execute("SELECT value FROM metadata WHERE key='recommended_scope'").fetchone()[0]
            == audit.RECOMMENDED_SCOPE
        )
        assert (
            con.execute("SELECT value FROM metadata WHERE key='untouched'").fetchone()[0]
            == "original"
        )
        stored = json.loads(
            con.execute(
                "SELECT result_json FROM raytrace_asset_validation WHERE valid_contract=1"
            ).fetchone()[0]
        )
        assert stored["image_quality_validated"] is False
    assert report["summary"]["recommendations_cleared"] == 1


@pytest.mark.parametrize("corruption", ["negative_psf", "archive", "source_changed"])
def test_corrupted_current_assets_never_recommend_a_lens(assets, corruption):
    db, root, arrays = assets
    if corruption == "negative_psf":
        arrays["psf_function"][0, 0, 0, 0] = -1.0
        np.savez_compressed(root / "raytrace_psf_v2/demo.npz", **arrays)
    elif corruption == "archive":
        (root / "raytrace_psf_v2/demo.npz").write_bytes(b"not a numpy archive")
    else:
        with sqlite3.connect(db) as con:
            con.execute("UPDATE lens_surfaces SET radius_mm=51 WHERE surface_order=2")
    report = audit.audit_database(db, write_db=True)
    assert report["summary"]["valid_contract_assets"] == 0
    assert report["summary"]["recommended_simulations"] == 0
    current = next(
        r for r in report["results"] if r["asset_relative_path"].startswith("raytrace_psf_v2/")
    )
    assert not current["valid_contract"]
    assert (
        current["reason_code"]
        == {
            "negative_psf": "invalid_contract",
            "archive": "corrupt_asset",
            "source_changed": "source_prescription_mismatch",
        }[corruption]
    )


def test_removed_current_manifest_cannot_leave_a_stale_valid_row(assets):
    db, root, _ = assets
    audit.audit_database(db, write_db=True)
    (root / "raytrace_psf_v2/manifest.json").unlink()
    second = audit.audit_database(db, write_db=True)
    assert second["summary"]["recommended_simulations"] == 0
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM raytrace_asset_validation").fetchone()[0] == 2
        assert (
            con.execute("SELECT sum(valid_contract) FROM raytrace_asset_validation").fetchone()[0]
            == 0
        )


def test_company_partition_uses_shared_parent_assets(assets):
    db, root, _ = assets
    company = root / "companies" / "example" / "company.sqlite"
    company.parent.mkdir(parents=True)
    shutil.copyfile(db, company)
    report = audit.audit_database(company)
    assert report["asset_root"] == str(root)
    assert report["summary"]["valid_contract_assets"] == 1
    assert all(not r["asset_relative_path"].startswith("../") for r in report["results"])


def test_auditor_invokes_fail_closed_loader_with_explicit_preview_radiometry(assets, monkeypatch):
    db, _, _ = assets
    original = audit.lens_patent_raytrace_optics
    calls = []

    def record(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(audit, "lens_patent_raytrace_optics", record)
    audit.audit_database(db)
    assert len(calls) == 3
    assert all("allow_legacy" not in call for call in calls)
    assert all(call["allow_approximate_radiometry"] is True for call in calls)


def test_duplicate_manifest_ids_cannot_validate_the_wrong_file(assets):
    db, root, _ = assets
    path = root / "raytrace_psf_v2/manifest.json"
    manifest = json.loads(path.read_text())
    manifest["rows"].append(
        {"simulation_id": "demo:base", "status": "generated", "file": "corrupt.npz"}
    )
    (path.parent / "corrupt.npz").write_bytes(b"invalid")
    path.write_text(json.dumps(manifest))
    report = audit.audit_database(db)
    assert report["summary"]["valid_contract_assets"] == 0
    assert report["summary"]["reason_counts"]["ambiguous_manifest_identity"] == 2


def test_cli_report_cannot_overwrite_database(assets):
    db, _, _ = assets
    before = db.read_bytes()
    with pytest.raises(SystemExit):
        audit.main(["--db", str(db), "--output", str(db), "--write-db"])
    assert db.read_bytes() == before
