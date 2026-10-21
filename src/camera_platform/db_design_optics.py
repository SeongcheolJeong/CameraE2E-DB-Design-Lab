"""Pinned second patent prescription on the shared Reference optical operator.

A new prescription is retraced, never substituted by a stored PSF from another
pupil model. Fields preserve the Reference object directions; native probes
have the same pitch and different angular widths when focal length changes.
"""

from __future__ import annotations

import copy
import math
from decimal import Decimal
from pathlib import Path

from . import reference_workshop_optics as shared

SOURCES = {
    "reference": {
        "id": "US10310255B2-p0014-intermediate",
        "name": "Canon US10310255B2 · p0014 intermediate",
        "revision_id": shared.REVISION,
        "csv_sha256": shared.CSV_SHA256,
        "image_height_mm": 13.66,
        "focal_length_mm": 35.14,
        "reported_f_number": 3.61,
    },
    "canon_p0248": {
        "id": "US20200174235A1-p0248-wide-angle",
        "name": "Canon US20200174235A1 · p0248 wide-angle",
        "revision_id": "4daeca8f-5d81-57b0-8ae3-928f23de2b36",
        "csv_sha256": "b0cfd975001ae7e8ddff2144743cf500031253ce6ff1f160b50d0ac7dfc7c318",
        "image_height_mm": 19.8,
        "focal_length_mm": 24.72,
        "reported_f_number": 4.12,
        "operating_f_number": 4.12,
        "f_number_reference_wavelength_nm": 587.6,
        "f_number_source_url": "https://patents.google.com/patent/US20200174235A1/en",
        # The stopped-down off-axis pupil is narrow in azimuth. Redistribute
        # quadrature under the existing Factory work limit; tolerances stay fixed.
        "reciprocal_sampling": {"polar_rings": 28, "azimuth_samples": 48},
    },
}

# The historical effective-aperture interpretation is retained as an explicit
# alternative operating condition of the same acquired prescription.
SOURCES["canon_p0248_open"] = {
    **{
        k: v
        for k, v in SOURCES["canon_p0248"].items()
        if k not in {"operating_f_number", "reciprocal_sampling"}
    },
    "id": "US20200174235A1-p0248-wide-angle-source-stop",
    "name": "Canon p0248 · F/2.751 source-stop alternative",
    "recipe_only": True,
}

APERTURE_SWEEP_DEFAULTS = [2.75, 3.2, 4.12, 5.6, 8.0, 11.0]


def aperture_key(f_number):
    """Stable operating-condition identity; historical apertures keep their keys."""
    if (
        type(f_number) not in (int, float)
        or not math.isfinite(f_number)
        or not 2.75 <= f_number <= 11
    ):
        raise ValueError("Aperture F-number requires a finite numeric value in [2.75,11]")
    value = float(f_number)
    if value == 2.75:
        return "canon_p0248_open"
    if value == 4.12:
        return "canon_p0248"
    label = format(Decimal(str(value)).normalize(), "f")
    return "canon_p0248_f_" + label.replace(".", "p")


def _source(key):
    if key in SOURCES:
        return copy.deepcopy(SOURCES[key])
    prefix = "canon_p0248_f_"
    if isinstance(key, str) and key.startswith(prefix):
        try:
            f_number = float(key[len(prefix) :].replace("p", "."))
            canonical_key = aperture_key(f_number)
        except ValueError:
            canonical_key = None
        if canonical_key == key:
            source = copy.deepcopy(SOURCES["canon_p0248"])
            source.update(
                id=source["id"] + "-design-f-" + key[len(prefix) :],
                name=f"Canon p0248 · F/{f_number:g} user design setting",
                operating_f_number=f_number,
                operating_f_number_origin="user_design_setting",
                reciprocal_profile="camera_db_aperture_dense_radiometry_v1",
                recipe_only=True,
            )
            return source
    raise ValueError("Choose a pinned reference or supported canon_p0248 operating condition")


def source_metadata(workspace, key):
    source = _source(key)
    result = shared.source_metadata(workspace, source_context=source)
    user_aperture = source.get("operating_f_number_origin") == "user_design_setting"
    if user_aperture:
        # Keep the shared numerical operator/cache identity untouched. The
        # recipe owns the provenance of a user-requested iris on this source.
        result["pupil_condition"].update(
            mode="user_f_number_at_reference_wavelength",
            requested_f_number_origin="user_design_setting",
            source_url=None,
            prescription_source_url=source["f_number_source_url"],
        )
        result["assumptions"] = [
            "operating_stop_derived_from_user_f_number"
            if item == "operating_stop_derived_from_patent_f_number"
            else item
            for item in result["assumptions"]
        ]
    derived = result.get("source_stop_paraxial", result["paraxial"])
    condition = result.get("pupil_condition")
    result["parameter_sources"] = [
        {
            "parameter": "prescription_surfaces",
            "value": len(result["surfaces"]),
            "origin": "acquired_patent_prescription",
            "source_sha256": source["csv_sha256"],
        },
        {
            "parameter": "nominal_focal_length_mm",
            "value": source["focal_length_mm"],
            "origin": "acquired_patent_table",
        },
        {
            "parameter": "reported_f_number",
            "value": source["reported_f_number"],
            "origin": "acquired_patent_table",
            "used_as_pupil_control": bool(condition) and not user_aperture,
            **(
                {
                    "source": source["f_number_source_url"],
                    "reference_wavelength_nm": condition["reference_wavelength_nm"],
                }
                if condition
                else {}
            ),
        },
        {
            "parameter": "derived_stop_pupil_f_number",
            "value": derived.get("effective_f_number"),
            "origin": "model_derived",
            "used_as_pupil_control": not bool(condition),
            "note": (
                "Source stop and effective-diameter aperture hypothesis; "
                "differs from nominal F-number"
            ),
        },
        {
            "parameter": "physical_clear_aperture",
            "value": "effective diameter proxy",
            "origin": "explicit_assumption",
        },
        {
            "parameter": "glass_dispersion",
            "value": "nd/Vd ModelGlass estimate",
            "origin": "model_derived_from_patent_nd_vd",
        },
        {"parameter": "stop_power_transmission", "value": 1.0, "origin": "explicit_assumption"},
        {
            "parameter": "physical_product_match",
            "value": "not_established",
            "origin": "not_available",
        },
    ]
    if condition:
        result["parameter_sources"].extend(
            [
                {
                    "parameter": "applied_f_number",
                    "value": condition["effective_f_number"],
                    "origin": "model_derived",
                    "used_as_pupil_control": True,
                    "unit": "f-number",
                    "source": condition["source_url"],
                    "note": (
                        "User design F-number at 587.6nm; derived iris, not a patent operating mode"
                        if user_aperture
                        else "Patent F-number at 587.6nm; derived iris, not a measured stop"
                    ),
                },
                {
                    "parameter": "tabulated_stop_effective_aperture",
                    "value": 2 * condition["source_stop_semi_diameter_mm"],
                    "origin": "acquired_patent_table",
                    "unit": "mm",
                    "applied": False,
                    "source": source["f_number_source_url"],
                    "note": "Patent Effective aperture column; adapter interprets it as diameter",
                },
                {
                    "parameter": "source_stop_diameter_mm",
                    "value": 2 * condition["source_stop_semi_diameter_mm"],
                    "origin": "model_derived",
                    "unit": "mm",
                    "applied": False,
                    "note": "Diameter interpretation of Effective aperture; not operating iris",
                },
                {
                    "parameter": "applied_stop_diameter_mm",
                    "value": 2 * condition["applied_stop_semi_diameter_mm"],
                    "origin": "model_derived",
                    "unit": "mm",
                    "applied": True,
                    "note": "Fixed iris shared by PSF and reciprocal radiometry across wavelengths",
                },
            ]
        )
        if user_aperture:
            result["parameter_sources"].append(
                {
                    "parameter": "requested_f_number",
                    "value": source["operating_f_number"],
                    "origin": "user_design_setting",
                    "used_as_pupil_control": True,
                    "reference_wavelength_nm": 587.6,
                    "note": "Configured aperture sweep; not an acquired patent F-number",
                }
            )
    result["comparison_scope"] = (
        "Same object-direction field nodes, 2.9um pitch and native256x256 probes; "
        "chart frequencies are per native pixel, not identical object-angle ROI. "
        "New optical transport with the recorded pupil condition and explicit PSF method; "
        "no reuse of an old pupil PSF."
    )
    if key != "reference":
        result["source_semantic_review"] = {
            "source_url": source["f_number_source_url"],
            "scope": "Numerical Example 1 wide-angle aperture and last surface",
            "reference_wavelength_nm": 587.6,
            "stop_effective_aperture_interpretation": (
                "19.96mm interpreted as diameter, not an operating iris"
            ),
            "last_numbered_surface": (
                "36: radius -56.995mm, gap 13.50mm; followed by separate Image plane caption"
            ),
            "geometry_crosscheck": (
                "Derived d-line EFL24.720264mm and entrance-pupil position32.2263mm "
                "match patent24.72 and32.23"
            ),
            "physical_validation": "not_established",
        }
    return result


def candidates(workspace, keys=None):
    output = []
    for key in SOURCES if keys is None else keys:
        source = _source(key)
        metadata = source_metadata(workspace, key)
        output.append(
            {
                "key": key,
                "label": source["name"],
                "revision_id": source["revision_id"],
                "parameter_sources": metadata["parameter_sources"],
                "source": metadata,
                "execution_ready": True,
                "recipe_only": source.get("recipe_only", False),
                "physical_validation": "not_established",
            }
        )
    return output


def template(key, *, optical_profile="geometric_retrace"):
    source = _source(key)
    spec = shared.template()
    spec["reference_revision_id"] = source["revision_id"]
    spec["optical_profile"] = optical_profile
    return shared.normalize(spec, source_context=source)


def cache_key(workspace, key, *, optical_profile="geometric_retrace"):
    return shared.cache_key(
        workspace, template(key, optical_profile=optical_profile), source_context=_source(key)
    )


def solve(workspace, key, output_dir, *, on_progress=None, optical_profile="geometric_retrace"):
    result = shared.solve(
        workspace,
        template(key, optical_profile=optical_profile),
        output_dir,
        on_progress=on_progress,
        source_context=_source(key),
    )
    # This extension annotates the retained shared optical result with the
    # source/assumption mapping without changing transport arrays.
    expected = source_metadata(workspace, key)
    if result.get("reference", {}).get("parameter_sources") != expected["parameter_sources"]:
        result["reference"] = expected
        result["comparison_scope"] = expected["comparison_scope"]
        shared.shared._write(Path(output_dir) / "result.json", result)
    return result


load_solution = shared.load_solution
apply_field = shared.apply_field
effective_pupil_response = shared.effective_pupil_response
