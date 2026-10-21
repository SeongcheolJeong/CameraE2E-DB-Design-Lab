"""Cross-bind located optical arrays, IMX662 cell and exact spectral Meep tasks.

File-level integrity alone does not establish that two individually valid
artifacts describe the same sensor, coordinates, wavelengths or lens revision.
The bridge checks those semantic links before it permits any RAW projection.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .design_workshop_sensor import _digest as cell_digest
from .registry import digest
from .sensor_structure_job import sha

FIELD_KEYS = {"center": "center", "right-edge": "outer-ne"}
NODES = (450., 550., 650.)


def _require(condition, message):
    if not condition:
        raise ValueError("Spectral preparation crosslink: " + message)


def _same_number(left, right, *, tolerance=1e-10):
    try:
        values = np.asarray([left, right], float)
        return bool(np.isfinite(values).all() and abs(values[0] - values[1]) <= tolerance)
    except (ValueError, TypeError):
        return False


def validate_prepared(prepared_root, tasks, cell, manifest=None):
    """Validate a preparation folder against frozen tasks and the loaded cell.

    ``prepared_root`` is ``outputs/ocl-spectral-camera-20261018`` (the parent of
    ``prepared/``). The caller still verifies every file in the outer manifest;
    this helper independently verifies the four metadata files it consumes and
    checks that every spectral array is bound by both nested/outer manifests.
    Returns a compact evidence record suitable for preserving in the result.
    """
    root = Path(prepared_root).resolve()
    if manifest is None:
        manifest = json.loads((root / "prepared-manifest.json").read_text())
    _require(manifest.get("schema") == "camera_located_optical_preparation_v1", "wrong preparation manifest")
    files = manifest.get("files", {})
    _require(isinstance(files, dict), "missing preparation file map")

    def read(relative):
        path = (root / relative).resolve()
        _require(path.is_relative_to(root), "metadata path leaves preparation")
        _require(relative in files and path.is_file() and sha(path) == files[relative],
                 "metadata not bound by preparation: " + relative)
        return json.loads(path.read_text())

    located = read("prepared/located-geometry.json")
    optical = read("prepared/optics/result.json")
    spectral = read("prepared/spectral/spectral-inputs.json")
    response = read("prepared/spectral/pixel-response.json")
    _require(cell.get("source_report") == "DEF-2206-805", "host is not IMX662")
    expected_cell = cell_digest({key: value for key, value in cell.items() if key != "cell_sha256"})
    _require(cell.get("cell_sha256") == expected_cell, "loaded host cell content digest differs")
    _require(spectral.get("cell_sha256") == expected_cell == response.get("cell_sha256"),
             "spectral rates and host cell differ")
    _require(spectral.get("pixel_response_sha256") == digest(response), "spectral pixel response differs")
    _require(spectral.get("pixel_response_file_sha256") == files["prepared/spectral/pixel-response.json"],
             "pixel response file binding differs")
    revision = optical.get("revision")
    _require(isinstance(revision, str) and revision == spectral.get("optical_revision") == manifest.get("optical_revision"),
             "optical revision differs across preparation and spectral rates")
    _require(optical.get("status") == "PROPOSED", "optical solution is not executable")
    _require(spectral.get("wavelength_nm") == list(range(450, 651, 10))
             and response.get("wavelength_nm") == list(range(450, 651, 10)), "spectral axes differ")
    _require(spectral.get("pixel_aperture_applications") == 1, "receiving pixel aperture must occur once")
    for entry in spectral.get("files", []):
        relative = "prepared/spectral/" + entry["path"]
        _require((root / relative).resolve().is_relative_to(root / "prepared/spectral"), "spectral array path leaves preparation")
        _require(files.get(relative) == entry.get("sha256"), "spectral array manifest link differs: " + relative)

    profile = located["geometry"]
    _require(profile.get("profile_sha256") == digest({k:v for k,v in profile.items() if k != "profile_sha256"}),
             "geometry profile digest differs")
    _require(profile.get("active_shape_row_col") == [1097, 1937] and _same_number(profile.get("pixel_pitch_um"), 2.9),
             "unexpected IMX662 active geometry")
    _require(_same_number(cell.get("pixel_pitch_um"), profile["pixel_pitch_um"]), "optical and cell pitch differ")
    rois = {row["id"]: row for row in profile["roi_layout"]}
    optical_fields = {row["id"]: row for row in optical["fields"]}
    _require(set(rois) == set(FIELD_KEYS.values()) == set(optical_fields), "center/right-edge fields missing or duplicated")
    _require(len(profile["roi_layout"]) == len(optical["fields"]) == 2, "duplicate optical or ROI field")
    for public, key in FIELD_KEYS.items():
        roi, field = rois[key], optical_fields[key]
        geom = field["geometry"]
        _require(field.get("status") == "succeeded", "one physical field was refused")
        _require(roi.get("physical_location") == public, "legacy key mislabeled as a different physical field")
        _require(roi["shape_row_col"] == [256, 256] and all(type(v) is int and v >= 0 and v % 2 == 0
                 for v in roi["origin_row_col"]), "crop shape or CFA origin differs")
        _require(all(origin + count <= bound for origin, count, bound in zip(
            roi["origin_row_col"], roi["shape_row_col"], profile["active_shape_row_col"], strict=True)),
            "native crop leaves active sensor")
        _require(geom.get("sensor_timing_profile_sha256") == profile["profile_sha256"], "optical geometry profile differs")
        _require(geom.get("roi_origin_active_row_col") == roi["origin_row_col"], "optical ROI origin differs")
        _require(geom.get("optical_anchor_active_row_col") == roi["optical_anchor_active_row_col"], "optical anchor differs")
        _require(np.allclose(field["image_xy_mm"], roi["optical_anchor_image_xy_mm"], rtol=0, atol=1e-7),
                 "retraced chief misses declared sensor anchor")
        _require(field.get("source_revision_id") == located["lens_source"]["revision_id"], "lens source revision differs")

    _require(isinstance(tasks, list) and len(tasks) == 144, "expected complete 144-sample frozen campaign")
    expected_grid = {(field, wave, channel, variant, pol, res) for field in FIELD_KEYS for wave in NODES
                     for channel in ("red", "green", "blue") for variant in ("reviewed", "ocl_shift")
                     for pol in ("s", "p") for res in (60, 80)}
    actual_grid = set()
    structures = set()
    geometric_fields = {"pitch_um": "pitch", "si_um": "si_thickness", "ocl_height_um": "lens_height",
                        "ocl_radius_um": "lens_vertex_radius_um"}
    cg = cell["geometry_um"]
    for task in tasks:
        _require(task.get("id") == digest({k:v for k,v in task.items() if k != "id"}), "task content identity differs")
        coordinates = (task["field"], task["wavelength_nm"], task["cfa_channel"], task["variant"],
                       task["polarization"], task["resolution"])
        _require(coordinates in expected_grid and coordinates not in actual_grid, "missing, repeated or unsupported solver coordinate")
        actual_grid.add(coordinates)
        _require(task.get("dimension") == 2 and task.get("source_model") == "periodic_homogeneous_cfa_plane_wave"
                 and task.get("control") == "mesh", "wrong numerical campaign model")
        key = FIELD_KEYS[task["field"]]
        matches = [row for row in located["chief_rays"][key] if row["wavelength_nm"] == task["wavelength_nm"]]
        _require(len(matches) == 1 and _same_number(task["angle_deg"], matches[0]["signed_x_cra_deg"]),
                 "Meep incidence differs from exact wavelength-specific signed chief CRA")
        structure = task["structure"]
        _require(structure.get("report_code") == cell["source_report"], "Meep and host source reports differ")
        structures.add(digest(structure))
        sg = structure["geometry"]
        for source_name, cell_name in geometric_fields.items():
            _require(_same_number(sg.get(source_name), cg.get(cell_name)), "Meep/host geometry differs at " + source_name)
        _require(_same_number(task["ocl_shift_pitch"], .1), "OCL shift hypothesis differs")
        _require(_same_number(sg.get("cfa_um"), cg["cfa_thickness_by_channel"]["green"]), "green CFA thickness differs")
        _require(_same_number(sg.get("passivation_um"), cg["passivation_thickness_by_channel"]["green"]),
                 "green passivation thickness differs")
        for color in ("red", "blue"):
            _require(_same_number(task["rb_cfa_thickness_um"], cg["cfa_thickness_by_channel"][color]), color + " CFA thickness differs")
            _require(_same_number(sg["passivation_um"] + sg["cfa_um"] - task["rb_cfa_thickness_um"],
                                  cg["passivation_thickness_by_channel"][color]), color + " passivation replacement differs")
    _require(actual_grid == expected_grid and len(structures) == 1, "numerical campaign contains mixed structures or missing nodes")
    return {"schema": "camera_ocl_spectral_preparation_crosslink_v1", "passed": True,
            "cell_sha256": expected_cell, "optical_revision": revision,
            "geometry_profile_sha256": profile["profile_sha256"], "structure_sha256": next(iter(structures)),
            "task_count": len(tasks), "checked_task_axes": ["field", "wavelength", "CFA", "variant", "polarization", "mesh"],
            "angle_policy": "exact wavelength-specific signed chief CRA; no 20-degree substitution",
            "qualification": "artifact identity and coordinate consistency only; not physical or 3D validation"}
