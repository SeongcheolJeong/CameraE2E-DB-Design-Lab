"""Conditional full-active-area field coverage for a pinned lens/sensor pair.

This checks the *outer boundary* of a declared sensor active mode against the
saved geometric raytrace tables.  It does not measure image-circle irradiance,
pixel-integrated MTF, distortion calibration, or product compatibility.  The
32 x 32 native CFA in a saved sensor run is an execution crop, not an active
product mode, and is never promoted to one here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam._catalog_store import file_hash
from pyisetcam.component_catalog import component_catalog_path
from pyisetcam.lens_patents import lens_patent_raytrace_optics

from .lens_catalog_bridge import _asset, _revision_source, geometric_lens_preview
from .sensor_db_bridge import inspect_sensor_revision

SCHEMA = "camerae2e_full_sensor_fov_feasibility_v1"
MODE_SCHEMA = "camerae2e_full_sensor_active_mode_v1"
MODEL = "strict_geometric_raytrace_radial_inverse_at_active_sensor_boundary"
_TOL_MM = 1e-9


def _finite_positive(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive.")
    return float(value)


def _axis(table: Mapping[str, Any], name: str) -> tuple[np.ndarray, np.ndarray]:
    if table.get("field_coordinate") != "ideal_image_height":
        raise ValueError(f"{name} needs the ideal_image_height coordinate.")
    field = np.asarray(table.get("field_height_mm"), dtype=float)
    waves = np.asarray(table.get("wavelength_nm"), dtype=float)
    if (
        field.ndim != 1 or field.size < 2 or waves.ndim != 1 or waves.size < 1
        or not np.isfinite(field).all() or not np.isfinite(waves).all()
        or abs(float(field[0])) > 1e-12 or np.any(np.diff(field) <= 0)
        or np.any(np.diff(waves) <= 0)
    ):
        raise ValueError(f"{name} has an unsupported field or wavelength axis.")
    return field, waves


def _mode(
    active_mode: Mapping[str, Any], inspection: Mapping[str, Any]
) -> tuple[int, int, float, tuple[float, float]]:
    if not isinstance(active_mode, Mapping) or set(active_mode) != {
        "schema", "mode_id", "sensor_run_id", "sensor_report_code",
        "sensor_result_sha256", "active_rows", "active_cols", "pixel_pitch_um",
        "sensor_center_offset_from_optical_axis_um_row_col", "sensor_plane",
        "geometry_source",
    }:
        raise ValueError("Declare a complete full-sensor active-mode geometry contract.")
    revision = inspection["revision"]
    if (
        active_mode["schema"] != MODE_SCHEMA
        or not isinstance(active_mode["mode_id"], str)
        or not active_mode["mode_id"].strip()
        or active_mode["sensor_run_id"] != revision["run_id"]
        or active_mode["sensor_report_code"] != revision["report_code"]
        or active_mode["sensor_result_sha256"] != revision["result_sha256"]
    ):
        raise ValueError("Active mode must bind to the selected saved sensor revision.")
    rows, cols = active_mode["active_rows"], active_mode["active_cols"]
    if any(type(value) is not int or not 2 <= value <= 1_000_000 for value in (rows, cols)):
        raise ValueError("Active rows and columns must be explicit positive integer counts.")
    pitch = _finite_positive(active_mode["pixel_pitch_um"], "Active-mode pixel pitch")
    saved_pitch = _finite_positive(
        inspection["native_response"]["pixel_pitch_um"], "Saved sensor pixel pitch"
    )
    if not math.isclose(pitch, saved_pitch, rel_tol=1e-9, abs_tol=1e-12):
        raise ValueError("Active-mode pitch differs from the saved sensor revision.")
    if active_mode["sensor_plane"] != "perpendicular_to_optical_axis":
        raise ValueError("Tilted or otherwise unspecified sensor planes are unsupported.")
    offset = active_mode["sensor_center_offset_from_optical_axis_um_row_col"]
    if (
        not isinstance(offset, (list, tuple)) or len(offset) != 2
        or any(type(value) not in (int, float) or not math.isfinite(value) for value in offset)
    ):
        raise ValueError("Declare finite row/column optical-axis offsets, including zero.")
    source = active_mode["geometry_source"]
    if (
        not isinstance(source, Mapping) or set(source) != {"kind", "reference"}
        or source["kind"] not in {"engineering_declaration", "external_reference_unverified"}
        or not isinstance(source["reference"], str) or not source["reference"].strip()
    ):
        raise ValueError("Active-mode geometry requires an explicit unverified source reference.")
    return rows, cols, pitch, (float(offset[0]), float(offset[1]))


def evaluate_full_sensor_fov(
    lens_preview: Mapping[str, Any],
    sensor_inspection: Mapping[str, Any],
    raytrace: Mapping[str, Any] | None,
    active_mode: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Evaluate saved *model support*, never actual full-sensor image quality.

    An active mode is mandatory.  Its dimensions and optical-axis placement
    are explicit, unverified external declarations; no catalog MP, simulated
    CFA crop, or die dimension is silently substituted.
    """
    if sensor_inspection.get("executable") is not True:
        raise ValueError("Selected saved sensor revision is not executable.")
    model = lens_preview.get("model")
    identity = lens_preview.get("identity")
    if not isinstance(model, Mapping) or not isinstance(identity, Mapping):
        raise ValueError("A pinned geometric lens preview is required.")
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "model": MODEL,
        "lens_identity": dict(identity),
        "sensor_revision": dict(sensor_inspection["revision"]),
        "active_mode": None,
        "status": "not_evaluable",
        "reason": "full_sensor_active_mode_geometry_missing",
        "source_classification": "model_and_declared_mode_only",
        "physical_validation": "not_established",
        "product_qualification_allowed": False,
        "image_quality_comparison_allowed": False,
    }
    if active_mode is None:
        return result
    rows, cols, pitch_um, offset_um = _mode(active_mode, sensor_inspection)
    if (
        raytrace is None or raytrace.get("physics_policy") != "strict"
        or raytrace.get("contract_version") != 2
        or raytrace.get("psf_kind") != "geometric"
        or raytrace.get("diffraction_included") is not False
        or raytrace.get("asset_sha256") != identity.get("asset_sha256")
        or raytrace.get("source_prescription_sha256") != identity.get("prescription_sha256")
        or model.get("response_kind") != "geometric_ray_histogram_psf"
        or model.get("object_distance_m") != "infinity"
    ):
        raise ValueError("A pinned, infinity-focus strict geometric raytrace is required.")
    focal_mm = _finite_positive(model.get("effective_focal_length_mm"), "Effective focal length")
    max_fov_deg = _finite_positive(model.get("max_fov_deg"), "Lens full field of view")
    if max_fov_deg >= 180:
        raise ValueError("Lens full field of view must be below 180 degrees.")
    tables = {name: raytrace.get(name) for name in ("geometry", "psf", "relative_illumination")}
    if not all(isinstance(table, Mapping) for table in tables.values()):
        raise ValueError("Geometry, PSF and relative-illumination field tables are required.")
    axes = {name: _axis(table, name) for name, table in tables.items()}
    waves = axes["geometry"][1]
    if any(not np.array_equal(axis[1], waves) for axis in axes.values()):
        raise ValueError("Saved lens field tables need the same exact wavelengths.")
    ideal = axes["geometry"][0]
    distortion = np.asarray(tables["geometry"].get("function"), dtype=float)
    if (
        distortion.shape != (ideal.size, waves.size)
        or not np.isfinite(distortion).all()
        or np.any(np.abs(distortion[0]) > 1e-12)
        or np.any(np.diff(distortion, axis=0) <= 0)
    ):
        raise ValueError("Saved radial distortion must be finite and strictly monotone.")
    psf_fields = axes["psf"][0]
    if not np.array_equal(psf_fields, np.asarray(model.get("field_samples_mm"), dtype=float)):
        raise ValueError("Pinned preview and raytrace PSF field support disagree.")

    # Outer active-area edges, not the last pixel centers.  For a monotone
    # radial map the largest image height in a rectangle occurs at a corner.
    half_row_mm, half_col_mm = rows * pitch_um / 2000.0, cols * pitch_um / 2000.0
    off_row_mm, off_col_mm = offset_um[0] / 1000.0, offset_um[1] / 1000.0
    corners = [
        (label, off_row_mm + row_sign * half_row_mm, off_col_mm + col_sign * half_col_mm)
        for label, row_sign, col_sign in (
            ("top_left", -1, -1), ("top_right", -1, 1),
            ("bottom_left", 1, -1), ("bottom_right", 1, 1),
        )
    ]
    result["active_mode"] = dict(active_mode)
    result["active_extent_mm_row_col"] = [2 * half_row_mm, 2 * half_col_mm]
    result["active_diagonal_mm"] = math.hypot(2 * half_row_mm, 2 * half_col_mm)
    by_wavelength = []
    all_covered = True
    for wi, wavelength in enumerate(waves):
        distorted = distortion[:, wi]
        points = []
        covered = True
        for label, row_mm, col_mm in corners:
            image_radius = math.hypot(row_mm, col_mm)
            inside_geometry = image_radius <= distorted[-1] + _TOL_MM
            if inside_geometry:
                ideal_radius = float(np.interp(min(image_radius, distorted[-1]), distorted, ideal))
                angle_deg = math.degrees(math.atan(ideal_radius / focal_mm))
                within_psf = ideal_radius <= psf_fields[-1] + _TOL_MM
                within_ri = ideal_radius <= axes["relative_illumination"][0][-1] + _TOL_MM
                within_fov = 2 * angle_deg <= max_fov_deg + 1e-9
                point_covered = within_psf and within_ri and within_fov
                scale = ideal_radius / image_radius if image_radius > 0 else 1.0
                angle_row_col = [
                    math.degrees(math.atan(row_mm * scale / focal_mm)),
                    math.degrees(math.atan(col_mm * scale / focal_mm)),
                ]
            else:
                ideal_radius = angle_deg = None
                angle_row_col = None
                within_psf = within_ri = within_fov = False
                point_covered = False
            points.append({
                "corner": label,
                "distorted_sensor_position_mm_row_col": [row_mm, col_mm],
                "distorted_sensor_radius_mm": image_radius,
                "ideal_image_radius_mm": ideal_radius,
                "pinhole_equivalent_half_angle_deg": angle_deg,
                "pinhole_equivalent_angle_deg_row_col": angle_row_col,
                "within_geometry_table": inside_geometry,
                "within_psf_field": within_psf,
                "within_relative_illumination_field": within_ri,
                "within_declared_lens_fov": within_fov,
                "within_saved_model_support": point_covered,
            })
            covered &= point_covered
        all_covered &= covered
        by_wavelength.append({
            "wavelength_nm": float(wavelength),
            "within_saved_model_support": covered,
            "geometry_distorted_radius_limit_mm": float(distorted[-1]),
            "geometry_ideal_radius_limit_mm": float(ideal[-1]),
            "psf_ideal_radius_limit_mm": float(psf_fields[-1]),
            "relative_illumination_ideal_radius_limit_mm": float(
                axes["relative_illumination"][0][-1]
            ),
            "declared_full_fov_limit_deg": max_fov_deg,
            "active_boundary_corners": points,
        })
    result["status"] = (
        "within_saved_model_support" if all_covered else "outside_saved_model_support"
    )
    result["reason"] = (
        None if all_covered
        else "active_boundary_exceeds_saved_lens_field_at_one_or_more_wavelengths"
    )
    result["wavelengths"] = by_wavelength
    result["angle_mapping_semantics"] = (
        "ideal_image_height_over_effective_focal_length_pinhole_equivalent"
    )
    result["limitations"] = [
        "active_mode_dimensions_and_mount_offset_are_user_declared_unverified",
        "saved_geometric_field_support_is_not_illuminated_image_circle_or_image_quality",
        "object_angle_is_pinhole_equivalent_not_a_measured_ray_direction",
        "geometric_psf_excludes_diffraction_and_pixel_integration",
        "no_measured_mount_alignment_or_full_sensor_capture",
    ]
    return result


def inspect_db_full_sensor_fov(
    lens_revision_id: str,
    sensor_identifier: str,
    *,
    sensor_run_id: str,
    active_mode: Mapping[str, Any] | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Use pinned DB revisions; never infer an active mode from saved crops."""
    db_root = component_catalog_path(root).parent.resolve()
    preview = geometric_lens_preview(lens_revision_id, catalog_root=db_root, wavelength_nm=550.0)
    inspection = inspect_sensor_revision(sensor_identifier, root=db_root, run_id=sensor_run_id)
    if active_mode is None:
        return evaluate_full_sensor_fov(preview, inspection, None, None)
    revision, snapshot, db_path, source_hash = _revision_source(lens_revision_id, db_root)
    simulation_id, asset_path, validation, _ = _asset(revision, snapshot, db_path, db_root)
    optics = lens_patent_raytrace_optics(
        simulation_id,
        db_path=db_path,
        psf_dir=asset_path.parent,
        allow_approximate_radiometry=True,
    )
    raytrace = optics["raytrace"]
    if (
        raytrace.get("asset_sha256") != validation["asset_sha256"]
        or raytrace.get("source_prescription_sha256") != validation["source_prescription_sha256"]
        or file_hash(db_path) != source_hash
        or file_hash(asset_path) != validation["asset_sha256"]
    ):
        raise ValueError("Pinned lens source or raytrace asset changed during FOV inspection.")
    return evaluate_full_sensor_fov(preview, inspection, raytrace, active_mode)


def install_routes(app: Any, service: Any) -> None:
    """Expose the read-only support check through the shared Workbench API."""
    @app.post("/api/v1/lens-sensor-fov/inspect", tags=["lens_sensor_fov"])
    def inspect(payload: dict[str, Any]) -> dict[str, Any]:
        required = {"lens_revision_id", "sensor_identifier", "sensor_run_id"}
        if not isinstance(payload, dict) or not required <= payload.keys() or (
            payload.keys() - required - {"active_mode"}
        ):
            raise ValueError(
                "FOV inspection requires lens_revision_id, sensor_identifier and "
                "sensor_run_id, with optional active_mode."
            )
        for key in required:
            if not isinstance(payload[key], str) or not payload[key].strip():
                raise ValueError(f"{key} must be a nonempty identifier.")
        return inspect_db_full_sensor_fov(
            payload["lens_revision_id"], payload["sensor_identifier"],
            sensor_run_id=payload["sensor_run_id"],
            active_mode=payload.get("active_mode"),
            root=Path(service.workspace) / "camerae2e_db",
        )
