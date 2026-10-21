"""Optional exact named-glass resolution from locally installed opticalglass data."""

from __future__ import annotations

import copy
from importlib.metadata import version
from typing import Any

from ._catalog_store import digest

_CATALOGS = {name.upper(): name for name in ("Schott", "Ohara", "Hoya", "CDGM", "Hikari", "Sumita")}


def resolve_catalog_glasses(parsed: dict[str, Any]) -> dict[str, Any]:
    """Resolve exact names only within the source's declared catalog list.

    No online queries, fuzzy material substitutes, or GLAS placeholder numbers.
    The installed dispersion coefficients and their hash accompany each lookup.
    A missing material remains unresolved. Requires the optional opticalglass
    package (also installed by RayOptics), only when explicitly requested.
    """
    from opticalglass import glasserror, glassfactory

    result = copy.deepcopy(parsed)
    declared = result.get("metadata", {}).get("glass_catalogs", [])
    catalogs = list(dict.fromkeys(_CATALOGS[c.upper()] for c in declared if c.upper() in _CATALOGS))
    resolved = []
    for row in result.get("surfaces", []):
        if row.get("material_model") != "catalog_required":
            continue
        name = row.get("material", "").strip()
        matches = []
        for catalog in catalogs:
            try:
                glass = glassfactory.create_glass(name, catalog)
            except glasserror.GlassError:
                continue
            if glass.name().strip().upper() != name.upper():
                continue  # The library's decoded-name fallback is not exact matching.
            matches.append(glass)
        if len(matches) != 1:
            row["material_resolution"] = {
                "status": "ambiguous" if matches else "unavailable",
                "declared_catalogs": declared,
                "matched_catalogs": [g.catalog_name() for g in matches],
            }
            continue
        glass = matches[0]
        nd, nf, nc = (float(glass.rindex(w)) for w in ("d", "F", "C"))
        dispersion = {
            "catalog": glass.catalog_name(),
            "name": glass.name(),
            "implementation": type(glass).__module__ + "." + type(glass).__name__,
            "coefficients": [float(x) for x in glass.coefs],
            "package": "opticalglass",
            "package_version": version("opticalglass"),
        }
        row.update(nd=nd, vd=(nd - 1) / (nf - nc), material_model="exact_catalog_lookup")
        row["material_resolution"] = {
            "status": "resolved_exact_name",
            "dispersion": dispersion,
            "dispersion_sha256": digest(dispersion),
            "index_reference": "Fraunhofer d/F/C lines via catalog dispersion",
        }
        resolved.append(row["surface_key"])
    result["metadata"]["material_resolution"] = {
        "enabled": True,
        "method": "unique_exact_name_in_declared_local_catalogs",
        "resolved_surface_keys": resolved,
        "package_version": version("opticalglass"),
    }
    return result
