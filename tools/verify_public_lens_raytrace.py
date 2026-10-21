#!/usr/bin/env python3
"""Independent RayOptics geometry and sparse-ray checks for acquired lens sources.

Run with the separately installed RayOptics interpreter. This is not a PSF,
MTF, diffraction, manufacturing-accuracy, or maximum-field validation.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import io
import json
import math
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Explicit bounded diagnostic sample; first source configuration is selected
# only through the parser's explicit configuration_index API.
CASES = [
    "lens_designs/raw/mobile_imaging/US07889441-1.ZMX",
    "lens_designs/raw/mobile_imaging/US07095571-1.ZMX",  # unavailable glass control
    "lens_designs/raw/mobile_imaging/EP1357414A1_ex1.zmx",
    "lens_designs/raw/photographic_prime/CN104101985_Example01P.zmx",
    "lens_designs/raw/photographic_prime/US20130057972_Example03P.zmx",
    "photons_to_photos/raw/prescriptions/JP2015-114366_Example01P.txt",
    "photons_to_photos/raw/prescriptions/KR1020140184971_Example01P.txt",
    "photons_to_photos/raw/prescriptions/US003874771_Example05P.txt",
    "photons_to_photos/raw/prescriptions/US20220066176_Example01P.txt",  # source dispute control
]
WAVELENGTH_NM = 587.5618
FIELD_DEG = [0.0, 5.0]
PUPIL_POINTS = [[0.0, 0.0], [0.0, 0.2], [0.0, -0.2], [0.2, 0.0]]


def compare_geometry(parsed: dict, native) -> dict:
    """Compare decoded scalar geometry to a separately implemented ZMX reader."""
    rows, seq = parsed["surfaces"], native.seq_model
    scale = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "inches": 25.4}.get(native.system_spec.dimensions)
    if scale is None:
        raise ValueError(f"Unsupported native units: {native.system_spec.dimensions}")
    mismatches, checks = [], Counter()

    def check(key, expected, actual, surface=None, atol=1e-10):
        checks[key] += 1
        same = (
            math.isclose(expected, actual, rel_tol=1e-10, abs_tol=atol)
            if isinstance(expected, (int, float)) and isinstance(actual, (int, float))
            else expected == actual
        )
        if not same:
            mismatches.append(
                {"surface": surface, "field": key, "parser": expected, "native": actual}
            )

    check("surface_count", len(rows), len(seq.ifcs))
    check("length_unit_mm", parsed["metadata"]["length_unit"], "mm")
    for index, (row, ifc) in enumerate(zip(rows, seq.ifcs, strict=False)):
        radius = 0.0 if ifc.profile.cv == 0 else scale / ifc.profile.cv
        check("radius_mm", row["radius_mm"], radius, index)
        check("conic", row["conic"], getattr(ifc.profile, "cc", 0.0), index)
        coefficients = getattr(ifc.profile, "coefs", [])
        powers = set(int(key[1:]) for key in row["coefficients"]) | set(
            range(2, len(coefficients) * 2 + 1, 2)
        )
        for power in sorted(powers):
            actual = (
                coefficients[power // 2 - 1] / scale ** (power - 1)
                if power // 2 <= len(coefficients)
                else 0.0
            )
            check(f"a{power}", row["coefficients"].get(f"a{power}", 0.0), actual, index, atol=1e-15)
        if index < len(seq.gaps):
            if row.get("thickness_state") == "infinite":
                check(
                    "object_infinity_1e10_native_convention",
                    1e10 * scale,
                    seq.gaps[index].thi * scale,
                    index,
                )
            else:
                check("thickness_mm", row["thickness_mm"], seq.gaps[index].thi * scale, index)
        # RayOptics assigns default aperture=1 to 0/missing boundary apertures.
        if row["surface_kind"] not in {"object", "image"} and row.get("semidiameter_mm"):
            check("semidiameter_mm", row["semidiameter_mm"], ifc.max_aperture * scale, index)
    stop_indices = [i for i, row in enumerate(rows) if row["surface_kind"] == "stop"]
    expected_stop = stop_indices[0] if len(stop_indices) == 1 else None
    check("stop_surface_index", expected_stop, seq.stop_surface)
    stop_bug = expected_stop == 1 and seq.stop_surface is None
    numerical_mismatches = [item for item in mismatches if item["field"] != "stop_surface_index"]
    return {
        "scalar_geometry_passed": not numerical_mismatches,
        "all_checks_passed": not mismatches,
        "check_counts": dict(checks),
        "mismatches": mismatches,
        "native_first_surface_stop_bug_observed": stop_bug,
        "stop_bug_explanation": (
            "RayOptics zmxread calls SequentialModel.set_stop while only object and surface 1 "
            "exist; set_stop requires more than two interfaces, leaving stop None."
        )
        if stop_bug
        else None,
        "native_dimensions": native.system_spec.dimensions,
        "source_to_mm": parsed["metadata"].get("source_to_mm"),
        "scope": (
            "Radii, finite gaps, infinite-object convention, conics, polynomial coefficients, "
            "full/semi aperture conversion, and stop; not optical-control equivalence."
        ),
    }


def diagnostic_specs(model, f_number: float) -> None:
    from rayoptics.raytr.opticalspec import FieldSpec, PupilSpec, WvlSpec

    osp = model.optical_spec
    osp["pupil"] = PupilSpec(osp, key=("image", "f/#"), value=f_number)
    osp["fov"] = FieldSpec(osp, key=("object", "angle"), flds=FIELD_DEG)
    osp["wvls"] = WvlSpec([(WAVELENGTH_NM, 1.0)], ref_wl=0)


def build_normalized_model(parsed: dict, native=None):
    from rayoptics.elem.profiles import EvenPolynomial
    from rayoptics.optical.opticalmodel import OpticalModel

    if parsed["status"] != "parsed":
        raise ValueError("Parser-blocked data is ineligible for raytrace smoke")
    rows = parsed["surfaces"]
    if rows[0]["surface_kind"] != "object" or rows[-1]["surface_kind"] != "image":
        raise ValueError("Explicit object/image boundaries required")
    if sum(row["surface_kind"] == "stop" for row in rows) != 1:
        raise ValueError("Exactly one explicit aperture stop is required")
    if rows[0].get("nd") != 1.0:
        raise ValueError("This diagnostic requires an explicit air object medium")
    if any(
        int(key[1:]) % 2 and value != 0
        for row in rows
        for key, value in row.get("coefficients", {}).items()
    ):
        raise ValueError("Odd-order aspheres require a different RayOptics profile")
    if not parsed["metadata"].get("f_number"):
        raise ValueError("Source f-number missing; no pupil inferred")
    model = OpticalModel()
    model.radius_mode = True
    sm = model.seq_model
    sm.do_apertures = False
    sm.gaps[0].thi = (
        1e10 if rows[0].get("thickness_state") == "infinite" else rows[0]["thickness_mm"]
    )
    if sm.gaps[0].thi is None:
        raise ValueError("Unknown object distance")
    materials = []
    for index, row in enumerate(rows[1:-1], 1):
        if row.get("thickness_mm") is None or row["thickness_mm"] < 0:
            raise ValueError(f"Unresolved/negative gap at {row['surface_key']}")
        if not row.get("semidiameter_mm"):
            raise ValueError(f"Unknown aperture at {row['surface_key']}")
        surface = [row["radius_mm"], row["thickness_mm"]]
        if row.get("material_model") == "catalog_required":
            if native is None:
                raise ValueError(f"Unresolved catalog glass {row['material']}")
            medium = native.seq_model.gaps[index].medium
            if medium.name().casefold() != row["material"].casefold():
                raise ValueError(f"Native glass name differs: {row['material']} / {medium.name()}")
            surface.append(medium)
            mode = "exact_named_native_catalog_medium"
        elif row.get("nd") == 1.0:
            surface.append("air")
            mode = "source_air"
        elif row.get("nd") and row.get("vd"):
            surface.extend([row["nd"], row["vd"]])
            mode = "source_nd_vd_model_glass"
        else:
            raise ValueError(f"Unresolved material at {row['surface_key']}")
        sm.add_surface(surface, sd=row["semidiameter_mm"])
        if row["is_aspheric"] or row["conic"]:
            max_order = max((int(key[1:]) for key in row["coefficients"]), default=2)
            coefficients = [
                row["coefficients"].get(f"a{order}", 0.0) for order in range(2, max_order + 1, 2)
            ]
            sm.ifcs[sm.cur_surface].profile = EvenPolynomial(
                r=row["radius_mm"], cc=row["conic"], coefs=coefficients
            )
        if row["surface_kind"] == "stop":
            sm.set_stop()
        materials.append(
            {"surface": row["surface_key"], "source_name": row["material"], "mode": mode}
        )
    diagnostic_specs(model, parsed["metadata"]["f_number"])
    model.update_model()
    return model, materials


def trace_smoke(model) -> dict:
    import numpy as np
    from rayoptics.raytr.trace import trace_ray

    rays = []
    for angle, field in zip(FIELD_DEG, model.optical_spec["fov"].fields, strict=False):
        for pupil in PUPIL_POINTS:
            item = {"field_angle_deg": angle, "relative_pupil": pupil}
            try:
                result = trace_ray(
                    model, pupil, field, WAVELENGTH_NM, check_apertures=True, apply_vignetting=False
                )
                package, error = result
                if error is not None:
                    item.update(status="failed", error_type=type(error).__name__, error=str(error))
                else:
                    ray, opd, wavelength = package
                    points = np.asarray([segment[0] for segment in ray], dtype=float)
                    directions = np.asarray([segment[1] for segment in ray], dtype=float)
                    finite = bool(
                        np.isfinite(points).all()
                        and np.isfinite(directions).all()
                        and math.isfinite(opd)
                    )
                    complete = len(ray) == len(model.seq_model.ifcs)
                    item.update(
                        status="passed" if finite and complete else "failed",
                        finite=finite,
                        complete=complete,
                        surface_intersections=len(ray),
                        image_intercept_mm=points[-1].tolist(),
                        optical_path_delta_mm=float(opd),
                    )
            except Exception as exc:
                item.update(status="failed", error_type=type(exc).__name__, error=str(exc))
            rays.append(item)
    return {
        "status": "passed" if all(r["status"] == "passed" for r in rays) else "failed",
        "apertures_checked": True,
        "ray_count": len(rays),
        "passed_ray_count": sum(r["status"] == "passed" for r in rays),
        "rays": rays,
    }


def run_case(
    relative: str, *, source_format: str | None = None, configuration_index: int = 0
) -> dict:
    from rayoptics.zemax import zmxread

    from pyisetcam.lens_source_parser import parse_lens_file

    path = ROOT / "camerae2e_db/sources" / relative
    source_bytes = path.read_bytes()
    parsed = parse_lens_file(
        path, source_format=source_format, configuration_index=configuration_index
    )
    case = {
        "path": str(path.relative_to(ROOT)),
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "parser_status": parsed["status"],
        "parser_issues": parsed["issues"],
        "format": parsed["format"],
        "configuration_index": parsed["metadata"].get("configuration_index"),
        "configuration_count": parsed["metadata"].get("configuration_count"),
        "image_quality_validated": False,
    }
    native = None
    native_info = ({}, {})
    if path.suffix.lower() == ".zmx":
        try:
            with tempfile.TemporaryDirectory(prefix="camerae2e_rayoptics_") as temp:
                copy = Path(temp) / path.name
                copy.write_bytes(source_bytes)
                native, native_info = zmxread.read_lens(
                    copy, source_bytes.decode(parsed["metadata"]["encoding"])
                )
            case["native_import"] = {
                "status": "imported",
                "contents": native_info[0],
                "missing_or_substituted_glasses": native_info[1],
            }
            case["native_geometry_comparison"] = compare_geometry(parsed, native)
        except Exception as exc:
            case["native_import"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    reasons = []
    if parsed["status"] != "parsed":
        reasons.append("Parser reports unresolved or unsupported source data")
    if native_info[1]:
        reasons.append(
            "Native importer reported missing/substituted glasses; its fallback media are rejected"
        )
    if path.suffix.lower() == ".zmx" and native is None:
        reasons.append("No independent native model available")
    if case.get("native_geometry_comparison", {}).get("scalar_geometry_passed") is False:
        reasons.append("Native scalar geometry mismatch")
    if reasons:
        case["normalized_raytrace"] = {"status": "blocked", "reasons": reasons}
        return case
    try:
        model, materials = build_normalized_model(parsed, native)
    except ValueError as exc:
        case["normalized_raytrace"] = {"status": "blocked", "reasons": [str(exc)]}
        return case
    try:
        case["normalized_materials"] = materials
        case["normalized_raytrace"] = trace_smoke(model)
        if native is not None and case["native_geometry_comparison"]["all_checks_passed"]:
            diagnostic_specs(native, parsed["metadata"]["f_number"])
            native.update_model()
            case["native_raytrace"] = trace_smoke(native)
            deltas = []
            for left, right in zip(
                case["normalized_raytrace"]["rays"], case["native_raytrace"]["rays"], strict=False
            ):
                if left["status"] == right["status"] == "passed":
                    deltas.append(
                        max(
                            abs(a - b)
                            for a, b in zip(
                                left["image_intercept_mm"],
                                right["image_intercept_mm"],
                                strict=False,
                            )
                        )
                    )
            case["native_normalized_max_image_intercept_delta_mm"] = max(deltas) if deltas else None
    except Exception as exc:
        case["normalized_raytrace"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    if path.read_bytes() != source_bytes:
        raise RuntimeError("Raw source changed during validation")
    return case


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "reports/lens_source_expansion/raytrace_smoke.json"
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Source-relative path; repeat to replace the default diagnostic sample",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        action="append",
        default=[],
        help="Resolve explicit source formats for selected --case paths",
    )
    parser.add_argument(
        "--case-configuration",
        nargs=2,
        action="append",
        default=[],
        metavar=("PATH", "INDEX"),
        help="Add a source path with an explicit zero-based configuration",
    )
    args = parser.parse_args()
    formats = {}
    for manifest in args.manifest:
        for entry in json.loads(manifest.read_text())["files"]:
            if entry.get("path"):
                formats[(manifest.parent / entry["path"]).resolve()] = entry.get("format")
    cases = []
    selected = [(relative, 0) for relative in args.case or CASES]
    selected += [(relative, int(index)) for relative, index in args.case_configuration]
    for relative, configuration in selected:
        path = (ROOT / "camerae2e_db/sources" / relative).resolve()
        if not path.is_relative_to((ROOT / "camerae2e_db/sources").resolve()):
            raise ValueError("Diagnostic input must be inside the source directory")
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                case = run_case(
                    relative, source_format=formats.get(path), configuration_index=configuration
                )
        except Exception as exc:
            case = {
                "path": relative,
                "normalized_raytrace": {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            }
        case["engine_output"] = output.getvalue()
        cases.append(case)
        print(f"{Path(relative).name}: {case['normalized_raytrace']['status']}")
    report = {
        "schema": "camerae2e_public_lens_raytrace_smoke_v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "tool_path": str(Path(__file__).resolve().relative_to(ROOT)),
        "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "parser_path": "src/pyisetcam/lens_source_parser.py",
        "parser_sha256": hashlib.sha256(
            (ROOT / "src/pyisetcam/lens_source_parser.py").read_bytes()
        ).hexdigest(),
        "parser_dependencies_sha256": {
            name: hashlib.sha256((ROOT / "src/pyisetcam" / name).read_bytes()).hexdigest()
            for name in ("lens_source_json.py",)
            if (ROOT / "src/pyisetcam" / name).is_file()
        },
        "interpreter": sys.executable,
        "rayoptics_version": importlib.metadata.version("rayoptics"),
        "scope": (
            "Independent scalar geometry comparison and sparse finite geometrical rays, "
            "not diffraction/PSF/MTF or source correctness."
        ),
        "settings": {
            "wavelength_nm": WAVELENGTH_NM,
            "field_angles_deg": FIELD_DEG,
            "relative_pupil_points": PUPIL_POINTS,
            "pupil": "Source f-number; diagnostic object field 0 and 5 degrees",
            "aperture_checks": True,
            "infinite_object_numeric_gap_mm": 1e10,
            "nd_vd_materials": (
                "RayOptics model-glass dispersion from source nd/vd; no guessed catalog substitutes"
            ),
            "p2p_configuration_policy": (
                "Explicit per-case zero-based source column, default configuration_index=0; "
                "only the listed configurations are tested."
            ),
        },
        "case_status_counts": dict(
            Counter(case["normalized_raytrace"]["status"] for case in cases)
        ),
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps(report["case_status_counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
