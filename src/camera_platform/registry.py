"""Immutable JSON revisions, content-addressed blobs, durable jobs, append-only audit.

SQLite is the single-host storage adapter. Domain services do not issue SQL.
Connections are short lived and mutations use explicit transactions.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

_BLOB_CHUNK_BYTES = 1024 * 1024


class ConflictError(ValueError):
    pass


class IntegrityError(ValueError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Registry(Protocol):
    def put(self, kind: str, document: dict, *, identity: str | None = None) -> dict: ...
    def get(self, kind: str, identity: str) -> dict: ...
    def list(self, kind: str) -> list[dict]: ...
    def artifact_binding(
        self, identity: str, *, owner_kind: str, owner_id: str
    ) -> dict: ...
    def artifact_bindings(
        self, identities: list[str], *, owner_kind: str, owner_id: str
    ) -> dict[str, dict]: ...
    def get_run_with_result_binding(self, identifier: str) -> tuple[dict, dict | None]: ...
    def publish_spectral_image(
        self, publication_key: str, asset_data: dict, image_data: dict
    ) -> dict: ...


class SQLiteRegistry:
    def __init__(self, directory: str | Path):
        self.busy_timeout_ms = 20_000
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.blobs = self.directory / "blobs"
        self.blobs.mkdir(exist_ok=True)
        self.path = self.directory / "registry.sqlite"
        with self.connection() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS records (
                  kind TEXT NOT NULL, id TEXT PRIMARY KEY, document TEXT NOT NULL,
                  content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS records_kind ON records(kind, created_at);
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, cache_key TEXT NOT NULL, status TEXT NOT NULL,
                  document TEXT NOT NULL, request_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL, result TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS jobs_cache ON jobs(cache_key, status);
                CREATE TRIGGER IF NOT EXISTS immutable_completed_job BEFORE UPDATE ON jobs
                  WHEN OLD.status IN ('succeeded','failed','cancelled')
                  BEGIN SELECT RAISE(ABORT, 'completed runs are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_job_delete BEFORE DELETE ON jobs
                  BEGIN SELECT RAISE(ABORT, 'run history is immutable'); END;
                CREATE TABLE IF NOT EXISTS audit (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                  action TEXT NOT NULL, subject TEXT NOT NULL, details TEXT NOT NULL,
                  previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_records_update BEFORE UPDATE ON records
                  BEGIN SELECT RAISE(ABORT, 'revisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_records_delete BEFORE DELETE ON records
                  BEGIN SELECT RAISE(ABORT, 'revisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_audit_update BEFORE UPDATE ON audit
                  BEGIN SELECT RAISE(ABORT, 'audit is append only'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_audit_delete BEFORE DELETE ON audit
                  BEGIN SELECT RAISE(ABORT, 'audit is append only'); END;
            """)
            versions = [r[0] for r in conn.execute("SELECT version FROM schema_version")]
            if not versions:
                conn.execute("INSERT INTO schema_version VALUES(1)")
                versions = [1]
            if versions not in ([1], [2]):
                raise ValueError("Unsupported registry schema; explicit migration required")
        if versions == [1]:
            self._migrate_artifact_index()
        else:
            with self.connection() as conn:
                # Missing derived tables are corruption, not an invitation to hide
                # missing owner bindings by silently recreating an empty index.
                conn.execute("SELECT * FROM artifact_owners LIMIT 0")
                conn.execute("SELECT * FROM artifact_heads LIMIT 0")
                indices = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )}
                if not {"artifact_owners_lookup", "audit_subject_action"} <= indices:
                    raise IntegrityError("Artifact owner lookup indexes are missing")

    # Public owner precedence matches the original artifact endpoint exactly.
    ARTIFACT_RECORD_KINDS = (
        "scene_assets", "spectral_images", "captures", "capture_analyses",
        "scene_sequences", "control_sequences", "control_policies",
        "control_programs", "capture_programs",
        "readout_calibrations", "readout_observations", "temporal_signal_references",
        "timing_modes", "lens_sensor_radiance_scenes",
    )
    ARTIFACT_COLUMNS = (
        "artifact_id", "owner_kind", "owner_id", "owner_priority",
        "owner_created_at", "descriptor_order", "descriptor_path", "descriptor_hash",
    )
    ARTIFACT_ORDER = "owner_priority,owner_created_at DESC,owner_id,descriptor_order"

    @classmethod
    def artifact_rows(cls, kind, identity, created_at, document, result=None):
        """Typed public descriptor bindings; arbitrary nested dicts grant no access."""
        entries = []
        if kind == "runs":
            priority = 0
            entries = [("result", "artifacts", i, a)
                       for i, a in enumerate((result or {}).get("artifacts", []))]
            entries += [("input_artifacts", i, a)
                        for i, a in enumerate(document.get("input_artifacts", []))]
        elif kind == "models":
            priority = 1
            values = document.get("response_artifacts", {})
            entries = [("response_artifacts", key, values[key]) for key in sorted(values)]
        elif kind == "measurements":
            priority = 2
            if "original_artifact" in document:
                entries = [("original_artifact", document["original_artifact"])]
        elif kind == "assets":
            # Only typed source-ingestion workflows may create these reserved
            # top-level fields. Generic AssetCreate rejects extra fields, and
            # arbitrary SHA strings in properties never grant blob access.
            if "native_response_source_artifacts" in document:
                if "lens_db_source_artifacts" in document:
                    raise IntegrityError("Asset cannot own two source-artifact contracts")
                artifacts = document["native_response_source_artifacts"]
                props = document.get("properties", {}).get("native_response_bundle", {})
                files = props.get("source_files") if isinstance(props, dict) else None
                manifest_sha = props.get("manifest_sha256") if isinstance(props, dict) else None
                if (
                    document.get("kind") != "sensor"
                    or document.get("method_and_version") != "native_source_bundle_import:1"
                    or document.get("source_uri") !=
                    "native-response-bundle://sha256/" + str(manifest_sha)
                    or not isinstance(files, list)
                    or not 1 <= len(files) <= 128
                    or not isinstance(artifacts, list)
                    or not 4 <= len(artifacts) <= len(files) + 3
                    or not all(isinstance(item, dict) for item in artifacts)
                    or [item.get("name") for item in artifacts[:3]] != [
                        "manifest.json", "response.json", "qualification.json"
                    ]
                    or [item.get("sha256") for item in artifacts[:3]] != [
                        manifest_sha, props.get("response_sha256"),
                        props.get("qualification_sha256")
                    ]
                    or any(
                        not isinstance(item, dict)
                        or set(item) != {"path", "role", "sha256", "artifact_index"}
                        or type(item["artifact_index"]) is not int
                        or not 3 <= item["artifact_index"] < len(artifacts)
                        or artifacts[item["artifact_index"]].get("sha256")
                        != item["sha256"]
                        for item in files
                    )
                    or [item.get("name") for item in artifacts[3:]] != [
                        f"source-{index:03d}" for index in range(len(artifacts) - 3)
                    ]
                    or {item["artifact_index"] for item in files}
                    != set(range(3, len(artifacts)))
                    or len({item.get("sha256") for item in artifacts}) != len(artifacts)
                ):
                    raise IntegrityError("Native Sensor asset has an invalid typed source bundle")
                priority = 3
                entries = [
                    ("native_response_source_artifacts", i, item)
                    for i, item in enumerate(artifacts)
                ]
            elif "lens_db_source_artifacts" not in document:
                return []
            else:
                artifacts = document["lens_db_source_artifacts"]
                props = document.get("properties", {})
                if not isinstance(props, dict):
                    raise IntegrityError("Lens DB source asset properties must be a mapping")
                source = props.get("source", {})
                if not isinstance(source, dict):
                    raise IntegrityError("Lens DB source revision identity must be a mapping")
                if (
                    document.get("kind") != "lens"
                    or document.get("source_uri") !=
                    "lens-db://revision/" + str(source.get("revision_id", ""))
                    or document.get("method_and_version") != "lens_db_wavefront_bridge:1"
                    or not isinstance(artifacts, list)
                    or len(artifacts) != 2
                    or not all(isinstance(item, dict) for item in artifacts)
                    or [item.get("name") for item in artifacts] != [
                        "reviewed-prescription.csv", "lens-db-source-snapshot.json"
                    ]
                    or props.get("reviewed_csv_artifact_sha256") != artifacts[0].get("sha256")
                    or props.get("source_snapshot_artifact_sha256") != artifacts[1].get("sha256")
                ):
                    raise IntegrityError("Lens DB source asset has an invalid typed artifact pair")
                priority = 3
                entries = [("lens_db_source_artifacts", i, item)
                           for i, item in enumerate(artifacts)]
        elif kind == "active_mode_declarations":
            # Only this exact, pinned declaration descriptor owns the source
            # bytes. An arbitrary checksum in a record never grants access.
            descriptor = document.get("declaration_artifact")
            if (
                document.get("schema") != "camerae2e_registered_active_mode_declaration_v1"
                or document.get("source_kind") != "engineering_declaration"
                or not isinstance(descriptor, dict)
                or descriptor.get("sha256") != document.get("declaration_artifact_sha256")
                or descriptor.get("bytes") != document.get("declaration_artifact_bytes")
                or descriptor.get("name") != "active-mode-declaration.json"
                or descriptor.get("media_type") != "application/json"
            ):
                raise IntegrityError("Active-mode declaration has an invalid typed artifact")
            priority = 3 + len(cls.ARTIFACT_RECORD_KINDS)
            entries = [("declaration_artifact", descriptor)]
        elif kind == "calibration_lab_measurements":
            # Append without changing existing owner priorities or derived indexes.
            if document.get("schema") != "camera_calibration_measurement_v1":
                raise IntegrityError("Calibration measurement has an invalid artifact contract")
            priority = 4 + len(cls.ARTIFACT_RECORD_KINDS)
            entries = [("artifacts", i, a)
                       for i, a in enumerate(document.get("artifacts", []))]
        elif kind == "calibration_db_bridges":
            artifacts = document.get("artifacts")
            source_files = document.get("source_files")
            if (document.get("schema") != "camera_calibration_db_bridge_v1"
                    or not isinstance(artifacts, list) or not artifacts
                    or not isinstance(source_files, dict) or not source_files
                    or not all(isinstance(a, dict) for a in artifacts)
                    or not all(isinstance(row, dict) for row in source_files.values())
                    or {a.get("sha256") for a in artifacts}
                    != {row.get("sha256") for row in source_files.values()}
                    or len({a.get("sha256") for a in artifacts}) != len(artifacts)):
                raise IntegrityError("DB calibration bridge has an invalid typed source bundle")
            # New owner priority is appended; existing owners retain their ordering.
            priority = 5 + len(cls.ARTIFACT_RECORD_KINDS)
            entries = [("artifacts", i, a) for i, a in enumerate(artifacts)]
        elif kind == "calibration_db_optical_profiles":
            artifacts = document.get("artifacts")
            originals = document.get("source_files")
            outputs = document.get("output_files")
            if (document.get("schema") != "camera_calibration_db_optical_profile_v1"
                    or not isinstance(artifacts, list) or not artifacts
                    or not isinstance(originals, dict) or not originals
                    or not isinstance(outputs, dict) or not outputs
                    or not all(isinstance(a, dict) for a in artifacts)
                    or not all(isinstance(row, dict)
                               for row in [*originals.values(), *outputs.values()])
                    or {a.get("sha256") for a in artifacts}
                    != {row.get("sha256") for row in [*originals.values(), *outputs.values()]}
                    or len({a.get("sha256") for a in artifacts}) != len(artifacts)):
                raise IntegrityError("DB optical profile has an invalid typed artifact bundle")
            priority = 6 + len(cls.ARTIFACT_RECORD_KINDS)
            entries = [("artifacts", i, a) for i, a in enumerate(artifacts)]
        elif kind == "calibration_completion_reports":
            artifacts, files = document.get("artifacts"), document.get("files")
            if (document.get("schema") != "camera_calibration_completion_record_v1"
                    or not isinstance(artifacts, list) or not artifacts
                    or not isinstance(files, dict)
                    or set(files) != {
                        "report.html", "report.json", "final-profile.json", "parameters.csv"
                    }
                    or not all(isinstance(a, dict) for a in artifacts)
                    or {a.get("name"): a.get("sha256") for a in artifacts} != files):
                raise IntegrityError("Calibration completion has an invalid artifact contract")
            priority = 7 + len(cls.ARTIFACT_RECORD_KINDS)
            entries = [("artifacts", i, a) for i, a in enumerate(artifacts)]
        elif kind == "calibration_color_tasks":
            artifacts, files = document.get("artifacts"), document.get("files")
            required = {"report.html", "report.json", "qe-profile.json", "ccm-profile.json",
                        "inputs.json"}
            allowed = required | {"raw-training.npy", "raw-validation.npy"}
            if (document.get("schema") != "camera_spectral_color_record_v1"
                    or not isinstance(artifacts, list) or not artifacts
                    or not isinstance(files, dict) or not required <= set(files) <= allowed
                    or not all(isinstance(a, dict) for a in artifacts)
                    or {a.get("name"): a.get("sha256") for a in artifacts} != files):
                raise IntegrityError("Spectral color task has an invalid artifact contract")
            priority = 8 + len(cls.ARTIFACT_RECORD_KINDS)
            entries = [("artifacts", i, a) for i, a in enumerate(artifacts)]
        elif kind == "calibration_db_color_models":
            artifacts = document.get("artifacts")
            files = document.get("source_files")
            if (document.get("schema") != "camera_db_color_model_v1"
                    or not isinstance(artifacts, list) or not artifacts
                    or not isinstance(files, dict) or not files
                    or not all(isinstance(a, dict) for a in artifacts)
                    or not all(isinstance(row, dict) for row in files.values())
                    or {a.get("sha256") for a in artifacts}
                    != {row.get("sha256") for row in files.values()}
                    or len({a.get("sha256") for a in artifacts}) != len(artifacts)):
                raise IntegrityError("DB color model has an invalid typed source bundle")
            priority = 9 + len(cls.ARTIFACT_RECORD_KINDS)
            entries = [("artifacts", i, a) for i, a in enumerate(artifacts)]
        elif kind == "camera_validation_esfr_captures":
            artifacts, files = document.get("artifacts"), document.get("files")
            if (document.get("schema") != "camera_esfr_capture_v1"
                    or not isinstance(artifacts, list) or not isinstance(files, dict)
                    or set(files) != {"raw", "isp", "preview"}
                    or not all(isinstance(a, dict) for a in artifacts)
                    or not all(isinstance(a, dict) for a in files.values())
                    or {a.get("sha256") for a in artifacts}
                    != {a.get("sha256") for a in files.values()}):
                raise IntegrityError("E-SFR capture has an invalid typed artifact bundle")
            # Append without renumbering the existing immutable owner ordering.
            priority = 10 + len(cls.ARTIFACT_RECORD_KINDS)
            entries = [("artifacts", i, a) for i, a in enumerate(artifacts)]
        elif kind in {"camera_esfr_iq_frozen", "camera_esfr_iq_reports", "camera_esfr_iq_comparisons", "camera_esfr_iq_exports"}:
            schemas = {"camera_esfr_iq_frozen": "camera_esfr_iq_frozen_v1",
                       "camera_esfr_iq_reports": "camera_esfr_iq_report_v1",
                       "camera_esfr_iq_comparisons": "camera_esfr_iq_comparison_v1",
                       "camera_esfr_iq_exports": "camera_esfr_iq_export_v1"}
            artifacts, files = document.get("artifacts"), document.get("files")
            if (document.get("schema") != schemas[kind]
                    or not isinstance(artifacts, list) or not artifacts
                    or not isinstance(files, dict) or not files
                    or not all(isinstance(a, dict) for a in artifacts)
                    or not all(isinstance(p, str) and 0 < len(p) <= 240
                               and not p.startswith("/") and "\\" not in p
                               and all(part not in {"", ".", ".."} for part in p.split("/"))
                               for p in files)
                    or {a.get("sha256") for a in artifacts} != set(files.values())):
                raise IntegrityError("E-SFR IQ has an invalid typed artifact bundle")
            priority = 11 + len(cls.ARTIFACT_RECORD_KINDS) + list(schemas).index(kind)
            entries = [("artifacts", i, a) for i, a in enumerate(artifacts)]
        elif kind in cls.ARTIFACT_RECORD_KINDS:
            priority = 3 + cls.ARTIFACT_RECORD_KINDS.index(kind)
            entries = [("artifacts", i, a)
                       for i, a in enumerate(document.get("artifacts", []))]
        else:
            return []
        rows = []
        for order, entry in enumerate(entries):
            path, descriptor = entry[:-1], entry[-1]
            if not isinstance(descriptor, dict):
                raise IntegrityError("Artifact owner has an invalid descriptor")
            checksum = descriptor.get("id")
            if (not isinstance(checksum, str) or len(checksum) != 64
                    or any(c not in "0123456789abcdef" for c in checksum)
                    or descriptor.get("sha256") != checksum
                    or type(descriptor.get("bytes")) is not int or descriptor["bytes"] < 0
                    or not isinstance(descriptor.get("name"), str)
                    or not isinstance(descriptor.get("media_type"), str)
                    or descriptor.get("url") != f"/api/v1/artifacts/{checksum}"):
                raise IntegrityError("Artifact owner has an invalid descriptor")
            rows.append(dict(zip(cls.ARTIFACT_COLUMNS, (
                checksum, kind, identity, priority, created_at, order,
                canonical(path), digest(descriptor),
            ), strict=True)))
        return rows

    @classmethod
    def _refresh_artifact_heads(cls, conn, checksums):
        for checksum in sorted(checksums):
            selected = conn.execute(
                "SELECT * FROM artifact_owners WHERE artifact_id=? ORDER BY "
                + cls.ARTIFACT_ORDER + " LIMIT 1", (checksum,),
            ).fetchone()
            if selected is None:
                conn.execute("DELETE FROM artifact_heads WHERE artifact_id=?", (checksum,))
            else:
                conn.execute("INSERT OR REPLACE INTO artifact_heads VALUES(?,?)",
                             (checksum, digest(dict(selected))))

    @classmethod
    def _index_owner(cls, conn, kind, identity, created_at, document, result=None):
        rows = cls.artifact_rows(kind, identity, created_at, document, result)
        affected = {r[0] for r in conn.execute(
            "SELECT artifact_id FROM artifact_owners WHERE owner_kind=? AND owner_id=?",
            (kind, identity),
        )} | {row["artifact_id"] for row in rows}
        # Normal publication must not turn partial index corruption into a valid
        # new preference guard. Check the old binding before changing any rows;
        # otherwise a deleted preferred owner could become a silent fallback.
        for checksum in sorted(affected):
            previous = conn.execute(
                "SELECT * FROM artifact_owners INDEXED BY artifact_owners_lookup "
                "WHERE artifact_id=? ORDER BY " + cls.ARTIFACT_ORDER + " LIMIT 1",
                (checksum,),
            ).fetchone()
            head = conn.execute(
                "SELECT binding_hash FROM artifact_heads WHERE artifact_id=?", (checksum,),
            ).fetchone()
            if previous is None and head is None:
                continue
            if previous is None or head is None or digest(dict(previous)) != head[0]:
                raise IntegrityError("Artifact owner index binding failed before publication")
        conn.execute("DELETE FROM artifact_owners WHERE owner_kind=? AND owner_id=?",
                     (kind, identity))
        for row in rows:
            conn.execute("INSERT INTO artifact_owners VALUES(?,?,?,?,?,?,?,?)",
                         tuple(row[k] for k in cls.ARTIFACT_COLUMNS))
            affected.add(row["artifact_id"])
        cls._refresh_artifact_heads(conn, affected)

    def index_record(self, conn, kind, record):
        """Publication hook: call inside the same transaction as its record INSERT."""
        self._index_owner(conn, kind, record["id"], record["created_at"], record)

    def index_job(self, conn, identity, request, created_at, *, result=None):
        """Publication hook for queued jobs and results, in their mutation transaction."""
        self._index_owner(conn, "runs", identity, created_at, request, result)

    def _migrate_artifact_index(self):
        with self.transaction() as conn:
            versions = [r[0] for r in conn.execute("SELECT version FROM schema_version")]
            if versions == [2]:  # A concurrent API/Worker may have finished migration.
                return
            if versions != [1]:
                raise IntegrityError("Artifact owner migration requires registry schema 1")
            conn.execute("""CREATE TABLE artifact_owners (
                artifact_id TEXT NOT NULL, owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL,
                owner_priority INTEGER NOT NULL, owner_created_at TEXT NOT NULL,
                descriptor_order INTEGER NOT NULL, descriptor_path TEXT NOT NULL,
                descriptor_hash TEXT NOT NULL,
                PRIMARY KEY(owner_kind,owner_id,descriptor_path))""")
            conn.execute("""CREATE INDEX artifact_owners_lookup ON artifact_owners(
                artifact_id,owner_priority,owner_created_at DESC,owner_id,descriptor_order)""")
            conn.execute("""CREATE TABLE artifact_heads (
                artifact_id TEXT PRIMARY KEY, binding_hash TEXT NOT NULL)""")
            conn.execute("CREATE INDEX audit_subject_action ON audit(subject,action,seq)")
            for row in conn.execute("SELECT * FROM records"):
                record = self._record(row)
                if self.artifact_rows(row["kind"], row["id"], row["created_at"], record):
                    self._verify_record_creation(conn, row)
                self.index_record(conn, row["kind"], record)
            events = {row["subject"]: row for row in conn.execute(
                "SELECT * FROM audit WHERE action='runs.succeeded' ORDER BY seq"
            )}
            for row in conn.execute("SELECT * FROM jobs"):
                job = self._job(row, events)
                self.index_job(conn, row["id"], self.decode_document(row["document"]),
                               row["created_at"], result=job["result"])
            conn.execute("UPDATE schema_version SET version=2 WHERE version=1")

    @staticmethod
    def _verify_record_creation(conn, row):
        events = conn.execute(
            "SELECT * FROM audit INDEXED BY audit_subject_action "
            "WHERE subject=? AND action=? ORDER BY seq DESC LIMIT 2",
            (row["id"], row["kind"] + ".created"),
        ).fetchall()
        if len(events) != 1:
            raise IntegrityError("Artifact owner has no unique creation audit")
        event = events[0]
        value = {key: event[key] for key in ("timestamp", "action", "subject", "previous_hash")}
        value["details"] = json.loads(event["details"])
        if (digest(value) != event["event_hash"]
                or value["details"].get("content_hash") != row["content_hash"]):
            raise IntegrityError("Artifact owner creation audit failed checksum")

    def artifact_owner(self, identity):
        try:
            return self._artifact_owner(identity)
        except sqlite3.DatabaseError as exc:
            raise IntegrityError("Artifact owner lookup index is unavailable") from exc

    def artifact_binding(self, identity, *, owner_kind, owner_id):
        """Verify a specific typed owner of a shared content-addressed artifact.

        The public artifact endpoint still uses artifact_owner's deterministic
        precedence. Source-pinned workflows must select their exact asset,
        because another valid revision may store the same source bytes.
        """
        if not isinstance(owner_kind, str) or not isinstance(owner_id, str):
            raise ValueError("An exact artifact owner kind and ID are required")
        try:
            return self._artifact_owner(identity, required_owner=(owner_kind, owner_id))
        except sqlite3.DatabaseError as exc:
            raise IntegrityError("Artifact owner lookup index is unavailable") from exc

    def artifact_bindings(self, identities, *, owner_kind, owner_id):
        """Verify a batch of exact-owner artifacts within one fresh read snapshot.

        The owner graph is verified once per call, never cached across calls.
        Every artifact still verifies its preferred head, exact typed binding,
        descriptor, byte hash and size. A batch is returned only if all pass.
        """
        if not isinstance(owner_kind, str) or not isinstance(owner_id, str):
            raise ValueError("An exact artifact owner kind and ID are required")
        if not isinstance(identities, (list, tuple)) or any(
            not isinstance(identity, str) for identity in identities
        ):
            raise ValueError("Artifact identities must be a list of strings")
        identities = list(dict.fromkeys(identities))
        if not identities:
            return {}
        try:
            with self.connection() as conn:
                conn.execute("BEGIN")
                if owner_kind == "runs":
                    original = conn.execute(
                        "SELECT * FROM jobs WHERE id=?", (owner_id,)
                    ).fetchone()
                    if original is None:
                        raise IntegrityError("Artifact owner run is missing")
                    event = conn.execute(
                        "SELECT * FROM audit INDEXED BY audit_subject_action "
                        "WHERE subject=? AND action=? ORDER BY seq DESC LIMIT 1",
                        (owner_id, f"runs.{original['status']}"),
                    ).fetchone()
                    owner, document = self._decode_job(
                        original, {owner_id: event} if event else {}
                    )
                    result = owner["result"]
                else:
                    original = conn.execute(
                        "SELECT * FROM records WHERE kind=? AND id=?", (owner_kind, owner_id)
                    ).fetchone()
                    if original is None:
                        raise IntegrityError("Artifact owner record is missing")
                    owner = self._record(original)
                    self._verify_record_creation(conn, original)
                    document, result = owner, None
                binding_rows = self.artifact_rows(
                    owner_kind, owner_id, original["created_at"], document, result
                )
                verified_bindings = {canonical(binding) for binding in binding_rows}
                result_bindings = {}
                for identity in identities:
                    row = self._artifact_index_row(conn, identity, (owner_kind, owner_id))
                    result_bindings[identity] = self._bound_artifact(
                        identity, row, original, owner, document, result,
                        verified_bindings=verified_bindings,
                    )
                return result_bindings
        except sqlite3.DatabaseError as exc:
            raise IntegrityError("Artifact owner lookup index is unavailable") from exc

    def _artifact_index_row(self, conn, identity, required_owner=None):
        """Validate the preferred head and exact owner in the caller's read transaction."""
        preferred = conn.execute(
            "SELECT * FROM artifact_owners INDEXED BY artifact_owners_lookup "
            "WHERE artifact_id=? ORDER BY " + self.ARTIFACT_ORDER + " LIMIT 1", (identity,),
        ).fetchone()
        head = conn.execute("SELECT binding_hash FROM artifact_heads WHERE artifact_id=?",
                            (identity,)).fetchone()
        if preferred is None and head is None:
            raise KeyError("Artifact is not associated with registered evidence")
        if preferred is None or head is None or digest(dict(preferred)) != head[0]:
            raise IntegrityError("Artifact owner index binding failed checksum")
        row = preferred
        if required_owner is not None:
            selected = conn.execute(
                "SELECT * FROM artifact_owners INDEXED BY artifact_owners_lookup "
                "WHERE artifact_id=? AND owner_kind=? AND owner_id=? "
                "ORDER BY descriptor_order LIMIT 2", (identity, *required_owner),
            ).fetchall()
            if not selected:
                raise KeyError("Artifact is not associated with the exact owner")
            if len(selected) != 1:
                raise IntegrityError("Artifact has ambiguous exact owner bindings")
            row = selected[0]
        return row

    def _bound_artifact(
        self, identity, row, original, owner, document, result, *, verified_bindings=None
    ):
        """Finish an internal operation using its freshly verified owner, never a cache."""
        kind = row["owner_kind"]
        if verified_bindings is None:
            bindings = self.artifact_rows(kind, row["owner_id"], original["created_at"],
                                          document, result)
            verified_bindings = {canonical(binding) for binding in bindings}
        if canonical(dict(row)) not in verified_bindings:
            raise IntegrityError("Artifact descriptor differs from its exact owner binding")
        entry = {**document, "result": result} if kind == "runs" else document
        descriptor_path = json.loads(row["descriptor_path"])
        for part in descriptor_path:
            entry = entry[part]
        # Every operation hashes actual target bytes, including equal-size edits.
        path = self.blob_path(identity)
        if path.stat().st_size != entry["bytes"]:
            raise IntegrityError("Artifact size differs from its owner descriptor")
        descriptor = (deepcopy(entry) if kind == "runs"
                      and descriptor_path[0] == "input_artifacts" else dict(entry))
        return {"kind": kind, "owner": owner, "descriptor": descriptor, "path": path}

    def _artifact_owner(self, identity, *, required_owner=None):
        """Resolve an indexed binding, freshly verify its original owner, never scan history.

        The head guards accidental/partial index tampering and lost preferred rows.
        It is derived local metadata, not a signature against a coherent database rewrite.
        """
        with self.connection() as conn:
            conn.execute("BEGIN")
            row = self._artifact_index_row(conn, identity, required_owner)
            kind = row["owner_kind"]
            if kind == "runs":
                original = conn.execute("SELECT * FROM jobs WHERE id=?",
                                        (row["owner_id"],)).fetchone()
                if original is None:
                    raise IntegrityError("Artifact owner run is missing")
                event = conn.execute(
                    "SELECT * FROM audit INDEXED BY audit_subject_action "
                    "WHERE subject=? AND action='runs.succeeded' "
                    "ORDER BY seq DESC LIMIT 1", (row["owner_id"],),
                ).fetchone()
                owner, document = self._decode_job(
                    original, {row["owner_id"]: event} if event else {})
                result = owner["result"]
            else:
                original = conn.execute("SELECT * FROM records WHERE kind=? AND id=?",
                                        (kind, row["owner_id"])).fetchone()
                if original is None:
                    raise IntegrityError("Artifact owner record is missing")
                owner = self._record(original)
                self._verify_record_creation(conn, original)
                document, result = owner, None
            return self._bound_artifact(identity, row, original, owner, document, result)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(
            self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _audit(conn, action, subject, details):
        previous = conn.execute("SELECT event_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        record = {
            "timestamp": now(),
            "action": action,
            "subject": subject,
            "details": details,
            "previous_hash": previous[0] if previous else "0" * 64,
        }
        conn.execute(
            "INSERT INTO audit(timestamp,action,subject,details,previous_hash,event_hash) "
            "VALUES(?,?,?,?,?,?)",
            (
                record["timestamp"],
                action,
                subject,
                canonical(details),
                record["previous_hash"],
                digest(record),
            ),
        )

    def put(self, kind: str, document: dict, *, identity: str | None = None) -> dict:
        for reserved in ("id", "created_at", "content_hash"):
            if reserved in document:
                raise ValueError(f"Reserved field: {reserved}")
        value = {**document, "id": identity or f"{kind.rstrip('s')}-{uuid.uuid4().hex}",
                 "created_at": now()}
        value["content_hash"] = digest(value)
        staged = self._stage_record(kind, value)
        with self.transaction() as conn:
            try:
                self._insert_staged_record(conn, staged)
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"Revision already exists: {value['id']}") from exc
        return value

    def put_verified_record(self, kind, document, references, *, identity=None):
        """Publish a new revision only while its already-verified references stay exact.

        Callers supply the complete typed reference closure. No history scan or
        graph encoding runs while holding the SQLite writer transaction.
        """
        if {"id", "created_at", "content_hash"} & document.keys():
            raise ValueError("Verified publication cannot override record identity")
        unique = {}
        for reference_kind, record in references:
            key = reference_kind, record["id"]
            if key in unique and unique[key] != record:
                raise IntegrityError("Conflicting verified reference snapshots")
            unique[key] = record
        queries = [("SELECT * FROM records WHERE kind=? AND id=?", key) for key in unique]
        value = {**document, "id": identity or f"{kind.rstrip('s')}-{uuid.uuid4().hex}",
                 "created_at": now()}
        value["content_hash"] = digest(value)
        staged = self._stage_record(kind, value)
        for _attempt in range(8):
            before = self._read_rows(queries)
            for rows, expected in zip(before, unique.values(), strict=True):
                if not rows or self._record(rows[0]) != expected:
                    raise IntegrityError("Verified reference changed before publication")
            with self.transaction() as conn:
                if not self._same_read_rows(conn, queries, before):
                    continue
                try:
                    self._insert_staged_record(conn, staged)
                except sqlite3.IntegrityError as exc:
                    raise ConflictError(f"Revision already exists: {value['id']}") from exc
                return value
        raise ConflictError("Reference rows changed during preparation; retry publication")

    def publish_readout_calibration(
        self, document, references, job, *, identity, ancestor_jobs=(),
    ):
        """Publish a fitted revision against one exact completed job and read set.

        Numerical work and graph encoding precede the writer transaction. The
        completion event is part of the CAS, so publication cannot outlive a
        cancelled, substituted or unproven result. The caller supplies a stable
        identity derived from its immutable publication request; an exact retry
        returns the existing revision and emits no second audit event.
        """
        if {"id", "created_at", "content_hash"} & document.keys():
            raise ValueError("Fitted publication cannot override record identity")
        if job.get("job_kind") != "readout_fit" or job.get("status") != "succeeded":
            raise ValueError("Only a succeeded readout fit can publish a calibration")
        if not isinstance(identity, str) or not identity.startswith("readout_calibration-"):
            raise ValueError("Fitted publication requires its deterministic calibration identity")
        unique = {}
        for kind, record in references:
            key = kind, record["id"]
            if key in unique and unique[key] != record:
                raise IntegrityError("Conflicting fitted publication references")
            unique[key] = record
        if len(unique) > 512:
            raise ValueError("Fitted publication exceeds 512 reference records")
        jobs = {job["id"]: job}
        for ancestor in ancestor_jobs:
            if (ancestor.get("job_kind") != "readout_fit"
                    or ancestor.get("status") != "succeeded"):
                raise ValueError("Fitted ancestry requires succeeded response fitting jobs")
            if ancestor["id"] in jobs and jobs[ancestor["id"]] != ancestor:
                raise IntegrityError("Conflicting fitted ancestry job snapshots")
            jobs[ancestor["id"]] = ancestor
        if len(jobs) > 100:
            raise ValueError("Fitted publication exceeds 100 ancestor jobs")
        queries = [("SELECT * FROM records WHERE kind=? AND id=?", key) for key in unique]
        for identity_job in jobs:
            queries.extend([
                ("SELECT * FROM jobs WHERE id=?", (identity_job,)),
                ("SELECT * FROM audit WHERE subject=? AND action='runs.succeeded' "
                 "ORDER BY seq DESC LIMIT 1", (identity_job,)),
            ])
        queries.append(
            ("SELECT * FROM records WHERE kind='readout_calibrations' AND id=?", (identity,))
        )
        value = {**document, "id": identity, "created_at": now()}
        value["content_hash"] = digest(value)
        staged = self._stage_record("readout_calibrations", value)
        for _attempt in range(8):
            before = self._read_rows(queries)
            for rows, expected in zip(before[:len(unique)], unique.values(), strict=True):
                if not rows or self._record(rows[0]) != expected:
                    raise IntegrityError("Fitted publication input changed")
            for offset, expected in enumerate(jobs.values()):
                start = len(unique) + offset * 2
                job_rows, events = before[start:start + 2]
                if not job_rows or not events:
                    raise IntegrityError("Fitted publication lacks a completed job proof")
                fresh = self._job(job_rows[0], {expected["id"]: events[0]})
                fresh["artifacts"] = (fresh.get("result") or {}).get("artifacts", [])
                if fresh != expected or fresh["status"] != "succeeded":
                    raise IntegrityError("Fitted publication job or result changed")
            existing = before[-1]
            saved = self._record(existing[0]) if existing else None
            if saved is not None and {
                k: v for k, v in saved.items() if k not in {"id", "created_at", "content_hash"}
            } != document:
                raise ConflictError("Fitted publication identity has different immutable inputs")
            with self.transaction() as conn:
                if not self._same_read_rows(conn, queries, before):
                    continue
                if saved is None:
                    self._insert_staged_record(conn, staged, audit_details={
                        "content_hash": value["content_hash"], "fit_job_id": job["id"],
                        "request_sha256": job["content_hash"],
                        "result_sha256": digest(job["result"]),
                    })
                return saved if saved is not None else value
        raise ConflictError("Fitted publication inputs changed during preparation; retry")

    def _stage_record(self, kind, record, *, error="Staged revision failed checksum"):
        """Perform large logical hashing and graph/blob publication before BEGIN IMMEDIATE."""
        document = {k: v for k, v in record.items() if k != "content_hash"}
        if digest(document) != record["content_hash"]:
            raise IntegrityError(error)
        return kind, record, self.encode_document(document)

    def _insert_staged_record(self, conn, staged, *, audit_details=None):
        kind, record, encoded = staged
        conn.execute("INSERT INTO records VALUES(?,?,?,?,?)", (
            kind, record["id"], encoded, record["content_hash"], record["created_at"],
        ))
        self.index_record(conn, kind, record)
        self._audit(conn, kind + ".created", record["id"], audit_details or {
            "content_hash": record["content_hash"],
        })

    def _read_rows(self, queries):
        """One read snapshot; decoding its rows happens after this connection closes."""
        with self.connection() as conn:
            conn.execute("BEGIN")
            return [conn.execute(sql, args).fetchall() for sql, args in queries]

    @staticmethod
    def _same_read_rows(conn, queries, before):
        # Exact stored bytes and metadata, including membership, bind preflight to
        # the short publication transaction. No logical JSON/graph is decoded here.
        return all(
            [tuple(row) for row in conn.execute(sql, args)]
            == [tuple(row) for row in rows]
            for (sql, args), rows in zip(queries, before, strict=True)
        )

    def encode_document(self, value):
        from .evidence_storage import encode

        return encode(self, value)

    def decode_document(self, text):
        from .evidence_storage import decode_registry

        return decode_registry(self, text)

    def _record(self, row):
        value = self.decode_document(row["document"])
        if (digest(value) != row["content_hash"] or value.get("id") != row["id"]
                or value.get("created_at") != row["created_at"]):
            raise IntegrityError(f"Record integrity failed: {row['id']}")
        return {**value, "content_hash": row["content_hash"]}

    def publish_spectral_image(self, publication_key, asset_data, image_data):
        """Atomically publish a derived scene asset and its image; retries reuse both."""
        identity = "spectral-image-projection-" + publication_key
        asset_id = "asset-projection-" + publication_key
        for data in (asset_data, image_data):
            if {"id", "created_at", "content_hash"} & data.keys():
                raise ValueError("Publication data cannot override record identity")
        queries = [("SELECT * FROM records WHERE id=?", (identity,)),
                   ("SELECT * FROM records WHERE id=?", (asset_id,))]

        def prior(rows):
            image_row, asset_rows = rows[0][0], rows[1]
            if not asset_rows:
                raise IntegrityError("Publication is missing its original scene asset")
            image, asset = self._record(image_row), self._record(asset_rows[0])
            expected = {**image_data, "asset_id": asset_id, "asset_snapshot": asset,
                        "publication_sha256": publication_key}
            record_fields = {"id", "created_at", "content_hash"}
            if (image_row["kind"] != "spectral_images" or asset_rows[0]["kind"] != "assets"
                    or {k: v for k, v in asset.items() if k not in record_fields} != asset_data
                    or {k: v for k, v in image.items() if k not in record_fields} != expected):
                raise ConflictError("Publication identity conflicts with frozen contents")
            return image

        before = self._read_rows(queries)
        if before[0]:
            return prior(before)
        stamp = now()
        asset = {**asset_data, "id": asset_id, "created_at": stamp}
        asset["content_hash"] = digest(asset)
        image = {**image_data, "asset_id": asset_id, "asset_snapshot": asset,
                 "publication_sha256": publication_key, "id": identity, "created_at": stamp}
        image["content_hash"] = digest(image)
        staged = [self._stage_record("assets", asset),
                  self._stage_record("spectral_images", image)]
        with self.transaction() as conn:
            current = [conn.execute(sql, args).fetchall() for sql, args in queries]
            if not current[0]:
                for record in staged:
                    self._insert_staged_record(conn, record)
        return prior(current) if current[0] else image

    def publish_capture_analysis(self, capture, analysis, measurement, *, new_capture=False):
        """Publish capture/analysis/measurement after verified read-only preparation."""
        queries = [("SELECT * FROM records WHERE kind='capture_analyses' AND id=?",
                    (analysis["id"],))]
        queries.append((
            "SELECT * FROM records WHERE kind IN ('captures','measurements') ORDER BY id"
            if new_capture else "SELECT * FROM records WHERE kind='captures' AND id=?",
            () if new_capture else (capture["id"],),
        ))
        staged = None
        for _attempt in range(8):
            before = self._read_rows(queries)
            if before[0]:
                stored = self._record(before[0][0])
                if stored["request_hash"] != analysis["request_hash"]:
                    raise ConflictError("Capture analysis identity collision")
                try:
                    measured = self.get("measurements", stored["measurement_id"])
                except KeyError as exc:
                    raise IntegrityError("Published capture analysis has no measurement") from exc
                return stored, measured
            if new_capture:
                for row in before[1]:
                    old = self._record(row)
                    if set(old.get("dataset_ids", [])) & set(capture["dataset_ids"]):
                        raise ConflictError("Dataset identity is already registered")
                    if row["kind"] == "captures" and (
                        set(old.get("raw_frame_hashes", [])) & set(capture["raw_frame_hashes"])
                    ):
                        raise ConflictError("RAW frames overlap an existing capture")
            elif not before[1] or self._record(before[1][0]) != capture:
                raise IntegrityError("Analysis acquisition differs from its stored revision")
            if (
                analysis["capture_id"] != capture["id"]
                or analysis["capture_hash"] != capture["content_hash"]
                or measurement["raw_capture_id"] != capture["id"]
                or measurement["capture_analysis_id"] != analysis["id"]
                or measurement["id"] != analysis["measurement_id"]
                or measurement["dataset_ids"] != capture["dataset_ids"]
                or measurement["source_kind"] != capture["source_kind"]
            ):
                raise IntegrityError("Analysis publication must preserve acquisition identity")
            if staged is None:
                records = [("captures", capture)] if new_capture else []
                records += [("capture_analyses", analysis), ("measurements", measurement)]
                staged = [self._stage_record(k, r, error="Staged capture analysis failed checksum")
                          for k, r in records]
            with self.transaction() as conn:
                if not self._same_read_rows(conn, queries, before):
                    continue
                for record in staged:
                    self._insert_staged_record(conn, record)
                return analysis, measurement
        raise ConflictError("Capture registry changed during preparation; retry publication")

    def publish_nonlinear_capture(self, capture):
        """Publish one acquisition after atomic dataset/group/exposure duplicate checks.

        Metadata, NPY parsing, ancestry and large graph encoding are prepared
        outside the write transaction. Exact row membership/bytes guard the
        verified reads until the immutable acquisition and artifact index publish.
        """
        snapshots = [("assemblies", capture["assembly_snapshot"]),
                     ("tests", capture["test_snapshot"])]
        snapshots += [("models", model) for model in capture["model_snapshots"]]
        queries = [(
            "SELECT * FROM records WHERE kind IN ('captures','measurements') ORDER BY id", ()
        )]
        queries += [("SELECT * FROM records WHERE kind=? AND id=?", (kind, record["id"]))
                    for kind, record in snapshots]
        staged = self._stage_record(
            "captures", capture, error="Staged nonlinear capture failed checksum"
        )
        for _attempt in range(8):
            before = self._read_rows(queries)
            seen_captures = {}
            for row in before[0]:
                old = self._record(row)
                if set(old.get("dataset_ids", [])) & set(capture["dataset_ids"]):
                    raise ConflictError("Capture dataset identity is already registered")
                if row["kind"] != "captures":
                    continue
                seen_captures[old["id"]] = old
                if old.get("acquisition_group_id") == capture["acquisition_group_id"]:
                    raise ConflictError("Capture acquisition group is already registered")
                if set(old.get("raw_frame_hashes", [])) & set(capture["raw_frame_hashes"]):
                    raise ConflictError("RAW exposure/path pixels overlap an existing capture")
            for binding in capture["parent_capture_bindings"]:
                parent = seen_captures.get(binding["id"])
                if parent is None or parent["content_hash"] != binding["content_hash"]:
                    raise IntegrityError("Capture parent differs from its verified ancestry")
            for rows, (_, expected) in zip(before[1:], snapshots, strict=True):
                if not rows or self._record(rows[0]) != expected:
                    raise IntegrityError(
                        "Capture pinned assembly/Test/model changed before publication"
                    )
            with self.transaction() as conn:
                if not self._same_read_rows(conn, queries, before):
                    continue
                self._insert_staged_record(conn, staged)
                return capture
        raise ConflictError("Capture registry changed during preparation; retry publication")

    def get(self, kind: str, identity: str) -> dict:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE kind=? AND id=?", (kind, identity)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown {kind}: {identity}")
        return self._record(row)

    def publish_case_review(self, identity, document):
        """Append latest-only revision; large graph work never holds the writer lock."""
        if {"id", "created_at", "content_hash"} & document.keys():
            raise ValueError("Case publication cannot override record identity")
        previous_id = document["previous_revision_id"]
        queries = [("SELECT * FROM records WHERE id=?", (identity,))]
        if previous_id:
            queries.append(("SELECT * FROM records WHERE kind='case_reviews' ORDER BY id", ()))
        staged = None
        for _attempt in range(8):
            before = self._read_rows(queries)
            if before[0]:
                row = before[0][0]
                record = self._record(row)
                if row["kind"] != "case_reviews" or any(
                    record.get(key) != value for key, value in document.items()
                ):
                    raise ConflictError("Case request identity conflicts with frozen contents")
                return record
            if previous_id:
                previous_row = next((r for r in before[1] if r["id"] == previous_id), None)
                if previous_row is None:
                    raise KeyError("Unknown previous case revision: " + previous_id)
                previous = self._record(previous_row)
                if (document["case_id"] != previous["case_id"]
                        or document["revision"] != previous["revision"] + 1):
                    raise ConflictError("Case revision does not continue its previous revision")
                for row in before[1]:
                    item = self._record(row)
                    if (item["case_id"] == previous["case_id"]
                            and item["revision"] >= document["revision"]):
                        raise ConflictError(
                            "Case already has a newer revision; reload before saving"
                        )
            elif document["case_id"] != identity or document["revision"] != 1:
                raise ConflictError("New case must start with its own first revision")
            if staged is None:
                value = {**document, "id": identity, "created_at": now()}
                value["content_hash"] = digest(value)
                staged = self._stage_record("case_reviews", value)
            with self.transaction() as conn:
                if not self._same_read_rows(conn, queries, before):
                    continue
                self._insert_staged_record(conn, staged, audit_details={
                    "content_hash": value["content_hash"], "case_id": document["case_id"],
                    "revision": document["revision"], "previous_revision_id": previous_id,
                })
                return value
        raise ConflictError("Case registry changed during preparation; retry publication")

    def list(self, kind: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM records WHERE kind=? ORDER BY created_at DESC,id", (kind,)
            ).fetchall()
        return [self._record(r) for r in rows]

    def store_blob(self, data: bytes, name: str, media_type: str) -> dict:
        checksum = hashlib.sha256(data).hexdigest()
        # Exclusive creation prevents a partially-written file from being replaced by a writer.
        temporary = self.blobs / f".{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._publish_blob(temporary, checksum)
        finally:
            temporary.unlink(missing_ok=True)
        return self._blob_descriptor(checksum, len(data), name, media_type)

    def store_file(self, source: str | Path, name: str, media_type: str) -> dict:
        """Publish a regular file as a content-addressed blob without buffering it in RAM.

        The temporary copy is hashed while it is written, then linked into the
        content-addressed store atomically. Local filesystem writes remain
        possible, so later reads recheck the digest. The source must remain unchanged
        during copying; a changing file is rejected instead of publishing an
        ambiguous snapshot. Callers still need to bind the returned descriptor
        to a typed owner before the public artifact endpoint will serve it.
        """
        temporary = self.blobs / f".{uuid.uuid4().hex}.tmp"
        checksum = hashlib.sha256()
        byte_count = 0
        try:
            with self._regular_file(Path(source)) as (reader, before):
                with temporary.open("xb") as writer:
                    while chunk := reader.read(_BLOB_CHUNK_BYTES):
                        writer.write(chunk)
                        checksum.update(chunk)
                        byte_count += len(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
                if byte_count != before.st_size or not self._same_file_state(
                    before, os.fstat(reader.fileno())
                ):
                    raise IntegrityError("Source file changed during blob ingestion")
            identity = checksum.hexdigest()
            self._publish_blob(temporary, identity)
        finally:
            temporary.unlink(missing_ok=True)
        return self._blob_descriptor(identity, byte_count, name, media_type)

    @staticmethod
    def _blob_descriptor(checksum: str, byte_count: int, name: str, media_type: str) -> dict:
        return {
            "id": checksum,
            "name": Path(name).name,
            "sha256": checksum,
            "bytes": byte_count,
            "media_type": media_type,
            "url": f"/api/v1/artifacts/{checksum}",
        }

    @staticmethod
    def _same_file_state(before: os.stat_result, after: os.stat_result) -> bool:
        return (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
            before.st_ctime_ns,
        ) == (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            after.st_ctime_ns,
        )

    @staticmethod
    @contextmanager
    def _regular_file(path: Path):
        # O_NOFOLLOW prevents a final-path symlink from turning a blob read
        # into an arbitrary local-file read, including a swap after is_symlink.
        if path.is_symlink():
            raise IntegrityError("Blob source or target is a symlink")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            if path.is_symlink():
                raise IntegrityError("Blob source or target is a symlink") from exc
            raise
        with os.fdopen(descriptor, "rb") as stream:
            initial = os.fstat(stream.fileno())
            if not stat.S_ISREG(initial.st_mode):
                raise IntegrityError("Blob source or target is not a regular file")
            yield stream, initial

    @classmethod
    def _file_digest(cls, path: Path) -> tuple[str, int]:
        checksum = hashlib.sha256()
        byte_count = 0
        with cls._regular_file(path) as (stream, before):
            while chunk := stream.read(_BLOB_CHUNK_BYTES):
                checksum.update(chunk)
                byte_count += len(chunk)
            if byte_count != before.st_size or not cls._same_file_state(
                before, os.fstat(stream.fileno())
            ):
                raise IntegrityError("Blob changed during checksum verification")
        return checksum.hexdigest(), byte_count

    def _publish_blob(self, temporary: Path, checksum: str) -> None:
        target = self.blobs / checksum
        try:
            os.link(temporary, target)
        except FileExistsError:
            actual, _ = self._file_digest(target)
            if actual != checksum:
                raise IntegrityError("Existing blob failed checksum") from None

    def blob_path(self, checksum: str) -> Path:
        if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise KeyError("Invalid artifact ID")
        path = self.blobs / checksum
        try:
            actual, _ = self._file_digest(path)
        except FileNotFoundError:
            raise KeyError("Artifact not found") from None
        if actual != checksum:
            raise IntegrityError("Artifact checksum mismatch")
        return path

    def submit(self, document: dict, cache_key: str, *, force=False) -> dict:
        cache_query = (
            "SELECT * FROM jobs WHERE cache_key=? AND status IN ('queued','running','succeeded') "
            "ORDER BY created_at DESC LIMIT 1", (cache_key,),
        )

        def reuse(cached):
            if self.decode_document(cached["document"]) != document:
                raise ConflictError("Cache key collision with different frozen inputs")
            return {**self._job(cached), "cache_hit": True}

        if not force:
            before = self._read_rows([cache_query])[0]
            if before:
                return reuse(before[0])
        identifier, stamp = "run-" + uuid.uuid4().hex, now()
        checksum, encoded = digest(document), self.encode_document(document)
        with self.transaction() as conn:
            cached = None if force else conn.execute(*cache_query).fetchone()
            if cached is None:
                count = conn.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()[0]
                if count >= 64:
                    raise ConflictError("Worker queue is full (64 runs)")
                conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", (
                    identifier, cache_key, "queued", encoded, checksum, stamp, stamp, None, None,
                ))
                self.index_job(conn, identifier, document, stamp)
                self._audit(conn, "runs.queued", identifier, {"cache_key": cache_key})
        if cached is not None:
            return reuse(cached)
        return {**document, "id": identifier, "status": "queued", "cache_key": cache_key,
                "content_hash": checksum, "created_at": stamp, "updated_at": stamp,
                "result": None, "error": None, "artifacts": []}

    def _job(self, row, completion_events=None):
        return self._decode_job(row, completion_events)[0]

    def _decode_job(self, row, completion_events=None):
        """Verify a job once and retain its original request within this operation."""
        document = self.decode_document(row["document"])
        if digest(document) != row["request_hash"]:
            raise IntegrityError("Frozen run inputs failed checksum")
        result = self.decode_document(row["result"]) if row["result"] else None
        completed_with_result = row["status"] == "succeeded" or (
            row["status"] == "failed" and row["result"] is not None
        )
        if completed_with_result:
            if completion_events is None:
                with self.connection() as conn:
                    event = conn.execute(
                        "SELECT * FROM audit WHERE subject=? AND action=? "
                        "ORDER BY seq DESC LIMIT 1",
                        (row["id"], f"runs.{row['status']}"),
                    ).fetchone()
            else:
                event = completion_events.get(row["id"])
            if event is not None:
                event_value = {
                    k: event[k] for k in ("timestamp", "action", "subject", "previous_hash")
                }
                event_value["details"] = json.loads(event["details"])
                if digest(event_value) != event["event_hash"]:
                    raise IntegrityError("Completion audit event failed checksum")
            if (event is None or event["action"] != f"runs.{row['status']}"
                    or digest(result) != json.loads(event["details"]).get("result_hash")):
                raise IntegrityError("Completed run result failed audit checksum")
        job = {
            **document,
            "id": row["id"],
            "status": row["status"],
            "cache_key": row["cache_key"],
            "content_hash": row["request_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "result": result,
            "error": row["error"],
        }
        return job, document

    def publish_study(self, plan, records, requests):
        """Pre-encode the complete batch, then atomically publish rows, indexes and audit."""
        identity = "study-" + plan["plan_hash"]
        query = ("SELECT * FROM records WHERE kind='studies' AND id=?", (identity,))

        def reuse(row):
            result = self._record(row)
            if any(result[k] != plan[k] for k in ("specification", "source_hashes", "environment")):
                raise ConflictError("Study identity collision")
            return result

        before = self._read_rows([query])[0]
        if before:
            return reuse(before[0])
        staged_records = [self._stage_record(k, r, error="Staged study revision failed checksum")
                          for k, r in records]
        staged_jobs = []
        for request in requests:
            staged_jobs.append(("run-" + uuid.uuid4().hex, now(), request,
                                digest(request), self.encode_document(request)))
        value = {**plan, "id": identity, "created_at": now(),
                 "run_ids": [job[0] for job in staged_jobs]}
        value["content_hash"] = digest(value)
        staged_study = self._stage_record("studies", value)
        with self.transaction() as conn:
            existing = conn.execute(*query).fetchone()
            if existing is None:
                active = conn.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()[0]
                if active + len(staged_jobs) > 64:
                    raise ConflictError(
                        "Insufficient queue capacity for the complete study (64 runs)"
                    )
                for record in staged_records:
                    self._insert_staged_record(conn, record)
                for run_id, stamp, request, checksum, encoded in staged_jobs:
                    conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", (
                        run_id, checksum, "queued", encoded, checksum, stamp, stamp, None, None,
                    ))
                    self.index_job(conn, run_id, request, stamp)
                    self._audit(conn, "runs.queued", run_id,
                                {"cache_key": checksum, "study_id": identity})
                self._insert_staged_record(conn, staged_study)
        return reuse(existing) if existing is not None else value

    def get_run(self, identifier: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {identifier}")
        result = self._job(row)
        result["artifacts"] = (result.get("result") or {}).get("artifacts", [])
        return result

    def get_run_state(self, identifier: str, expected_request_hash: str | None = None) -> dict:
        """Read operational state without claiming to verify request/result evidence.

        Workers can bind this inexpensive cancellation check to the request they
        already verified. Evidence readers must continue to use get_run, and
        finish independently verifies the complete request before publication.
        """
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id,status,request_hash,updated_at,error FROM jobs WHERE id=?",
                (identifier,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {identifier}")
        if expected_request_hash is not None and row["request_hash"] != expected_request_hash:
            raise IntegrityError("Operational run state differs from the expected request hash")
        return {**dict(row), "scope": "operational_state_only_not_evidence_verification"}

    def get_run_with_result_binding(self, identifier: str) -> tuple[dict, dict | None]:
        """Read a run and bind its first result artifact with one verified decode.

        All database inputs come from this read transaction. No decoded owner is
        accepted from a caller or retained between operations. Evidence objects,
        completion proof, index/head, descriptor and artifact bytes are freshly
        verified exactly as in get_run followed by artifact_binding.
        """
        try:
            with self.connection() as conn:
                conn.execute("BEGIN")
                original = conn.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
                if original is None:
                    raise KeyError(f"Unknown run: {identifier}")
                event = conn.execute(
                    "SELECT * FROM audit INDEXED BY audit_subject_action "
                    "WHERE subject=? AND action=? ORDER BY seq DESC LIMIT 1",
                    (identifier, f"runs.{original['status']}"),
                ).fetchone()
                owner, document = self._decode_job(
                    original, {identifier: event} if event else {}
                )
                result = owner.get("result")
                artifacts = (result or {}).get("artifacts", [])
                binding = None
                if owner["status"] == "succeeded" and artifacts:
                    entry = artifacts[0]
                    if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
                        raise IntegrityError("Run result artifact has an invalid descriptor")
                    identity = entry["sha256"]
                    row = self._artifact_index_row(conn, identity, ("runs", identifier))
                    binding = self._bound_artifact(
                        identity, row, original, owner, document, result
                    )
                return {**owner, "artifacts": artifacts}, binding
        except sqlite3.DatabaseError as exc:
            raise IntegrityError("Run artifact binding read index is unavailable") from exc

    def list_runs(self, *, job_kinds=None) -> list[dict]:
        with self.connection() as conn:
            # Jobs and completion proofs must belong to the same read snapshot.
            # Verify each request/result once, without an N+1 connection per job.
            conn.execute("BEGIN")
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC,id").fetchall()
            events = {
                row["subject"]: row
                for row in conn.execute(
                    "SELECT * FROM audit WHERE action IN ('runs.succeeded','runs.failed') "
                    "ORDER BY seq"
                )
            }
        if job_kinds is not None:
            # Select by the verified request before reading large result blobs.
            # This preserves the same snapshot and completion checks for selected jobs.
            selected = []
            for row in rows:
                document = self.decode_document(row["document"])
                if digest(document) != row["request_hash"]:
                    raise IntegrityError("Frozen run inputs failed checksum")
                if document.get("job_kind") in job_kinds:
                    selected.append(row)
            rows = selected
        result = [self._job(row, events) for row in rows]
        for job in result:
            job["artifacts"] = (job.get("result") or {}).get("artifacts", [])
        return result

    def publish_electrical_response(self, document, records, child_requests, *, force=False):
        from .electrical_study_registry import publish

        return publish(self, document, records, child_requests, force=force)

    def cancel_electrical_response(self, identifier):
        from .electrical_study_registry import cancel

        return cancel(self, identifier)

    def claim(self) -> dict | None:
        # Decode/verify immutable input graphs without excluding publishers.
        # The raw selected row is compared again under the writer lock before CAS.
        query = ("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at,id", ())
        for _attempt in range(8):
            before = self._read_rows([query])[0]
            if not before:
                return None
            dependency_queries, dependency_rows = [], []
            for row in before:
                job = self._job(row)
                if job.get("job_kind") == "electrical_response_study":
                    from .electrical_study_registry import TERMINAL, dependencies

                    dependency_queries, dependency_rows = dependencies(self, job)
                    if any(rows[0]["status"] not in TERMINAL for rows in dependency_rows):
                        continue
                else:
                    dependency_queries, dependency_rows = [], []
                break
            else:
                return None
            stamp = now()
            with self.transaction() as conn:
                current = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
                if current is None or tuple(current) != tuple(row):
                    continue
                if not self._same_read_rows(conn, dependency_queries, dependency_rows):
                    continue
                conn.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?",
                             (stamp, row["id"]))
                self._audit(conn, "runs.running", row["id"], {})
            # No fallible database read after commit: retrying BUSY must never
            # lose an already-claimed job and accidentally claim another one.
            return {**job, "status": "running", "updated_at": stamp,
                    "artifacts": (job.get("result") or {}).get("artifacts", [])}
        raise ConflictError("Queued runs changed during claim preparation; retry claim")

    def finish(self, identifier: str, *, result=None, error=None) -> dict:
        if result is None and not error:
            raise ValueError("A successful run must provide an explicit result")
        state = "failed" if error else "succeeded"
        query = ("SELECT * FROM jobs WHERE id=?", (identifier,))
        encoded = checksum = None
        prepared = False
        for _attempt in range(8):
            before = self._read_rows([query])[0]
            if not before:
                raise KeyError(f"Unknown run: {identifier}")
            row = before[0]
            job = self._job(row)
            if row["status"] != "running":
                return {**job, "artifacts": (job.get("result") or {}).get("artifacts", [])}
            request = self.decode_document(row["document"])
            if not prepared:
                encoded = self.encode_document(result) if result is not None else None
                checksum = digest(result) if result is not None else None
                prepared = True
            stamp = now()
            with self.transaction() as conn:
                if not self._same_read_rows(conn, [query], [before]):
                    continue
                conn.execute("UPDATE jobs SET status=?,updated_at=?,result=?,error=? "
                             "WHERE id=? AND status='running'",
                             (state, stamp, encoded, error, identifier))
                self.index_job(conn, identifier, request, row["created_at"], result=result)
                self._audit(conn, f"runs.{state}", identifier,
                            {"result_hash": checksum, "error": error})
            return {**job, "status": state, "updated_at": stamp, "result": result,
                    "error": error, "artifacts": (result or {}).get("artifacts", [])}
        raise ConflictError("Run changed during completion preparation; retry publication")

    def cancel(self, identifier: str) -> dict:
        if self.get_run(identifier).get("job_kind") == "electrical_response_study":
            return self.cancel_electrical_response(identifier)
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status='cancelled',updated_at=? "
                "WHERE id=? AND status IN ('queued','running')",
                (now(), identifier),
            )
            if cursor.rowcount:
                self._audit(conn, "runs.cancelled", identifier, {})
        return self.get_run(identifier)

    def recover_interrupted(self):
        """Call only while holding the exclusive worker process lock."""
        with self.transaction() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM jobs WHERE status='running'")]
            for identifier in ids:
                conn.execute(
                    "UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?",
                    (
                        "Worker interrupted; frozen inputs retained. Submit again to retry.",
                        now(),
                        identifier,
                    ),
                )
                self._audit(conn, "runs.interrupted", identifier, {})

    def audit(self) -> dict:
        with self.connection() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM audit ORDER BY seq")]
        previous = "0" * 64
        for row in rows:
            row["details"] = json.loads(row["details"])
            event = {
                k: row[k] for k in ("timestamp", "action", "subject", "details", "previous_hash")
            }
            if row["previous_hash"] != previous or digest(event) != row["event_hash"]:
                raise IntegrityError("Audit hash chain verification failed")
            previous = row["event_hash"]
        return {
            "integrity": "verified",
            "events": rows,
            "head_hash": previous,
            "scope": "Local tamper evidence; not an externally signed audit log",
        }
