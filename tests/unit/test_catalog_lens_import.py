import json
import shutil
import sqlite3

from pyisetcam._catalog_lens import import_lenses
from pyisetcam._catalog_store import CatalogWriter, connect, file_hash


def _legacy(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE metadata(key TEXT, value TEXT);
        CREATE TABLE lenses(lens_id TEXT, prescription_id INTEGER, company TEXT,
          publication_number TEXT, example_label TEXT, title TEXT, source_url TEXT);
        INSERT INTO lenses VALUES ('p0014',14,'Canon','US123','Example 1','Test lens','https://example.test/patent');
        CREATE TABLE lens_surfaces(lens_id TEXT, configuration TEXT, surface_order INTEGER,
          radius_mm REAL, thickness_mm REAL, effective_aperture_mm REAL, material TEXT,
          nd REAL, vd REAL, conic REAL, coefficients_json TEXT, raw_json TEXT);
        INSERT INTO lens_surfaces VALUES ('p0014','wide',1,40,1,20,'',1.5,60,NULL,'{}','{}');
        CREATE TABLE simulation_results(simulation_id TEXT,lens_id TEXT,configuration TEXT,
          simulation_status TEXT,simulation_model TEXT,focal_length_mm REAL,
          focal_length_source TEXT,f_number REAL,f_number_source TEXT,image_height_mm REAL,
          optics_json TEXT);
        INSERT INTO simulation_results VALUES ('p0014:wide','p0014','wide',
          'camerae2e_ready','thin_lens_proxy',35,'variable_distance:focal_length',
          NULL,'',10,'{}');
        CREATE TABLE raytrace_asset_validation(simulation_id TEXT,asset_relative_path TEXT,
          contract_version INTEGER,asset_sha256 TEXT,valid_contract INTEGER,result_json TEXT);
    """)
    con.execute(
        "INSERT INTO metadata VALUES ('build_info',?)",
        (json.dumps({"source_version": "expanded_v9"}),),
    )
    con.commit()
    con.close()


def _writer(tmp_path):
    root = tmp_path / "output"
    return CatalogWriter(connect(root / "catalog.sqlite", write=True), root)


def _counts(writer):
    return {
        table: writer.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "components",
            "model_revisions",
            "operating_points",
            "simulation_runs",
            "lens_surfaces",
            "spec_observations",
            "artifacts",
            "validations",
        )
    }


def test_reimport_and_relocation_keep_identity_and_revision(tmp_path):
    source = tmp_path / "source" / "lens.sqlite"
    _legacy(source)
    writer = _writer(tmp_path)
    before_hash = file_hash(source)
    first = import_lenses(writer, source)
    counts = _counts(writer)
    import_lenses(writer, source)
    assert _counts(writer) == counts
    moved = tmp_path / "moved.sqlite"
    shutil.copy2(source, moved)
    import_lenses(writer, moved)
    assert _counts(writer) == counts
    assert file_hash(source) == before_hash
    assert first["components"] == first["revisions"] == 1
    assert writer.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_changed_prescription_creates_frozen_revision(tmp_path):
    source = tmp_path / "lens.sqlite"
    _legacy(source)
    writer = _writer(tmp_path)
    import_lenses(writer, source)
    previous = dict(writer.conn.execute("SELECT * FROM model_revisions").fetchone())
    frozen_bytes = (writer.root / previous["snapshot_path"]).read_bytes()
    with sqlite3.connect(source) as con:
        con.execute("UPDATE lens_surfaces SET radius_mm=50")
    import_lenses(writer, source)
    assert _counts(writer)["components"] == 1
    assert _counts(writer)["model_revisions"] == 2
    assert (writer.root / previous["snapshot_path"]).read_bytes() == frozen_bytes
    assert (
        writer.conn.execute("SELECT radius_mm FROM lens_surfaces ORDER BY radius_mm").fetchall()[0][
            0
        ]
        == 40
    )


def test_v2_validation_only_asset_preserved_without_array_loading(tmp_path):
    source = tmp_path / "source" / "lens.sqlite"
    _legacy(source)
    asset = source.parent / "raytrace_psf_v2" / "only.npz"
    asset.parent.mkdir()
    asset.write_bytes(b"opaque bytes: importer must not load numpy arrays")
    with sqlite3.connect(source) as con:
        con.execute(
            "INSERT INTO raytrace_asset_validation VALUES (?,?,?,?,?,?)",
            (
                "p0014:wide",
                "raytrace_psf_v2/only.npz",
                2,
                file_hash(asset),
                1,
                json.dumps({"image_quality_validated": False, "valid_contract": True}),
            ),
        )
    writer = _writer(tmp_path)
    report = import_lenses(writer, source)
    assert report["artifacts"] == 1
    record = dict(writer.conn.execute("SELECT * FROM artifacts").fetchone())
    assert record["availability"] == "available"
    assert record["integrity_status"] == "passed"
    assert (writer.root / record["path"]).read_bytes() == asset.read_bytes()
    check = writer.conn.execute(
        "SELECT status,details_json FROM validations WHERE gate='historical_raytrace_contract'"
    ).fetchone()
    assert check[0] == "unknown"
    assert json.loads(check[1])["historical_result"]["valid_contract"] == 1
    assert json.loads(record["metadata_json"])["axes"]["wavelength_nm"] is None


def test_missing_facts_and_aperture_convention_remain_unknown(tmp_path):
    source = tmp_path / "lens.sqlite"
    _legacy(source)
    writer = _writer(tmp_path)
    import_lenses(writer, source)
    observation = writer.conn.execute(
        "SELECT origin_kind,missing_reason,value_json FROM spec_observations "
        "WHERE property_name='f_number'"
    ).fetchone()
    assert tuple(observation) == ("unknown", "not_reported", "null")
    surface = writer.conn.execute("SELECT material,metadata_json FROM lens_surfaces").fetchone()
    assert surface[0] is None
    assert json.loads(surface[1])["aperture_convention"] == "unknown"
    assert (
        writer.conn.execute("SELECT image_circle_mm FROM lens_configurations").fetchone()[0] is None
    )
