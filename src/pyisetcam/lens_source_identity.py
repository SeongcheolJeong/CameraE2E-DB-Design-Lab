"""Conservative identities for parsed optical prescriptions.

Documentary changes do not create new optics. Uninterpreted source controls do:
every non-documentary command/section remains in the identity, even when the
surface parser does not model its effect. This is an exact, same-format identity,
not a glass substitution, tolerance match, or patent-family matcher. Callers must
only automatically merge fully accepted parses, never partial/blocked parses.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from ._catalog_store import digest

_DOCUMENTARY_METADATA = {
    "title",
    "notes",
    "copyright",
    "author",
    "filename",
    "source_filename",
    "source_path",
    "source_url",
    "encoding",
}
_DOCUMENTARY_COMMANDS = {"NAME", "NOTE", "VERS", "AUTH", "FCOM", "COMM"}
_DOCUMENTARY_SECTIONS = {"notes", "figure"}
_DOCUMENTARY_DESCRIPTION_KEYS = {"title", "notes", "copyright", "author"}
_SURFACE_SOURCE_KEYS = {
    "raw",
    "raw_json",
    "raw_lines",
    "source_line",
    "surface_key",
    "source_surface_index",
}
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eEdD][+-]?\d+)?\Z")


def _exact_numeric_token(value: str) -> Any:
    """Normalize a numeric spelling without Decimal context rounding or floats."""
    if not _NUMBER.fullmatch(value):
        return value
    try:
        decimal = Decimal(value.replace("d", "e").replace("D", "E"))
    except InvalidOperation:
        return value
    sign, digits, exponent = decimal.as_tuple()
    digits = list(digits)
    if not any(digits):
        return {"number": [0, "0", 0]}
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    return {"number": [sign, "".join(map(str, digits)), exponent]}


def _properties(value: Any) -> Any:
    """Preserve strings (material names/identifiers) and canonicalize numbers."""
    if isinstance(value, dict):
        return {str(key): _properties(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_properties(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return _exact_numeric_token(str(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            return {"nonfinite_number": str(value)}
        return _exact_numeric_token(str(value))
    return value


def _tokens(values: Any) -> Any:
    if isinstance(values, (tuple, list)):
        return [_tokens(value) for value in values]
    if isinstance(values, str):
        return _exact_numeric_token(values)
    return _properties(values)


def _records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for record in records:
        command = str(record.get("command", "")).upper()
        if command in _DOCUMENTARY_COMMANDS:
            continue
        if not command:
            # A malformed record has no interpreted semantics. Retain its text;
            # callers should already block any parse containing such a record.
            result.append({"unparsed_record": record.get("raw", "")})
            continue
        result.append({"command": command, "values": _tokens(record.get("values", []))})
    return result


def _descriptions(rows: Any) -> Any:
    if isinstance(rows, dict):
        return {
            key: _tokens(value)
            for key, value in rows.items()
            if str(key).lower() not in _DOCUMENTARY_DESCRIPTION_KEYS
        }
    return [
        _tokens(row)
        for row in rows
        if not row or str(row[0]).lower() not in _DOCUMENTARY_DESCRIPTION_KEYS
    ]


def _sections(sections: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for name, rows in sections.items():
        name = name.lower()
        if name in _DOCUMENTARY_SECTIONS:
            continue
        result[name] = _descriptions(rows) if name == "descriptive data" else _tokens(rows)
    return result


def optical_fingerprint(parsed: dict[str, Any]) -> str:
    """Hash exact normalized properties plus source optical settings.

    Cosmetic filenames, titles, source line numbers, notes, whitespace, numeric
    spellings and Zemax writer versions are excluded. Field, pupil, environment,
    wavelength, aperture, configuration and unknown control values remain.
    Format identity deliberately prevents unproved cross-format auto-merges.
    """
    surfaces = [
        _properties({key: value for key, value in row.items() if key not in _SURFACE_SOURCE_KEYS})
        for row in parsed.get("surfaces", [])
    ]
    metadata = {}
    for key, value in parsed.get("metadata", {}).items():
        if key in _DOCUMENTARY_METADATA:
            continue
        if key == "raw_records":
            metadata[key] = _records(value)
        elif key == "raw_sections":
            metadata[key] = _sections(value)
        elif key == "descriptive_data":
            metadata[key] = _descriptions(value)
        elif key == "environment_record":
            metadata[key] = _tokens(value)
        elif key == "variable_distances":
            metadata[key] = {name: _tokens(values) for name, values in value.items()}
        else:
            metadata[key] = _properties(value)
    return digest(
        {
            "identity_schema": "camerae2e_optical_source_identity_v1",
            "format": parsed.get("format"),
            "surfaces": surfaces,
            "metadata": metadata,
        }
    )
