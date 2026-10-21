"""Read and execute a pinned CameraE2E sensor-simulation revision.

The saved sensor model retains its native CFA, spectral response and readout
profile inside pyisetcam. This bridge deliberately does not translate those
quantities into the narrower R1 RGB pixel/electrical component contracts.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam import camera_get, camerae2e_run_scenario
from pyisetcam.assets import AssetStore
from pyisetcam.sensor_simulation_db import (
    _load_runtime_snapshot,
    sensor_simulation_db_config,
    sensor_simulation_db_get,
    sensor_simulation_db_search,
)

from .offline_assets import offline_asset_identity, offline_asset_store

SCHEMA = "camerae2e_sensor_revision_capture_v1"
INSPECTION_SCHEMA = "camerae2e_sensor_revision_inspection_v1"
COMPARISON_SCHEMA = "camerae2e_sensor_revision_comparison_v1"
_READOUT_FIELDS = {
    "gain": "unitless",
    "full_well_e": "electron/pixel",
    "read_noise_e": "electron RMS/pixel",
    "dark_current_e_per_s": "electron/pixel/s",
    "bit_depth": "bit",
    "black_level_dn": "DN",
}


def sensor_revision_catalog(
    *, root: str | Path | None = None, query: str | None = None
) -> list[dict[str, Any]]:
    """List current saved revisions, including unavailable ones, without arrays."""
    return [
        {
            key: row[key]
            for key in (
                "sensor_id",
                "report_code",
                "device_name",
                "manufacturer",
                "model",
                "status",
                "reason",
                "run_id",
                "result_sha256",
            )
        }
        for row in sensor_simulation_db_search(query, root=root)
    ]


def inspect_sensor_revision(
    identifier: str, *, root: str | Path | None = None, run_id: str | None = None
) -> dict[str, Any]:
    """Hash-verify a saved run and expose evidence separately from assumptions."""
    saved = sensor_simulation_db_get(identifier, root=root, run_id=run_id)
    row, metadata = saved["db_entry"], saved["metadata"]
    status = row["status"]
    result: dict[str, Any] = {
        "schema": INSPECTION_SCHEMA,
        "status": status,
        "executable": status == "simulated",
        "reason": None if status == "simulated" else row["reason"],
        "revision": {
            key: row[key]
            for key in (
                "sensor_id",
                "report_code",
                "device_name",
                "manufacturer",
                "model",
                "run_id",
                "result_sha256",
                "config_sha256",
            )
        },
        "physical_validation": "not_established",
    }
    if status != "simulated":
        return result
    # The saved result JSON can be intact while its pinned pyisetcam runtime
    # differs. Check the same compatibility gate used by actual execution now,
    # before a user can queue a comparison that is guaranteed to fail later.
    try:
        configuration = sensor_simulation_db_config(
            row["report_code"], root=root, run_id=row["run_id"]
        )
        _load_runtime_snapshot(configuration["scenario"]["sensor_model"])
    except (ValueError, FileNotFoundError) as exc:
        result["executable"] = False
        result["reason"] = f"Saved runtime incompatible: {exc}"
        result["runtime_compatibility"] = "incompatible"
    else:
        result["runtime_compatibility"] = "compatible"
    model = metadata["model_snapshot"]
    if model["kind"] == "fdtd_estimate_v1" and result["executable"]:
        # The current CameraE2E scenario path reconstructs this model from a
        # LUT path instead of consuming the stored snapshot. Its frozen
        # descriptor does not even carry that path, so do not advertise a
        # saved FDTD run as executable until that contract is implemented.
        result["executable"] = False
        result["reason"] = (
            "Saved FDTD execution is not frozen-snapshot compatible; "
            "use the separate LUT sensitivity workflow."
        )
    profile = metadata.get("readout_profile") or {}
    profile_provenance = profile.get("provenance") or {}
    saved_readout = model["readout"]
    fields = profile_provenance.get("fields") or {}
    frozen_config = metadata.get("config", {})
    layers = {
        layer: frozen_config.get("scenario", {}).get(layer, frozen_config.get(layer))
        for layer in ("fdtd", "tcad")
    }
    attached = {
        layer: isinstance(payload, Mapping) and payload.get("enabled") is True
        for layer, payload in layers.items()
    }
    if any(attached.values()):
        result["executable"] = False
        result["reason"] = (
            "Saved estimated response declares an active FDTD/TCAD layer; "
            "the frozen model cannot be compounded with that layer."
        )
    model_parameters = {
        "pixel_pitch_um": {
            "value": model["pixel_pitch_um"],
            "unit": "um",
            "classification": "database_derived_geometry",
            "source": model.get("provenance", {}).get("pixel_pitch_source")
            or model.get("material_bundle", {}).get("provenance", {}).get("geometry_source"),
        },
    }
    if model["kind"] == "physical_estimate_v1":
        model_parameters["collection_efficiency"] = {
            "value": model["collection_efficiency"],
            "unit": "collected_carrier/absorbed_photon",
            "classification": "assumption",
            "source": model.get("provenance", {}).get("collection_efficiency_definition"),
        }
    elif model["kind"] == "db_estimate_v1":
        model_parameters["effective_efficiency"] = {
            "value": model["assumptions"]["effective_efficiency"],
            "unit": "unitless",
            "classification": model.get("provenance", {})
            .get("assumption_sources", {})
            .get("effective_efficiency", "unclassified"),
            "source": model.get("provenance", {}).get("effective_efficiency_definition"),
        }
    result.update(
        {
            "native_response": {
                "kind": model["kind"],
                "pixel_pitch_um": model["pixel_pitch_um"],
                "wavelength_nm": model["wavelength_nm"],
                "filter_names": model["filter_names"],
                "native_cfa": metadata.get("native_cfa"),
                "model_provenance": model.get("provenance", {}),
                "source_scope_policy": metadata.get("source_scope_policy"),
                "estimate_only": model.get("estimate_only", True),
                "parameter_sources": model_parameters,
            },
            "readout": {
                "saved_values": {
                    key: {
                        "value": saved_readout[key],
                        "unit": unit,
                        "classification": fields.get(key, {}).get("classification", "unclassified"),
                        "source": fields.get(key, {}).get("notes"),
                    }
                    for key, unit in _READOUT_FIELDS.items()
                },
                "profile": profile.get("profile"),
                "operating_point": profile.get("operating_point"),
                "classification": profile_provenance.get("classification", "unclassified"),
                "fixed_pattern": profile.get("fixed_pattern"),
                "profile_provenance": profile_provenance,
            },
            "component_tiers": metadata.get("config", {})
            .get("policy", {})
            .get("component_tiers", {}),
            "physics_binding": {
                "fdtd_attached": attached["fdtd"],
                "tcad_attached": attached["tcad"],
                "note": "Saved DB/physical estimates cannot be compounded with FDTD/TCAD LUTs.",
            },
            "r1_component_compatibility": {
                "directly_compatible": False,
                "reason": (
                    "R1 needs an optics/scene wavelength and pixel-pitch match, three-channel "
                    "QE, its declared angular model and a six-field default electrical law; "
                    "this saved native response/readout has not been converted or validated "
                    "against those contracts. Use the pinned pyisetcam execution path."
                ),
            },
        }
    )
    return result


def _size(value: Sequence[int], name: str) -> tuple[int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(type(v) is not int or not 8 <= v <= 32 for v in value)
    ):
        raise ValueError(f"{name} must be two integer dimensions from 8 to 32")
    return int(value[0]), int(value[1])


def _scene(value: Mapping[str, Any] | None, size: tuple[int, int]) -> dict[str, Any]:
    if value is None:
        return {"type": "uniform ee", "args": [list(size)]}
    if not isinstance(value, Mapping) or set(value) != {"type", "args"}:
        raise ValueError("scene must contain exactly type and args")
    if value["type"] != "uniform ee":
        raise ValueError("sensor revision v1 supports the uniform ee scene only")
    args = value["args"]
    if (
        not isinstance(args, (list, tuple))
        or len(args) != 1
        or not isinstance(args[0], (list, tuple))
    ):
        raise ValueError("uniform ee scene args must contain one [rows, cols] size")
    scene_size = _size(args[0], "scene size")
    if scene_size != size:
        raise ValueError("scene size must equal sensor_size in this bounded capture")
    return {"type": "uniform ee", "args": [list(scene_size)]}


def _overrides(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value) - set(_READOUT_FIELDS):
        raise ValueError("readout_overrides accepts only the saved six-field readout keys")
    result = dict(value)
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
        for v in result.values()
    ):
        raise ValueError("readout_overrides requires finite numeric values")
    return result


def run_sensor_revision(
    identifier: str,
    *,
    root: str | Path | None = None,
    workspace: str | Path | None = None,
    run_id: str | None = None,
    scene: Mapping[str, Any] | None = None,
    exposure_s: float = 0.004,
    sensor_size: Sequence[int] = (8, 8),
    readout_overrides: Mapping[str, Any] | None = None,
    noise_flag: int = 0,
    seed: int = 0,
) -> dict[str, Any]:
    """Run a short native RAW capture using the selected immutable DB revision.

    The same explicit scene and capture settings can be submitted for another
    revision. This does not by itself create a matched physical field of view.
    """
    size = _size(sensor_size, "sensor_size")
    selected_scene = _scene(scene, size)
    if (
        isinstance(exposure_s, bool)
        or not isinstance(exposure_s, (int, float))
        or not math.isfinite(exposure_s)
        or not 0 < exposure_s <= 1
    ):
        raise ValueError("exposure_s must be finite and in (0, 1] seconds")
    if type(noise_flag) is not int or noise_flag not in {0, 2}:
        raise ValueError("noise_flag must be 0 (deterministic) or 2 (sampled noise)")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be a uint32 integer")
    changed = _overrides(readout_overrides)
    inspection = inspect_sensor_revision(identifier, root=root, run_id=run_id)
    if not inspection["executable"]:
        raise ValueError(
            f"Sensor revision {inspection['revision']['report_code']} cannot execute: "
            f"{inspection['status']}: {inspection['reason']}"
        )
    revision = inspection["revision"]
    # Pin the run chosen by inspection. The source catalog is not reopened to
    # regenerate optical parameters or infer a new readout profile.
    configuration = sensor_simulation_db_config(
        revision["report_code"], root=root, run_id=revision["run_id"]
    )
    scenario = {
        **configuration["scenario"],
        "scene": selected_scene,
        "sensor": {
            "size": list(size),
            "integration_time": float(exposure_s),
            "noise_flag": noise_flag,
        },
        "sensor_resize": False,
    }
    if changed:
        scenario["readout"] = changed
    assets = offline_asset_store(workspace=workspace, source=root)
    with AssetStore.bound_default(assets):
        simulated = camerae2e_run_scenario(
            scenario, asset_store=assets, seed=seed, include_arrays=False
        )
    sensor = camera_get(simulated["camera"], "sensor")
    binding = sensor.metadata.get("sensor_simulation_db")
    if (
        not isinstance(binding, Mapping)
        or binding.get("run_id") != revision["run_id"]
        or binding.get("result_sha256") != revision["result_sha256"]
    ):
        raise ValueError("Executed sensor revision differs from the selected saved run")
    raw = np.asarray(sensor.data["raw_dn"])
    photo = np.asarray(sensor.data["mean_electrons"], dtype=float)
    if raw.shape != size or raw.dtype != np.dtype("int32") or photo.shape != size:
        raise ValueError("Saved sensor capture returned an unsupported RAW shape or dtype")
    capture = simulated["sensor_capture"]
    return {
        "schema": SCHEMA,
        "status": "simulated",
        "revision": revision,
        "physical_validation": "not_established",
        "scene": selected_scene,
        "capture": {
            "exposure_s": float(exposure_s),
            "sensor_size": list(size),
            "noise_flag": noise_flag,
            "seed": seed,
            "saved_readout": inspection["readout"]["saved_values"],
            "readout_overrides": changed,
            "effective_readout": capture["readout"],
            "readout_units": _READOUT_FIELDS,
            "saturation": capture.get("saturation", {}),
        },
        "raw": {
            "dtype": "int32",
            "unit": "DN",
            "shape": list(raw.shape),
            "min_dn": int(raw.min()),
            "max_dn": int(raw.max()),
            "values": raw.tolist(),
        },
        "photoelectrons": {
            "unit": "electron/pixel",
            "mean": float(photo.mean()),
            "values": photo.tolist(),
        },
        "evidence": {
            "upstream_assets": offline_asset_identity(workspace=workspace, source=root),
            "model_kind": inspection["native_response"]["kind"],
            "model_provenance": inspection["native_response"]["model_provenance"],
            "readout_profile": inspection["readout"]["profile"],
            "readout_classification": inspection["readout"]["classification"],
            "component_tiers": inspection["component_tiers"],
            "source_scope_policy": inspection["native_response"]["source_scope_policy"],
            "fdtd_attached": inspection["physics_binding"]["fdtd_attached"],
            "tcad_attached": inspection["physics_binding"]["tcad_attached"],
            "interpretation": (
                "Numerically checked estimate with the saved optical/readout revision; "
                "no measured product accuracy or product ranking is established."
            ),
        },
    }


def _revision_choice(value: Mapping[str, Any]) -> tuple[str, str | None]:
    if (
        not isinstance(value, Mapping)
        or set(value) - {"identifier", "run_id"}
        or not isinstance(value.get("identifier"), str)
        or not value["identifier"].strip()
    ):
        raise ValueError("Each revision needs a nonempty identifier and optional run_id")
    run_id = value.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError("run_id must be a nonempty string when supplied")
    return value["identifier"], run_id


def _change(first: Any, second: Any) -> dict[str, Any]:
    return {"first": first, "second": second, "different": first != second}


def _numeric_change(first: float, second: float, unit: str) -> dict[str, Any]:
    delta = float(second - first)
    return {
        "first": float(first),
        "second": float(second),
        "unit": unit,
        "second_minus_first": delta,
        "absolute_difference": abs(delta),
        "relative_difference_to_first": delta / float(first) if first != 0 else None,
        "relative_denominator": "first; null when first is zero",
    }


def compare_sensor_revisions(
    revisions: Sequence[Mapping[str, Any]],
    *,
    root: str | Path | None = None,
    workspace: str | Path | None = None,
    scene: Mapping[str, Any] | None = None,
    exposure_s: float = 0.004,
    sensor_size: Sequence[int] = (8, 8),
    readout_overrides: Mapping[str, Any] | None = None,
    noise_flag: int = 0,
    seed: int = 0,
) -> dict[str, Any]:
    """Compare aggregate RAW/photocharge from two pinned saved revisions.

    A shared partial readout override is valid only if all remaining saved
    readout values already match. No native pixels are spatially registered,
    so only aggregate differences are calculated.
    """
    if not isinstance(revisions, (list, tuple)) or len(revisions) != 2:
        raise ValueError("compare_sensor_revisions requires exactly two revisions")
    choices = [_revision_choice(item) for item in revisions]
    changed = _overrides(readout_overrides)
    inspections = [
        inspect_sensor_revision(identifier, root=root, run_id=run_id)
        for identifier, run_id in choices
    ]
    for inspection in inspections:
        if not inspection["executable"]:
            raise ValueError(
                f"Sensor revision {inspection['revision']['report_code']} cannot execute: "
                f"{inspection['reason']}"
            )
    identities = [
        (item["revision"]["sensor_id"], item["revision"]["run_id"]) for item in inspections
    ]
    if identities[0] == identities[1]:
        raise ValueError("The two selections resolve to the same saved revision")
    expected_readouts = []
    for item in inspections:
        values = {key: entry["value"] for key, entry in item["readout"]["saved_values"].items()}
        expected_readouts.append({**values, **changed})
    different_readout = sorted(
        key for key in _READOUT_FIELDS if expected_readouts[0][key] != expected_readouts[1][key]
    )
    if different_readout:
        raise ValueError(
            "Effective readout differs between saved revisions for "
            f"{different_readout}; supply shared readout_overrides for those fields"
        )
    if (
        noise_flag == 2
        and inspections[0]["readout"]["fixed_pattern"] != inspections[1]["readout"]["fixed_pattern"]
    ):
        raise ValueError(
            "Sampled-noise comparison requires matching saved fixed-pattern profiles; "
            "use noise_flag=0 for a deterministic optical/readout comparison"
        )
    captures = [
        run_sensor_revision(
            identifier,
            root=root,
            workspace=workspace,
            run_id=item["revision"]["run_id"],
            scene=scene,
            exposure_s=exposure_s,
            sensor_size=sensor_size,
            readout_overrides=changed,
            noise_flag=noise_flag,
            seed=seed,
        )
        for (identifier, _), item in zip(choices, inspections, strict=True)
    ]
    if captures[0]["capture"]["effective_readout"] != captures[1]["capture"]["effective_readout"]:
        raise ValueError("Executed readout conditions differ despite the shared preflight")
    raw = [np.asarray(item["raw"]["values"], dtype=float) for item in captures]
    black = captures[0]["capture"]["effective_readout"]["black_level_dn"]
    photo = [np.asarray(item["photoelectrons"]["values"], dtype=float) for item in captures]
    first, second = inspections
    first_model = first["native_response"]
    second_model = second["native_response"]
    first_source = first_model["model_provenance"]
    second_source = second_model["model_provenance"]
    return {
        "schema": COMPARISON_SCHEMA,
        "physical_validation": "not_established",
        "conditions": {
            "scene": captures[0]["scene"],
            "exposure_s": float(exposure_s),
            "sensor_size": captures[0]["capture"]["sensor_size"],
            "noise_flag": noise_flag,
            "seed": seed,
            "readout_overrides": changed,
            "effective_readout": captures[0]["capture"]["effective_readout"],
        },
        "captures": captures,
        "differences": {
            "mean_raw_dn": _numeric_change(float(raw[0].mean()), float(raw[1].mean()), "DN"),
            "mean_black_relative_raw_dn": _numeric_change(
                float(raw[0].mean() - black), float(raw[1].mean() - black), "DN"
            ),
            "mean_photoelectrons": _numeric_change(
                float(photo[0].mean()), float(photo[1].mean()), "electron/pixel"
            ),
        },
        "source_differences": {
            "run_id": _change(first["revision"]["run_id"], second["revision"]["run_id"]),
            "result_sha256": _change(
                first["revision"]["result_sha256"], second["revision"]["result_sha256"]
            ),
            "sensor_id": _change(first["revision"]["sensor_id"], second["revision"]["sensor_id"]),
            "model_kind": _change(first_model["kind"], second_model["kind"]),
            "pixel_pitch_um": _change(
                first_model["pixel_pitch_um"], second_model["pixel_pitch_um"]
            ),
            "filter_names": _change(first_model["filter_names"], second_model["filter_names"]),
            "source_model_identity": _change(
                first_source.get("material_bundle_sha256")
                or first_source.get("optical_model_path"),
                second_source.get("material_bundle_sha256")
                or second_source.get("optical_model_path"),
            ),
            "readout_profile_classification": _change(
                first["readout"]["classification"], second["readout"]["classification"]
            ),
            "fixed_pattern": _change(
                first["readout"]["fixed_pattern"], second["readout"]["fixed_pattern"]
            ),
            "readout_field_classifications": {
                key: _change(
                    first["readout"]["saved_values"][key]["classification"],
                    second["readout"]["saved_values"][key]["classification"],
                )
                for key in _READOUT_FIELDS
            },
            "component_tiers": _change(first["component_tiers"], second["component_tiers"]),
            "source_scope_policy": _change(
                first_model["source_scope_policy"], second_model["source_scope_policy"]
            ),
        },
        "interpretation": (
            "Aggregate differences for the same declared uniform scene and capture controls. "
            "Native pixel pitches and CFA responses can differ; no common physical field of "
            "view, pixelwise registration, product ranking, MTF or color accuracy is established."
        ),
    }
