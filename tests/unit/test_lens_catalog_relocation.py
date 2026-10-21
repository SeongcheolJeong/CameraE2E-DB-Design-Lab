"""A copied component catalog may use only its own hash-pinned lens source."""

import shutil
import sqlite3
from contextlib import closing

import pytest

from camera_platform import lens_catalog_bridge as bridge
from pyisetcam._catalog_store import CatalogWriter, connect, file_hash


@pytest.fixture
def copied_catalog(tmp_path):
    original = tmp_path / "original" / "camerae2e_db"
    relative = (
        "lens_db/example_v9/data/lens_patents/lens_patent_simulation_v9.sqlite"
    )
    source = original / relative
    source.parent.mkdir(parents=True)
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE fixture(value TEXT)")
        conn.execute("INSERT INTO fixture VALUES ('pinned')")
    source_hash = file_hash(source)
    with closing(connect(original / "catalog.sqlite", write=True)) as conn, conn:
        writer = CatalogWriter(conn, original)
        component_id = writer.component(
            "lens", "fixture-patent", "example", "lens", "patent_design"
        )
        source_id = writer.source(
            "legacy_lens_database",
            f"lens_patent_db:{source_hash}",
            uri=source.as_uri(),
            sha256=source_hash,
        )
        revision_id = writer.revision(
            component_id,
            "patent_prescription",
            {
                "identity": {
                    "source": "lens_patent_db",
                    "publication_number": "fixture-patent",
                    "prescription_id": 1,
                },
                "configuration": "intermediate",
            },
            source_id=source_id,
        )
    moved = tmp_path / "moved" / "camerae2e_db"
    shutil.copytree(original, moved)
    return original, moved, relative, revision_id, source_id, source_hash


def _error_code(revision_id, root):
    with pytest.raises(bridge.LensCatalogBridgeError) as error:
        bridge._revision_source(revision_id, root)
    return error.value.code


def test_copied_catalog_resolves_local_identical_source_without_rewriting_history(copied_catalog):
    original, moved, relative, revision_id, source_id, source_hash = copied_catalog
    catalog_hash = file_hash(moved / "catalog.sqlite")
    revision, _, path, observed_hash = bridge._revision_source(revision_id, moved)
    assert path == moved / relative
    assert observed_hash == source_hash
    assert revision["source_id"] == source_id
    assert file_hash(moved / "catalog.sqlite") == catalog_hash
    with closing(connect(moved / "catalog.sqlite")) as conn:
        stored_uri = conn.execute(
            "SELECT uri FROM sources WHERE source_id=?", (source_id,)
        ).fetchone()[0]
    assert stored_uri == (original / relative).as_uri()


def test_relocated_source_must_exist_and_match_pinned_hash(copied_catalog):
    _, moved, relative, revision_id, _, _ = copied_catalog
    local_source = moved / relative
    local_source.unlink()
    assert _error_code(revision_id, moved) == "source_relocation_unavailable"
    with sqlite3.connect(local_source) as conn:
        conn.execute("CREATE TABLE replacement(value TEXT)")
    assert _error_code(revision_id, moved) == "source_hash_mismatch"


def test_relocated_source_rejects_symlink_even_when_bytes_match(copied_catalog):
    original, moved, relative, revision_id, _, _ = copied_catalog
    local_source = moved / relative
    local_source.unlink()
    local_source.symlink_to(original / relative)
    assert _error_code(revision_id, moved) == "unsafe_source_path"


def test_relocated_source_rejects_symlinked_search_directory(copied_catalog):
    original, moved, _, revision_id, _, _ = copied_catalog
    shutil.rmtree(moved / "lens_db")
    (moved / "lens_db").symlink_to(original / "lens_db", target_is_directory=True)
    assert _error_code(revision_id, moved) == "unsafe_source_path"


def test_relocated_source_rejects_duplicate_hash_matches(copied_catalog):
    _, moved, relative, revision_id, _, _ = copied_catalog
    duplicate = (
        moved
        / "lens_db"
        / "second_v9"
        / "data"
        / "lens_patents"
        / "lens_patent_simulation_v9.sqlite"
    )
    duplicate.parent.mkdir(parents=True)
    shutil.copy2(moved / relative, duplicate)
    assert _error_code(revision_id, moved) == "source_relocation_ambiguous"


def test_unrelated_historical_uri_cannot_trigger_relocation(copied_catalog):
    _, moved, _, _, _, source_hash = copied_catalog
    with pytest.raises(bridge.LensCatalogBridgeError) as error:
        bridge._source_path("file:///private/tmp/unrelated.sqlite", moved, source_hash)
    assert error.value.code == "unsafe_source_path"


def test_local_uri_mismatch_cannot_fall_back_to_another_source(copied_catalog):
    original, _, relative, revision_id, _, _ = copied_catalog
    with sqlite3.connect(original / relative) as conn:
        conn.execute("INSERT INTO fixture VALUES ('changed')")
    assert _error_code(revision_id, original) == "source_hash_mismatch"
