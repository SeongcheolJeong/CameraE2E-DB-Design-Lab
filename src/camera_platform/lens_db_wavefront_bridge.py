"""Research-only bridge from a pinned Lens DB prescription to OPD/PSF.

The reviewed CSV and the imported SQLite revision must describe the *same*
prescription.  The source table's effective diameter is used as a hard
clear-aperture proxy only under an explicit research hypothesis.  This is a
reproducible calculation input, not a measured aperture or camera-performance
claim.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import sqlite3
from pathlib import Path

import numpy as np

from pyisetcam._catalog_store import file_hash
from pyisetcam.lens_patents import lens_patent_surfaces
from pyisetcam.lens_validation import normalize_lens_surfaces, validate_lens_prescription

from . import lens_factory, optics_surfaces, prescription_psf, pupil_transmission
from .lens_catalog_bridge import LensCatalogBridgeError, _asset, _revision_source
from .lens_patent_source_review import reviewed_source_semantics

SCHEMA = "camerae2e_db_lens_wavefront_research_v1"
APERTURE_HYPOTHESIS = "effective_diameter_as_hard_clear_aperture_proxy"
DISPERSION_HYPOTHESIS = "opticalglass_ModelGlass_nd_vd_Buchdahl2_estimate"
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class LensDBWavefrontError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise LensDBWavefrontError(code, message)


def _reviewed_csv(db_path: Path, relative: str, expected_sha256: str) -> tuple[Path, str, bytes]:
    _require(
        isinstance(expected_sha256, str) and bool(_HASH.fullmatch(expected_sha256)),
        "csv_hash_invalid",
        "Pin reviewed CSV SHA-256",
    )
    _require(isinstance(relative, str), "csv_path_invalid", "Reviewed CSV path is missing")
    path_part = Path(relative)
    _require(
        not path_part.is_absolute()
        and ".." not in path_part.parts
        and len(path_part.parts) >= 4
        and path_part.parts[-2] == "configured_surfaces"
        and path_part.suffix == ".csv",
        "csv_path_invalid",
        "Require a relative reviewed configured-surfaces CSV",
    )
    parent = db_path.parent / "reviewed_prescriptions"
    path = parent / path_part
    _require(
        not parent.is_symlink()
        and all(
            not (parent.joinpath(*path_part.parts[:i])).is_symlink()
            for i in range(1, len(path_part.parts) + 1)
        )
        and path.is_file()
        and path.resolve().is_relative_to(parent.resolve())
        and path.stat().st_size <= 2 * 1024 * 1024,
        "csv_unavailable",
        "Reviewed CSV must be a bounded local file under the pinned Lens DB",
    )
    source_bytes = path.read_bytes()
    actual = hashlib.sha256(source_bytes).hexdigest()
    _require(actual == expected_sha256, "csv_hash_mismatch", "Reviewed CSV bytes changed")
    return path, actual, source_bytes


def _block(code: str, message: str, surface_order: int | None = None) -> dict:
    issue = {"code": code, "message": message}
    if surface_order is not None:
        issue["surface_order"] = surface_order
    return issue


def _is_terminal_caption(row: dict, raw: dict) -> bool:
    """Match the legacy generator's narrow omission, not every `image` row."""
    return (
        row["surface_kind"] == "image"
        and str(raw.get("surface_label", "")).strip().lower()
        in {"image", "image plane", "ima", "im"}
        and row["radius_mm"] in (None, 0.0)
        and row["thickness_mm"] in (None, 0.0)
        and row["nd"] in (None, 1.0)
    )


def _indices(row: dict, wavelengths: list[float]) -> list[float]:
    nd, vd = row["nd"], row["vd"]
    if nd is None or nd == 1.0:
        _require(
            not row["material"] or row["material"].lower() == "air",
            "material_unresolved",
            "Named medium lacks an explicit nd/Vd or catalog model",
        )
        return [1.0] * len(wavelengths)
    _require(
        math.isfinite(nd) and 1.0 < nd <= 3.0 and vd is not None and math.isfinite(vd) and vd > 0,
        "material_unresolved",
        "Dispersive medium requires finite nd and Vd",
    )
    try:
        from opticalglass.modelglass import ModelGlass
    except ImportError as exc:
        raise LensDBWavefrontError(
            "dispersion_backend_unavailable", "Install the pinned opticalglass ModelGlass backend"
        ) from exc
    glass = ModelGlass(nd, vd, "")
    indices = [float(glass.rindex(wavelength)) for wavelength in wavelengths]
    _require(
        all(math.isfinite(value) and 1.0 <= value <= 3.0 for value in indices),
        "material_model_invalid",
        "nd/Vd estimate returned an invalid spectral index",
    )
    return indices


def _surface(row: dict, wavelengths: list[float]) -> dict:
    order = row["surface_order"]
    radius, gap, aperture = (row["radius_mm"], row["thickness_mm"], row["effective_aperture_mm"])
    _require(
        isinstance(order, int)
        and order > 0
        and gap is not None
        and math.isfinite(gap)
        and 1e-5 <= gap <= 5000,
        "surface_gap_unresolved",
        f"S{order}: finite positive gap after this interface is required",
    )
    _require(
        aperture is not None and math.isfinite(aperture) and aperture > 0,
        "aperture_unknown",
        f"S{order}: source has no effective aperture; do not invent one",
    )
    _require(
        radius is not None and math.isfinite(radius),
        "radius_unresolved",
        f"S{order}: explicit finite radius or plane token is required",
    )
    terms = {int(key[1:]): value for key, value in row["coefficients"].items() if value != 0}
    _require(
        all(degree in {4, 6, 8, 10, 12} for degree in terms),
        "asphere_order_unsupported",
        f"S{order}: source has profile terms outside the exact A4/A6/A8/A10/A12 contract",
    )
    is_asphere = bool(terms or row["conic"] not in (None, 0.0))
    _require(
        not is_asphere or radius != 0.0,
        "asphere_base_unsupported",
        f"S{order}: powered asphere requires a nonzero vertex radius",
    )
    if row["surface_kind"] == "stop":
        _require(
            radius == 0.0 and not is_asphere,
            "stop_unsupported",
            f"S{order}: stop must be an unpowered plane",
        )
    surface = {
        "id": f"db-surface-{order}-{row['surface_key']}",
        "profile": "even_asphere" if is_asphere else "plane" if radius == 0 else "spherical",
        "radius_mm": None if radius == 0 else float(radius),
        "thickness_mm": float(gap),
        "semi_diameter_mm": float(aperture / 2),
        "refractive_index_after": _indices(row, wavelengths),
        "is_stop": row["surface_kind"] == "stop",
    }
    if is_asphere:
        use_a12 = 12 in terms
        use_a10 = 10 in terms
        orders = [4, 6, 8, 10, 12] if use_a12 else [4, 6, 8, 10] if use_a10 else [4, 6, 8]
        surface["asphere"] = {
            "schema": (
                "camera_even_asphere_v3" if use_a12 else
                "camera_even_asphere_v2" if use_a10 else "camera_even_asphere_v1"
            ),
            "conic_constant": float(row["conic"] or 0.0),
            "orders": orders,
            "coefficients": [float(terms.get(degree, 0.0)) for degree in orders],
            "coefficient_units": [f"mm^-{degree - 1}" for degree in orders],
        }
    return surface


def _paraxial_diagnostic(
    request: dict, revision: dict, snapshot: dict, db_path: Path, root: Path, source_sha256: str
) -> dict:
    """Build one RayOptics paraxial model; this does not calculate OPD or PSF."""
    waves = request["prescription"]["wavelength_nm"]
    wavelength_index = min(range(len(waves)), key=lambda i: abs(waves[i] - 550.0))
    wavelength = waves[wavelength_index]
    try:
        model = prescription_psf._model(request, wavelength_index)
        fod = model["analysis_results"]["parax_data"].fod
        efl, radius = float(fod.efl), float(abs(fod.enp_radius))
        _require(
            math.isfinite(efl) and math.isfinite(radius) and efl > 0 and radius > 0,
            "paraxial_invalid",
            "RayOptics returned invalid paraxial lens values",
        )
    except (ValueError, RuntimeError, ImportError) as exc:
        return {
            "status": "unavailable",
            "reason": str(exc),
            "not_diffraction": True,
            "physical_validation": "not_established",
        }
    result = {
        "status": "computed_paraxial_only",
        "wavelength_nm": wavelength,
        "effective_focal_length_mm": efl,
        "entrance_pupil_radius_mm": radius,
        "effective_f_number": efl / (2 * radius),
        "comparison_to_saved_geometric_psf": {"status": "not_available"},
        "not_diffraction": True,
        "physical_validation": "not_established",
    }
    try:
        _sim_id, asset_path, check, _artifact = _asset(revision, snapshot, db_path, root)
    except LensCatalogBridgeError as exc:
        result["comparison_to_saved_geometric_psf"] = {
            "status": "unavailable",
            "reason_code": exc.code,
        }
        return result
    with np.load(asset_path, allow_pickle=False) as stored:
        quality = json.loads(str(stored["quality_json"]))
    _require(
        quality["source_prescription_sha256"] == source_sha256,
        "geometric_reference_mismatch",
        "Saved geometric PSF does not match the current prescription",
    )
    reference_efl = float(quality["actual_efl_mm"])
    reference_radius = float(quality["entrance_pupil_radius_mm"])
    result["comparison_to_saved_geometric_psf"] = {
        "status": "compared_paraxial_only",
        "asset_sha256": check["asset_sha256"],
        "saved_effective_focal_length_mm": reference_efl,
        "saved_entrance_pupil_radius_mm": reference_radius,
        "focal_length_delta_mm": efl - reference_efl,
        "entrance_pupil_radius_delta_mm": radius - reference_radius,
        "entrance_pupil_radius_relative_delta": radius / reference_radius - 1,
        "pupil_spec_difference": (
            "Saved geometric generator uses declared image f-number; "
            "OPD builder maps entrance pupil to the source stop semi-diameter. "
            "Equal EFL does not establish equal pupil support or PSF."
        ),
    }
    return result


def db_wavefront_preflight(
    revision_id: str,
    *,
    catalog_root: str | Path,
    reviewed_csv_sha256: str,
    aperture_hypothesis: str,
    wavelength_nm: tuple[float, ...] = (450.0, 550.0, 650.0),
    field_x_mm: tuple[float, ...] = (-0.25, 0.25),
    field_y_mm: tuple[float, ...] = (-0.25, 0.25),
    pixel_pitch_um: float = 3.0,
    pupil_samples: int = 16,
    kernel_size: int = 5,
    subpixel_samples: int = 5,
) -> dict:
    """Bind one DB revision and return a request only if no fact was invented.

    ``aperture_hypothesis`` is mandatory because a tabulated effective diameter
    is not a verified physical clear aperture.  Source reviews may establish
    the column's units and terminal-row role but do not establish clipping.
    """
    _require(
        aperture_hypothesis == APERTURE_HYPOTHESIS,
        "aperture_hypothesis_required",
        "Explicitly select the effective-diameter clear-aperture proxy for research use",
    )
    _require(
        isinstance(revision_id, str) and bool(revision_id), "revision_invalid", "Pin a revision"
    )
    _require(
        isinstance(wavelength_nm, (tuple, list))
        and all(type(value) in (int, float) for value in wavelength_nm),
        "wavelength_invalid",
        "Declare numeric exact wavelengths",
    )
    waves = [float(value) for value in wavelength_nm]
    _require(
        2 <= len(waves) <= 16
        and all(math.isfinite(value) and 350 <= value <= 1100 for value in waves)
        and all(right > left for left, right in zip(waves[:-1], waves[1:], strict=True)),
        "wavelength_invalid",
        "Declare 2–16 ordered exact wavelengths in 350–1100 nm",
    )
    _require(
        isinstance(field_x_mm, (tuple, list)) and isinstance(field_y_mm, (tuple, list)),
        "field_invalid",
        "Declare explicit Cartesian field axes",
    )
    axes = [list(field_x_mm), list(field_y_mm)]
    _require(
        all(
            2 <= len(axis) <= 5
            and all(type(v) in (int, float) and math.isfinite(v) for v in axis)
            and all(b > a for a, b in zip(axis[:-1], axis[1:], strict=True))
            and axis[0] <= 0 <= axis[-1]
            for axis in axes
        )
        and len(axes[0]) * len(axes[1]) <= 25,
        "field_invalid",
        "Declare bounded sensor-centered increasing Cartesian field axes",
    )
    _require(
        type(pixel_pitch_um) in (int, float)
        and math.isfinite(pixel_pitch_um)
        and 0.2 <= pixel_pitch_um <= 20.0,
        "pixel_pitch_invalid",
        "Pixel pitch must be a finite 0.2–20 um number",
    )
    root = Path(catalog_root).resolve()
    revision, snapshot, db_path, db_sha256 = _revision_source(revision_id, root)
    identity = snapshot["identity"]
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        simulations = conn.execute(
            """SELECT s.* FROM simulation_results s JOIN lenses l ON l.lens_id=s.lens_id
            WHERE l.publication_number=? AND l.prescription_id=? AND s.configuration=?""",
            (
                identity["publication_number"],
                identity["prescription_id"],
                snapshot["configuration"],
            ),
        ).fetchall()
    _require(len(simulations) == 1, "simulation_ambiguous", "Revision must bind one DB simulation")
    simulation = dict(simulations[0])
    _require(
        simulation["simulation_status"] == "camerae2e_ready",
        "simulation_not_ready",
        "Source simulation has no reviewed CameraE2E prescription",
    )
    csv_path, csv_sha256, csv_bytes = _reviewed_csv(
        db_path, simulation["source_surfaces_csv"], reviewed_csv_sha256
    )
    csv_rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"), newline="")))
    db_rows = lens_patent_surfaces(
        simulation["lens_id"], simulation["configuration"], db_path=db_path
    )
    kwargs = {
        "focal_length_mm": simulation["focal_length_mm"],
        "f_number": simulation["f_number"],
        "configuration": simulation["configuration"],
    }
    csv_validation = validate_lens_prescription(csv_rows, **kwargs)
    db_validation = validate_lens_prescription(db_rows, **kwargs)
    _require(
        csv_validation["valid"]
        and db_validation["valid"]
        and csv_validation["prescription_sha256"] == db_validation["prescription_sha256"],
        "prescription_mismatch",
        "Reviewed CSV and pinned SQLite revision do not describe the same valid prescription",
    )
    _require(
        file_hash(db_path) == db_sha256,
        "source_hash_mismatch",
        "Lens DB changed while resolving the reviewed prescription",
    )
    rows = normalize_lens_surfaces(csv_rows)
    objects = [row for row in rows if row["surface_kind"] == "object"]
    _require(
        not objects
        or (len(objects) == 1 and rows[0] is objects[0]
            and objects[0]["thickness_state"] == "infinite"),
        "object_conjugate_unsupported",
        "The infinity-only OPD bridge requires a single leading object row with "
        "explicit infinite distance; finite, unknown, or misplaced object rows "
        "need a separately declared finite-conjugate contract",
    )
    source_review = reviewed_source_semantics(revision_id, csv_sha256, rows)
    sequential = [
        row
        for row, raw in zip(rows, csv_rows, strict=True)
        if row["surface_kind"] != "object" and not _is_terminal_caption(row, raw)
    ]
    max_height = simulation["image_height_mm"]
    _require(
        type(max_height) in (int, float) and math.isfinite(max_height) and max_height > 0,
        "field_extent_unknown",
        "Source has no positive reviewed image-height field extent",
    )
    corner = math.hypot(max(abs(float(v)) for v in axes[0]), max(abs(float(v)) for v in axes[1]))
    _require(
        corner <= max_height,
        "field_outside_source",
        "Cartesian field corner exceeds DB image-height support",
    )
    blockers: list[dict] = []
    if len(sequential) > optics_surfaces.MAX_SURFACES:
        blockers.append(
            _block(
                "surface_budget_exceeded",
                f"Source has {len(sequential)} sequential rows; Factory limit is "
                f"{optics_surfaces.MAX_SURFACES}",
            )
        )
    surfaces = []
    for row in sequential:
        try:
            surfaces.append(_surface(row, waves))
        except LensDBWavefrontError as exc:
            blockers.append(_block(exc.code, str(exc), row["surface_order"]))
    if len(sequential) != len(surfaces):
        surfaces = []  # Never return a partially converted optical prescription.
    if sum(row["surface_kind"] == "stop" for row in sequential) != 1:
        blockers.append(_block("stop_unsupported", "Require exactly one explicit stop"))
    provenance = {
        "revision_id": revision_id,
        "revision_content_sha256": revision["content_sha256"],
        "source_database_sha256": db_sha256,
        "simulation_id": simulation["simulation_id"],
        "reviewed_csv_relative_path": csv_path.relative_to(db_path.parent).as_posix(),
        "reviewed_csv_sha256": csv_sha256,
        "prescription_sha256": csv_validation["prescription_sha256"],
    }
    warning_codes = sorted(
        {issue["code"] for issue in csv_validation["issues"] if issue["severity"] == "warning"}
    )
    source_profile_orders = sorted(
        {
            int(key[1:])
            for row in sequential
            for key, value in row["coefficients"].items()
            if value != 0
        }
    )
    a10_surface_orders = [
        row["surface_order"] for row in sequential if row["coefficients"].get("a10", 0) != 0
    ]
    evidence = {
        "schema": SCHEMA,
        "status": "blocked" if blockers else "research_request_ready",
        "source": provenance,
        "source_surface_count": len(rows),
        "sequential_surface_count": len(sequential),
        "translation_audit": {
            "converted_surface_count": len(surfaces),
            "nonzero_profile_orders": source_profile_orders,
            "a10_surface_orders": a10_surface_orders,
            "retained_numbered_image_surface_orders": [
                row["surface_order"] for row in sequential if row["surface_kind"] == "image"
            ],
            "stop_surface_orders": [
                row["surface_order"] for row in sequential if row["surface_kind"] == "stop"
            ],
        },
        "source_validation_warning_codes": warning_codes,
        "source_semantics_review": source_review,
        "source_warning_resolution": {
            "aperture_convention_unverified": (
                "Patent heading confirms effective diameter in mm for this pinned table; "
                "whether it is a physical clear aperture remains unverified."
            )
        } if source_review is not None else {},
        "assumptions": [
            APERTURE_HYPOTHESIS,
            DISPERSION_HYPOTHESIS,
            "numbered_image_rows_with_finite_gap_follow_legacy_sequential_interpretation"
            if source_review is None else
            "source_reviewed_numbered_terminal_interface_before_image_plane",
            "unity_stop_power_is_an_ideal_lossless_hypothesis",
        ],
        "limitations": [
            "No verified physical clear-aperture clipping, glass dispersion, "
            "or absolute transmission",
            "No measured OPD, diffraction accuracy, absolute radiometry, CRA/pixel angular "
            "coupling, or product ranking",
        ],
        "physical_validation": "not_established",
        "product_ranking_allowed": False,
        "blockers": blockers,
        "psf_request": None,
        "directional_radiometry_preflight": {"status": "not_evaluated"},
        "paraxial_translation_diagnostic": None,
        "next_evidence_needed": [
            "Verify physical clear apertures and glass dispersion beyond the patent table",
            "Improve the Factory's full rear-plane angular sampler or prove a bounded "
            "accepted-ray domain without dropping the terminal interface",
            "Establish OPD, PSF, radiometry and angular-sensor numerical convergence",
        ],
    }
    if blockers:
        return evidence
    focal_length = float(simulation["focal_length_mm"])
    _require(
        0.5 <= focal_length <= 500 and math.isfinite(focal_length),
        "focal_length_unsupported",
        "Source focal length exceeds bounded Factory contract",
    )
    prescription = lens_factory.request_template(version=4)
    prescription["wavelength_nm"] = waves
    prescription["surfaces"] = surfaces
    prescription["image_height_mm"] = [0.0, corner / 2, corner]
    prescription["field_radius"] = [0.0, 0.5, 1.0]
    prescription["stop_transmission"] = pupil_transmission.uniform_transmission(prescription)
    request = prescription_psf.request_template(version=4)
    request["prescription"] = prescription
    request["reference_focal_length_mm"] = focal_length
    request["field_x_mm"] = [float(v) for v in axes[0]]
    request["field_y_mm"] = [float(v) for v in axes[1]]
    request["pixel_pitch_um"] = float(pixel_pitch_um)
    # The default is a bounded diagnostic whose finite support and refinement
    # will normally be refused. Explicit controls permit a convergence study.
    request["pupil_samples"] = pupil_samples
    request["kernel_size"] = kernel_size
    request["subpixel_samples"] = subpixel_samples
    try:
        evidence["psf_request"] = prescription_psf.validate_request(request)
    except ValueError as exc:
        evidence["status"] = "blocked"
        evidence["blockers"].append(_block("wavefront_contract_rejected", str(exc)))
        return evidence
    evidence["paraxial_translation_diagnostic"] = _paraxial_diagnostic(
        request, revision, snapshot, db_path, root, csv_validation["prescription_sha256"]
    )
    try:
        lens_factory.validate_request(prescription)
    except lens_factory.LensFactoryError as exc:
        rear = surfaces[-1]
        if "angular support must remain below 80 degrees" in str(exc):
            bound_radius = max(rear["semi_diameter_mm"], corner)
            bound_deg = math.degrees(math.atan2(corner + bound_radius, rear["thickness_mm"]))
            evidence["directional_radiometry_preflight"] = {
                "status": "blocked",
                "scope": "directional_radiometry_only_not_opd",
                "code": "rear_angular_bound_exceeds_factory_domain",
                "conservative_bound_deg": bound_deg,
                "factory_limit_deg": 80.0,
                "rear_surface_order": sequential[-1]["surface_order"],
                "rear_semi_diameter_mm": rear["semi_diameter_mm"],
                "rear_gap_mm": rear["thickness_mm"],
                "reason": (
                    "The full rear-plane quadrature exceeds the directional "
                    "Factory domain. The OPD pupil trace has a separate cone gate."
                ),
            }
        else:
            evidence["directional_radiometry_preflight"] = {
                "status": "blocked", "scope": "directional_radiometry_only_not_opd",
                "code": "factory_contract_rejected", "reason": str(exc),
            }
    else:
        evidence["directional_radiometry_preflight"] = {
            "status": "input_domain_supported_uncharacterized",
            "scope": "directional_radiometry_only_not_opd",
        }
    return evidence


def characterize_db_wavefront_revision(*args, **kwargs) -> dict:
    """Run existing RayOptics/Prysm only after the exact DB preflight passes."""
    preflight = db_wavefront_preflight(*args, **kwargs)
    _require(
        preflight["status"] == "research_request_ready",
        "preflight_blocked",
        f"DB prescription cannot be traced: {preflight['blockers']}",
    )
    result = prescription_psf.characterize(preflight["psf_request"])
    result["db_source"] = preflight["source"]
    result["db_assumptions"] = preflight["assumptions"]
    result["product_ranking_allowed"] = False
    return result
