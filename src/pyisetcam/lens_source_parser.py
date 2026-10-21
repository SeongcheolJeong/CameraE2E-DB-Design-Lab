"""Conservative import of public sequential lens prescriptions.

``parsed`` means the supported source syntax was decoded, not that a lens is
traceable or its image quality has been checked. Unknown optical commands,
solves and configurations block the import while retaining their raw records.
No catalog material is replaced with a guessed glass.

Format evidence: RayOptics' ``zemax/zmxread.py`` and ``optical/obench.py`` at
https://github.com/mjhoptics/ray-optics/tree/master/src/rayoptics . Zemax DIAM
is a *semi*-diameter, EVENASPH PARM 1 is the r**2 term, and OpticalBench's fifth
lens-data column is the full diameter. All output lengths are millimetres;
radial coefficients have units mm**(1-order). Raw source values are retained.
"""

from __future__ import annotations

import math
import re
import shlex
from pathlib import Path
from typing import Any

PARSER_SCHEMA = "camerae2e.lens_source_prescription.v1"
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eEdD][+-]?\d+)?\Z")
_INFINITY = {"inf", "+inf", "infinity", "+infinity", "∞", "+∞"}
_UNIT_SCALE = {"MM": 1.0, "CM": 10.0, "M": 1000.0, "IN": 25.4, "INCH": 25.4}
# These records do not alter the normalized sequential surface prescription.
# Their raw text is still retained; field/pupil settings are not trace validation.
_ZMX_METADATA = set(
    """
VERS NOTE GCAT NAME UNIT MODE FNUM ENPD OBNA PUPD RAIM FTYP ROPD PICB
XFLD XFLN YFLD YFLN FWGT FWGN WAVL WAVN WAVM WWGT WWGN PWAV
POLS GLRS GSTD NSCD COFN MNUM MOFF TOLE TOL TCMM TCED DMFS
OPDX PFIL COMM LANG FIMP VERS VDSZ HIDE SLAB POPS COAT
MNEA MNCA MNCG MNEG MXCA MXCG RGLA TRAC EFFL
BLNK MEMA AUTH IWDP LUID SSID FCOM EFLY REAY OPLT OPGT
""".split()
)
_ZMX_ZERO_ONLY = set(
    """
OBDC SDMA PUSH GFAC ZVDX ZVDY ZVCX ZVCY ZVAN VDXN VDYN VCXN VCYN VANN
""".split()
)
_ZMX_SOLVES = {"PKUP", "PZUP", "SOLV", "SOLVE", "PICKUP", "VPAR", "VCON", "PPAR"}


def _number(token: str) -> float:
    token = token.strip().replace("−", "-")
    if not _NUMBER.fullmatch(token):
        raise ValueError(f"Invalid finite number {token!r}")
    result = float(token.replace("D", "E").replace("d", "e"))
    if not math.isfinite(result):
        raise ValueError(f"Nonfinite number {token!r}")
    return 0.0 if result == 0 else result


def _integer(token: str) -> int:
    value = _number(token)
    if not value.is_integer():
        raise ValueError(f"Expected integer, found {token!r}")
    return int(value)


def _issue(
    result: dict[str, Any],
    code: str,
    message: str,
    *,
    severity: str = "error",
    line: int | None = None,
) -> None:
    item: dict[str, Any] = {"code": code, "severity": severity, "message": message}
    if line is not None:
        item["line"] = line
    result["issues"].append(item)


def _result(fmt: str) -> dict[str, Any]:
    return {
        "schema": PARSER_SCHEMA,
        "format": fmt,
        "surfaces": [],
        "metadata": {"length_unit": "mm", "coefficient_convention": "sag=sum(aN*r**N)"},
        "issues": [],
        "status": "blocked",
    }


def _surface(order: int, key: str, kind: str = "surface") -> dict[str, Any]:
    return {
        "surface_order": order,
        "surface_key": key,
        "surface_kind": kind,
        "radius_mm": None,
        "thickness_mm": None,
        "nd": None,
        "vd": None,
        "material": "",
        "effective_aperture_mm": None,
        "semidiameter_mm": None,
        "aperture_convention": "effective_aperture_mm_is_full_diameter",
        "conic": 0.0,
        "is_aspheric": False,
        "coefficients": {},
        "raw": {},
    }


def _set_length(row: dict[str, Any], name: str, token: str, scale: float) -> None:
    row["raw"][name] = token
    if token.strip().lower() in _INFINITY:
        row[f"{name}_mm"] = 0.0 if name == "radius" else None
        row[f"{name}_state"] = "plane" if name == "radius" else "infinite"
        return
    value = _number(token) * scale
    if not math.isfinite(value):
        raise ValueError(f"Overflow converting {name} to mm")
    row[f"{name}_mm"] = value
    row[f"{name}_state"] = "plane" if name == "radius" and value == 0 else "finite"


def _set_aperture(row: dict[str, Any], value: float, *, semi: bool) -> None:
    if value < 0 or not math.isfinite(value):
        raise ValueError("Aperture must be finite and nonnegative")
    row["semidiameter_mm"] = value if semi else value / 2
    row["effective_aperture_mm"] = value * 2 if semi else value


def _finish(result: dict[str, Any]) -> dict[str, Any]:
    overflow_paths: list[str] = []

    def finite_json(value: Any, path: str = "") -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            overflow_paths.append(path)
            return None
        if isinstance(value, dict):
            return {k: finite_json(v, f"{path}.{k}") for k, v in value.items()}
        if isinstance(value, list):
            return [finite_json(v, f"{path}[{i}]") for i, v in enumerate(value)]
        return value

    result = finite_json(result)
    if overflow_paths:
        _issue(
            result, "numeric_conversion_overflow", f"Nonfinite converted values: {overflow_paths}"
        )
    surfaces = result["surfaces"]
    if not surfaces:
        _issue(result, "empty_prescription", "No surface records found.")
    keys = [row["surface_key"] for row in surfaces]
    if len(keys) != len(set(keys)):
        _issue(result, "duplicate_surface_key", "Surface identifiers are not unique.")
    stops = [row for row in surfaces if row["surface_kind"] == "stop"]
    if len(stops) > 1:
        _issue(result, "multiple_stops", "Multiple aperture stops require review.")
    for row in surfaces:
        if row["surface_kind"] in {"object", "image"}:
            continue
        if row["radius_mm"] is None:
            _issue(result, "unresolved_radius", f"Surface {row['surface_key']} has no radius.")
        if row["thickness_mm"] is None:
            _issue(
                result, "unresolved_thickness", f"Surface {row['surface_key']} has no finite gap."
            )
        elif row["thickness_mm"] < 0:
            _issue(result, "negative_thickness", "Negative propagation gaps require review.")
        if row["effective_aperture_mm"] in (None, 0):
            _issue(
                result,
                "aperture_unknown",
                f"Surface {row['surface_key']} aperture is missing/zero.",
                severity="warning",
            )
    result["status"] = (
        "blocked" if any(i["severity"] == "error" for i in result["issues"]) else "parsed"
    )
    return result


def parse_lens_file(
    path: str | Path, *, source_format: str | None = None, configuration_index: int | None = None
) -> dict[str, Any]:
    """Read a ZMX, OpticalBench export or explicit DeepLens JSON source.

    Accepted aliases include zmx/zemax, p2p/photons_to_photos and deeplens_json.
    Infinity is represented by a null mm value plus a state and raw token,
    never JSON Infinity. File I/O errors are raised; syntax errors become issues.
    """
    path = Path(path)
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif b"\x00" in data[:256]:
        encoding = "utf-16-le" if data[:2] == b"V\x00" else "utf-16-be"
    else:
        encoding = "utf-8-sig"
    try:
        content = data.decode(encoding)
    except UnicodeError:
        encoding = "cp1252"
        content = data.decode(encoding, errors="replace")
    result = parse_lens_text(
        content, source_format=source_format, configuration_index=configuration_index
    )
    result["metadata"].update({"source_filename": path.name, "encoding": encoding})
    return result


def parse_lens_configurations(
    path: str | Path, *, source_format: str | None = None
) -> list[dict[str, Any]]:
    """Expand every explicitly tabulated P2P configuration; never solve ZMX MCE.

    Configuration indices are zero-based source-column indices. Single-valued
    rows are shared constants. Ragged multi-valued rows block their configuration.
    """
    result = parse_lens_file(path, source_format=source_format)
    if result["format"] != "photons_to_photos":
        return [result]
    count = result["metadata"].get("configuration_count", 1)
    return [
        parse_lens_file(path, source_format=source_format, configuration_index=index)
        for index in range(count)
    ]


def parse_lens_text(
    content: str, *, source_format: str | None = None, configuration_index: int | None = None
) -> dict[str, Any]:
    """Parse text without requiring dependencies or a glass catalog."""
    fmt = (source_format or "").lower()
    if not fmt:
        fmt = "photons_to_photos" if re.search(r"(?im)^\s*\[lens data\]", content) else "zmx"
    if fmt in {"zmx", "zemax", "zemax_zmx"}:
        return _parse_zmx(content)
    if fmt in {"p2p", "photons_to_photos", "opticalbench", "optical_bench"}:
        return _parse_p2p(content, configuration_index=configuration_index)
    if fmt in {"deeplens", "deeplens_json"}:
        from .lens_source_json import parse_deeplens_text

        result = parse_deeplens_text(content)
        if configuration_index not in (None, 0):
            _issue(result, "invalid_configuration_index", "DeepLens file has one configuration.")
            result["status"] = "blocked"
        return result
    result = _result(fmt)
    _issue(result, "unsupported_format", f"Unsupported source format {fmt!r}.")
    return result


def _parse_zmx(content: str) -> dict[str, Any]:
    result = _result("zmx")
    meta = result["metadata"]
    records: list[dict[str, Any]] = []
    meta["raw_records"] = records
    for line_no, line in enumerate(content.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith(("!", "#")):
            continue
        try:
            head = line.strip().split(None, 1)
            if head[0].upper() in {"NAME", "NOTE", "COMM", "BLNK", "AUTH"}:
                # An inch mark or apostrophe in free-form comments is not a
                # broken quoted optical operand.
                parts = [head[0]] + ([head[1].strip('"')] if len(head) > 1 else [])
            else:
                parts = shlex.split(line, posix=True)
        except ValueError as exc:
            _issue(result, "invalid_record", str(exc), line=line_no)
            records.append({"line": line_no, "raw": line})
            continue
        if parts:
            records.append(
                {"line": line_no, "command": parts[0].upper(), "values": parts[1:], "raw": line}
            )
    unit_records = [r for r in records if r.get("command") == "UNIT"]
    scale = 1.0
    if not unit_records or not unit_records[0]["values"]:
        _issue(result, "units_missing", "ZMX does not explicitly declare lens units.")
        meta["source_length_unit"] = None
    else:
        unit = unit_records[0]["values"][0].upper()
        meta["source_length_unit"] = unit
        if unit not in _UNIT_SCALE:
            _issue(result, "unsupported_units", f"Unsupported ZMX lens unit {unit!r}.")
        else:
            scale = _UNIT_SCALE[unit]
        if any(r["values"][0].upper() != unit for r in unit_records[1:] if r["values"]):
            _issue(result, "conflicting_units", "Multiple different UNIT declarations.")
    meta["source_to_mm"] = scale if meta.get("source_length_unit") in _UNIT_SCALE else None
    current = None
    seen: set[tuple[int, str]] = set()
    waves: dict[int, dict[str, float | int]] = {}
    legacy_waves: list[float] = []
    conf_indices: set[int] = set()
    for record in records:
        cmd, args = record.get("command", ""), record.get("values", [])
        line_no = record["line"]
        try:
            if cmd == "SURF":
                key = _integer(args[0])
                current = _surface(len(result["surfaces"]) + 1, str(key))
                current.update({"source_surface_index": key, "surface_type": "STANDARD"})
                current["raw"]["records"] = [record]
                result["surfaces"].append(current)
                continue
            if current is not None:
                current["raw"]["records"].append(record)
            if cmd in {"CURV", "DISZ", "TYPE", "GLAS", "DIAM", "CONI", "STOP"}:
                if current is None:
                    raise ValueError(f"{cmd} occurs before SURF")
                marker = (current["surface_order"], cmd)
                if marker in seen:
                    _issue(
                        result,
                        "duplicate_surface_record",
                        f"Repeated {cmd} on a surface.",
                        line=line_no,
                    )
                seen.add(marker)
            if cmd == "TYPE":
                current["surface_type"] = args[0].upper()
                current["is_aspheric"] = args[0].upper() == "EVENASPH"
                if args[0].upper() not in {"STANDARD", "EVENASPH"}:
                    _issue(
                        result,
                        "unsupported_surface_type",
                        f"Surface type {args[0]} is unsupported.",
                        line=line_no,
                    )
            elif cmd == "CURV":
                curvature = _number(args[0])
                radius = 0.0 if curvature == 0 else scale / curvature
                if not math.isfinite(radius):
                    raise ValueError("Curvature inversion overflow")
                current["radius_mm"] = radius
                current["radius_state"] = "plane" if curvature == 0 else "finite"
                current["raw"]["curvature"] = args[0]
                if len(args) > 1 and any(_number(v) != 0 for v in args[1:] if v):
                    _issue(
                        result,
                        "unsupported_solve",
                        "CURV contains nonzero solve/control fields.",
                        line=line_no,
                    )
            elif cmd == "DISZ":
                if len(args) != 1:
                    raise ValueError("DISZ contains unsupported extra values/solve")
                _set_length(current, "thickness", args[0], scale)
            elif cmd == "CONI":
                current["conic"] = _number(args[0])
                if any(_number(v) != 0 for v in args[1:] if v):
                    _issue(
                        result,
                        "unsupported_solve",
                        "CONI contains nonzero control fields.",
                        line=line_no,
                    )
            elif cmd == "PARM":
                if current is None:
                    raise ValueError("PARM occurs before SURF")
                index, value = _integer(args[0]), _number(args[1])
                if len(args) != 2 or index < 1 or index > 8:
                    raise ValueError("Unsupported EVENASPH parameter index or control fields")
                if current["surface_type"] != "EVENASPH":
                    _issue(
                        result,
                        "unsupported_parameter",
                        "PARM requires supported EVENASPH type.",
                        line=line_no,
                    )
                else:
                    key = f"a{2 * index}"
                    if key in current["coefficients"]:
                        _issue(
                            result,
                            "duplicate_asphere_coefficient",
                            f"Repeated {key}.",
                            line=line_no,
                        )
                    current["coefficients"][key] = value * scale ** (1 - 2 * index)
            elif cmd == "GLAS":
                name = args[0]
                current["material"] = name
                if name.upper() == "MIRROR":
                    _issue(
                        result,
                        "unsupported_reflection",
                        "Mirror propagation is unsupported.",
                        line=line_no,
                    )
                elif name.upper() in {"AIR", "VACUUM"}:
                    current["nd"], current["vd"] = 1.0, None
                elif name == "___BLANK" or (len(args) > 1 and args[1] == "1"):
                    current["nd"], current["vd"] = _number(args[3]), _number(args[4])
                    current["material_model"] = "source_nd_vd"
                    if len(args) > 5 and _number(args[5]) != 0:
                        _issue(
                            result,
                            "unsupported_material_dispersion",
                            "Nonzero model-glass partial dispersion needs evaluation.",
                            line=line_no,
                        )
                else:
                    # Catalog GLAS often stores placeholder 1.5/40. Retain them
                    # only as raw data; a catalog lookup must establish nd/vd.
                    current["material_model"] = "catalog_required"
                    _issue(
                        result,
                        "catalog_material_unresolved",
                        f"Catalog glass {name!r} needs exact lookup.",
                        severity="warning",
                        line=line_no,
                    )
            elif cmd == "DIAM":
                _set_aperture(current, _number(args[0]) * scale, semi=True)
                current["computed_semidiameter_mm"] = current["semidiameter_mm"]
                current["source_aperture_convention"] = "semidiameter"
                if len(args) > 1 and _integer(args[1]) not in {0, 1}:
                    _issue(
                        result,
                        "unsupported_aperture",
                        "Noncircular or unknown DIAM aperture type.",
                        line=line_no,
                    )
                if len(args) > 2 and _number(args[2]) != 0:
                    _issue(
                        result, "unsupported_aperture", "Nonzero DIAM control field.", line=line_no
                    )
                for index, value in enumerate(args[3:], 3):
                    if value and _number(value) != 0 and not (index == 4 and _number(value) == 1):
                        _issue(
                            result,
                            "unsupported_aperture",
                            "Nondefault additional DIAM field.",
                            line=line_no,
                        )
            elif cmd in {"CLAP", "FLAP"}:
                if current is None or len(args) < 2:
                    raise ValueError(f"Invalid {cmd}")
                inner, outer = _number(args[0]) * scale, _number(args[1]) * scale
                if inner != 0 or outer < 0 or any(_number(v) != 0 for v in args[2:]):
                    _issue(
                        result,
                        "unsupported_aperture",
                        f"Unsupported annulus/control in {cmd}.",
                        line=line_no,
                    )
                elif cmd == "CLAP":
                    # Optiland's primary reader documents CLAP as r_min,r_max
                    # of the physical aperture, independent of computed DIAM.
                    current["clear_aperture_radius_mm"] = outer
                    current["clear_aperture_inner_radius_mm"] = inner
                elif current["semidiameter_mm"] is None:
                    _set_aperture(current, outer, semi=True)
                elif not math.isclose(
                    outer, current["semidiameter_mm"], rel_tol=1e-8, abs_tol=1e-9
                ):
                    _issue(
                        result, "conflicting_aperture", f"{cmd} differs from DIAM.", line=line_no
                    )
            elif cmd == "STOP":
                current["surface_kind"] = "stop"
            elif cmd == "MIRR":
                # The ordinary sequential display default occurs on refracting
                # surfaces too; physical reflection is declared by GLAS MIRROR.
                if args != ["2", "0"]:
                    _issue(
                        result,
                        "unsupported_optical_control",
                        "Nondefault MIRR retained for review.",
                        line=line_no,
                    )
            elif cmd == "NAME":
                meta["title"] = " ".join(args)
            elif cmd == "GCAT":
                meta["glass_catalogs"] = args
            elif cmd == "MODE":
                meta["mode"] = args[0]
                if args[0].upper() != "SEQ":
                    _issue(
                        result,
                        "unsupported_mode",
                        "Only sequential ZMX is supported.",
                        line=line_no,
                    )
            elif cmd == "FNUM":
                meta["f_number"] = _number(args[0])
                if len(args) > 1:
                    meta["f_number_kind"] = "paraxial_image" if _integer(args[1]) == 1 else "image"
            elif cmd == "ENPD":
                meta["entrance_pupil_diameter_mm"] = _number(args[0]) * scale
            elif cmd == "WAVM":
                index = _integer(args[0])
                waves[index] = {
                    "index": index,
                    "wavelength_nm": _number(args[1]) * 1000,
                    "weight": _number(args[2]),
                }
            elif cmd == "WAVL":
                legacy_waves = [_number(v) * 1000 for v in args]
            elif cmd == "PWAV":
                meta["primary_wavelength_index"] = _integer(args[0])
            elif cmd == "FTYP":
                meta["field_type"] = _integer(args[0])
                if len(args) >= 4:
                    meta["declared_field_count"] = _integer(args[2])
                    meta["declared_wavelength_count"] = _integer(args[3])
                if len(args) >= 7 and _integer(args[6]) != 0:
                    _issue(
                        result,
                        "unsupported_afocal_mode",
                        "Afocal image-space mode needs a separate trace contract.",
                        line=line_no,
                    )
            elif cmd == "ENVD":
                meta["environment_record"] = args
                _issue(
                    result,
                    "environment_not_applied",
                    "Source temperature/pressure retained; material environment is not evaluated.",
                    severity="warning",
                    line=line_no,
                )
            elif cmd == "CONF":
                conf_indices.add(_integer(args[0]))
                if _integer(args[0]) > 1:
                    _issue(
                        result,
                        "unsupported_multiconfiguration",
                        "Multiple configurations require expansion.",
                        line=line_no,
                    )
            elif cmd in {"MCON", "MCE", "MULT", "MCOP", "THIC"}:
                _issue(
                    result,
                    "unsupported_multiconfiguration",
                    f"Configuration record {cmd} requires expansion.",
                    line=line_no,
                )
            elif cmd in _ZMX_SOLVES:
                _issue(
                    result,
                    "unsupported_solve",
                    f"{cmd} requires solve/pickup evaluation.",
                    line=line_no,
                )
            elif cmd in _ZMX_ZERO_ONLY:
                # SDMA/GFAC also contain enabled flags; even their default zero
                # first field cannot justify discarding an unknown nonzero field.
                if cmd == "SDMA" and args in (["0", "1", "0"], ["0", "0", "0"]):
                    pass
                elif any(_number(v) != 0 for v in args):
                    severity = "warning" if cmd.startswith(("ZV", "VD", "VC", "VA")) else "error"
                    _issue(
                        result,
                        "unsupported_optical_control",
                        f"Nonzero {cmd} is retained but unsupported.",
                        severity=severity,
                        line=line_no,
                    )
            elif cmd and cmd not in _ZMX_METADATA:
                _issue(
                    result, "unsupported_command", f"Unknown optical effect of {cmd}.", line=line_no
                )
        except (ValueError, IndexError, TypeError, OverflowError) as exc:
            _issue(result, "invalid_record", f"{cmd}: {exc}", line=line_no)
    if waves:
        meta["wavelength_records"] = [waves[i] for i in sorted(waves)]
    if legacy_waves or waves:
        meta["wavelengths_nm"] = legacy_waves or [waves[i]["wavelength_nm"] for i in sorted(waves)]
        meta["wavelength_scope"] = "source_declared; inactive entries may be present"
        count = meta.get("declared_wavelength_count")
        if count is not None and 0 < count <= len(meta["wavelengths_nm"]):
            meta["wavelengths_nm"] = meta["wavelengths_nm"][:count]
            meta["wavelength_scope"] = "active count declared by FTYP"
    meta["configuration_indices"] = sorted(conf_indices)
    surfaces = result["surfaces"]
    if surfaces:
        if surfaces[0]["source_surface_index"] != 0:
            _issue(result, "object_surface_missing", "Sequential ZMX must start at SURF 0.")
        surfaces[0]["surface_kind"] = "object"
        if len(surfaces) < 2:
            _issue(result, "image_surface_missing", "No separate image surface.")
        else:
            surfaces[-1]["surface_kind"] = "image"
        if [s["source_surface_index"] for s in surfaces] != list(range(len(surfaces))):
            _issue(
                result,
                "nonsequential_surface_indices",
                "SURF identifiers are not contiguous from zero.",
            )
        meta["object_distance_mm"] = surfaces[0]["thickness_mm"]
        meta["object_distance_state"] = surfaces[0].get("thickness_state", "unknown")
        for row in surfaces:
            if row.get("clear_aperture_radius_mm") is not None:
                _set_aperture(row, row["clear_aperture_radius_mm"], semi=True)
                row["aperture_basis"] = "explicit CLAP physical aperture"
            if (row["surface_order"], "GLAS") not in seen:
                row.update({"material": "AIR", "nd": 1.0, "material_model": "source_air"})
    return _finish(result)


def _parse_p2p(content: str, *, configuration_index: int | None = None) -> dict[str, Any]:
    result = _result("photons_to_photos")
    meta = result["metadata"]
    meta.update(
        {
            "source_length_unit": "mm",
            "source_to_mm": 1.0,
            "unit_basis": "OpticalBench format convention",
        }
    )
    sections: dict[str, list[list[str]]] = {}
    meta["raw_sections"] = sections
    current: str | None = None
    for line_no, line in enumerate(content.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith(("#", "//")):
            continue
        if re.fullmatch(r"\s*\[[^]]+\]\s*", line):
            current = line.strip()[1:-1].lower()
            if current in sections and current not in {"figure", "notes"}:
                _issue(result, "duplicate_section", f"Repeated section {current!r}.", line=line_no)
            sections.setdefault(current, [])
        elif current is None:
            _issue(
                result,
                "unsectioned_content",
                "Content outside OpticalBench sections.",
                line=line_no,
            )
        else:
            sections[current].append([cell.strip() for cell in line.split("\t")])

    def mapping(name: str) -> dict[str, list[str]]:
        values: dict[str, list[str]] = {}
        for row in sections.get(name, []):
            if row[0] in values:
                _issue(result, "duplicate_variable", f"Repeated {name} key {row[0]!r}.")
            values[row[0]] = row[1:]
        return values

    constants, variables = mapping("constants"), mapping("variable distances")
    descriptions = mapping("descriptive data")
    meta["descriptive_data"] = descriptions
    if descriptions.get("title"):
        meta["title"] = descriptions["title"][0]
    meta["variable_distances"] = variables
    # Ignore only globally empty trailing TSV columns. A blank within an active
    # configuration remains missing data, never a copied neighbor or zero.
    configurations = max(
        [1]
        + [
            index + 1
            for values in variables.values()
            for index, token in enumerate(values)
            if token
        ]
    )
    if any(len(values) > configurations for values in variables.values()):
        meta["configuration_padding_policy"] = "globally_empty_trailing_columns_ignored"
    meta["configuration_count"] = configurations
    if configuration_index is not None:
        meta["configuration_index"] = configuration_index
        meta["configuration_name"] = f"column_{configuration_index + 1}"
        meta["available_configurations"] = list(range(configurations))
        if configuration_index < 0 or configuration_index >= configurations:
            _issue(
                result,
                "invalid_configuration_index",
                "Requested source configuration does not exist.",
            )
    elif configurations > 1:
        _issue(
            result,
            "unsupported_multiconfiguration",
            "Multiple OpticalBench values require explicit configuration selection.",
        )

    def resolve(token: str) -> str:
        visited: set[str] = set()
        while token in variables:
            if token in visited:
                raise ValueError(f"Cyclic variable {token!r}")
            visited.add(token)
            values = variables[token][:configurations]
            if len(values) == 1:
                token = values[0]
            elif configuration_index is not None and 0 <= configuration_index < len(values):
                if len(values) != configurations:
                    _issue(
                        result,
                        "ragged_configuration_row",
                        f"Variable {token!r} has inconsistent configuration count.",
                    )
                token = values[configuration_index]
            else:
                raise ValueError(f"Variable {token!r} has {len(values)} configurations")
        return token

    for name, dest in (
        ("Focal Length", "focal_length_mm"),
        ("F-Number", "f_number"),
        ("Image Height", "image_height_source_mm"),
        ("Angle of View", "angle_of_view_deg"),
    ):
        if name in variables and (len(variables[name]) == 1 or configuration_index is not None):
            try:
                meta[dest] = _number(resolve(name))
            except ValueError as exc:
                _issue(result, "unresolved_metadata", str(exc), severity="warning")
    obj = _surface(1, "OBJ", "object")
    obj["radius_mm"], obj["radius_state"] = 0.0, "plane"
    obj["raw"]["synthesized"] = "OpticalBench object boundary"
    if "d0" in variables:
        try:
            _set_length(obj, "thickness", resolve("d0"), 1.0)
        except ValueError as exc:
            _issue(result, "unresolved_object_distance", str(exc))
            obj["raw"]["thickness"] = "d0"
    else:
        _issue(
            result, "object_distance_missing", "No explicit d0 object distance.", severity="warning"
        )
    if "ObjectGlass" in constants:
        try:
            obj["nd"], obj["vd"] = map(_number, constants["ObjectGlass"][:2])
            obj["material"] = "ObjectGlass"
        except (ValueError, TypeError):
            _issue(result, "invalid_object_material", "Invalid ObjectGlass definition.")
    else:
        obj.update({"nd": 1.0, "material": "AIR"})
    meta.update(
        {
            "object_distance_mm": obj["thickness_mm"],
            "object_distance_state": obj.get("thickness_state", "unknown"),
        }
    )
    lens_rows = sections.get("lens data", [])
    if lens_rows:
        result["surfaces"].append(obj)
    by_key: dict[str, dict[str, Any]] = {}
    for cells in lens_rows:
        row = _surface(len(result["surfaces"]) + 1, cells[0])
        row["raw"]["cells"] = cells
        result["surfaces"].append(row)
        by_key[cells[0]] = row
        if len(cells) < 5:
            _issue(
                result,
                "invalid_lens_row",
                f"Surface {cells[0]} has fewer than five tab-separated cells.",
            )
            continue
        radius_token = cells[1]
        if radius_token in {"AS", "FS", "CG"}:
            row["source_surface_marker"] = radius_token
            if radius_token == "AS":
                row["surface_kind"] = "stop"
            elif radius_token == "FS":
                row["surface_kind"] = "field_stop"
            radius_token = "0"
        for name, token in (("radius", radius_token), ("thickness", cells[2])):
            try:
                _set_length(row, name, resolve(token), 1.0)
                row["raw"][f"source_{name}"] = cells[1 if name == "radius" else 2]
            except ValueError as exc:
                row["raw"][name] = token
                _issue(result, f"unresolved_{name}", f"Surface {cells[0]}: {exc}")
        try:
            aperture_token = cells[4]
            if row["surface_kind"] == "stop" and "Aperture Diameter" in variables:
                override = resolve("Aperture Diameter")
                # OpticalBench.js selects this column per configuration and
                # overrides AS.y=diameter/2 at the base (zero stopdown) aperture.
                # Its explicit undefined/NaN optional value retains lens-data AS.
                if override not in {"", "undefined"}:
                    aperture_token = override
                    row["aperture_basis"] = "configuration Aperture Diameter; base stopdown=0"
                    row["raw"]["lens_data_diameter"] = cells[4]
            if aperture_token:
                _set_aperture(row, _number(resolve(aperture_token)), semi=False)
                row["source_aperture_convention"] = "diameter"
        except ValueError as exc:
            _issue(result, "invalid_aperture", f"Surface {cells[0]}: {exc}")
        row["material"] = cells[6] if len(cells) > 6 else ""
        if len(cells) > 7:
            row["material_catalog"] = cells[7]
        try:
            if cells[3]:
                row["nd"] = _number(cells[3])
                row["vd"] = _number(cells[5]) if len(cells) > 5 and cells[5] else None
                row["material_model"] = "source_nd_vd"
            elif row["material"]:
                row["material_model"] = "catalog_required"
                _issue(
                    result,
                    "catalog_material_unresolved",
                    "Named glass has no numeric refractive index.",
                    severity="warning",
                )
            else:
                row.update({"nd": 1.0, "material": "AIR", "material_model": "source_air"})
        except ValueError as exc:
            _issue(result, "invalid_material", f"Surface {cells[0]}: {exc}")
    for cells in sections.get("aspherical data", []):
        if len(cells) < 3 or cells[0] not in by_key:
            _issue(
                result,
                "invalid_asphere_row",
                f"No matching surface or incomplete asphere {cells!r}.",
            )
            continue
        row = by_key[cells[0]]
        if row["is_aspheric"]:
            _issue(result, "duplicate_asphere_row", f"Repeated asphere surface {cells[0]}.")
        row["is_aspheric"] = True
        row["raw"]["aspherical_cells"] = cells
        try:
            asphere_radius: dict[str, Any] = {"raw": {}}
            _set_length(asphere_radius, "radius", cells[1], 1.0)
            if row["radius_mm"] != asphere_radius["radius_mm"]:
                _issue(
                    result,
                    "asphere_radius_conflict",
                    f"Asphere radius differs on surface {cells[0]}.",
                )
            row["conic"] = _number(cells[2])
            odd = "AsphericalOddCount" in constants
            start = 3 if odd else (2 if "AsphericalA2" in constants else 4)
            step = 1 if odd else 2
            coefficient_cells = cells[3:]
            # Tab-delimited files often pad omitted highest-order columns.
            while coefficient_cells and not coefficient_cells[-1]:
                coefficient_cells.pop()
            for index, cell in enumerate(coefficient_cells):
                if not cell:
                    _issue(
                        result,
                        "missing_asphere_coefficient",
                        f"Blank asphere term on surface {cells[0]}.",
                    )
                    continue
                row["coefficients"][f"a{start + step * index}"] = _number(cell)
        except ValueError as exc:
            _issue(result, "invalid_asphere_coefficient", f"Surface {cells[0]}: {exc}")
    known = {
        "descriptive data",
        "constants",
        "variable distances",
        "lens data",
        "aspherical data",
        "figure",
        "notes",
        "group data",
        "element data",
    }
    for name, rows in sections.items():
        if name not in known and rows:
            _issue(
                result, "unsupported_section", f"Section {name!r} is retained but not interpreted."
            )
    # Unknown constants can alter the surface equation (e.g. diffractive orders).
    safe_constants = {"Aspherical", "AsphericalA2", "AsphericalOddCount", "ObjectGlass", "Scaled"}
    for name in constants:
        if name not in safe_constants:
            _issue(result, "unsupported_constant", f"Constant {name!r} requires review.")
    if lens_rows:
        img = _surface(len(result["surfaces"]) + 1, "IMG", "image")
        img.update({"radius_mm": 0.0, "radius_state": "plane"})
        img["raw"]["synthesized"] = (
            "OpticalBench terminal image boundary after final prescribed gap"
        )
        result["surfaces"].append(img)
    return _finish(result)
