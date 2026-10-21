"""Revision-specific camera readout proxies for practical sensor estimates.

The packaged observations describe complete cameras using the named sensors.
They are not bare-sensor measurements. Discrete vendor operating points keep
charge capacity and noise together; no undocumented gain curve is interpolated.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

import numpy as np

from .sensor_capture import _readout_values, configure_e2e_sensor
from .types import Sensor

_READOUT_KEYS = {
    "gain",
    "full_well_e",
    "read_noise_e",
    "dark_current_e_per_s",
    "bit_depth",
    "black_level_dn",
}
_FPN_KEYS = {"prnu_fraction", "dsnu_e", "column_prnu_fraction", "column_dsnu_e"}
_ALIASES = {
    "imx307": "def_1807_801_sony_imx307lqd_c",
    "imx462": "def_2011_804_sony_imx462",
    "imx662": "def_2206_805_sony_imx662",
}


def _bundle() -> tuple[dict[str, Any], str]:
    source = files("pyisetcam").joinpath("data/sensor_estimate/readout_profiles_v1.json")
    raw = source.read_bytes()
    value = json.loads(raw)
    if value.get("schema") != "camerae2e_readout_profiles_v1":
        raise ValueError("Unsupported packaged readout profile schema.")
    return value, hashlib.sha256(raw).hexdigest()


def _sensor_identity(
    sensor_id: str,
    bundle: Mapping[str, Any],
    root: str | Path | None = None,
) -> tuple[str, str]:
    key = str(sensor_id).lower().strip()
    key = _ALIASES.get(key, key)
    for record in bundle["sensors"]:
        if key in {record["sensor_id"], record["report_code"].lower()}:
            return record["sensor_id"], record["report_code"]
    from .image_sensor_db import image_sensor_db_get

    record = image_sensor_db_get(key, root=root)
    return record["sensor_id"], record["code"]


def _pre_adc_noise(point: Mapping[str, Any]) -> tuple[float, list[float], dict[str, Any]]:
    """Remove nominal white quantization variance from reported total noise."""
    total = point["fields"]["reported_total_read_noise_e"]
    step = point["fields"]["reported_electrons_per_adu"]
    residual = float(total["value"]) ** 2 - float(step["value"]) ** 2 / 12
    if residual <= 0:
        raise ValueError("Reported noise cannot support positive pre-ADC noise at this LSB.")
    low = max(0.0, float(total["range"][0]) ** 2 - float(step["range"][1]) ** 2 / 12)
    high = float(total["range"][1]) ** 2 - float(step["range"][0]) ** 2 / 12
    if high < 0:
        raise ValueError("Readout uncertainty bounds are inconsistent with ADC quantization.")
    value = float(np.sqrt(residual))
    interval = [float(np.sqrt(low)), float(np.sqrt(high))]
    return (
        value,
        interval,
        {
            "classification": "derived_camera_proxy",
            "value": value,
            "range": interval,
            "source_id": total.get("source_id"),
            "notes": (
                "Pre-ADC Gaussian RMS estimate: sqrt(reported total RN^2 - source LSB^2/12). "
                "Assumes white independent quantization; "
                "this is not a measured analog noise separation."
            ),
        },
    )


def resolve_sensor_readout(
    sensor_id: str,
    *,
    profile: str | None = None,
    operating_point: str | None = None,
    overrides: Mapping[str, Any] | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve a coherent discrete readout point and per-field provenance.

    ``profile=None`` uses a versioned camera proxy when available, otherwise
    ``generic_assumption``. ``operating_point`` is ``low_gain`` or ``hcg`` for
    camera proxies, and ``assumed_reference`` for the generic fallback. Unknown
    points raise instead of interpolating. Overrides accept the six digital RAW
    parameters plus four fixed-pattern controls (fractional PRNU, electron RMS
    offsets). ``gain`` is *additional physical amplification* relative to the
    selected point; it does not select a vendor gain register or HCG mode.
    ``root`` selects the catalog used to resolve additional sensor identities.
    """
    bundle, bundle_hash = _bundle()
    selected_id, code = _sensor_identity(sensor_id, bundle, root=root)
    matches = [r for r in bundle["sensors"] if r["sensor_id"] == selected_id]
    selected_profile = profile or (
        matches[0]["default_profile"] if matches else "generic_assumption"
    )
    if selected_profile not in bundle["profiles"]:
        raise ValueError(f"Unknown readout profile: {selected_profile}.")
    definition = copy.deepcopy(bundle["profiles"][selected_profile])
    if definition.get("sensor_ids") and selected_id not in definition["sensor_ids"]:
        raise ValueError("Readout profile belongs to a different sensor.")
    selected_point = operating_point or definition["default_operating_point"]
    if selected_point not in definition["operating_points"]:
        raise ValueError(
            f"Unsupported operating point {selected_point!r} for {selected_profile}; "
            "vendor gain curves are not interpolated."
        )
    point = definition["operating_points"][selected_point]
    fields = copy.deepcopy(bundle["common_assumptions"])
    fields.update(point["fields"])
    if "reported_total_read_noise_e" in point["fields"]:
        _, _, noise = _pre_adc_noise(point)
        fields["read_noise_e"] = noise
    supplied = dict(overrides or {})
    unknown = set(supplied) - _READOUT_KEYS - _FPN_KEYS
    if unknown:
        raise ValueError(f"Unknown readout profile overrides: {sorted(unknown)}.")
    for name, value in supplied.items():
        value = float(value)
        if not np.isfinite(value):
            raise ValueError(f"Readout profile {name} must be finite.")
        original = fields[name]
        fields[name] = {
            "classification": "user_assumption",
            "value": value,
            "range": [value, value],
            "replaces": copy.deepcopy(original),
            "notes": "Explicit override; source operating-point equivalence is not guaranteed.",
        }
    readout = _readout_values({name: fields[name]["value"] for name in _READOUT_KEYS})
    fixed = {name: float(fields[name]["value"]) for name in _FPN_KEYS}
    if any(not np.isfinite(value) or value < 0 for value in fixed.values()):
        raise ValueError("Fixed-pattern RMS parameters must be finite and nonnegative.")
    source_ids = definition.get("source_ids", [])
    if selected_id == _ALIASES["imx307"]:
        source_ids = list(dict.fromkeys([*source_ids, "sony_imx307_flyer_2018"]))
    source_ids = list(dict.fromkeys(source_ids))
    white = 2 ** readout["bit_depth"] - 1
    simulation_lsb = readout["full_well_e"] / (
        (white - readout["black_level_dn"]) * readout["gain"]
    )
    provenance = {
        "schema": bundle["schema"],
        "bundle_sha256": bundle_hash,
        "classification": definition["classification"],
        "estimate_only": True,
        "bare_sensor_measurement": False,
        "camera_model": definition.get("camera_model"),
        "fields": fields,
        "conditions": copy.deepcopy(point["conditions"]),
        "sources": {key: copy.deepcopy(bundle["sources"][key]) for key in source_ids},
        "gain_semantics": {
            "gain": "Additional physical amplification relative to the selected operating point.",
            "vendor_gain_register": point["conditions"].get("vendor_gain_register"),
            "vendor_gain_unit": point["conditions"].get("vendor_gain_unit"),
            "conversion_gain_v_per_electron": "Not measured; synthetic voltage_swing/full_well_e.",
            "full_well_e": "Effective usable charge at this operating point; may be ADC limited.",
            "reported_electrons_per_adu": "Camera system gain; not pixel conversion gain in V/e-.",
        },
        "adc_noise": {
            "read_noise_e_domain": "pre_adc",
            "deembedded": "reported_total_read_noise_e" in point["fields"]
            and "read_noise_e" not in supplied,
            "source_lsb_e": point["fields"].get("reported_electrons_per_adu", {}).get("value"),
            "simulation_lsb_e": simulation_lsb,
            "simulation_total_read_noise_e_white_quantization": float(
                np.hypot(readout["read_noise_e"], simulation_lsb / np.sqrt(12))
            ),
            "notes": (
                "Source nominal ADC step and simulated pedestal-reserved ADC step differ. "
                "The latter is quantized by sensor_capture. White quantization estimates are "
                "asymptotic, not exact at sub-LSB signal or near clipping."
            ),
        },
        "source_operating_point_preserved": not bool(
            set(supplied) & {"gain", "full_well_e", "read_noise_e", "bit_depth"}
        ),
        "overrides": sorted(supplied),
        "limitations": copy.deepcopy(definition["limitations"]),
    }
    return {
        "sensor_id": selected_id,
        "report_code": code,
        "profile": selected_profile,
        "operating_point": selected_point,
        "readout": readout,
        "fixed_pattern": fixed,
        "provenance": provenance,
    }


def apply_readout_profile(sensor: Sensor, resolved: Mapping[str, Any]) -> Sensor:
    """Configure a clone with a resolved profile, preserving mean photocharge.

    Fixed offset controls use electron RMS per capture, not dark-current
    nonuniformity in e-/s. Cached maps are invalidated when their physical
    scale or operating point changes; the fixed-pattern seed stays unchanged.
    """
    selected_sensor = sensor.metadata.get("sensor_estimate", {}).get("sensor_id")
    if selected_sensor and selected_sensor != resolved["sensor_id"]:
        raise ValueError("Readout profile sensor identity does not match the optical estimate.")
    updated = configure_e2e_sensor(sensor, readout=resolved["readout"])
    fixed = {key: float(resolved["fixed_pattern"][key]) for key in _FPN_KEYS}
    if any(not np.isfinite(value) or value < 0 for value in fixed.values()):
        raise ValueError("Fixed-pattern RMS parameters must be finite and nonnegative.")
    previous = sensor.metadata.get("sensor_readout_profile", {})
    old_values = {
        "prnu_fraction": float(sensor.fields["pixel"].get("prnu_sigma", 0)),
        "dsnu_e": float(sensor.fields["pixel"].get("dsnu_sigma_v", 0)),
        "column": np.asarray(sensor.fields.get("column_fpn", [0.0, 0.0])).tolist(),
        "conversion": float(sensor.fields["pixel"]["conversion_gain_v_per_electron"]),
    }
    conversion = float(updated.fields["pixel"]["conversion_gain_v_per_electron"])
    updated.fields["pixel"]["prnu_sigma"] = fixed["prnu_fraction"]
    updated.fields["pixel"]["dsnu_sigma_v"] = fixed["dsnu_e"] * conversion
    updated.fields["column_fpn"] = [
        fixed["column_dsnu_e"] * conversion,
        fixed["column_prnu_fraction"],
    ]
    new_values = {
        "prnu_fraction": fixed["prnu_fraction"],
        "dsnu_e": fixed["dsnu_e"] * conversion,
        "column": updated.fields["column_fpn"],
        "conversion": conversion,
    }
    changed_point = any(
        previous.get(key) != resolved.get(key)
        for key in ("profile", "operating_point", "sensor_id")
    )
    if old_values != new_values or changed_point:
        for key in ("offset_fpn_image", "gain_fpn_image", "column_offset_fpn", "column_gain_fpn"):
            updated.fields.pop(key, None)
    applied = copy.deepcopy(dict(resolved))
    applied["readout"] = copy.deepcopy(updated.fields["sensor_readout"])
    applied["fixed_pattern"] = fixed
    provenance = applied["provenance"]
    changed = set()
    for name, value in {**applied["readout"], **fixed}.items():
        prior = provenance["fields"][name]
        if value != prior["value"]:
            changed.add(name)
            provenance["fields"][name] = {
                "classification": "user_assumption",
                "value": value,
                "range": [value, value],
                "replaces": prior,
                "notes": "Effective scenario override applied after resolving the source profile.",
            }
    provenance["overrides"] = sorted(set(provenance["overrides"]) | changed)
    if changed & {"gain", "full_well_e", "read_noise_e", "bit_depth"}:
        provenance["source_operating_point_preserved"] = False
    if "read_noise_e" in changed:
        provenance["adc_noise"]["deembedded"] = False
    readout = applied["readout"]
    white = 2 ** readout["bit_depth"] - 1
    step = readout["full_well_e"] / ((white - readout["black_level_dn"]) * readout["gain"])
    provenance["adc_noise"]["simulation_lsb_e"] = step
    provenance["adc_noise"]["simulation_total_read_noise_e_white_quantization"] = float(
        np.hypot(readout["read_noise_e"], step / np.sqrt(12))
    )
    updated.metadata["sensor_readout_profile"] = applied
    return updated
