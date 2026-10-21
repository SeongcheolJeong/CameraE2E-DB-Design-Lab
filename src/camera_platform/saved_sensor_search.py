"""Bounded exposure/gain search over one immutable sensor-simulation run.

The pyisetcam optimizer owns candidate calculation. This adapter pins its
saved sensor response/readout, fixes the ideal optical and scene contract, and
reports only the declared electron-signal objective and clipping constraints.
It does not export RAW arrays or claim a camera-product optimum.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from pyisetcam.assets import AssetStore
from pyisetcam.camera import camera_create, camera_set
from pyisetcam.optimization import camerae2e_optimize_parameters
from pyisetcam.scene import scene_adjust_luminance, scene_create
from pyisetcam.sensor_simulation_db import sensor_simulation_db_config, sensor_simulation_db_get

from .offline_assets import offline_asset_identity, offline_asset_store
from .sensor_db_bridge import inspect_sensor_revision

SCHEMA = "camerae2e_saved_sensor_search_v1"
_OBJECTIVE = {"metric": "stage_summaries.sensor_mean_electrons.mean", "direction": "maximize"}
_SATURATION_PATHS = (
    "sensor_capture.saturation.well_fraction",
    "sensor_capture.saturation.adc_white_fraction",
)
_LIMITATION = (
    "This is a deterministic, static, uniform-scene search over one pinned saved "
    "sensor response/readout with fixed ideal F/4, 4 mm optics. Its objective is "
    "mean photoelectrons before gain and clipping; the constraints inspect "
    "deterministic well/ADC-white fractions. The selected case is only best among "
    "the declared candidates and metric. It is not temporal SNR, measured product "
    "performance, general lens selection, or physical validation. RAW arrays are "
    "not exported by this report."
)


def _number(value: Any, name: str, low: float, high: float) -> float:
    if type(value) not in (float, int):
        raise ValueError(f"{name} must be finite and in [{low}, {high}].")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be finite and in [{low}, {high}].") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{name} must be finite and in [{low}, {high}].")
    return number


def _axis(value: Sequence[float], name: str, low: float, high: float) -> list[float]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 4:
        raise ValueError(f"{name} needs one to four explicit candidates.")
    result = [_number(item, name, low, high) for item in value]
    if len(set(result)) != len(result):
        raise ValueError(f"{name} candidates must be distinct.")
    return result


def _size(value: Sequence[int]) -> tuple[int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(type(item) is not int or not 8 <= item <= 32 for item in value)
    ):
        raise ValueError("sensor_size must contain two integer dimensions from 8 to 32.")
    return value[0], value[1]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _case_payload(
    case: Mapping[str, Any], binding: Mapping[str, Any], revision: Mapping[str, Any]
) -> dict[str, Any]:
    report = case["report"]
    scenario = report["scenario"]
    if scenario.get("sensor_model") != binding:
        raise ValueError("Optimizer candidate lost the pinned sensor-simulation binding.")
    parameters = case["parameters"]
    capture = report["sensor_capture"]
    gain = float(parameters["sensor.analog_gain"])
    exposure = float(parameters["sensor.integration_time"])
    if (
        capture.get("noise_flag") != 0
        or capture.get("mode") != "digital_raw"
        or not math.isclose(float(capture["readout"]["gain"]), gain, rel_tol=1e-12)
        or not math.isclose(float(capture["exposure_time_s"]), exposure, rel_tol=1e-12)
    ):
        raise ValueError("Executed capture differs from the declared exposure/gain candidate.")
    stages = report["stage_summaries"]
    electrons = stages["sensor_mean_electrons"]
    raw = stages["sensor_raw"]
    if (
        not electrons.get("available")
        or electrons.get("unit") != "e-/pixel"
        or not raw.get("available")
        or raw.get("unit") != "DN"
        or raw.get("dtype") != "int32"
    ):
        raise ValueError("Candidate omitted the required electron or integer-RAW summary.")
    if not math.isclose(
        float(case["objective_values"][_OBJECTIVE["metric"]]), float(electrons["mean"]),
        rel_tol=1e-12,
    ):
        raise ValueError("Candidate objective differs from the reported electron signal.")
    return {
        "case_index": case["case_index"],
        "seed": case["seed"],
        "parameters": dict(parameters),
        "source_binding": {
            **binding["simulation_db"],
            "sensor_id": binding["sensor_id"],
            "model_kind": binding["kind"],
            "config_sha256": revision["config_sha256"],
        },
        "mean_electrons_e_per_pixel": float(electrons["mean"]),
        "mean_raw_dn": float(raw["mean"]),
        "capture": capture,
        "objective_values": dict(case["objective_values"]),
        "constraint_results": list(case["constraint_results"]),
        "feasible": bool(case["feasible"]),
        "score": float(case["score"]) if math.isfinite(float(case["score"])) else None,
        "parameter_lineage": report["parameter_lineage"],
    }


def search_saved_sensor_capture(
    identifier: str,
    *,
    run_id: str,
    root: str | Path,
    workspace: str | Path,
    exposure_s: Sequence[float],
    analog_gain: Sequence[float],
    luminance_cd_m2: float = 1000.0,
    sensor_size: Sequence[int] = (8, 8),
    max_saturation_fraction: float = 0.0,
    seed: int = 7,
) -> dict[str, Any]:
    """Search at most four explicit settings without rebuilding the saved model."""

    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("A sensor identifier is required.")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("An explicit saved run_id is required.")
    exposures = _axis(exposure_s, "exposure_s", 1e-6, 1.0)
    gains = _axis(analog_gain, "analog_gain", 1.0, 16.0)
    if len(exposures) * len(gains) > 4:
        raise ValueError("Exposure × gain exceeds the four-candidate limit.")
    size = _size(sensor_size)
    luminance = _number(luminance_cd_m2, "luminance_cd_m2", 0.01, 10000.0)
    ceiling = _number(max_saturation_fraction, "max_saturation_fraction", 0.0, 1.0)
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be a uint32 integer.")

    inspection = inspect_sensor_revision(identifier, root=root, run_id=run_id)
    if not inspection["executable"]:
        raise ValueError(f"Saved sensor run cannot execute: {inspection['reason']}")
    revision = inspection["revision"]
    if revision["model"] not in {"db_estimate_v1", "physical_estimate_v1"}:
        raise ValueError("Saved search supports pinned DB/physical estimate snapshots only.")
    saved = sensor_simulation_db_get(
        revision["report_code"], root=root, run_id=revision["run_id"]
    )
    settings = saved["metadata"].get("simulation_settings") or {}
    if settings.get("fnumber") != 4.0 or settings.get("focal_length_m") != 0.004:
        raise ValueError("Saved search requires a saved F/4, 4 mm optics contract.")
    scenario = sensor_simulation_db_config(
        revision["report_code"], root=root, run_id=revision["run_id"]
    )["scenario"]
    if scenario.get("sensor_output_mode") != "digital_raw":
        raise ValueError("Saved search requires a saved digital_raw output contract.")
    binding = deepcopy(scenario["sensor_model"])
    if binding["simulation_db"]["result_sha256"] != revision["result_sha256"]:
        raise ValueError("Saved source binding differs from the selected run.")
    sensor_settings = deepcopy(scenario.get("sensor", {}))
    sensor_settings.update(size=list(size), noise_flag=0)
    scenario["sensor"] = sensor_settings
    scenario["sensor_resize"] = False

    constraints = [
        {"metric": path, "op": "<=", "value": ceiling} for path in _SATURATION_PATHS
    ]
    source_before = _canonical(inspection)
    assets_before = offline_asset_identity(workspace=workspace, source=root)
    assets = offline_asset_store(workspace=workspace, source=root)
    with AssetStore.bound_default(assets):
        scene = scene_create("uniform ee", list(size), asset_store=assets)
        scene_adjust_luminance(scene, luminance, asset_store=assets)
        camera = camera_create(asset_store=assets)
        camera = camera_set(camera, "optics fnumber", 4.0)
        camera = camera_set(camera, "optics focal length", 0.004)
        optimized = camerae2e_optimize_parameters(
            scenario,
            {"sensor.integration_time": exposures, "sensor.analog_gain": gains},
            _OBJECTIVE,
            method="grid",
            max_cases=len(exposures) * len(gains),
            constraints=constraints,
            scene=scene,
            camera=camera,
            asset_store=assets,
            seed=seed,
            top_k=len(exposures) * len(gains),
            include_arrays=False,
        )
    if optimized["case_count"] != len(exposures) * len(gains):
        raise ValueError("Optimizer did not evaluate the complete declared grid.")
    cases = [_case_payload(case, binding, revision) for case in optimized["cases"]]
    source_after = inspect_sensor_revision(identifier, root=root, run_id=run_id)
    if _canonical(source_after) != source_before:
        raise ValueError("Saved sensor source changed during the search.")
    if offline_asset_identity(workspace=workspace, source=root) != assets_before:
        raise ValueError("Offline upstream assets changed during the search.")
    best_index = None if optimized["best_case"] is None else optimized["best_case"]["case_index"]
    return {
        "schema": SCHEMA,
        "status": "simulated",
        "revision": revision,
        "source_inspection": inspection,
        "upstream_assets": assets_before,
        "conditions": {
            "scene": "uniform ee",
            "luminance_cd_m2": luminance,
            "sensor_size": list(size),
            "optics": {"kind": "ideal_fixed", "fnumber": 4.0, "focal_length_m": 0.004},
            "noise_flag": 0,
            "exposure_s": exposures,
            "analog_gain": gains,
            "seed": seed,
        },
        "objective": dict(_OBJECTIVE),
        "constraints": constraints,
        "candidate_plan": optimized["candidate_plan"],
        "case_count": optimized["case_count"],
        "feasible_count": optimized["feasible_count"],
        "cases": cases,
        "best_case": next((case for case in cases if case["case_index"] == best_index), None),
        "raw_arrays_exported": False,
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
        "limitation": _LIMITATION,
    }
