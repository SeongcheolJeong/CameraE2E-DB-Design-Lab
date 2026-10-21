import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from pyisetcam.component_catalog import component_catalog_validate
from pyisetcam.lens_sources import example_key, import_lens_sources, publication_key

ZMX = """VERS 190513 19 0
MODE SEQ
NAME Test singlet
UNIT MM
ENPD 5
WAVM 1 0.5875618 1
SURF 0
 TYPE STANDARD
 CURV 0
 DISZ INFINITY
SURF 1
 TYPE STANDARD
 STOP
 CURV 0.02
 DISZ 3
 GLAS ___BLANK 1 0 1.5168 64.17
 DIAM 2.5
SURF 2
 TYPE STANDARD
 CURV -0.02
 DISZ 48
 DIAM 2.5
SURF 3
 TYPE STANDARD
 CURV 0
 DISZ 0
 DIAM 5
"""


def _manifest(tmp_path, name="a", text=ZMX):
    source = tmp_path / name
    source.mkdir()
    path = source / "lens.zmx"
    path.write_text(text)
    manifest = source / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "source": name,
                "files": [
                    {
                        "path": path.name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "source_url": "https://example.test/" + name + "/lens.zmx",
                        "format": "zmx",
                        "title": "Example lens",
                        "publication_number": "US001234567A1",
                        "example_label": "Example01P",
                        "license": {"status": "unknown"},
                    }
                ],
            }
        )
    )
    return manifest


def test_publication_matching_does_not_guess_families():
    assert publication_key("US004995709") == publication_key("US4995709A")
    assert publication_key("JP2024-062005A") == "JP2024062005"
    assert publication_key("US20180321574A1") != publication_key("US10310255B2")
    assert publication_key("not a publication") is None
    assert example_key("Numerical Embodiment 02") == example_key("Example02P") == "2"
    assert example_key("") is None


def test_idempotent_and_duplicate_source_preserves_both_provenances(tmp_path):
    a = _manifest(tmp_path)
    b = _manifest(tmp_path, "b")
    root = tmp_path / "catalog"
    first = import_lens_sources([a, b], root=root)
    assert first["files_by_status"] == {"normalized": 2}
    assert first["added"]["components"] == 1
    assert len(first["exact_prescription_groups"]) == 1
    again = import_lens_sources([a, b], root=root)
    assert not any(again["added"].values())
    assert component_catalog_validate(root=root, verify_files=True)["status"] == "passed"
    with sqlite3.connect(root / "catalog.sqlite") as con:
        assert con.execute("SELECT count(*) FROM sources").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM simulation_runs").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM component_preferences").fetchone()[0] == 0
        assert (
            con.execute(
                "SELECT status FROM validations WHERE gate='scientific_accuracy'"
            ).fetchone()[0]
            == "unknown"
        )
        aperture, metadata = con.execute(
            "SELECT aperture_mm,metadata_json FROM lens_surfaces WHERE surface_order=2"
        ).fetchone()
        assert aperture == 5
        assert json.loads(metadata)["aperture_convention"] == "diameter"


def test_aperture_state_is_not_deduplicated_and_legacy_is_read_only(tmp_path):
    a = _manifest(tmp_path)
    b = _manifest(tmp_path, "b", ZMX.replace("ENPD 5", "ENPD 4"))
    legacy = tmp_path / "legacy.sqlite"
    with sqlite3.connect(legacy) as con:
        con.execute("CREATE TABLE lenses(lens_id, publication_number, example_label, company)")
        con.execute(
            "INSERT INTO lenses VALUES ('p1','US1234567B2','Numerical Embodiment 1','Test')"
        )
    before = legacy.read_bytes()
    result = import_lens_sources([a, b], root=tmp_path / "catalog", legacy_db=legacy)
    assert result["added"]["components"] == 2
    assert result["legacy_example_overlap_files"] == 2
    assert legacy.read_bytes() == before
    assert result["legacy_unchanged"]


def test_hash_mismatch_rolls_back_entire_batch(tmp_path):
    a = _manifest(tmp_path)
    b = _manifest(tmp_path, "b")
    (b.parent / "lens.zmx").write_text(ZMX + "corrupted")
    root = tmp_path / "catalog"
    with pytest.raises(ValueError, match="hash"):
        import_lens_sources([a, b], root=root)
    with sqlite3.connect(root / "catalog.sqlite") as con:
        assert con.execute("SELECT count(*) FROM components").fetchone()[0] == 0


def test_path_escape_rejected(tmp_path):
    a = _manifest(tmp_path)
    payload = json.loads(a.read_text())
    payload["files"][0]["path"] = "../outside.zmx"
    a.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="inside"):
        import_lens_sources([a], root=tmp_path / "catalog")


def test_unsupported_format_is_archived_without_creating_lens(tmp_path):
    root = tmp_path / "catalog"
    root.mkdir()
    manifest = _manifest(root, text="Unsupported model contents")
    payload = json.loads(manifest.read_text())
    payload["files"][0]["format"] = "roa"
    manifest.write_text(json.dumps(payload))
    result = import_lens_sources([manifest], root=root)
    assert result["added"]["components"] == 0
    assert result["added"]["artifacts"] == 1
    assert result["records"][0]["status"] == "blocked"
    assert result["records"][0]["source_artifact_id"]
    again = import_lens_sources([manifest], root=root)
    assert not any(again["added"].values())
    (manifest.parent / "lens.zmx").write_text("later replacement")
    assert component_catalog_validate(root=root, verify_files=True)["status"] == "passed"


def test_changed_source_creates_revision_under_stable_component(tmp_path):
    root = tmp_path / "catalog"
    root.mkdir()
    manifest = _manifest(root)
    first = import_lens_sources([manifest], root=root)
    raw = manifest.parent / "lens.zmx"
    raw.write_text(ZMX.replace("DISZ 3", "DISZ 4"))
    payload = json.loads(manifest.read_text())
    payload["files"][0]["sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(payload))
    second = import_lens_sources([manifest], root=root)
    assert second["added"]["components"] == 0
    assert second["added"]["model_revisions"] == 1
    assert first["records"][0]["component_id"] == second["records"][0]["component_id"]
    assert first["records"][0]["revision_id"] != second["records"][0]["revision_id"]
    assert component_catalog_validate(root=root, verify_files=True)["status"] == "passed"


def test_p2p_configurations_are_distinct_revisions_of_one_design(tmp_path):
    text = """[descriptive data]
title\tExample
[constants]
[variable distances]
Focal Length\t50\t49
F-Number\t2\t2.1
d0\tInfinity\t1000
Bf\t48\t49
[lens data]
1\t50\t3\t1.5168\t10\t64.17
2\tAS\t0\t\t10\t
3\t-50\tBf\t\t10\t
"""
    manifest = _manifest(tmp_path, text=text)
    payload = json.loads(manifest.read_text())
    payload["files"][0]["format"] = "p2p"
    manifest.write_text(json.dumps(payload))
    root = tmp_path / "catalog"
    result = import_lens_sources([manifest], root=root)
    assert result["files_by_status"] == {"normalized": 1}
    assert result["configurations_by_status"] == {"normalized": 2}
    assert result["added"]["components"] == 1
    assert result["added"]["model_revisions"] == 2
    assert result["same_publication_example_candidates"] == []


def test_optional_exact_glass_lookup_never_uses_glass_placeholders():
    pytest.importorskip("opticalglass")
    from pyisetcam.lens_source_glass import resolve_catalog_glasses
    from pyisetcam.lens_source_parser import parse_lens_text

    text = ZMX.replace("MODE SEQ", "MODE SEQ\nGCAT SCHOTT")
    text = text.replace("___BLANK 1 0 1.5168 64.17", "N-BK7 0 0 1.5 40")
    parsed = parse_lens_text(text)
    resolved = resolve_catalog_glasses(parsed)
    assert parsed["surfaces"][1]["nd"] is None
    assert resolved["surfaces"][1]["nd"] == pytest.approx(1.5168, abs=1e-5)
    assert resolved["surfaces"][1]["vd"] > 60
    missing = resolve_catalog_glasses(parse_lens_text(text.replace("N-BK7", "NOT-A-GLASS")))
    assert missing["surfaces"][1]["nd"] is None


def test_corrected_configuration_aperture_flags_prior_revision(tmp_path, monkeypatch):
    from pyisetcam import lens_source_parser

    text = """[variable distances]
Focal Length\t50
F-Number\t2
d0\tInfinity
Bf\t48
Aperture Diameter\t6
[lens data]
1\t50\t3\t1.5168\t4\t64.17
2\tAS\t0\t\t4
3\t-50\tBf\t\t4
"""
    manifest = _manifest(tmp_path, text=text)
    payload = json.loads(manifest.read_text())
    payload["files"][0]["format"] = "p2p"
    manifest.write_text(json.dumps(payload))
    current_parser = lens_source_parser.parse_lens_configurations

    def old_parser(*args, **kwargs):
        variants = current_parser(*args, **kwargs)
        for variant in variants:
            row = variant["surfaces"][2]
            row.update(effective_aperture_mm=4.0, semidiameter_mm=2.0)
            row.pop("aperture_basis", None)
        return variants

    root = tmp_path / "catalog"
    monkeypatch.setattr(lens_source_parser, "parse_lens_configurations", old_parser)
    before = import_lens_sources([manifest], root=root)
    monkeypatch.setattr(lens_source_parser, "parse_lens_configurations", current_parser)
    after = import_lens_sources([manifest], root=root)
    assert after["added"]["components"] == 0
    assert after["added"]["model_revisions"] == 1
    assert after["records"][0]["corrects_prior_revisions"] == [before["records"][0]["revision_id"]]
    with sqlite3.connect(root / "catalog.sqlite") as con:
        assert (
            con.execute(
                "SELECT status FROM validations WHERE gate='source_aperture_consistency'"
            ).fetchone()[0]
            == "failed"
        )
    again = import_lens_sources([manifest], root=root)
    assert not any(again["added"].values())


def test_diagnostic_report_uses_nested_ray_status_and_is_bound_to_parser(tmp_path):
    from pyisetcam import lens_source_parser
    from pyisetcam._catalog_store import file_hash
    from pyisetcam.lens_sources import register_lens_source_trace_report

    manifest = _manifest(tmp_path)
    root = tmp_path / "catalog"
    result = import_lens_sources([manifest], root=root)
    report = {
        "schema": "camerae2e_public_lens_raytrace_smoke_v1",
        "parser_sha256": file_hash(Path(lens_source_parser.__file__)),
        "tool_sha256": "test-only",
        "scope": "test diagnostic only",
        "rayoptics_version": "test-only",
        "settings": {"wavelength_nm": 587.5618},
        "cases": [
            {
                "sha256": result["records"][0]["sha256"],
                "configuration_index": None,
                "normalized_raytrace": {"status": "passed"},
            }
        ],
    }
    report_path = tmp_path / "raytrace.json"
    report_path.write_text(json.dumps(report))
    added = register_lens_source_trace_report(report_path, result, root=root)
    assert added["counts"] == {"passed": 1}
    result["records"][0]["format"] = "deeplens_json"
    with pytest.raises(ValueError, match="Matching JSON source"):
        register_lens_source_trace_report(report_path, result, root=root)
    result["records"][0]["format"] = "zmx"
    report["parser_sha256"] = "mismatched"
    report_path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="current source parser"):
        register_lens_source_trace_report(report_path, result, root=root)
    assert component_catalog_validate(root=root, verify_files=True)["status"] == "passed"
