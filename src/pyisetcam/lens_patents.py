"""Lens patent prescription database helpers.

The bundled database is generated from Lens_Patent_DB normalized exports. It is
intended as a CameraE2E optics input catalog, not as a claim that every patent
prescription has been fully sequentially ray traced.
"""

# ruff: noqa: N816

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import warnings
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_DB_NAMES = ("lens_patent_simulation_v9.sqlite", "lens_patent_simulation_v6.sqlite")
DEFAULT_RAYTRACE_PSF_DIR_NAME = "raytrace_psf"
VALIDATED_RAYTRACE_PSF_DIR_NAME = "raytrace_psf_v2"
DEFAULT_COMPANY_SETS_DIR_NAME = "companies"
DEFAULT_CAMERA_E2E_MANIFEST_NAME = "camerae2e_manifest.json"
DEFAULT_LENS_DATA_ROOT_ENV = "PYISETCAM_LENS_DB_ROOT"
DEFAULT_LENS_DB_ENV = "PYISETCAM_LENS_PATENT_DB"
DEFAULT_LENS_PSF_DIR_ENV = "PYISETCAM_LENS_PATENT_PSF_DIR"
DEFAULT_CAMERA_DB_ROOT_ENV = "PYISETCAM_CAMERA_DB_ROOT"


def lens_patent_default_data_dir() -> Path:
    """Return the active lens-patent CameraE2E data directory.

    Resolution order:
    1. explicit ``PYISETCAM_LENS_PATENT_DB`` parent directory,
    2. explicit ``PYISETCAM_LENS_DB_ROOT`` directory,
    3. local RayOptics v9 package if present,
    4. bundled package data.
    """

    env_db_path = os.environ.get(DEFAULT_LENS_DB_ENV)
    if env_db_path:
        return _lens_data_dir(env_db_path)

    for candidate in _candidate_lens_data_dirs():
        if _find_default_db(candidate) is not None:
            return candidate
    return _bundled_lens_data_dir()


def lens_patent_default_db_path() -> Path:
    """Return the default lens patent SQLite database path."""

    env_path = os.environ.get(DEFAULT_LENS_DB_ENV)
    if env_path:
        return Path(env_path).expanduser()

    for candidate in _candidate_lens_data_dirs():
        db_path = _find_default_db(candidate)
        if db_path is not None:
            return db_path
    return _bundled_lens_data_dir() / DEFAULT_DB_NAMES[-1]


def lens_patent_camerae2e_manifest(
    data_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the active CameraE2E lens package manifest."""

    if data_dir is not None:
        root = Path(data_dir).expanduser()
    else:
        root = _lens_data_dir(db_path)
    path = root / DEFAULT_CAMERA_E2E_MANIFEST_NAME
    if not path.exists():
        raise FileNotFoundError(f"CameraE2E lens manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def lens_patent_db_summary(db_path: str | Path | None = None) -> dict[str, Any]:
    """Return generation and status counts for the lens patent database."""

    with _connect(db_path) as con:
        row = con.execute("SELECT value FROM metadata WHERE key = 'summary'").fetchone()
        summary = json.loads(str(row["value"])) if row is not None else _computed_summary(con)
        tables = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "prescription_validation" in tables:
            summary["prescription_validation"] = {
                "valid_structure": con.execute(
                    "SELECT count(*) FROM prescription_validation WHERE valid=1"
                ).fetchone()[0],
                "invalid_structure": con.execute(
                    "SELECT count(*) FROM prescription_validation WHERE valid=0"
                ).fetchone()[0],
                "image_quality_validated": False,
            }
        if "raytrace_asset_validation" in tables:
            summary["raytrace_asset_validation"] = {
                "valid_contract_simulations": con.execute(
                    "SELECT count(DISTINCT simulation_id) FROM raytrace_asset_validation "
                    "WHERE valid_contract=1"
                ).fetchone()[0],
                "scope": "geometric_preview_not_calibrated_IQ",
            }
        return summary


def lens_patent_companies(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return companies represented in the lens patent database."""

    with _connect(db_path) as con:
        rows = con.execute(
            """
            SELECT *
            FROM companies
            ORDER BY company
            """
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def lens_patent_company_sets_manifest(
    company_sets_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the company-specific DB sets manifest."""

    path = _company_sets_dir(company_sets_dir, db_path=db_path) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"Lens patent company sets manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def lens_patent_company_db_path(
    company: str,
    company_sets_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Path:
    """Return the generated company-specific SQLite DB path."""

    requested_slug = _slugify(company)
    manifest = lens_patent_company_sets_manifest(company_sets_dir, db_path=db_path)
    for row in manifest.get("companies", []):
        company_matches = str(row.get("company", "")).lower() == str(company).lower()
        if row.get("company_slug") == requested_slug or company_matches:
            path = _company_sets_dir(company_sets_dir, db_path=db_path) / str(row["db"])
            if not path.exists():
                raise FileNotFoundError(f"Lens patent company DB not found: {path}")
            return path
    raise KeyError(f"No lens patent company DB for company: {company}")


def lens_patent_search(
    company: str | None = None,
    readiness: str | None = None,
    require_camerae2e: bool = False,
    limit: int | None = None,
    db_path: str | Path | None = None,
    *,
    require_raytrace: bool = False,
) -> list[dict[str, Any]]:
    """Search lens configurations, optionally enforcing input validation.

    ``require_camerae2e`` requires usable specifications, an accepted package
    quality row when present, and a structurally valid prescription. It is not
    evidence of measured or diffraction accuracy. ``require_raytrace`` further
    requires a current-contract asset in the active PSF manifest.
    """

    clauses: list[str] = []
    params: list[Any] = []
    if company:
        company_slug = _slugify(company)
        clauses.append("(company_slug = ? OR lower(company) LIKE ?)")
        params.extend([company_slug, f"%{company.lower()}%"])
    if readiness:
        clauses.append("readiness = ?")
        params.append(readiness)
    if require_camerae2e or require_raytrace:
        clauses.append("simulation_status = 'camerae2e_ready'")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT *
        FROM simulation_results
        {where}
        ORDER BY company, publication_number, example_label, configuration
    """
    if limit is not None and not (require_camerae2e or require_raytrace):
        sql += " LIMIT ?"
        params.append(_positive_limit(limit))

    with _connect(db_path) as con:
        rows = con.execute(sql, params).fetchall()
        quality_exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='camerae2e_quality'"
        ).fetchone()
        accepted = (
            None
            if not quality_exists
            else {
                row[0]
                for row in con.execute(
                    "SELECT simulation_id FROM camerae2e_quality WHERE recommended_for_camerae2e=1"
                )
            }
        )
    results = [_simulation_row_to_dict(row, include_optics=False) for row in rows]
    if require_camerae2e or require_raytrace:
        selected = []
        for row in results:
            if accepted is not None and row["simulation_id"] not in accepted:
                continue
            validation = lens_patent_validation(row["simulation_id"], db_path=db_path)
            if validation["valid"]:
                row["prescription_validation"] = validation
                selected.append(row)
        results = selected
    if require_raytrace:
        try:
            current_assets = {
                row["simulation_id"]
                for row in lens_patent_raytrace_psf_search(db_path=db_path)
                if row.get("status") in {"generated", "exists"}
                and int(row.get("contract_version", 0)) == 2
            }
        except FileNotFoundError:
            current_assets = set()
        results = [row for row in results if row["simulation_id"] in current_assets]
    return results if limit is None else results[: _positive_limit(limit)]


def lens_patent_get(
    simulation_id: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return one simulation result row, including parsed optics metadata."""

    with _connect(db_path) as con:
        row = con.execute(
            "SELECT * FROM simulation_results WHERE simulation_id = ?",
            (str(simulation_id),),
        ).fetchone()
    if row is None:
        raise KeyError(f"Unknown lens patent simulation_id: {simulation_id}")
    return _simulation_row_to_dict(row, include_optics=True)


def lens_patent_surfaces(
    lens_id: str,
    configuration: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return normalized surface rows for a lens and optional configuration."""

    clauses = ["lens_id = ?"]
    params: list[Any] = [str(lens_id)]
    if configuration is not None:
        clauses.append("configuration = ?")
        params.append(str(configuration))
    with _connect(db_path) as con:
        rows = con.execute(
            f"""
            SELECT *
            FROM lens_surfaces
            WHERE {" AND ".join(clauses)}
            ORDER BY configuration, surface_order
            """,
            params,
        ).fetchall()
    return [_surface_row_to_dict(row) for row in rows]


def lens_patent_optics(
    simulation_id: str,
    db_path: str | Path | None = None,
    default_f_number: float | None = None,
    *,
    allow_unverified_prescription: bool = False,
) -> dict[str, Any]:
    """Return an explicitly labelled ideal diffraction-limited proxy.

    This function does not trace the patent surfaces or reproduce their
    aberrations. Use ``lens_patent_raytrace_optics`` for a geometric PSF.
    """

    result = lens_patent_get(simulation_id, db_path=db_path)
    validation = lens_patent_validation(simulation_id, db_path=db_path)
    if not validation["valid"] and not allow_unverified_prescription:
        _raise_prescription_error(simulation_id, validation)
    optics = dict(result["optics"])
    if "focal_length_m" not in optics:
        raise ValueError(f"Lens patent simulation has no focal_length_m: {simulation_id}")
    if "f_number" not in optics:
        if default_f_number is None:
            raise ValueError(
                f"Lens patent simulation has no f_number: {simulation_id}; "
                "pass default_f_number to use it as a CameraE2E proxy input"
            )
        optics["f_number"] = float(default_f_number)

    optics.setdefault("model", "diffractionlimited")
    optics.setdefault("compute_method", "opticsotf")
    optics.setdefault("offaxis_method", "cos4th")
    optics.setdefault("name", str(simulation_id))
    optics["focal_length_m"] = float(optics["focal_length_m"])
    optics["f_number"] = float(optics["f_number"])
    if not np.isfinite(optics["focal_length_m"]) or optics["focal_length_m"] <= 0:
        raise ValueError("Proxy focal length must be finite and positive")
    if not np.isfinite(optics["f_number"]) or optics["f_number"] <= 0:
        raise ValueError("Proxy f-number must be finite and positive")
    optics["fidelity"] = "diffraction_limited_proxy"
    optics.setdefault("nominal_focal_length_m", optics["focal_length_m"])
    lens_patent = dict(optics.get("lens_patent", {}))
    lens_patent.update(
        {
            "simulation_id": result["simulation_id"],
            "simulation_model": result["simulation_model"],
            "simulation_status": result["simulation_status"],
            "prescription_applied": False,
            "prescription_validation": validation,
        }
    )
    optics["lens_patent"] = lens_patent
    return optics


def lens_patent_validation(
    simulation_id: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate current source rows instead of trusting a cached ready flag."""
    from .lens_validation import validate_lens_prescription

    row = lens_patent_get(simulation_id, db_path=db_path)
    surfaces = lens_patent_surfaces(row["lens_id"], row["configuration"], db_path=db_path)
    return validate_lens_prescription(
        surfaces,
        focal_length_mm=row["focal_length_mm"],
        f_number=row["f_number"],
        configuration=row["configuration"],
    )


def _raise_prescription_error(simulation_id: str, validation: dict[str, Any]) -> None:
    reasons = "; ".join(
        f"{issue['code']}: {issue['message']}"
        for issue in validation["issues"]
        if issue["severity"] == "error"
    )
    raise ValueError(f"Invalid lens prescription {simulation_id}: {reasons}")


def lens_patent_raytrace_psf_manifest(
    psf_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the generated RayOptics PSF-grid manifest, if present."""

    path = _raytrace_psf_dir(psf_dir, db_path=db_path) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"Lens patent raytrace PSF manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def lens_patent_raytrace_psf_search(
    company: str | None = None,
    status: str | None = None,
    psf_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Search generated RayOptics PSF-grid manifest rows."""

    rows = list(lens_patent_raytrace_psf_manifest(psf_dir, db_path=db_path).get("rows", []))
    if company:
        company_slug = _slugify(company)
        rows = [
            row
            for row in rows
            if _slugify(str(row.get("company", ""))) == company_slug
            or str(row.get("company", "")).lower() == str(company).lower()
        ]
    if status:
        rows = [row for row in rows if row.get("status") == status]
    return rows


def lens_patent_raytrace_psf_path(
    simulation_id: str,
    psf_dir: str | Path | None = None,
    db_path: str | Path | None = None,
) -> Path:
    """Return the `.npz` path for a generated raytrace PSF grid."""

    psf_root = _raytrace_psf_dir(psf_dir, db_path=db_path)
    for row in lens_patent_raytrace_psf_search(psf_dir=psf_root, db_path=db_path):
        is_generated = row.get("status") in {"generated", "exists"}
        if row.get("simulation_id") == simulation_id and is_generated:
            path = psf_root / str(row["file"])
            if not path.exists():
                raise FileNotFoundError(f"Lens patent raytrace PSF file not found: {path}")
            return path
    raise KeyError(f"No generated raytrace PSF grid for simulation_id: {simulation_id}")


def lens_patent_downsample_psf(
    psf_function: np.ndarray,
    target_psf_size: int,
) -> np.ndarray:
    """Downsample a square 4-D PSF grid while preserving each PSF slice sum."""

    target_size = int(target_psf_size)
    if target_size < 1:
        raise ValueError("target_psf_size must be positive")

    psf = np.asarray(psf_function, dtype=float)
    if psf.ndim != 4:
        raise ValueError("PSF function must have shape (psf_y, psf_x, field, wavelength)")
    if psf.shape[0] != psf.shape[1]:
        raise ValueError("Only square PSF grids are supported")
    if not np.all(np.isfinite(psf)) or np.any(psf < 0):
        raise ValueError("PSF samples must be finite and nonnegative")
    if np.any(psf.sum(axis=(0, 1)) <= 0):
        raise ValueError("Every PSF slice must have positive energy")
    if target_size > psf.shape[0]:
        raise ValueError("target_psf_size must not exceed the source PSF size")
    if target_size == psf.shape[0]:
        return psf.copy()

    # Integrate source probability mass over destination pixel areas. This
    # preserves energy even for a noninteger ratio without adding a Gaussian
    # blur or inventing a delta when an interpolation misses sparse samples.
    source_size = psf.shape[0]
    source_lo = np.arange(source_size, dtype=float)
    target_edges = np.linspace(0.0, source_size, target_size + 1)
    weights = np.maximum(
        0.0,
        np.minimum(target_edges[1:, None], source_lo[None, :] + 1.0)
        - np.maximum(target_edges[:-1, None], source_lo[None, :]),
    )
    return np.einsum("ai,ijhw,bj->abhw", weights, psf, weights, optimize=True)


def lens_patent_raytrace_optics(
    simulation_id: str,
    psf_dir: str | Path | None = None,
    target_psf_size: int | None = None,
    db_path: str | Path | None = None,
    *,
    allow_legacy: bool = False,
    allow_approximate_radiometry: bool = False,
) -> dict[str, Any]:
    """Load a checked geometric PSF with explicit fidelity assumptions.

    Old unversioned assets require ``allow_legacy=True`` for reproduction.
    Current assets with unknown RI/transmission require the separate explicit
    ``allow_approximate_radiometry=True`` option; this is not a calibrated RAW
    irradiance model. Source prescription identity is checked before loading.
    """

    path = lens_patent_raytrace_psf_path(simulation_id, psf_dir, db_path=db_path)
    with np.load(path, allow_pickle=False) as data:
        optics = json.loads(str(data["optics_json"]))
        raytrace = dict(optics.get("raytrace", {}))
        identity = optics.get("lens_patent_raytrace_psf", {}).get("simulation_id")
        if identity != simulation_id:
            raise ValueError(f"PSF identity mismatch: requested {simulation_id}, asset {identity}")
        current_contract = int(raytrace.get("contract_version", 0)) == 2
        if not current_contract:
            if not allow_legacy:
                raise ValueError(
                    f"Legacy PSF {simulation_id} has no validated physical contract. "
                    "Regenerate with tools/build_lens_patent_raytrace_psf_grid.py; "
                    "allow_legacy=True is only for explicit historical reproduction."
                )
            warnings.warn(
                f"Loading unvalidated legacy geometric PSF {simulation_id}; "
                "prescription, aperture, sampling and radiometry are not certified.",
                UserWarning,
                stacklevel=2,
            )
            optics["fidelity"] = "legacy_geometric_unverified"
        else:
            validation = lens_patent_validation(simulation_id, db_path=db_path)
            if not validation["valid"]:
                _raise_prescription_error(simulation_id, validation)
            expected_hash = raytrace.get("source_prescription_sha256")
            if expected_hash != validation["prescription_sha256"]:
                raise ValueError(
                    f"PSF prescription fingerprint mismatch for {simulation_id}; "
                    "regenerate the asset"
                )
            raytrace["prescription_validation"] = validation
            raytrace["physics_policy"] = "strict"
            optics["fidelity"] = "geometric"
            if raytrace.get("radiometry_status") != "validated":
                if not allow_approximate_radiometry:
                    raise ValueError(
                        "This geometric PSF has no validated relative illumination/transmission. "
                        "Use allow_approximate_radiometry=True for a labelled geometric preview."
                    )
                raytrace["radiometry_assumption_accepted"] = True
                raytrace["radiometry_assumptions"] = [
                    "Unity relative illumination and spectral transmission are placeholders",
                    "Pupil clipping throughput is diagnostic, not calibrated irradiance",
                ]
        psf_function = np.asarray(data["psf_function"], dtype=float)
        if psf_function.ndim != 4 or not np.all(np.isfinite(psf_function)):
            raise ValueError("PSF must be a finite (y,x,field,wavelength) array")
        if np.any(psf_function < 0) or np.any(psf_function.sum(axis=(0, 1)) <= 0):
            raise ValueError("Every PSF must be nonnegative with positive energy")
        sample_spacing_mm = np.asarray(data["sample_spacing_mm"], dtype=float)
        if not np.all(np.isfinite(sample_spacing_mm)) or np.any(sample_spacing_mm <= 0):
            raise ValueError("PSF sample spacing must be finite and positive")
        source_psf_shape = psf_function.shape
        if target_psf_size is not None:
            psf_function = lens_patent_downsample_psf(psf_function, target_psf_size)
            sample_spacing_mm = sample_spacing_mm * (source_psf_shape[0] / int(target_psf_size))
            raytrace["source_psf_shape"] = list(source_psf_shape)
            raytrace["source_sample_spacing_mm"] = np.asarray(
                data["sample_spacing_mm"],
                dtype=float,
            ).tolist()
            raytrace["target_psf_size"] = int(target_psf_size)
        if "build_settings_json" in data.files:
            raytrace["build_settings"] = json.loads(str(data["build_settings_json"]))
        raytrace["geometry"] = {
            **dict(raytrace.get("geometry", {})),
            "function": np.asarray(data["geometry_function"], dtype=float),
            "field_height_mm": np.asarray(data["field_height_mm"], dtype=float),
            "wavelength_nm": np.asarray(data["wavelength_nm"], dtype=float),
        }
        raytrace["relative_illumination"] = {
            **dict(raytrace.get("relative_illumination", {})),
            "function": np.asarray(data["relative_illumination_function"], dtype=float),
            "field_height_mm": np.asarray(data["field_height_mm"], dtype=float),
            "wavelength_nm": np.asarray(data["wavelength_nm"], dtype=float),
        }
        raytrace["psf"] = {
            **dict(raytrace.get("psf", {})),
            "function": psf_function,
            "field_height_mm": np.asarray(data["field_height_mm"], dtype=float),
            "wavelength_nm": np.asarray(data["wavelength_nm"], dtype=float),
            "sample_spacing_mm": sample_spacing_mm,
        }
        for key in (
            "pupil_throughput_fraction",
            "image_direction_cosines",
            "pupil_weights",
            "pupil_coordinates",
            "chief_direction_cosines",
            "chief_ray_angle_deg",
            "retained_energy_fraction",
        ):
            if key in data.files:
                raytrace[key] = np.asarray(data[key]).copy()
        if current_contract:
            raytrace["psf"].setdefault("coordinate_convention", "pixel_centers")
            for key in ("psf", "geometry", "relative_illumination"):
                raytrace[key].setdefault("field_coordinate", "ideal_image_height")
        optics["raytrace"] = raytrace
        transmittance = dict(optics.get("transmittance", {}))
        if "wave" in transmittance:
            transmittance["wave"] = np.asarray(transmittance["wave"], dtype=float)
        if "scale" in transmittance:
            transmittance["scale"] = np.asarray(transmittance["scale"], dtype=float)
        optics["transmittance"] = transmittance
    optics["model"] = "raytrace"
    optics.setdefault("compute_method", "opticspsf")
    optics.setdefault("offaxis_method", "skip")
    optics["raytrace"]["asset_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    if current_contract:
        from .optics import validate_raytrace_contract

        validate_raytrace_contract(optics)
        _validate_pupil_samples(optics["raytrace"])
    return optics


def _validate_pupil_samples(raytrace: dict[str, Any]) -> None:
    """Check optional geometric direction data before sensor-side reuse."""
    if "pupil_weights" not in raytrace and "image_direction_cosines" not in raytrace:
        return
    weights = np.asarray(raytrace.get("pupil_weights", []), dtype=float)
    directions = np.asarray(raytrace.get("image_direction_cosines", []), dtype=float)
    psf = np.asarray(raytrace["psf"]["function"])
    if weights.ndim != 3 or weights.shape[:2] != psf.shape[2:]:
        raise ValueError("Pupil weights must match the field/wavelength axes")
    if directions.shape != (*weights.shape, 3):
        raise ValueError("Pupil direction shape does not match pupil weights")
    if not np.all(np.isfinite(weights)) or np.any(weights < 0):
        raise ValueError("Pupil weights must be finite and nonnegative")
    if not np.allclose(weights.sum(axis=-1), 1.0, atol=1e-8, rtol=1e-8):
        raise ValueError("Conditional pupil weights must sum to one")
    admitted = directions[weights > 0]
    if not np.all(np.isfinite(admitted)) or not np.allclose(
        np.linalg.norm(admitted, axis=-1), 1.0, atol=1e-7, rtol=1e-7
    ):
        raise ValueError("Admitted pupil directions must be finite unit vectors")


def _connect(db_path: str | Path | None) -> sqlite3.Connection:
    path = Path(db_path).expanduser() if db_path is not None else lens_patent_default_db_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Lens patent database not found: {path}. "
            "Run tools/build_lens_patent_simulation_db.py to generate it."
        )
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _bundled_lens_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data" / "lens_patents"


def _candidate_lens_data_dirs() -> list[Path]:
    candidates: list[Path] = []
    env_root = os.environ.get(DEFAULT_LENS_DATA_ROOT_ENV)
    if env_root:
        candidates.extend(_normalize_lens_data_root(Path(env_root).expanduser()))
    camera_db_root = os.environ.get(DEFAULT_CAMERA_DB_ROOT_ENV)
    if camera_db_root:
        candidates.extend(_normalize_camera_db_lens_root(Path(camera_db_root).expanduser()))

    home = Path.home()
    for root in (_in_repo_camera_db_root(), _sibling_camera_db_root()):
        candidates.extend(_normalize_camera_db_lens_root(root))
    candidates.extend(
        [
            home / "RayOptics" / "CameraE2E_Lens_DB_v9_20260627" / "data" / "lens_patents",
            home / "RayOptics" / "Lens_DB_portable_v9_20260623" / "data" / "lens_patents",
            _bundled_lens_data_dir(),
        ]
    )

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            deduped.append(candidate)
            seen.add(key)
    return deduped


def _normalize_lens_data_root(root: Path) -> list[Path]:
    return [
        root,
        root / "data" / "lens_patents",
        root / "CameraE2E_Lens_DB_v9_20260627" / "data" / "lens_patents",
        root / "Lens_DB_portable_v9_20260623" / "data" / "lens_patents",
    ]


def _normalize_camera_db_lens_root(root: Path) -> list[Path]:
    return [
        root / "lens_db" / "CameraE2E_Lens_DB_v9_20260627" / "data" / "lens_patents",
        root / "lens_db" / "CameraE2E_Lens_DB_v9_20260627",
        root / "lens_db",
        *_normalize_lens_data_root(root),
    ]


def _in_repo_camera_db_root() -> Path:
    return Path(__file__).resolve().parents[2] / "camerae2e_db"


def _sibling_camera_db_root() -> Path:
    return Path(__file__).resolve().parents[2].parent / "CameraE2E-DB"


def _find_default_db(data_dir: Path) -> Path | None:
    for db_name in DEFAULT_DB_NAMES:
        path = data_dir / db_name
        if path.exists():
            return path
    return None


def _lens_data_dir(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        parent = Path(db_path).expanduser().parent
        return parent.parent.parent if parent.parent.name == "companies" else parent
    return lens_patent_default_data_dir()


def _raytrace_psf_dir(psf_dir: str | Path | None, db_path: str | Path | None = None) -> Path:
    if psf_dir is not None:
        return Path(psf_dir).expanduser()

    env_psf_dir = os.environ.get(DEFAULT_LENS_PSF_DIR_ENV)
    if env_psf_dir:
        return Path(env_psf_dir).expanduser()

    root = _lens_data_dir(db_path)
    if (root / VALIDATED_RAYTRACE_PSF_DIR_NAME / "manifest.json").exists():
        return root / VALIDATED_RAYTRACE_PSF_DIR_NAME
    return root / DEFAULT_RAYTRACE_PSF_DIR_NAME


def _company_sets_dir(
    company_sets_dir: str | Path | None,
    db_path: str | Path | None = None,
) -> Path:
    if company_sets_dir is not None:
        return Path(company_sets_dir).expanduser()
    return _lens_data_dir(db_path) / DEFAULT_COMPANY_SETS_DIR_NAME


def _computed_summary(con: sqlite3.Connection) -> dict[str, Any]:
    def one(query: str) -> int:
        return int(con.execute(query).fetchone()[0])

    return {
        "companies": one("SELECT count(*) FROM companies"),
        "lenses": one("SELECT count(*) FROM lenses"),
        "surfaces": one("SELECT count(*) FROM lens_surfaces"),
        "simulation_results": one("SELECT count(*) FROM simulation_results"),
    }


def _simulation_row_to_dict(row: sqlite3.Row, include_optics: bool) -> dict[str, Any]:
    payload = _row_to_dict(row)
    notes = str(payload.get("notes", ""))
    payload["notes"] = [note.strip() for note in notes.split(";") if note.strip()]
    if include_optics:
        payload["optics"] = json.loads(str(payload.get("optics_json") or "{}"))
    payload.pop("optics_json", None)
    return payload


def _surface_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload = _row_to_dict(row)
    payload["coefficients"] = json.loads(str(payload.get("coefficients_json") or "{}"))
    payload["raw"] = json.loads(str(payload.get("raw_json") or "{}"))
    payload.pop("coefficients_json", None)
    payload.pop("raw_json", None)
    payload["is_aspheric"] = bool(payload["is_aspheric"])
    return payload


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _positive_limit(limit: int) -> int:
    parsed = int(limit)
    if parsed <= 0:
        raise ValueError("limit must be positive")
    return parsed


def _slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
    return "-".join(part for part in cleaned.split())


lensPatentDefaultDBPath = lens_patent_default_db_path
lensPatentDefaultDataDir = lens_patent_default_data_dir
lensPatentCameraE2EManifest = lens_patent_camerae2e_manifest
lensPatentDBSummary = lens_patent_db_summary
lensPatentCompanies = lens_patent_companies
lensPatentCompanySetsManifest = lens_patent_company_sets_manifest
lensPatentCompanyDBPath = lens_patent_company_db_path
lensPatentDownsamplePSF = lens_patent_downsample_psf
lensPatentSearch = lens_patent_search
lensPatentGet = lens_patent_get
lensPatentSurfaces = lens_patent_surfaces
lensPatentOptics = lens_patent_optics
lensPatentValidation = lens_patent_validation
lensPatentRaytracePSFManifest = lens_patent_raytrace_psf_manifest
lensPatentRaytracePSFSearch = lens_patent_raytrace_psf_search
lensPatentRaytracePSFPath = lens_patent_raytrace_psf_path
lensPatentRaytraceOptics = lens_patent_raytrace_optics
