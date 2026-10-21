"""Conservative adapter for pinned DeepLens sequential refractive JSON data.

Format evidence (archived by ``tools/acquire_open_lens_examples.py``):
https://github.com/vccimaging/DeepLens/tree/2e3bcb24f4b550258ab14f35c0dae5635d0f6d6d
See geometric_surface/{aspheric,spheric,aperture,base_surface}.py,
geolens_pkg/io.py, and material/materials.py. No upstream Python is executed.

``r`` is an aperture radius; ``roc`` is curvature radius (precedence over
``c = 1 / roc``). Geometry follows d_next prefix sums. In serialized JSON,
use_ai2=True means ai starts at A2, False means A4. Unflagged legacy lists
require corroborating named coefficients. Named media are resolved only by
exact names in the pinned source's own catalogues; these values identify the
source simulation model, not an independently verified production material.
"""

from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any

from .lens_source_parser import _finish, _issue, _result, _set_aperture, _surface

DEEPLENS_COMMIT = "2e3bcb24f4b550258ab14f35c0dae5635d0f6d6d"
_BASE = f"https://raw.githubusercontent.com/vccimaging/DeepLens/{DEEPLENS_COMMIT}/"
_CATALOG_HASHES = {
    "SCHOTT.AGF": "897734ab5185e6c8ae168588671f7d4fabadae49a42abff83aa8aa2190462963",
    "MISC.AGF": "997146d02baf2482b708f25f092e57b4069f38e63c5ea09c08b18950c93b8fe4",
    "PLASTIC2022.AGF": "c70b0a9f67ac86709b25f33f336fa81796cb0b0a9c3ed0c5ac33e8b922d36d5a",
    "CDGM.AGF": "6fc3e8719cc536192dfb1c87963602d06977d17893cf8f97f792a6a474d76df6",
}
# (catalogue, declared nd, declared Vd, AGF formula, six original coefficients).
# Exact transcription of NM/CD records with upstream catalogue precedence:
# MISC < PLASTIC2022 < CDGM < SCHOTT. No aliases or punctuation substitutions.
_MATERIALS = {
    "sk16": (
        "SCHOTT.AGF",
        1.62041,
        60.323649,
        2,
        [1.34317774, 0.00704687339, 0.241144399, 0.0229005, 0.994317969, 92.7508526],
    ),
    "n-sk16": (
        "SCHOTT.AGF",
        1.62041,
        60.32,
        2,
        [1.34317774, 0.00704687339, 0.241144399, 0.0229005, 0.994317969, 92.7508526],
    ),
    "f2": (
        "SCHOTT.AGF",
        1.62004,
        36.37,
        2,
        [1.34533359, 0.00997743871, 0.209073176, 0.0470450767, 0.937357162, 111.886764],
    ),
    "bk7": (
        "SCHOTT.AGF",
        1.5168,
        64.167336,
        2,
        [1.03961212, 0.00600069867, 0.231792344, 0.0200179144, 1.01046945, 103.560653],
    ),
    "coc": (
        "MISC.AGF",
        1.533732,
        56.227932,
        1,
        [2.28448546, 0.0102952211, 0.0373493703, -0.00928409653, 0.00173289808, -0.000115203047],
    ),
    "pmma": (
        "MISC.AGF",
        1.491756,
        57.440791,
        1,
        [2.1864582, -0.00024475348, 0.014155787, -0.00044329781, 7.7664259e-05, -2.9936382e-06],
    ),
    "polycarb": (
        "MISC.AGF",
        1.58547,
        29.909185,
        1,
        [
            2.42838566,
            -3.86116645e-05,
            0.0287574474,
            -0.000197897366,
            0.000148358968,
            1.38651935e-06,
        ],
    ),
    "polystyr": (
        "MISC.AGF",
        1.590481,
        30.866877,
        1,
        [2.44598368, 2.21428933e-05, 0.0272988569, 0.000301210852, 8.88934888e-05, -1.75707929e-06],
    ),
    "okp4": (
        "PLASTIC2022.AGF",
        1.607283,
        26.645105,
        1,
        [1.6768, 0.55567, 0.49774, -0.1329, 0.01906, -0.001068],
    ),
    "h-k51": (
        "CDGM.AGF",
        1.52307,
        58.639792,
        2,
        [0.960161808, 116.24248, 1.18368963, 0.0118029997, 0.102338243, -0.0189575777],
    ),
}
_TOP_KEYS = {
    "info",
    "foclen",
    "fnum",
    "r_sensor",
    "sensor_size",
    "(sensor_size)",
    "d_sensor",
    "(d_sensor)",
    "surfaces",
    "sensor_res",
    "primary_wvln",
    "wvln_rgb",
    "obj_depth",
    "enpd",
}
_SURFACE_KEYS = {
    "type",
    "idx",
    "r",
    "c",
    "roc",
    "(c)",
    "d",
    "(d)",
    "d_next",
    "mat1",
    "mat2",
    "k",
    "ai",
    "use_ai2",
    "is_square",
    "diffraction",
    "is_aperture",
    "pos_xy",
    "vec_local",
    "device",
    "(mat2_n)",
    "(mat2_V)",
}
_COEFFICIENT_KEY = re.compile(r"\(?ai([1-9][0-9]*)\)?\Z")


def _finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"Expected numeric scalar, found {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Nonfinite source value")
    return value


def _near(a: float, b: float, *, absolute: float = 1e-7) -> bool:
    return math.isclose(a, b, rel_tol=2e-6, abs_tol=absolute)


def _set_material(result: dict, row: dict, name: Any) -> None:
    if not isinstance(name, str) or not name.strip():
        _issue(result, "missing_material", f"Surface {row['surface_key']} lacks mat2.")
        return
    row["material"] = name.strip()
    name = name.strip().lower()
    if name in {"air", "vacuum", "occluder"}:
        # Explicit aliases documented by DeepLens Material.__init__.
        row.update(nd=1.0, vd=None, material_model="air")
        if name != "air":
            row["material_resolution"] = {"source_alias": name, "resolved_name": "air"}
            row["material"] = "air"
    elif "/" in name:
        try:
            nd_string, vd_string = name.split("/")
            nd, vd = float(nd_string), float(vd_string)
            if not (math.isfinite(nd) and math.isfinite(vd) and 1 < nd <= 3 and vd > 0):
                raise ValueError("Invalid nd/Vd pair")
        except ValueError:
            _issue(result, "invalid_inline_material", f"Invalid source n/V medium {name!r}.")
            return
        row.update(nd=nd, vd=vd, material_model="source_nd_vd")
        row["material_resolution"] = {
            "status": "explicit_source_nd_vd",
            "source_model": "two-term Cauchy",
            "note": "Inline n/V is an approximate dispersion model declared by the source.",
        }
    elif name in _MATERIALS:
        catalog, nd, vd, mode, coefficients = _MATERIALS[name]
        row.update(nd=nd, vd=vd, material_model="deeplens_exact_catalog")
        row["material_resolution"] = {
            "status": "resolved_exact_source_catalog_name",
            "dispersion": {
                "catalog": catalog,
                "name": name,
                "formula": "schott" if mode == 1 else "sellmeier",
                "agf_formula_number": mode,
                "coefficients": list(coefficients),
                "wavelength_unit": "um",
                "source_url": _BASE + "deeplens/material/" + catalog,
                "source_sha256": _CATALOG_HASHES[catalog],
                "commit": DEEPLENS_COMMIT,
            },
            "index_reference": "Explicit source catalogue NM record nd/Vd",
        }
    else:
        row["material_model"] = "catalog_required"
        code = "conflicting_source_material" if name == "hk51" else "unresolved_material"
        explanation = (
            "Source custom hk51 declares nd=1.5501 but its Sellmeier coefficients yield about "
            "1.5231; it must not be silently replaced with h-k51."
            if name == "hk51"
            else f"No audited exact source-catalogue entry for {name!r}."
        )
        _issue(result, code, explanation)


def _medium_name(value: Any) -> str:
    value = str(value).strip().lower()
    return "air" if value in {"air", "vacuum", "occluder"} else value


def _coefficients(result: dict, row: dict, source: dict) -> None:
    values = source.get("ai", [])
    if not isinstance(values, list):
        raise ValueError("ai must be a coefficient list")
    values = [_finite(v) for v in values]
    flag = source.get("use_ai2")
    if "use_ai2" in source and not isinstance(flag, bool):
        raise ValueError("use_ai2 must be a JSON boolean")
    named = {}
    for key, value in source.items():
        match = _COEFFICIENT_KEY.fullmatch(key)
        if match:
            order = int(match[1])
            val = _finite(value)
            if order % 2:
                _issue(result, "unsupported_coefficient_order", f"Odd-order {key} is unsupported.")
            if order in named and named[order] != val:
                _issue(result, "conflicting_coefficient_alias", f"Conflicting ai{order} records.")
            named[order] = val
    if values and flag is None:
        # Serialization historically included A2. Require independent named
        # fields, or an all-zero polynomial whose indexing is immaterial.
        matches = all(
            2 * i + 2 in named and _near(named[2 * i + 2], v, absolute=1e-20)
            for i, v in enumerate(values)
        )
        if not matches and any(values):
            _issue(
                result,
                "ambiguous_asphere_order",
                "Missing use_ai2 and incomplete legacy A2 corroboration.",
            )
            return
        flag = True
        _issue(
            result,
            "corroborated_legacy_ai2",
            "Legacy ai starts at A2; named terms or zero polynomial corroborate it.",
            severity="warning",
        )
    start = 2 if flag else 4
    terms = {start + 2 * i: v for i, v in enumerate(values)}
    for order, value in named.items():
        if not _near(terms.get(order, 0.0), value, absolute=1e-20):
            _issue(
                result,
                "conflicting_asphere_coefficients",
                f"Named ai{order} disagrees with ai list.",
            )
    row["coefficients"] = {f"a{order}": value for order, value in terms.items()}
    row["source_coefficient_start_order"] = start


def parse_deeplens_json(payload: Any) -> dict[str, Any]:
    """Parse a decoded DeepLens JSON object, preserving unsupported raw data."""
    result = _result("deeplens_json")
    if not isinstance(payload, dict):
        _issue(result, "invalid_json_root", "DeepLens JSON root must be an object.")
        return _finish(result)
    result["metadata"].update(
        {
            "raw_source": copy.deepcopy(payload),
            "format_reference_commit": DEEPLENS_COMMIT,
            "geometry_convention": "d_next prefix sums from the first physical vertex at z=0",
            "configuration_count": 1,
            "configuration_index": 0,
            "configuration_name": "source_default",
        }
    )
    unknown = sorted(set(payload) - _TOP_KEYS)
    if unknown:
        _issue(
            result,
            "unsupported_global_records",
            f"Unsupported top-level optical fields: {unknown}.",
        )
    metadata = result["metadata"]
    if isinstance(payload.get("info"), str):
        metadata["title"] = payload["info"]
    for key, dest in (
        ("foclen", "focal_length_mm"),
        ("fnum", "f_number"),
        ("r_sensor", "source_sensor_semidiagonal_mm"),
        ("enpd", "entrance_pupil_diameter_mm"),
    ):
        if payload.get(key) is not None:
            try:
                metadata[dest] = _finite(payload[key])
                if metadata[dest] <= 0:
                    _issue(result, "nonpositive_specification", f"{key} must be positive.")
            except ValueError as exc:
                _issue(result, "invalid_specification", str(exc))
    for key in ("primary_wvln", "wvln_rgb"):
        if key in payload:
            values = payload[key] if isinstance(payload[key], list) else [payload[key]]
            try:
                if len(values) != (1 if key == "primary_wvln" else 3):
                    raise ValueError(f"{key} has an unsupported number of wavelengths")
                wavelengths = [_finite(v) * 1000 for v in values]
                if any(not 100 < v < 10000 for v in wavelengths):
                    raise ValueError("Wavelength outside source's documented range")
                metadata["primary_wavelength_nm" if key == "primary_wvln" else "wavelengths_nm"] = (
                    wavelengths[0] if key == "primary_wvln" else wavelengths
                )
            except (ValueError, IndexError) as exc:
                _issue(result, "invalid_wavelength", str(exc))
    source_surfaces = payload.get("surfaces")
    if not isinstance(source_surfaces, list) or not source_surfaces:
        _issue(result, "missing_surface_list", "Source has no nonempty surface list.")
        return _finish(result)
    # The lens files omit an object surface. Preserve unknown object distance;
    # never substitute an assumed infinite conjugate or a finite default.
    obj = _surface(0, "object", "object")
    obj.update(
        radius_mm=0.0,
        radius_state="plane",
        thickness_state="unknown",
        material="air",
        nd=1.0,
        material_model="air",
    )
    if "obj_depth" in payload:
        try:
            depth = _finite(payload["obj_depth"])
            if depth >= 0:
                raise ValueError("Source obj_depth must be negative")
            obj.update(thickness_mm=-depth, thickness_state="finite")
            metadata["object_distance_mm"] = -depth
        except ValueError as exc:
            _issue(result, "invalid_object_depth", str(exc))
    else:
        _issue(
            result,
            "object_distance_unknown",
            "JSON does not declare object distance.",
            severity="warning",
        )
    result["surfaces"].append(obj)
    position = 0.0
    previous_medium = "air"
    for index, source in enumerate(source_surfaces, 1):
        row = _surface(index, str(index))
        result["surfaces"].append(row)
        if not isinstance(source, dict):
            row["raw"] = {"source": source}
            _issue(result, "invalid_surface_record", f"Surface {index} must be an object.")
            continue
        row["raw"] = copy.deepcopy(source)
        typ = source.get("type")
        if typ not in {"Spheric", "Aspheric", "Aperture", "Stop"}:
            _issue(
                result,
                "unsupported_surface_type",
                f"Surface {index}: {typ!r} requires a separate optical model.",
            )
        unknown = sorted(
            key
            for key in source
            if key not in _SURFACE_KEYS and not _COEFFICIENT_KEY.fullmatch(key)
        )
        if unknown:
            _issue(
                result,
                "unsupported_surface_records",
                f"Surface {index} has unsupported keys {unknown}.",
            )
        try:
            if "is_aperture" in source and not isinstance(source["is_aperture"], bool):
                _issue(result, "invalid_aperture_flag", "is_aperture must be a JSON boolean.")
            if source.get("is_square", False) is not False:
                _issue(
                    result,
                    "unsupported_square_aperture",
                    "Square apertures are not normalized to circles.",
                )
            if source.get("diffraction", False) is not False:
                _issue(
                    result,
                    "unsupported_diffraction",
                    "A diffractive aperture needs a wave-optics model.",
                )
            if source.get("pos_xy", [0, 0]) != [0, 0] or source.get("vec_local", [0, 0, 1]) != [
                0,
                0,
                1,
            ]:
                _issue(
                    result,
                    "unsupported_decenter_or_tilt",
                    "Only coaxial forward surfaces are supported.",
                )
            if "d_next" in source:
                gap = _finite(source["d_next"])
                row.update(thickness_mm=gap, thickness_state="finite")
            else:
                gap = 0.0
                _issue(result, "missing_d_next", f"Surface {index} has no explicit following gap.")
            # Parenthesized values are rounded informational fields. Keep all
            # raw values, but only unparenthesized d must numerically agree.
            if "d" in source and not _near(_finite(source["d"]), position, absolute=2e-4):
                _issue(
                    result,
                    "conflicting_axial_position",
                    f"Surface {index} d disagrees with d_next prefix sum.",
                )
            row["axial_position_mm"] = position
            position += gap
            if typ in {"Aperture", "Stop"}:
                row.update(surface_kind="stop", radius_mm=0.0, radius_state="plane")
                if previous_medium != "air" or _medium_name(source.get("mat2", "air")) != "air":
                    _issue(
                        result,
                        "unsupported_immersed_aperture",
                        "Source aperture in a non-air medium needs review.",
                    )
                if source.get("c", 0) != 0 or source.get("roc", 0) != 0 or source.get("k", 0) != 0:
                    _issue(
                        result,
                        "conflicting_aperture_shape",
                        "Aperture carries non-planar optical shape fields.",
                    )
                _set_material(result, row, "air")
            else:
                if "roc" in source:
                    radius = _finite(source["roc"])
                    curvature = 0.0 if radius == 0 else 1.0 / radius
                    if "c" in source and not _near(_finite(source["c"]), curvature):
                        _issue(
                            result, "conflicting_curvature", f"Surface {index} roc and c disagree."
                        )
                elif "c" in source:
                    curvature = _finite(source["c"])
                    radius = 0.0 if curvature == 0 else 1.0 / curvature
                else:
                    radius = None
                    _issue(result, "missing_curvature", f"Surface {index} lacks c or roc.")
                row.update(radius_mm=radius, radius_state="plane" if radius == 0 else "finite")
                _set_material(result, row, source.get("mat2"))
                if source.get("is_aperture") is True:
                    row["surface_kind"] = "stop"
            if "mat1" in source and _medium_name(source["mat1"]) != previous_medium:
                _issue(
                    result,
                    "conflicting_incident_medium",
                    f"Surface {index} mat1 disagrees with previous mat2.",
                )
            previous_medium = _medium_name(row["material"])
            row["source_aperture_convention"] = "r_is_circular_semidiameter_mm"
            _set_aperture(row, _finite(source["r"]), semi=True)
            if typ == "Aspheric":
                row.update(conic=_finite(source["k"]), is_aspheric=True)
                _coefficients(result, row, source)
            elif any(source.get(k) for k in ("k", "ai")) or any(
                value for key, value in source.items() if _COEFFICIENT_KEY.fullmatch(key)
            ):
                _issue(
                    result,
                    "unexpected_asphere_parameters",
                    f"Surface {index} has asphere parameters with type {typ!r}.",
                )
        except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
            _issue(result, "invalid_surface_values", f"Surface {index}: {exc}")
    metadata["sensor_axial_position_mm"] = position
    if "d_sensor" in payload:
        try:
            if not _near(_finite(payload["d_sensor"]), position, absolute=2e-4):
                _issue(
                    result,
                    "conflicting_sensor_position",
                    "d_sensor disagrees with the sum of d_next gaps.",
                )
        except ValueError as exc:
            _issue(result, "invalid_sensor_position", str(exc))
    image = _surface(len(source_surfaces) + 1, "image", "image")
    image.update(
        radius_mm=0.0,
        radius_state="plane",
        thickness_mm=0.0,
        thickness_state="finite",
        axial_position_mm=position,
        material=previous_medium,
        material_model="image_plane",
    )
    result["surfaces"].append(image)
    return _finish(result)


def parse_deeplens_text(content: str) -> dict[str, Any]:
    """Decode JSON without code execution, retaining malformed-source failure."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(content, object_pairs_hook=unique_object)
    except (ValueError, TypeError) as exc:
        result = _result("deeplens_json")
        result["metadata"]["raw_source_text"] = content
        _issue(result, "invalid_json", str(exc))
        return _finish(result)
    return parse_deeplens_json(payload)


def parse_deeplens_file(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    result = parse_deeplens_text(path.read_text(encoding="utf-8-sig"))
    result["metadata"].update(source_filename=path.name, encoding="utf-8-sig")
    return result
