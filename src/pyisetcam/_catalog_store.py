"""Storage contract for the versioned lens/sensor component catalog."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SCHEMA_NAME = "camerae2e_component_catalog_v1"


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def json_text(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def stable_id(namespace: str, identity: Any) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"camerae2e:{namespace}:{json_text(identity)}"))


def file_hash(path: Path | str) -> str:
    with Path(path).open("rb") as stream:
        result = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


_SCHEMA = """
CREATE TABLE catalog_metadata(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE components(
 component_id TEXT PRIMARY KEY, family TEXT NOT NULL CHECK(family IN ('lens','sensor')),
 identity_key TEXT NOT NULL UNIQUE, entity_kind TEXT NOT NULL,
 manufacturer TEXT NOT NULL, model TEXT NOT NULL);
CREATE TABLE aliases(
 component_id TEXT NOT NULL REFERENCES components, namespace TEXT NOT NULL, alias TEXT NOT NULL,
 PRIMARY KEY(namespace, alias, component_id));
CREATE INDEX aliases_lookup ON aliases(alias COLLATE NOCASE);
CREATE TABLE sources(
 source_id TEXT PRIMARY KEY, kind TEXT NOT NULL, identity_key TEXT NOT NULL,
 uri TEXT, sha256 TEXT, metadata_json TEXT NOT NULL);
CREATE TABLE model_revisions(
 revision_id TEXT PRIMARY KEY, component_id TEXT NOT NULL REFERENCES components,
 model_kind TEXT NOT NULL, content_sha256 TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 source_id TEXT REFERENCES sources, created_at TEXT NOT NULL, snapshot_path TEXT NOT NULL,
 UNIQUE(component_id,model_kind,content_sha256));
CREATE TABLE operating_points(
 operating_point_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES model_revisions,
 label TEXT NOT NULL, conditions_json TEXT NOT NULL);
CREATE TABLE spec_observations(
 observation_id TEXT PRIMARY KEY, component_id TEXT NOT NULL REFERENCES components,
 revision_id TEXT REFERENCES model_revisions,
 operating_point_id TEXT REFERENCES operating_points, property_name TEXT NOT NULL,
 value_json TEXT NOT NULL, numeric_value REAL, text_value TEXT, unit TEXT,
 origin_kind TEXT NOT NULL, source_id TEXT REFERENCES sources,
 conditions_json TEXT NOT NULL, source_locator_json TEXT NOT NULL,
 missing_reason TEXT, extraction_method TEXT);
CREATE INDEX observations_lookup ON spec_observations(property_name,numeric_value);
CREATE TABLE lens_configurations(
 operating_point_id TEXT PRIMARY KEY REFERENCES operating_points,
 focal_length_mm REAL, f_number REAL, image_height_mm REAL, image_circle_mm REAL,
 metadata_json TEXT NOT NULL);
CREATE TABLE lens_surfaces(
 revision_id TEXT NOT NULL REFERENCES model_revisions, surface_order INTEGER NOT NULL,
 radius_mm REAL, thickness_mm REAL, aperture_mm REAL, material TEXT, nd REAL, vd REAL,
 conic REAL, coefficients_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
 PRIMARY KEY(revision_id,surface_order));
CREATE TABLE sensor_geometry(
 revision_id TEXT PRIMARY KEY REFERENCES model_revisions, pixel_pitch_um REAL,
 width_px INTEGER, height_px INTEGER, active_width_mm REAL, active_height_mm REAL,
 cfa_pattern TEXT, metadata_json TEXT NOT NULL);
CREATE TABLE sensor_layers(
 revision_id TEXT NOT NULL REFERENCES model_revisions, layer_order INTEGER NOT NULL,
 name TEXT, material TEXT, thickness_um REAL, metadata_json TEXT NOT NULL,
 PRIMARY KEY(revision_id,layer_order));
CREATE TABLE sensor_readout_modes(
 operating_point_id TEXT PRIMARY KEY REFERENCES operating_points, mode_name TEXT NOT NULL,
 bit_depth INTEGER, metadata_json TEXT NOT NULL);
CREATE TABLE simulation_runs(
 run_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES model_revisions,
 operating_point_id TEXT REFERENCES operating_points, identity_key TEXT NOT NULL,
 status TEXT NOT NULL, solver TEXT, settings_json TEXT NOT NULL,
 source_id TEXT REFERENCES sources, input_sha256 TEXT, metadata_json TEXT NOT NULL,
 created_at TEXT);
CREATE INDEX runs_revision ON simulation_runs(revision_id);
CREATE TABLE artifacts(
 artifact_id TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT, external_uri TEXT,
 sha256 TEXT, expected_sha256 TEXT, availability TEXT NOT NULL,
 integrity_status TEXT NOT NULL CHECK(integrity_status IN ('passed','failed','unknown')),
 metadata_json TEXT NOT NULL);
CREATE TABLE artifact_links(
 artifact_id TEXT NOT NULL REFERENCES artifacts, run_id TEXT REFERENCES simulation_runs,
 revision_id TEXT NOT NULL REFERENCES model_revisions,
 UNIQUE(artifact_id,run_id,revision_id));
CREATE TABLE validations(
 validation_id TEXT PRIMARY KEY, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
 gate TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('passed','failed','unknown')),
 details_json TEXT NOT NULL);
CREATE TABLE component_preferences(
 component_id TEXT PRIMARY KEY REFERENCES components,
 revision_id TEXT NOT NULL REFERENCES model_revisions);
CREATE TABLE camera_configurations(
 configuration_id TEXT PRIMARY KEY, name TEXT NOT NULL,
 lens_revision_id TEXT NOT NULL REFERENCES model_revisions,
 sensor_revision_id TEXT NOT NULL REFERENCES model_revisions,
 lens_operating_point_id TEXT REFERENCES operating_points,
 sensor_operating_point_id TEXT REFERENCES operating_points,
 settings_json TEXT NOT NULL, compatibility_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE releases(
 release_id TEXT PRIMARY KEY, name TEXT NOT NULL, manifest_json TEXT NOT NULL,
 path TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE import_batches(
 batch_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, inputs_json TEXT NOT NULL,
 report_json TEXT NOT NULL);
"""


def connect(path: Path, *, write: bool = False) -> sqlite3.Connection:
    if not write and not path.is_file():
        raise FileNotFoundError(f"Component catalog not found: {path}")
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path if write else path.as_uri() + "?mode=ro", uri=not write, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    if not tables and write:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.execute("INSERT INTO catalog_metadata VALUES ('schema',?)", (json_text(SCHEMA_NAME),))
        conn.commit()
    else:
        try:
            schema = conn.execute(
                "SELECT value_json FROM catalog_metadata WHERE key='schema'"
            ).fetchone()
            valid = version == SCHEMA_VERSION and schema and json.loads(schema[0]) == SCHEMA_NAME
        except sqlite3.DatabaseError:
            valid = False
        if not valid:
            conn.close()
            raise ValueError(f"Unsupported component catalog schema/version at {path}")
    return conn


def write_json(path: Path, value: Any) -> None:
    """Atomic, deterministic immutable payload publication."""
    data = json_text(value) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != data:
            raise ValueError(f"Immutable catalog payload differs: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(data, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class CatalogWriter:
    """Insert immutable records within a transaction owned by the caller."""

    def __init__(self, conn: sqlite3.Connection, root: Path):
        self.conn = conn
        self.root = root.resolve()

    def put(self, table: str, **values: Any) -> None:
        allowed = {
            r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if table not in allowed:
            raise ValueError(f"Unknown catalog table: {table}")
        columns = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        if not set(values) <= columns:
            raise ValueError(f"Unknown columns for {table}: {set(values) - columns}")
        names = list(values)
        # DO NOTHING handles duplicate identity only; NOT NULL/CHECK/FK errors still fail.
        self.conn.execute(
            f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)}) "
            "ON CONFLICT DO NOTHING",
            list(values.values()),
        )

    def component(self, family, identity, manufacturer, model, entity_kind):
        component_id = stable_id("component", [family, identity])
        self.put(
            "components",
            component_id=component_id,
            family=family,
            identity_key=json_text([family, identity]),
            entity_kind=entity_kind,
            manufacturer=manufacturer or "unknown",
            model=model or "unknown",
        )
        return component_id

    def alias(self, component_id, namespace, alias):
        if alias:
            self.put("aliases", component_id=component_id, namespace=namespace, alias=str(alias))

    def source(self, kind, identity, *, uri=None, sha256=None, metadata=None):
        source_id = stable_id("source", [kind, identity, sha256])
        self.put(
            "sources",
            source_id=source_id,
            kind=kind,
            identity_key=json_text(identity),
            uri=None if uri is None else str(uri),
            sha256=sha256,
            metadata_json=json_text(metadata or {}),
        )
        return source_id

    def revision(self, component_id, model_kind, snapshot, *, source_id=None):
        content_hash = digest(snapshot)
        revision_id = stable_id("revision", [component_id, model_kind, content_hash])
        family = self.conn.execute(
            "SELECT family FROM components WHERE component_id=?", (component_id,)
        ).fetchone()[0]
        relative = (
            Path("models") / ("lenses" if family == "lens" else "sensors") / f"{revision_id}.json"
        )
        write_json(self.root / relative, snapshot)
        self.put(
            "model_revisions",
            revision_id=revision_id,
            component_id=component_id,
            model_kind=model_kind,
            content_sha256=content_hash,
            snapshot_json=json_text(snapshot),
            source_id=source_id,
            created_at=now(),
            snapshot_path=relative.as_posix(),
        )
        return revision_id

    def operating_point(self, revision_id, label, conditions):
        identity = stable_id("operating_point", [revision_id, label, conditions])
        self.put(
            "operating_points",
            operating_point_id=identity,
            revision_id=revision_id,
            label=label,
            conditions_json=json_text(conditions),
        )
        return identity

    def observation(
        self,
        component_id,
        property_name,
        value,
        *,
        unit=None,
        origin_kind="reported",
        source_id=None,
        revision_id=None,
        operating_point_id=None,
        conditions=None,
        source_locator=None,
        missing_reason=None,
        extraction_method=None,
    ):
        row = dict(
            component_id=component_id,
            property_name=property_name,
            value_json=json_text(value),
            unit=unit,
            origin_kind=origin_kind,
            source_id=source_id,
            revision_id=revision_id,
            operating_point_id=operating_point_id,
            conditions_json=json_text(conditions or {}),
            source_locator_json=json_text(source_locator or {}),
            missing_reason=missing_reason,
            extraction_method=extraction_method,
        )
        identity = stable_id("observation", row)
        numeric = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        if numeric is not None and not math.isfinite(numeric):
            numeric = None
        self.put(
            "spec_observations",
            observation_id=identity,
            **row,
            numeric_value=numeric,
            text_value=value if isinstance(value, str) else None,
        )
        return identity

    def run(
        self,
        revision_id,
        operating_point_id,
        identity,
        status,
        *,
        solver=None,
        settings=None,
        source_id=None,
        input_sha256=None,
        metadata=None,
        created_at=None,
    ):
        run_id = stable_id(
            "run",
            [revision_id, operating_point_id, identity, status, solver, settings, input_sha256],
        )
        self.put(
            "simulation_runs",
            run_id=run_id,
            revision_id=revision_id,
            operating_point_id=operating_point_id,
            identity_key=json_text(identity),
            status=status,
            solver=solver,
            settings_json=json_text(settings or {}),
            source_id=source_id,
            input_sha256=input_sha256,
            metadata_json=json_text(metadata or {}),
            created_at=created_at,
        )
        return run_id

    def artifact(
        self, path, kind, *, run_id=None, revision_id=None, expected_sha256=None, metadata=None
    ):
        original = None if path is None else Path(path).expanduser().resolve()
        exists = original is not None and original.is_file()
        actual = file_hash(original) if exists else None
        integrity = (
            "failed"
            if exists and expected_sha256 and expected_sha256 != actual
            else ("passed" if exists else "unknown")
        )
        relative = None
        if exists:
            try:
                relative = original.relative_to(self.root).as_posix()
            except ValueError:
                relative = (Path("artifacts") / actual[:2] / (actual + original.suffix)).as_posix()
                destination = self.root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    temporary = destination.with_suffix(destination.suffix + ".tmp")
                    shutil.copyfile(original, temporary)
                    temporary.replace(destination)
                if file_hash(destination) != actual:
                    raise ValueError(f"Copied artifact hash mismatch: {destination}") from None
        artifact_id = stable_id(
            "artifact",
            [kind, actual, expected_sha256, str(original) if not exists else None, metadata or {}],
        )
        self.put(
            "artifacts",
            artifact_id=artifact_id,
            kind=kind,
            path=relative,
            external_uri=str(original) if original else None,
            sha256=actual,
            expected_sha256=expected_sha256,
            availability="available" if exists else "missing",
            integrity_status=integrity,
            metadata_json=json_text(metadata or {}),
        )
        if revision_id is None and run_id is not None:
            revision_id = self.conn.execute(
                "SELECT revision_id FROM simulation_runs WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        if revision_id is not None:
            # NULL in a UNIQUE tuple is not unique in SQLite; explicitly check revision-only links.
            if not self.conn.execute(
                "SELECT 1 FROM artifact_links WHERE artifact_id=? "
                "AND run_id IS ? AND revision_id=?",
                (artifact_id, run_id, revision_id),
            ).fetchone():
                self.put(
                    "artifact_links",
                    artifact_id=artifact_id,
                    run_id=run_id,
                    revision_id=revision_id,
                )
        return artifact_id

    def validation(self, target_type, target_id, gate, status, details=None):
        identity = stable_id("validation", [target_type, target_id, gate, status, details])
        self.put(
            "validations",
            validation_id=identity,
            target_type=target_type,
            target_id=target_id,
            gate=gate,
            status=status,
            details_json=json_text(details or {}),
        )
        return identity
