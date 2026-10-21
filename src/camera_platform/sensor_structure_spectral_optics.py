"""Located IMX662 spectral inputs for an explicitly limited 2D OCL projection.

The sensor window is the vendor active array, not the previous full-frame lens
bench field.  Optical transport remains the existing geometric, receiving-pixel
integrated PSF operator.  This module does not make a 2D Meep response into a
qualified 3D QE or a spatial crosstalk kernel.
"""

from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path

import numpy as np

from . import db_design_optics as db_optics
from . import design_workshop_engine as charts_engine
from . import reference_workshop_optics as optics
from . import reference_workshop_sensor as pixels
from .db_design_sensor_shutter import located_fields
from .registry import digest
from .virtual_lab_engine import _spectra
from .virtual_lab_job import sha256

SCHEMA = "camera_imx662_located_spectral_inputs_v1"
VENDOR_URL = "https://www.sony-semicon.com/files/62/flyer_security/IMX662-AAQR_AAQR1_Flyer.pdf"
WAVELENGTH_NM = tuple(range(450, 651, 10))
CHARTS = ("uniform", "color", "texture", "resolution")


def geometry_profile(source_evidence=None):
    """Vendor dimensions plus independently labelled mounting/crop assumptions.

    ``outer-ne`` is retained only as the existing two-field optical API key. Its
    physical location and display label are the right edge, never north-east.
    """
    evidence = copy.deepcopy(source_evidence or {})
    if evidence.get("url", VENDOR_URL) != VENDOR_URL:
        raise ValueError("IMX662 geometry requires the pinned Sony document")
    document_sha = evidence.get("document_sha256")
    if document_sha is not None and (not isinstance(document_sha, str)
                                    or len(document_sha) != 64
                                    or any(c not in "0123456789abcdef" for c in document_sha)):
        raise ValueError("Vendor document digest must be a SHA256 hex string")
    pitch, optical_axis = 2.9, [548.0, 968.0]
    layout = []
    for key, label, origin, anchor in (
        ("center", "실제 센서 중심", [420, 840], [548.0, 968.0]),
        ("outer-ne", "실제 센서 오른쪽 주변부", [420, 1680], [548.0, 1808.0]),
    ):
        center = [v + 127.5 for v in origin]
        image_xy = lambda rc: [(rc[1] - optical_axis[1]) * pitch / 1000,
                               -(rc[0] - optical_axis[0]) * pitch / 1000]
        layout.append({
            "id": key, "label": label, "physical_location": "center" if key == "center" else "right-edge",
            "origin_row_col": origin, "shape_row_col": [256, 256],
            "center_active_row_col": center, "roi_center_image_xy_mm": image_xy(center),
            "optical_anchor_active_row_col": anchor,
            "optical_anchor_local_row_col": [anchor[i] - origin[i] for i in range(2)],
            "optical_anchor_image_xy_mm": image_xy(anchor),
            "cfa_origin": {"pattern": "RGGB", "active_origin_phase": "assumed"},
            "optical_anchor_definition": "native pixel at local row128,col128; geometric crop center is127.5,127.5",
        })
    profile = {
        "schema": "camera_imx662_vendor_geometry_overlay_v1",
        "active_shape_row_col": [1097, 1937], "effective_shape_row_col": [1113, 1965],
        "recording_shape_row_col": [1080, 1920], "total_shape_row_col": [1196, 2014],
        "pixel_pitch_um": pitch, "optical_axis_active_row_col": optical_axis,
        "roi_layout": layout,
        "dimension_source": {
            **evidence, "url": VENDOR_URL,
            "classification": "official_vendor_document",
            "variant": "IMX662-AAQR/AAQR1", "physical_die_unit_match": "not_verified",
            "values": {"active_columns_rows": [1937, 1097], "effective_columns_rows": [1965, 1113],
                       "recording_columns_rows": [1920, 1080], "total_columns_rows": [2014, 1196],
                       "pixel_pitch_um": 2.9},
        },
        "assumptions": {
            "mounting": "lens optical axis at active-array geometric center; no measured module decenter or tilt",
            "orientation": "optical x right/y up; sensor row down/column right",
            "cfa_phase": "RGGB at active-array origin; both even crop origins preserve phase",
            "optical_sampling": "one local stationary PSF and pupil distribution at each declared anchor",
            "source_halo": "optical radiance outside crop/active edge supplies convolution support; no RAW pixels outside active array",
            "two_dimensional_direction": "right-edge meridional section aligned with +x OCL offset; no corner-azimuth generalization",
            "mode": "native crop of active dimensions, not a decoded product readout mode",
        },
        "physical_validation": "not_established",
    }
    profile["profile_sha256"] = digest(profile)
    return profile


def locate(workspace, *, source_evidence=None):
    """Return exact physical field anchors and signed per-wavelength chief CRA.

    The returned CRA nodes may be used as explicit Meep inputs. No rounded20°
    assignment or angle interpolation is performed here.
    """
    from rayoptics.raytr import trace
    from rayoptics.raytr.opticalspec import Field

    context = db_optics._source("reference")
    specification = db_optics.template("reference", optical_profile="geometric_retrace")
    profile = geometry_profile(source_evidence)
    fields = located_fields(workspace, specification, context, profile)
    for field, roi in zip(fields, profile["roi_layout"], strict=True):
        field["physical_location"] = roi["physical_location"]
        field["legacy_field_key_note"] = "outer-ne API key refers to right-edge coordinates in this version"
    cra = {f["id"]: [] for f in fields}
    for start in range(0, len(WAVELENGTH_NM), 3):
        waves = list(map(float, WAVELENGTH_NM[start:start + 3]))
        request = optics._request(workspace, specification, waves, source_context=context)
        for wi, wave in enumerate(waves):
            model = optics.prescription_psf._model(request, wi)
            for field in fields:
                node = Field(y=field["object_angle_deg"], fov=model.optical_spec["fov"])
                node.aim_info = trace.aim_chief_ray(model, node, wave)
                ray, _, _ = trace.trace_base(model, [0.0, 0.0], node, wave,
                                              check_apertures=True, apply_vignetting=False)
                optics.prescription_psf._validate_physical_ray(
                    ray, model, request["prescription"]["surfaces"], "IMX662 located chief")
                direction = np.asarray(ray[-1][1], float)
                xy = optics._rotate(direction[:2], field["azimuth_deg"])
                if abs(xy[1]) > 1e-12 or direction[2] <= 0:
                    raise ValueError("Located OCL comparison requires forward x-z meridional incidence")
                cra[field["id"]].append({
                    "wavelength_nm": wave,
                    "signed_x_cra_deg": math.degrees(math.atan2(float(xy[0]), float(direction[2]))),
                    "polar_cra_deg": math.degrees(math.acos(float(np.clip(direction[2], -1, 1)))),
                    "direction_xyz": [float(xy[0]), float(xy[1]), float(direction[2])],
                    "chief_image_xy_mm": optics._rotate(ray[-1][0][:2], field["azimuth_deg"]).tolist(),
                })
    context["located_fields"] = fields
    return {
        "schema": SCHEMA, "geometry": profile, "fields": fields,
        "chief_rays": cra, "optical_specification": specification,
        "source_context": context,
        "lens_source": db_optics.source_metadata(workspace, "reference"),
        "qualification": "located_raytrace_only_not_meep_or_product_validation",
    }


def prepare_optics(workspace, directory, *, source_evidence=None, on_progress=None,
                   dense_radiometry=False):
    """Retrace both actual sensor fields with existing unchanged optical gates.

    Dense reciprocal quadrature is an explicit configuration; it changes only
    sampling, retaining the same existing tolerances. Failed gates are not hidden.
    """
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    located = locate(workspace, source_evidence=source_evidence)
    context = copy.deepcopy(located["source_context"])
    if dense_radiometry:
        context["reciprocal_profile"] = optics.db_design_radiometry.PROFILE
    located["source_context"] = context
    located["reciprocal_sampling_profile"] = context.get("reciprocal_profile", "existing_factory_default")
    charts_engine.write_json(root / "located-geometry.json", located)
    optics.solve(workspace, located["optical_specification"], root / "optics",
                 on_progress=on_progress, source_context=context)
    solution = optics.load_solution(root / "optics")
    if solution["result"]["status"] != "PROPOSED":
        raise ValueError("Located IMX662 optics failed unchanged qualification gates")
    for field in solution["result"]["fields"]:
        if field["status"] != "succeeded":
            raise ValueError("Both actual sensor fields must pass before spectral RAW generation")
    return located, solution


def spectral_inputs(workspace, directory, solution, cell, *, luminance_cd_m2=1000.0,
                    charts=CHARTS, on_progress=None):
    """Save pre-noise [wavelength,row,col,site] electron-rate contributions.

    They include the existing absolute pupil-averaged planar QE exactly once.
    A later explicit 2D OCL *relative* projection may modify each band before
    spectral summation; applying another absolute QE or pixel aperture is wrong.
    """
    if cell.get("source_report") != "DEF-2206-805" or cell.get("pixel_pitch_um") != 2.9:
        raise ValueError("Located spectral bridge requires the pinned native IMX662 cell")
    if not np.isfinite(luminance_cd_m2) or luminance_cd_m2 <= 0:
        raise ValueError("Luminance must be finite and positive")
    if not charts or any(chart not in CHARTS for chart in charts) or len(set(charts)) != len(charts):
        raise ValueError("Choose unique existing uniform/color/texture/resolution charts")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    response = pixels.build_response(cell, wavelength_nm=np.asarray(WAVELENGTH_NM, float))
    spectra = _spectra(workspace, "D65", float(luminance_cd_m2))
    callback = lambda theta, phi, wave: pixels.absolute_angular_response(response, theta, phi, wave)
    manifest = {
        "schema": SCHEMA, "optical_revision": solution["result"]["revision"],
        "cell_sha256": cell["cell_sha256"], "pixel_response_sha256": digest(response),
        "wavelength_nm": list(WAVELENGTH_NM), "luminance_cd_m2": float(luminance_cd_m2),
        "illuminant": "D65", "spectral_asset": spectra["asset"],
        "array_unit": "collected_electrons_per_second_in_each_declared_10nm_bin",
        "array_axes": ["wavelength", "row", "column", "R_Gr_Gb_B_site"],
        "input_profile": "same_native_sensor_plane_charts_with_optical_halo",
        "pixel_aperture_applications": 1,
        "files": [], "fields": [],
    }
    for field in solution["result"]["fields"]:
        placement = field["geometry"]
        if (placement.get("active_shape_row_col") != [1097, 1937]
                or placement.get("field_approximation") != "local_shift_invariant_at_declared_inside_ROI_optical_anchor"):
            raise ValueError("Old virtual optical fields cannot supply located spectral inputs")
        record = {"id": field["id"], "label": field["label"], "charts": []}
        for kind in charts:
            if on_progress:
                on_progress(f"{field['label']} · {kind} · 21-band pre-noise contributions")
            photons, _ = charts_engine._scene(kind, field["geometry"], spectra)
            applied = optics.apply_field(solution, field["id"], photons,
                                          angular_response=callback, return_spectral=True)
            bands = applied["spectral_planes_electron_rate"]
            if bands.shape != (21, 256, 256, 4) or not np.isfinite(bands).all() or np.min(bands) < 0:
                raise ValueError("Finite nonnegative 21-band native four-site rates required")
            np.testing.assert_allclose(bands.sum(axis=0), applied["planes_electron_rate"], rtol=1e-12, atol=1e-10)
            folder = root / field["id"] / kind
            folder.mkdir(parents=True, exist_ok=True)
            files = {}
            for name, array in (("spectral-planes-electron-rate.npy", bands),
                                ("mean-electron-rate.npy", applied["mean_electron_rate"]),
                                ("planes-electron-rate.npy", applied["planes_electron_rate"])):
                path = folder / name
                np.save(path, array, allow_pickle=False)
                item = {"path": path.relative_to(root).as_posix(), "sha256": sha256(path),
                        "bytes": path.stat().st_size, "shape": list(array.shape)}
                manifest["files"].append(item)
                files[name] = item
            record["charts"].append({"chart": kind, "files": files, "contract": applied["metadata"]})
        manifest["fields"].append(record)
    charts_engine.write_json(root / "pixel-response.json", response)
    manifest["pixel_response_file_sha256"] = sha256(root / "pixel-response.json")
    manifest["adapter_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    charts_engine.write_json(root / "spectral-inputs.json", manifest)
    return manifest
