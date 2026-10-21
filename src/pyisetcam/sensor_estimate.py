"""Visible-light, database-derived sensor estimates with explicit assumptions.

This model combines common CFA transmission proxies, reported pixel geometry,
and bulk-silicon absorption. It is neither measured product EQE nor FDTD/TCAD.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

import numpy as np

from .types import Sensor

_KIND = "db_estimate_v1"
_WAVE = np.arange(400.0, 701.0, 10.0)
_READOUT_DEFAULTS = {
    "gain": 1.0,
    "full_well_e": 10000.0,
    "read_noise_e": 10.0,
    "dark_current_e_per_s": 10.0,
    "bit_depth": 12,
    "black_level_dn": 64.0,
}
_SUPPORTED_PATTERNS = {
    "bayer",
    "rggb_bayer",
    "quad_bayer",
    "tetracell_bayer",
    "monochrome",
    "clear",
}
_SUPPORTED_MODALITIES = {
    "cmos_image_sensor",
    "image_sensor",
    "global_shutter_cis",
    "nir_cis",
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _positive(value: Any, name: str, *, zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a finite {'nonnegative' if zero else 'positive'} number."
        ) from exc
    if not np.isfinite(number) or number < 0 or (not zero and number == 0):
        raise ValueError(f"{name} must be a finite {'nonnegative' if zero else 'positive'} number.")
    return number


def _assumptions(value: Mapping[str, Any] | None) -> dict[str, Any]:
    supplied = dict(value or {})
    allowed = {"effective_efficiency", "active_si_thickness_um", *_READOUT_DEFAULTS}
    unknown = set(supplied) - allowed
    if unknown:
        raise ValueError(f"Unknown db_estimate_v1 assumptions: {sorted(unknown)}.")
    resolved = {"effective_efficiency": 0.75, **_READOUT_DEFAULTS, **supplied}
    for key in resolved:
        resolved[key] = _positive(
            resolved[key],
            key,
            zero=key
            in {"effective_efficiency", "read_noise_e", "dark_current_e_per_s", "black_level_dn"},
        )
    if resolved["effective_efficiency"] > 1:
        raise ValueError("effective_efficiency must lie in [0, 1].")
    bits = resolved["bit_depth"]
    if int(bits) != bits or not 1 <= bits <= 24:
        raise ValueError("bit_depth must be an integer from 1 to 24.")
    resolved["bit_depth"] = int(bits)
    if resolved["black_level_dn"] >= 2 ** int(bits) - 1:
        raise ValueError("black_level_dn must be below the ADC maximum code.")
    return resolved


def _enriched_context(root: str | Path | None) -> tuple[Path, dict[str, dict[str, Any]]]:
    from .image_sensor_db import image_sensor_db_root

    selected = image_sensor_db_root(root).resolve()
    candidates = [selected, selected.parent / "image_sensor_db"]
    for candidate in candidates:
        if (candidate / "sensor_catalog.json").is_file() and (
            candidate / "optical_qe_db/models"
        ).is_dir():
            records: dict[str, dict[str, Any]] = {}
            for item in _read_json(candidate / "sensor_catalog.json").get("records", []):
                code = str(item.get("code", "")).lower()
                if not code or code in records:
                    raise ValueError(
                        "Enriched sensor catalog requires unique, nonempty report codes."
                    )
                records[code] = item
            return candidate, records
    raise FileNotFoundError(
        f"db_estimate_v1 needs an enriched image_sensor_db beside {selected}, "
        "including sensor_catalog.json and optical_qe_db/models; supply root to that catalog."
    )


def _local_generated_path(root: Path, value: Any, directory: str) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    # Relocated catalogs retain the original absolute authoring paths.
    local = root / directory / path.name
    if local.is_file():
        return local
    if not path.is_absolute() and (root / path).is_file():
        return root / path
    return None


def _optical_model(root: Path, raw: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    code = str(raw.get("code", ""))
    stack = raw.get("generated_files", {}).get("stack_config")
    model_dir = root / "optical_qe_db/models"
    candidates = [] if not stack else [model_dir / f"{Path(stack).stem}.json"]
    candidates.extend(model_dir.glob(f"{code.lower().replace('-', '_')}_*.json"))
    matches = {}
    for path in candidates:
        if path.is_file():
            model = _read_json(path)
            if str(model.get("code", "")).lower() == code.lower():
                matches[path.resolve()] = model
    if len(matches) != 1:
        raise ValueError(
            f"{code}: expected one optical model matched by report code; found {len(matches)}."
        )
    return next(iter(matches.items()))


def _field_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, Mapping) else value


def _cfa_transmission(
    optical: Mapping[str, Any], pattern: str, code: str
) -> tuple[list[str], np.ndarray]:
    names = ["clear"] if pattern in {"monochrome", "clear"} else ["red", "green", "blue"]
    proxy = optical.get("cfa_proxy_nk", {})
    channels = proxy.get("channels", {})
    if not proxy.get("enabled", False) or any(name not in channels for name in names):
        raise ValueError(
            f"{code}: missing CFA transmission curves for {names}; enrich optical_qe_db first."
        )
    curves = []
    for name in names:
        rows = channels[name].get("data", [])
        try:
            wave = np.asarray([row["wavelength_nm"] for row in rows], dtype=float)
            curve = np.asarray([row["transmission_absorption_only"] for row in rows], dtype=float)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{code}: malformed {name} CFA transmission curve.") from exc
        if (
            wave.size < 2
            or not np.all(np.isfinite(wave))
            or not np.all(np.diff(wave) > 0)
            or not np.all(np.isfinite(curve))
            or np.any((curve < 0) | (curve > 1))
            or wave[0] > _WAVE[0]
            or wave[-1] < _WAVE[-1]
        ):
            raise ValueError(
                f"{code}: {name} CFA curve must cover 400–700 nm "
                "with unique ordered samples in [0, 1]."
            )
        curves.append(np.interp(_WAVE, wave, curve))
    return names, np.column_stack(curves)


def _recover_reviewed_missing_cfa(
    optical: Mapping[str, Any], code: str
) -> tuple[Mapping[str, Any], dict[str, Any] | None]:
    """Recover one source-confirmed mono report, never a generic missing CFA."""
    from .sensor_simulation_policy import sensor_simulation_policy

    pattern = _field_value(optical.get("optical", {}).get("cfa", {}).get("pattern"))
    if pattern is not None or code.upper() != "DEP-1902-801":
        return optical, None
    policy = sensor_simulation_policy(code)
    recovery = policy.get("missing_cfa_recovery")
    if recovery is None:
        return optical, None
    proxy = optical.get("cfa_proxy_nk", {})
    if proxy.get("enabled") or proxy.get("channels"):
        raise ValueError(f"{code}: missing CFA conflicts with existing optical curves.")
    updated = copy.deepcopy(dict(optical))
    updated["optical"]["cfa"]["pattern"] = {
        "value": "monochrome",
        "source_kind": "reviewed_report_text",
        "evidence": copy.deepcopy(policy["evidence"]),
    }
    updated["cfa_proxy_nk"] = {
        "enabled": True,
        "source_kind": "explicit_unity_color_filter_absorption_assumption",
        "thickness_um": {
            "value": None,
            "source_kind": "not_applicable_color_filter_absorption_removed",
        },
        "channels": {
            "clear": {
                "data": [
                    {"wavelength_nm": float(wave), "transmission_absorption_only": 1.0}
                    for wave in _WAVE
                ],
            },
        },
    }
    provenance = {
        "original_cfa_pattern": None,
        "applied_cfa_pattern": "monochrome",
        "policy": policy,
        "transmission_scope": recovery["transmission_scope"],
        "measured_transmission": False,
        "measured_qe": False,
    }
    return updated, provenance


def _model_from_record(
    record: Mapping[str, Any],
    context: tuple[Path, dict[str, dict[str, Any]]],
    assumptions: Mapping[str, Any] | None,
) -> dict[str, Any]:
    root, records = context
    code = str(record["code"])
    raw = records.get(code.lower())
    if raw is None:
        raise ValueError(f"{code}: no paired enriched record with this report code.")
    path, optical = _optical_model(root, raw)
    optical, cfa_recovery = _recover_reviewed_missing_cfa(optical, code)
    specs = raw.get("derived_specs", {})
    geometry = optical.get("optical", {}).get("geometry", {})
    modality = specs.get("sensor_modality")
    if modality is None:
        modality = _field_value(
            optical.get("optical", {}).get("sensor_context", {}).get("sensor_modality")
        )
    if modality not in _SUPPORTED_MODALITIES:
        raise ValueError(
            f"{code}: unsupported or missing sensor modality {modality!r}; "
            "visible CMOS estimates only."
        )
    pattern = _field_value(optical.get("optical", {}).get("cfa", {}).get("pattern"))
    if pattern not in _SUPPORTED_PATTERNS:
        raise ValueError(
            f"{code}: unsupported or missing CFA pattern {pattern!r}; "
            "provide known RGB or clear CFA data."
        )
    pitch = specs.get("pixel_pitch_um")
    pitch_source = "extracted_catalog"
    if pitch is None:
        pitch_field = geometry.get("pixel_pitch_um", {})
        pitch = _field_value(pitch_field)
        pitch_source = pitch_field.get("source_kind", "unavailable")
        if pitch_source != "extracted":
            pitch = None
    try:
        pitch = _positive(pitch, "pixel_pitch_um")
    except ValueError as exc:
        raise ValueError(
            f"{code}: missing or invalid extracted pixel_pitch_um; enrich the catalog geometry."
        ) from exc
    resolved = _assumptions(assumptions)
    thickness = specs.get("active_si_thickness_um")
    thickness_source = "extracted_catalog"
    if thickness is None:
        thickness = _field_value(geometry.get("active_si_thickness_um"))
        thickness_source = geometry.get("active_si_thickness_um", {}).get(
            "source_kind", "unavailable"
        )
    stack_path = None
    if thickness is None:
        stack_path = _local_generated_path(
            root, raw.get("generated_files", {}).get("stack_config"), "generated_stack_configs"
        )
        if stack_path is not None:
            stack = _read_json(stack_path)
            source_code = stack.get("calibration_status", {}).get("source_report_code")
            if str(source_code).lower() != code.lower():
                raise ValueError(f"{code}: generated stack report code mismatch.")
            thickness = stack.get("geometry_um", {}).get("si_thickness")
            thickness_source = "generated_stack_fallback"
    if "active_si_thickness_um" in resolved:
        thickness = resolved["active_si_thickness_um"]
        thickness_source = "user_assumption"
    try:
        thickness = _positive(thickness, "active_si_thickness_um")
    except ValueError as exc:
        raise ValueError(
            f"{code}: missing valid active Si thickness; enrich it or "
            "supply assumptions.active_si_thickness_um."
        ) from exc
    names, transmission = _cfa_transmission(optical, pattern, code)
    material = json.loads(
        files("pyisetcam")
        .joinpath("data/sensor_estimate/si_green_2008_visible.json")
        .read_text(encoding="utf-8")
    )
    alpha = np.interp(_WAVE, material["wavelength_nm"], material["alpha_per_um"])
    absorption = -np.expm1(-alpha * thickness)
    eqe = transmission * absorption[:, None] * resolved["effective_efficiency"]
    return {
        "kind": _KIND,
        "sensor_id": record["sensor_id"],
        "report_code": code,
        "estimate_only": True,
        "wavelength_nm": _WAVE.tolist(),
        "eqe": eqe.tolist(),
        "filter_names": names,
        "cfa_pattern": pattern,
        "pixel_pitch_um": pitch,
        "active_si_thickness_um": thickness,
        "cfa_transmission": transmission.tolist(),
        "silicon_absorption": absorption.tolist(),
        "assumptions": resolved,
        "readout": {key: resolved[key] for key in _READOUT_DEFAULTS},
        "provenance": {
            "catalog_path": str(root / "sensor_catalog.json"),
            "optical_model_path": str(path),
            "matched_by": "report_code",
            "pixel_pitch_source": pitch_source,
            "active_si_thickness_source": thickness_source,
            "generated_stack_path": str(stack_path) if stack_path else None,
            "cfa_source": (
                "source_reviewed_monochrome_unity_color_filter_absorption_assumption"
                if cfa_recovery is not None
                else "common_material_proxy_with_sensor_thickness_scaling"
            ),
            "source_scope_recovery": cfa_recovery,
            "cfa_thickness": copy.deepcopy(optical.get("cfa_proxy_nk", {}).get("thickness_um")),
            "silicon_material": material["source"],
            "readout_source": "model_defaults_with_user_overrides",
            "assumption_sources": {
                key: "user_assumption" if key in (assumptions or {}) else "db_estimate_v1_default"
                for key in resolved
            },
            "measured_accuracy_verified": False,
            "effective_efficiency_definition": "legacy_lumped_reflection_collection_other_losses",
            "formula": "T_CFA * (1 - exp(-alpha_Si * thickness_um)) * effective_efficiency",
            "limitations": [
                "Visible 400–700 nm estimate; common CFA proxies are not measured product spectra.",
                "Bulk Si at 300 K; no thin-film interference, CRA, microlens "
                "or spatial crosstalk model.",
                "Collection, reflection and other losses are lumped into effective_efficiency.",
                "Readout defaults are assumptions, not extracted product specifications.",
                "Catalog array dimensions and shutter flags are not consumed.",
            ],
        },
    }


def image_sensor_db_model(
    sensor_id: str,
    *,
    root: str | Path | None = None,
    assumptions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe optical/readout estimate for a selected report.

    ``root`` may point to sensor_db or its enriched image_sensor_db sibling.
    Missing geometry/CFA evidence is an error, never an implicit generic Bayer
    sensor. Supported assumptions are effective_efficiency (default 0.75),
    active_si_thickness_um, and the electron-domain keys in ``readout``.
    """
    from .image_sensor_db import image_sensor_db_get

    return _model_from_record(
        image_sensor_db_get(sensor_id, root), _enriched_context(root), assumptions
    )


def image_sensor_db_model_inventory(
    *,
    root: str | Path | None = None,
    assumptions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Report estimate eligibility and explicit exclusion reasons for every record."""
    from .image_sensor_db import image_sensor_db_records

    _assumptions(assumptions)  # Invalid user choices are errors, not inventory exclusions.
    records = image_sensor_db_records(root=root)
    try:
        context = _enriched_context(root)
        context_error = None
    except (FileNotFoundError, ValueError) as exc:
        context, context_error = None, str(exc)
    inventory = []
    for record in records:
        item = {key: record[key] for key in ("sensor_id", "code", "manufacturer", "device_name")}
        try:
            if context is None:
                raise ValueError(context_error)
            model = _model_from_record(record, context, assumptions)
            item.update(
                available=True,
                reason=None,
                pixel_pitch_um=model["pixel_pitch_um"],
                cfa_pattern=model["cfa_pattern"],
            )
        except (FileNotFoundError, ValueError, KeyError, TypeError) as exc:
            item.update(available=False, reason=str(exc))
        inventory.append(item)
    count = sum(item["available"] for item in inventory)
    return {
        "kind": _KIND,
        "record_count": len(inventory),
        "available_count": count,
        "excluded_count": len(inventory) - count,
        "records": inventory,
    }


def apply_sensor_estimate(sensor: Sensor, model: Mapping[str, Any]) -> Sensor:
    """Return a clone with aggregate EQE applied exactly once per incident photon.

    The model replaces CFA/QE/IR/fill-factor optical factors: aggregate EQE is
    stored in filter_spectra, pixel_qe and IR transmission are unity, and the
    detector covers one full pixel. Array size is retained. Readout assumptions
    are stored in e2e_model_readout for the E2E capture configurator; this helper
    does not modify legacy noise/ADC behavior. Active FDTD/TCAD layers are errors.
    Existing capture arrays are invalidated and require optical recomputation.
    """
    if model.get("kind") != _KIND:
        raise ValueError("apply_sensor_estimate requires a db_estimate_v1 model.")
    for layer in ("fdtd_sensor", "tcad_sensor"):
        value = sensor.fields.get(layer)
        if value is not None and (not isinstance(value, Mapping) or value.get("enabled", True)):
            raise ValueError(
                f"db_estimate_v1 cannot overlap active {layer}; use model='legacy' for FDTD/TCAD."
            )
    wave = np.asarray(model.get("wavelength_nm"), dtype=float)
    eqe = np.asarray(model.get("eqe"), dtype=float)
    names = list(model.get("filter_names", []))
    pattern = model.get("cfa_pattern")
    expected_names = ["clear"] if pattern in {"monochrome", "clear"} else ["red", "green", "blue"]
    if (
        not np.array_equal(wave, _WAVE)
        or eqe.shape != (wave.size, len(names))
        or not np.all(np.isfinite(eqe))
        or np.any((eqe < 0) | (eqe > 1))
        or pattern not in _SUPPORTED_PATTERNS
        or names != expected_names
    ):
        raise ValueError("Invalid db_estimate_v1 wavelength, EQE, channel or CFA data.")
    pitch = _positive(model.get("pixel_pitch_um"), "pixel_pitch_um") * 1e-6
    result = sensor.clone()
    result.fields.pop("fdtd_estimate", None)
    result.fields.pop("fdtd_illumination", None)
    result.fields.pop("physical_estimate", None)
    result.fields.pop("physical_illumination", None)
    result.fields.pop("e2e_readout_profile", None)
    result.data.clear()
    result.metadata.pop("sensor_capture", None)
    result.metadata.pop("sensor_readout_profile", None)
    result.metadata.pop("sensor_simulation_db", None)
    result.fields.update(
        wave=wave.copy(),
        spectrum={"wave": wave.copy()},
        filter_spectra=eqe.copy(),
        filter_names=names,
        pixel_qe=np.ones(wave.size),
        ir_filter=np.ones(wave.size),
        etendue=None,
        vignetting=0,
        ocl_group_proxy=None,
        e2e_model_readout=copy.deepcopy(model["readout"]),
    )
    mosaic = (
        np.array([[1]], dtype=int) if len(names) == 1 else np.array([[1, 2], [2, 3]], dtype=int)
    )
    if pattern in {"quad_bayer", "tetracell_bayer"}:
        mosaic = np.repeat(np.repeat(mosaic, 2, axis=0), 2, axis=1)
    result.fields["pattern"] = mosaic
    result.fields["pixel"].update(
        size_m=np.full(2, pitch),
        width_gap_m=0.0,
        height_gap_m=0.0,
        fill_factor=1.0,
        pd_size_m=np.full(2, pitch),
        pd_position_m=np.zeros(2),
        spectrum={"wave": wave.copy()},
    )
    result.metadata["sensor_estimate"] = copy.deepcopy(dict(model))
    return result
