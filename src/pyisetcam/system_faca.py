"""System-level Field/Angle/Color/Artifact/Control Analysis helpers."""

from __future__ import annotations

import copy
import itertools
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, is_dataclass, replace
from typing import Any

import numpy as np

from .assets import AssetStore
from .camera import camera_compute, camera_create, camera_get, camera_set
from .db_catalog import camerae2e_db_lineage, camerae2e_db_summary, camerae2e_db_validate
from .fdtd_sensor import sensor_attach_fdtd_lut
from .hwisp import HWIspConfig, hw_isp_config, hw_isp_simulate_sequence
from .image_sensor_db import image_sensor_db_reconcile_scenario
from .ip import ip_get
from .optics import si_synthetic
from .scene import scene_create, scene_get
from .sensor import mlens_create, mlens_set, sensor_get, sensor_set, sensor_vignetting
from .sensor_capture import configure_e2e_sensor
from .sensor_estimate import apply_sensor_estimate, image_sensor_db_model
from .tcad_sensor import sensor_attach_tcad_lut
from .types import Camera, Scene


def camerae2e_run_scenario(
    scenario: Mapping[str, Any] | None = None,
    *,
    scene: Scene | Callable[[float], Scene] | str | Mapping[str, Any] | None = None,
    camera: Camera | None = None,
    asset_store: AssetStore | None = None,
    seed: int = 0,
    include_arrays: bool = True,
    sensor_output_mode: str | None = None,
) -> dict[str, Any]:
    """Run a FACA scenario; ``seed`` controls temporal sensor noise and HW jitter.

    The scenario seed takes precedence over an embedded HW ISP config seed.
    Sensor fixed-pattern maps use their separate sensor identity/seed.
    A time-dependent scene is a pure ``scene_at(t_seconds) -> Scene`` callable
    and requires ``hw_isp.enabled=True``. Its t=0 scene is reported as a
    reference; the HW sequence integrates the actual row exposure windows.
    """

    store = asset_store or AssetStore.default()
    config = image_sensor_db_reconcile_scenario(dict(scenario or {}))
    output_mode = str(sensor_output_mode or config.get("sensor_output_mode", "digital_raw"))
    if output_mode not in {"digital_raw", "legacy"}:
        raise ValueError("sensor_output_mode must be digital_raw or legacy.")
    config["sensor_output_mode"] = output_mode
    scenario_name = str(config.get("name", "camerae2e_faca_scenario"))
    scene_source = scene if scene is not None else config.get("scene")
    temporal_scene = scene_source if callable(scene_source) else None
    hw_payload = dict(config.get("hw_isp", {})) if isinstance(config.get("hw_isp"), Mapping) else {}
    use_hw_isp = bool(hw_payload.get("enabled", False))
    if temporal_scene is not None:
        if not use_hw_isp:
            raise ValueError(
                "A time-dependent scene requires hw_isp.enabled=True; "
                "use nframes=1 for a single temporal capture."
            )
        reference_scene = temporal_scene(0.0)
        if not isinstance(reference_scene, Scene):
            raise TypeError("scene_at(t_seconds) must return a Scene.")
        resolved_scene = reference_scene.clone()
        config["scene"] = temporal_scene
    else:
        resolved_scene = _resolve_scene(scene_source, store)
    resolved_camera = _resolve_camera(camera if camera is not None else config.get("camera"), store)
    resolved_camera, parameter_lineage = _apply_physics_and_sensor_overrides(
        resolved_camera, config, store
    )
    resolved_camera.fields["sensor"] = _configure_scenario_readout(
        camera_get(resolved_camera, "sensor"), config, output_mode,
    )
    sensor_fields = camera_get(resolved_camera, "sensor").fields
    config = image_sensor_db_reconcile_scenario(config, actual_physics={
        "fdtd": sensor_fields.get("fdtd_sensor"),
        "tcad": sensor_fields.get("tcad_sensor"),
    })

    hw_sequence = None
    if use_hw_isp:
        hw_config = replace(
            _resolve_hw_config(hw_payload.get("config", hw_payload)), seed=int(seed)
        )
        nframes = int(hw_payload.get("nframes", 3))
        hw_sequence = hw_isp_simulate_sequence(
            resolved_camera,
            temporal_scene if temporal_scene is not None else resolved_scene,
            hw_config,
            nframes=nframes,
            asset_store=store,
            sensor_output_mode=output_mode,
            sensor_resize=bool(config.get("sensor_resize", True)),
        )
        computed_camera = hw_sequence.frames[-1].camera
    else:
        computed_camera = camera_compute(
            resolved_camera.clone(), resolved_scene, asset_store=store, seed=int(seed),
            sensor_resize=bool(config.get("sensor_resize", True)),
        )

    stages = _collect_stages(resolved_scene, computed_camera, include_arrays=include_arrays)
    if temporal_scene is not None:
        stages["scene_photons"].update({"reference_time_s": 0.0, "role": "reference_scene"})
        stages["oi_photons"].update({
            "role": "reference_optical_image",
            "reference_time_s": computed_camera.metadata.get("temporal_capture", {}).get("reference_time_s"),
        })
    metrics = _faca_metrics(stages, hw_sequence.aggregate if hw_sequence is not None else None)
    return {
        "schema_version": "camerae2e_faca_scenario_v1",
        "name": scenario_name,
        "seed": int(seed),
        "scenario": _jsonable(_scenario_without_objects(config)),
        "camera": computed_camera,
        "hw_isp_sequence": hw_sequence,
        "stages": stages,
        "metrics": metrics,
        "parameter_lineage": parameter_lineage,
        "artifact_lineage": _artifact_lineage_summary(),
        "sensor_capture": _jsonable(
            camera_get(computed_camera, "sensor").metadata.get("sensor_capture", {})
        ),
        "sensor_estimate": _jsonable(
            camera_get(computed_camera, "sensor").metadata.get("sensor_estimate", {})
        ),
        "temporal_capture": _jsonable(computed_camera.metadata.get("temporal_capture", {})),
    }


def camerae2e_run_sweep(
    base_scenario: Mapping[str, Any] | None = None,
    sweep_axes: Mapping[str, Iterable[Any]] | None = None,
    *,
    scene: Scene | Callable[[float], Scene] | str | Mapping[str, Any] | None = None,
    camera: Camera | None = None,
    asset_store: AssetStore | None = None,
    seed: int = 0,
    include_arrays: bool = False,
) -> dict[str, Any]:
    """Run a deterministic Cartesian FACA sweep."""

    axes = {str(key): list(values) for key, values in dict(sweep_axes or {}).items()}
    if not axes:
        result = camerae2e_run_scenario(
            base_scenario,
            scene=scene,
            camera=camera,
            asset_store=asset_store,
            seed=seed,
            include_arrays=include_arrays,
        )
        return {
            "schema_version": "camerae2e_faca_sweep_v1",
            "seed": int(seed),
            "axes": {},
            "cases": [camerae2e_faca_report(result)],
        }

    cases = []
    keys = list(axes)
    for index, values in enumerate(itertools.product(*(axes[key] for key in keys))):
        scenario = _deep_dict(base_scenario or {})
        scenario["name"] = str(scenario.get("name", "camerae2e_faca_sweep"))
        axis_payload = {}
        for key, value in zip(keys, values, strict=True):
            _assign_axis_value(scenario, key, value)
            axis_payload[key] = value
        result = camerae2e_run_scenario(
            scenario,
            scene=scene,
            camera=camera,
            asset_store=asset_store,
            seed=int(seed) + index,
            include_arrays=include_arrays,
        )
        report = camerae2e_faca_report(result)
        report["axis_values"] = _jsonable(axis_payload)
        cases.append(report)

    return {
        "schema_version": "camerae2e_faca_sweep_v1",
        "seed": int(seed),
        "axes": _jsonable(axes),
        "case_count": len(cases),
        "cases": cases,
    }


def camerae2e_faca_report(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe FACA report for a scenario or sweep case."""

    if result.get("schema_version") == "camerae2e_faca_sweep_v1":
        return _jsonable(result)
    return {
        "schema_version": "camerae2e_faca_report_v1",
        "name": str(result.get("name", "camerae2e_faca_scenario")),
        "seed": int(result.get("seed", 0)),
        "scenario": _jsonable(result.get("scenario", {})),
        "stage_summaries": {
            str(name): _jsonable({key: value for key, value in payload.items() if key != "array"})
            for name, payload in dict(result.get("stages", {})).items()
        },
        "metrics": _jsonable(result.get("metrics", {})),
        "parameter_lineage": _jsonable(result.get("parameter_lineage", [])),
        "artifact_lineage": _jsonable(result.get("artifact_lineage", {})),
        "sensor_capture": _jsonable(result.get("sensor_capture", {})),
        "sensor_estimate": _jsonable(result.get("sensor_estimate", {})),
        "temporal_capture": _jsonable(result.get("temporal_capture", {})),
    }


def _resolve_scene(source: Scene | str | Mapping[str, Any] | None, store: AssetStore) -> Scene:
    if isinstance(source, Scene):
        return source.clone()
    if isinstance(source, Mapping):
        scene_type = str(source.get("type", source.get("name", "uniform ee")))
        args = list(source.get("args", [16]))
        kwargs = dict(source.get("kwargs", {}))
        kwargs.setdefault("asset_store", store)
        return scene_create(scene_type, *args, **kwargs)
    if isinstance(source, str):
        return scene_create(source, asset_store=store)
    return scene_create("uniform ee", 16, asset_store=store)


def _resolve_camera(source: Camera | Mapping[str, Any] | None, store: AssetStore) -> Camera:
    if isinstance(source, Camera):
        return source.clone()
    if isinstance(source, Mapping):
        camera_type = str(source.get("type", source.get("name", "default")))
        args = list(source.get("args", []))
        created = camera_create(camera_type, *args, asset_store=store)
    else:
        created = camera_create(asset_store=store)
    if isinstance(created, list):
        if not created:
            raise ValueError("camera_create returned an empty camera list.")
        return created[0].clone()
    return created.clone()


def _apply_physics_and_sensor_overrides(
    camera: Camera, config: Mapping[str, Any], store: AssetStore
) -> tuple[Camera, list[dict[str, Any]]]:
    updated = camera.clone()
    lineage: list[dict[str, Any]] = []
    parameter_overrides = {}
    for bucket_name in ("parameters", "camera_parameters"):
        bucket = config.get(bucket_name)
        if isinstance(bucket, Mapping):
            parameter_overrides.update(dict(bucket))
    for key, value in parameter_overrides.items():
        if _camera_parameter_name(key).lower().startswith("sensor "):
            # Apply all sensor settings after loading its optical model, with
            # the same units regardless of the accepted scenario spelling.
            continue
        special = _apply_special_camera_parameter(updated, key, value)
        if special is not None:
            updated, parameter_name, before, after = special
            lineage.append(
                _parameter_lineage_entry(
                    str(key),
                    parameter_name,
                    value,
                    before,
                    after,
                    status="applied",
                )
            )
            continue
        parameter_name = _camera_parameter_name(key)
        before = _safe_camera_get(updated, parameter_name)
        updated = camera_set(updated, parameter_name, value)
        after = _safe_camera_get(updated, parameter_name)
        lineage.append(
            _parameter_lineage_entry(
                str(key),
                parameter_name,
                value,
                before,
                after,
                status="applied",
            )
        )

    sensor = camera_get(updated, "sensor").clone()
    descriptor = config.get("sensor_model")
    if isinstance(descriptor, Mapping):
        sensor.metadata.pop("sensor_simulation_db", None)
        model_kind = descriptor.get("kind")
        if model_kind not in {"db_estimate_v1", "fdtd_estimate_v1", "physical_estimate_v1"}:
            raise ValueError("Unsupported CameraE2E sensor model kind.")
        if config.get("sensor_output_mode") == "legacy":
            raise ValueError(f"{model_kind} requires digital_raw; use model='legacy' instead.")
        for layer in ("fdtd", "tcad"):
            requested = config.get(layer, {})
            inherited = sensor.fields.get(f"{layer}_sensor", {})
            if isinstance(requested, Mapping) and requested.get("enabled") is False:
                sensor.fields[f"{layer}_sensor"] = {"enabled": False}
            elif (
                isinstance(requested, Mapping)
                and (requested.get("lut") is not None or requested.get("db") is not None)
            ) or (isinstance(inherited, Mapping) and inherited.get("enabled", False)):
                raise ValueError(f"{model_kind} cannot also attach FDTD/TCAD response factors.")
        resolved_profile = None
        frozen = None
        if descriptor.get("simulation_db") is not None:
            from .sensor_simulation_db import _load_runtime_snapshot

            frozen = _load_runtime_snapshot(descriptor)
            resolved_profile = copy.deepcopy(frozen.get("readout_profile"))
            if model_kind == "db_estimate_v1" and config.get("sensor_illumination") is not None:
                raise ValueError("Stored DB estimate does not support angular illumination.")
        elif model_kind == "physical_estimate_v1" or descriptor.get("readout_profile") is not None:
            from .sensor_readout_profiles import resolve_sensor_readout

            resolved_profile = resolve_sensor_readout(
                descriptor["sensor_id"], profile=descriptor.get("readout_profile"),
                operating_point=descriptor.get("operating_point"),
                overrides=descriptor.get("readout_overrides"), root=descriptor.get("root"),
            )
        if model_kind == "physical_estimate_v1":
            from .physical_estimate import apply_physical_estimate, image_sensor_physical_model

            if frozen:
                model = copy.deepcopy(frozen["model_snapshot"])
            else:
                model = image_sensor_physical_model(
                    descriptor["sensor_id"], root=descriptor.get("root"),
                    collection_efficiency=descriptor.get("collection_efficiency", 0.75),
                    optical_assumptions=descriptor.get("optical_assumptions"),
                    readout=resolved_profile["readout"],
                )
            if (
                "reviewed_evidence_sha256" in descriptor
                and descriptor["reviewed_evidence_sha256"]
                != model["provenance"]["reviewed_evidence_sha256"]
            ):
                raise ValueError(
                    "Reviewed TechInsights evidence changed since this scenario was built; "
                    "rebuild the sensor DB configuration to adopt the new evidence."
                )
            model["readout_profile"] = resolved_profile
            sensor = apply_physical_estimate(sensor, model, config.get("sensor_illumination"))
        elif model_kind == "fdtd_estimate_v1":
            from .fdtd_estimate import apply_fdtd_estimate, image_sensor_fdtd_model

            model = image_sensor_fdtd_model(
                descriptor["sensor_id"], descriptor["lut_path"], root=descriptor.get("root"),
                collection_efficiency=descriptor.get("collection_efficiency", 0.75),
            )
            model["readout"].update(dict(descriptor.get("readout", {})))
            sensor = apply_fdtd_estimate(sensor, model, config.get("sensor_illumination"))
        else:
            model = copy.deepcopy(frozen["model_snapshot"]) if frozen else image_sensor_db_model(
                descriptor["sensor_id"], root=descriptor.get("root"),
                assumptions=descriptor.get("assumptions"),
            )
            sensor = apply_sensor_estimate(sensor, model)
        if frozen is not None:
            sensor.metadata["sensor_simulation_db"] = copy.deepcopy(descriptor["simulation_db"])
        # The profile is applied after final readout overrides so electron FPN
        # offsets use the actual conversion gain. Never inherit another sensor's profile.
        sensor.fields.pop("e2e_readout_profile", None)
        sensor.metadata.pop("sensor_readout_profile", None)
        if resolved_profile is not None:
            sensor.fields["e2e_readout_profile"] = resolved_profile
        lineage.append(_parameter_lineage_entry(
            "sensor_model", "sensor optical estimate", descriptor, None,
            {"kind": model_kind, "sensor_id": descriptor["sensor_id"]},
            status="estimated",
        ))
    sensor_overrides = _scenario_sensor_overrides(config)
    delayed_sensor_overrides: list[tuple[Any, Any]] = []
    for key, value in sensor_overrides.items():
        if (
            config.get("sensor_output_mode") == "digital_raw"
            and str(key).replace("_", " ").lower() in {"analog gain", "gain"}
        ):
            gain = float(value)
            if not np.isfinite(gain) or gain <= 0:
                raise ValueError("CameraE2E gain must be a finite positive amplification.")
            before = 1.0 / float(sensor.fields["analog_gain"])
            sensor = sensor_set(sensor, "analog gain", 1.0 / gain)
            lineage.append(_parameter_lineage_entry(
                "sensor.analog_gain", "physical analog gain", gain, before, gain,
                status="applied",
            ))
            continue
        if _is_sensor_vignetting_override(key):
            delayed_sensor_overrides.append((key, value))
            continue
        special = _apply_special_sensor_override(sensor, key, value, store)
        if special is not None:
            sensor, sensor_parameter, before, after = special
            lineage.append(
                _parameter_lineage_entry(
                    f"sensor.{str(key).replace(' ', '_')}",
                    sensor_parameter,
                    value,
                    before,
                    after,
                    status="applied",
                )
            )
            continue
        sensor_parameter = _sensor_override_parameter_name(key)
        if sensor_parameter is None:
            raise KeyError(f"Unsupported CameraE2E sensor override: {key!r}")
        before = _safe_sensor_get(sensor, sensor_parameter)
        sensor = sensor_set(sensor, sensor_parameter, _sensor_override_value(key, value))
        after = _safe_sensor_get(sensor, sensor_parameter)
        lineage.append(
            _parameter_lineage_entry(
                f"sensor.{str(key).replace(' ', '_')}",
                sensor_parameter,
                value,
                before,
                after,
                status="applied",
            )
        )
    for key, value in delayed_sensor_overrides:
        special = _apply_special_sensor_override(sensor, key, value, store)
        if special is None:
            raise KeyError(f"Unsupported CameraE2E sensor override: {key!r}")
        sensor, sensor_parameter, before, after = special
        lineage.append(
            _parameter_lineage_entry(
                f"sensor.{str(key).replace(' ', '_')}",
                sensor_parameter,
                value,
                before,
                after,
                status="applied",
            )
        )

    fdtd = dict(config.get("fdtd", {})) if isinstance(config.get("fdtd"), Mapping) else {}
    if not bool(fdtd.get("enabled", True)):
        before = _safe_sensor_get(sensor, "fdtd sensor")
        disabled = dict(before) if isinstance(before, Mapping) else {}
        disabled["enabled"] = False
        sensor = sensor_set(sensor, "fdtd sensor", disabled)
        lineage.append(_parameter_lineage_entry(
            "fdtd.enabled", "fdtd enabled", False, before, {"enabled": False},
            status="disabled",
        ))
    elif fdtd.get("lut") is not None:
        fdtd_kwargs = {key: value for key, value in fdtd.items() if key not in {"lut", "enabled"}}
        sensor = sensor_attach_fdtd_lut(sensor, fdtd["lut"], **fdtd_kwargs)
        lineage.append(
            _parameter_lineage_entry(
                "fdtd.lut",
                "fdtd lut",
                fdtd["lut"],
                None,
                {"attached": True, "options": fdtd_kwargs},
                status="attached",
            )
        )
    elif fdtd.get("enabled") is True and isinstance(sensor.fields.get("fdtd_sensor"), Mapping):
        sensor.fields["fdtd_sensor"] = {**sensor.fields["fdtd_sensor"], "enabled": True}
        lineage.append(_parameter_lineage_entry(
            "fdtd.enabled", "fdtd enabled", True, None, {"enabled": True}, status="enabled",
        ))

    tcad = dict(config.get("tcad", {})) if isinstance(config.get("tcad"), Mapping) else {}
    if not bool(tcad.get("enabled", True)):
        before = _safe_sensor_get(sensor, "tcad sensor")
        disabled = dict(before) if isinstance(before, Mapping) else {}
        disabled["enabled"] = False
        sensor = sensor_set(sensor, "tcad sensor", disabled)
        lineage.append(_parameter_lineage_entry(
            "tcad.enabled", "tcad enabled", False, before, {"enabled": False},
            status="disabled",
        ))
    elif tcad.get("db") is not None:
        tcad_kwargs = {key: value for key, value in tcad.items() if key not in {"db", "enabled"}}
        if "collection_mode" in tcad_kwargs and "mode" not in tcad_kwargs:
            tcad_kwargs["mode"] = tcad_kwargs.pop("collection_mode")
        sensor = sensor_attach_tcad_lut(sensor, tcad["db"], **tcad_kwargs)
        lineage.append(
            _parameter_lineage_entry(
                "tcad.db",
                "tcad db",
                tcad["db"],
                None,
                {"attached": True, "options": tcad_kwargs},
                status="attached",
            )
        )
    elif tcad.get("enabled") is True and isinstance(sensor.fields.get("tcad_sensor"), Mapping):
        sensor.fields["tcad_sensor"] = {**sensor.fields["tcad_sensor"], "enabled": True}
        lineage.append(_parameter_lineage_entry(
            "tcad.enabled", "tcad enabled", True, None, {"enabled": True}, status="enabled",
        ))

    updated.fields["sensor"] = sensor
    return updated, lineage


def _configure_scenario_readout(sensor: Any, config: Mapping[str, Any], mode: str) -> Any:
    if mode == "legacy":
        return configure_e2e_sensor(sensor, mode="legacy", readout=config.get("readout"))
    readout = dict(sensor.fields.get("e2e_model_readout", {}))
    overrides = _scenario_sensor_overrides(config)
    names = {str(k).replace("_", " ").lower() for k in overrides}
    if names & {"analog gain", "gain"}:
        readout["gain"] = 1.0 / float(sensor.fields["analog_gain"])
    else:
        readout.setdefault("gain", 1.0 / float(sensor.fields["analog_gain"]))
    pixel = sensor.fields["pixel"]
    conversion_gain = float(pixel["conversion_gain_v_per_electron"])
    conversion_changed = bool(names & {"pixel conversion gain", "conversion gain"})
    if conversion_changed or names & {"pixel voltage swing", "voltage swing"}:
        readout["full_well_e"] = float(pixel["voltage_swing"]) / conversion_gain
    if conversion_changed or names & {
        "pixel read noise v", "pixel read noise", "pixel read noise volts",
        "read noise", "read noise v",
    }:
        readout["read_noise_e"] = float(pixel["read_noise_v"]) / conversion_gain
    if names & {"pixel read noise electrons", "read noise electrons"}:
        readout["read_noise_e"] = float(pixel["read_noise_v"]) / conversion_gain
    if conversion_changed or names & {
        "pixel dark voltage", "dark voltage", "dark voltage v per sec",
    }:
        readout["dark_current_e_per_s"] = float(pixel["dark_voltage_v_per_sec"]) / conversion_gain
    if names & {"nbits", "bit depth", "bits"}:
        readout["bit_depth"] = int(sensor.fields["nbits"])
    if names & {"black level", "black level dn"}:
        readout["black_level_dn"] = float(sensor_get(sensor, "black level"))
    if names & {"quantization", "quantization method"}:
        method = str(sensor.fields["quantization"]).lower().strip()
        bits = method.replace("bits", "").replace("bit", "").strip()
        if bits.isdigit():
            readout["bit_depth"] = int(bits)
        elif method != "linear":
            raise ValueError("Digital RAW requires linear quantization; use legacy mode.")
    supplied = config.get("readout", {})
    if not isinstance(supplied, Mapping):
        raise ValueError("readout must be a mapping of physical sensor settings.")
    readout.update(supplied)
    configured = configure_e2e_sensor(sensor, readout=readout)
    profile = sensor.fields.get("e2e_readout_profile")
    if isinstance(profile, Mapping):
        from .sensor_readout_profiles import apply_readout_profile

        effective_profile = copy.deepcopy(dict(profile))
        effective_profile["readout"] = dict(configured.fields["sensor_readout"])
        # Explicit legacy sensor overrides retain their units and precedence.
        fixed = effective_profile["fixed_pattern"]
        final_conversion = float(configured.fields["pixel"]["conversion_gain_v_per_electron"])
        if names & {"prnu level", "prnu sigma"}:
            fixed["prnu_fraction"] = float(sensor.fields["pixel"].get("prnu_sigma", 0))
        if names & {"dsnu level", "dsnu sigma"}:
            offset_v = float(sensor.fields["pixel"].get("dsnu_sigma_v", 0))
            fixed["dsnu_e"] = offset_v / final_conversion
        if names & {"column fpn"}:
            column = np.asarray(sensor.fields.get("column_fpn", [0.0, 0.0]), float)
            fixed["column_dsnu_e"], fixed["column_prnu_fraction"] = (
                float(column[0]) / final_conversion, float(column[1])
            )
        configured = apply_readout_profile(configured, effective_profile)
    estimate = configured.metadata.get("sensor_estimate")
    if isinstance(estimate, dict):
        estimate["scenario_sensor_overrides"] = _jsonable(dict(overrides))
        estimate["effective_readout"] = _jsonable(configured.fields["sensor_readout"])
        if profile is not None:
            estimate["readout_profile"] = _jsonable(configured.metadata["sensor_readout_profile"])
    return configured


def _scenario_sensor_overrides(config: Mapping[str, Any]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for bucket in ("parameters", "camera_parameters"):
        values = config.get(bucket, {})
        if isinstance(values, Mapping):
            for key, value in values.items():
                name = _camera_parameter_name(key).lower()
                if name.startswith("sensor "):
                    overrides[name[7:]] = value
    values = config.get("sensor", {})
    if isinstance(values, Mapping):
        for key, value in values.items():
            overrides[str(key).replace("_", " ").lower()] = value
    return overrides


def _apply_special_camera_parameter(
    camera: Camera, key: Any, value: Any
) -> tuple[Camera, str, Any, Any] | None:
    normalized = str(key).strip().replace("_", " ").lower()
    if normalized in {
        "optics.psf radius um",
        "optics.si psf radius um",
        "optics.shift invariant psf radius um",
    }:
        radius_um = float(np.asarray(value, dtype=float).reshape(-1)[0])
        if radius_um <= 0.0:
            raise ValueError("optics.si_psf_radius_um must be positive.")
        before = _safe_camera_get(camera, "optics model")
        oi = camera_get(camera, "oi")
        pillbox_diameter_mm = 2.0 * radius_um * 1e-3
        optics = si_synthetic("pillbox", oi, pillbox_diameter_mm)
        updated = camera_set(camera, "optics", optics)
        after = {
            "optics_model": _safe_camera_get(updated, "optics model"),
            "proxy_model": "shift_invariant_pillbox_psf",
            "radius_um": radius_um,
            "diameter_mm": pillbox_diameter_mm,
            "truth_boundary": (
                "synthetic_shift_invariant_proxy_not_rayoptics_or_wave_optics_signoff"
            ),
        }
        return updated, "optics si psf radius um", before, after
    return None


def _apply_special_sensor_override(
    sensor: Any, key: Any, value: Any, store: AssetStore
) -> tuple[Any, str, Any, Any] | None:
    normalized = str(key).strip().replace("_", " ").lower()
    if normalized in {
        "cfa preset",
        "cfa pattern preset",
        "cfa type",
        "cfa layout",
    }:
        before = {
            "pattern": _safe_sensor_get(sensor, "pattern"),
            "cfa_name": _safe_sensor_get(sensor, "cfa name"),
        }
        updated = sensor_set(sensor, "cfa preset", value)
        after = {
            "preset": value,
            "pattern": _safe_sensor_get(updated, "pattern"),
            "cfa_name": _safe_sensor_get(updated, "cfa name"),
        }
        return updated, "sensor cfa preset", before, after
    if normalized in {
        "ocl group proxy",
        "shared ocl aperture",
        "shared ocl aperture proxy",
    }:
        before = _safe_sensor_get(sensor, "ocl group proxy")
        updated = sensor_set(sensor, "ocl group proxy", value)
        after = _safe_sensor_get(updated, "ocl group proxy")
        return updated, "sensor ocl group proxy", before, after
    if normalized in {
        "ocl group shape",
        "shared ocl shape",
        "shared ocl aperture shape",
    }:
        before = _safe_sensor_get(sensor, "ocl group proxy")
        updated = sensor_set(sensor, "ocl group shape", value)
        after = _safe_sensor_get(updated, "ocl group proxy")
        return updated, "sensor ocl group shape", before, after
    if normalized in {
        "ocl group equalization",
        "ocl equalization",
        "shared ocl equalization",
        "shared ocl aperture equalization",
    }:
        before = _safe_sensor_get(sensor, "ocl group proxy")
        updated = sensor_set(sensor, "ocl group equalization", value)
        after = _safe_sensor_get(updated, "ocl group proxy")
        return updated, "sensor ocl group equalization", before, after
    if normalized in {
        "binning factor",
        "pixel binning factor",
        "readout binning factor",
    }:
        before = _safe_sensor_get(sensor, "sensor compute method")
        payload = _sensor_binning_factor_value(value)
        updated = sensor_set(sensor, "sensor compute method", payload)
        after = _safe_sensor_get(updated, "sensor compute method")
        return updated, "sensor compute method", before, after
    if normalized in {
        "ocl vignetting",
        "microlens vignetting",
        "pixel vignetting",
        "vignetting",
    }:
        before = {
            "vignetting": _safe_sensor_get(sensor, "vignetting"),
            "etendue": _array_summary(_safe_sensor_get(sensor, "etendue")),
        }
        updated = sensor_vignetting(sensor, value, asset_store=store)
        updated = sensor_set(updated, "vignetting", _sensor_vignetting_value(value))
        after = {
            "vignetting": _safe_sensor_get(updated, "vignetting"),
            "vignetting_name": _safe_sensor_get(updated, "vignetting name"),
            "etendue": _array_summary(_safe_sensor_get(updated, "etendue")),
        }
        return updated, "sensor ocl vignetting", before, after
    microlens_parameter = _microlens_override_parameter_name(normalized)
    if microlens_parameter is None:
        return None
    before = _safe_sensor_get(sensor, "microlens")
    microlens = before if isinstance(before, Mapping) else mlens_create(sensor, asset_store=store)
    if normalized in {"ocl focal length um", "microlens focal length um"}:
        updated_microlens = mlens_set(microlens, microlens_parameter, value, "microns")
    else:
        updated_microlens = mlens_set(microlens, microlens_parameter, value)
    updated = sensor_set(sensor, "microlens", updated_microlens)
    updated.fields["etendue"] = None
    after = _safe_sensor_get(updated, "microlens")
    return updated, f"microlens {microlens_parameter}", before, after


def _microlens_override_parameter_name(normalized: str) -> str | None:
    if normalized in {
        "ocl fnumber",
        "ocl f number",
        "microlens fnumber",
        "microlens f number",
    }:
        return "microlens fnumber"
    if normalized in {
        "ocl focal length um",
        "microlens focal length um",
    }:
        return "microlens focal length"
    if normalized in {
        "ocl refractive index",
        "microlens refractive index",
    }:
        return "microlens refractive index"
    if normalized in {
        "ocl chief ray angle deg",
        "microlens chief ray angle deg",
        "microlens ray angle deg",
    }:
        return "chief ray angle"
    return None


def _is_sensor_vignetting_override(key: Any) -> bool:
    normalized = str(key).strip().replace("_", " ").lower()
    return normalized in {
        "ocl vignetting",
        "microlens vignetting",
        "pixel vignetting",
        "vignetting",
    }


def _camera_parameter_name(key: Any) -> str:
    value = str(key).strip().replace("_", " ")
    if "." in value:
        prefix, remainder = value.split(".", 1)
        return f"{prefix} {remainder.replace('.', ' ')}"
    return value


def _sensor_override_parameter_name(key: Any) -> str | None:
    normalized = str(key).replace("_", " ").lower()
    if normalized in {"size", "sensor size"}:
        return "size"
    if normalized in {"nbits", "bit depth", "bits"}:
        return "nbits"
    if normalized in {"black level", "black level dn"}:
        return "black level"
    if normalized in {"quantization", "quantization method"}:
        return "quantization method"
    if normalized in {"pixel read noise electrons", "read noise electrons"}:
        return "pixel read noise electrons"
    if normalized in {"fixed pattern seed", "fpn seed"}:
        return "fixed pattern seed"
    if normalized in {"prnu level", "prnu sigma"}:
        return "prnu level"  # Existing sensor API uses percent.
    if normalized in {"dsnu level", "dsnu sigma"}:
        return "dsnu level"  # Existing sensor API uses volts per capture.
    if normalized == "column fpn":
        return "column fpn"
    if normalized in {"noise flag", "noise"}:
        return "noise flag"
    if normalized in {"integration time", "integration"}:
        return "integration time"
    if normalized in {"exposure duration", "exposure time", "exposure time s"}:
        return "exposure duration"
    if normalized in {"analog gain", "gain"}:
        return "analog gain"
    if normalized in {"pixel size", "pixel size m", "pixel pitch", "pixel pitch m"}:
        return "pixel size"
    if normalized in {"pixel fill factor", "fill factor", "pd fill factor"}:
        return "pixel fill factor"
    if normalized in {"n samples per pixel", "samples per pixel", "spatial samples per pixel"}:
        return "n samples per pixel"
    if normalized in {"cfa pattern", "pattern", "pattern and size"}:
        return "pattern and size"
    if normalized in {"cfa preset", "cfa pattern preset", "cfa type", "cfa layout"}:
        return "cfa preset"
    if normalized in {"cfa", "color filter array"}:
        return "cfa"
    if normalized in {"filter names", "filter name"}:
        return "filter names"
    if normalized in {"filter spectra", "color filters", "filter transmissivities"}:
        return "filter spectra"
    if normalized in {
        "pixel read noise v", "pixel read noise", "pixel read noise volts",
        "read noise", "read noise v",
    }:
        return "pixel read noise"
    if normalized in {"pixel dark voltage", "dark voltage", "dark voltage v per sec"}:
        return "pixel dark voltage"
    if normalized in {"pixel voltage swing", "voltage swing"}:
        return "pixel voltage swing"
    if normalized in {"pixel conversion gain", "conversion gain"}:
        return "pixel conversion gain"
    if normalized in {"sensor compute method", "compute method"}:
        return "sensor compute method"
    if normalized in {"binning", "binning method", "pixel binning", "pixel binning method"}:
        return "sensor compute method"
    if normalized in {"microlens", "micro lens", "ocl"}:
        return "microlens"
    if normalized in {"microlens offset", "microlens offset microns", "ocl offset um"}:
        return "microlens offset"
    if normalized in {"ocl group proxy", "shared ocl aperture", "shared ocl aperture proxy"}:
        return "ocl group proxy"
    if normalized in {"ocl group shape", "shared ocl shape", "shared ocl aperture shape"}:
        return "ocl group shape"
    if normalized in {
        "ocl group equalization",
        "ocl equalization",
        "shared ocl equalization",
        "shared ocl aperture equalization",
    }:
        return "ocl group equalization"
    return None


def _sensor_override_value(key: Any, value: Any) -> Any:
    normalized = str(key).replace("_", " ").lower()
    if normalized not in {"binning", "binning method", "pixel binning", "pixel binning method"}:
        return value
    if value is None:
        return None
    if isinstance(value, Mapping):
        payload = dict(value)
        payload.setdefault("name", "binning")
        return payload
    normalized_value = str(value).strip().lower()
    if normalized_value in {"", "off", "none", "default", "disabled", "false", "0"}:
        return None
    return {"name": "binning", "method": str(value)}


def _sensor_binning_factor_value(value: Any) -> dict[str, Any] | None:
    factor = int(np.asarray(value, dtype=float).reshape(-1)[0])
    if factor <= 1:
        return None
    if factor != 2:
        raise ValueError("sensor.binning_factor currently supports 1/off or the legacy 2x proxy.")
    return {"name": "binning", "method": "kodak2008", "factor": factor}


def _sensor_vignetting_value(value: Any) -> Any:
    if isinstance(value, str):
        normalized = value.strip().lower().replace("_", " ")
        if normalized in {"off", "skip", "none", "disabled", "false", "0"}:
            return 0
        if normalized in {"bare", "no microlens", "nomicrolens", "1"}:
            return 1
        if normalized in {"centered", "2"}:
            return 2
        if normalized in {"optimal", "optimized", "3"}:
            return 3
    return value


def _array_summary(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    array = np.asarray(value, dtype=float)
    if array.size == 0:
        return {"shape": list(array.shape), "available": False}
    return {
        "shape": list(array.shape),
        "available": True,
        "min": float(np.nanmin(array)),
        "max": float(np.nanmax(array)),
        "mean": float(np.nanmean(array)),
    }


def _parameter_lineage_entry(
    path: str,
    parameter: str,
    requested: Any,
    before: Any,
    after: Any,
    *,
    status: str,
) -> dict[str, Any]:
    return {
        "path": str(path),
        "parameter": str(parameter),
        "requested": _jsonable(requested),
        "before": _jsonable(before),
        "after": _jsonable(after),
        "status": str(status),
    }


def _resolve_hw_config(raw: Any) -> HWIspConfig:
    if isinstance(raw, HWIspConfig):
        return raw
    if isinstance(raw, Mapping):
        return hw_isp_config(
            **{
                key: value
                for key, value in raw.items()
                if key not in {"enabled", "nframes", "config"}
            }
        )
    return hw_isp_config()


def _collect_stages(
    scene: Scene | None, camera: Camera, *, include_arrays: bool
) -> dict[str, dict[str, Any]]:
    oi = camera_get(camera, "oi")
    sensor = camera_get(camera, "sensor")
    ip = camera_get(camera, "ip")
    stages = {
        "scene_photons": _stage_payload(
            _safe_get(lambda: scene_get(scene, "photons")), include_arrays=include_arrays
        ),
        "oi_photons": _stage_payload(oi.data.get("photons"), include_arrays=include_arrays),
        "sensor_raw": _stage_payload(_sensor_raw(sensor), include_arrays=include_arrays),
        "sensor_digital": _stage_payload(_sensor_digital(sensor), include_arrays=include_arrays),
        "sensor_mean_electrons": _stage_payload(
            sensor.data.get("mean_electrons"), include_arrays=include_arrays,
        ),
        "sensor_reference_dn": _stage_payload(
            sensor.data.get("reference_dn"), include_arrays=include_arrays,
        ),
        "sensor_analog_volts": _stage_payload(
            sensor.data.get("volts"), include_arrays=include_arrays,
        ),
        "ip_result": _stage_payload(
            _safe_get(lambda: ip_get(ip, "result")), include_arrays=include_arrays
        ),
        "ip_srgb": _stage_payload(
            _safe_get(lambda: ip_get(ip, "srgb")), include_arrays=include_arrays
        ),
    }
    digital = sensor.fields.get("sensor_output_mode") == "digital_raw"
    for name, unit in (
        ("sensor_raw", "DN" if digital else "V"), ("sensor_digital", "DN"),
        ("sensor_mean_electrons", "e-/pixel"), ("sensor_reference_dn", "DN"),
        ("sensor_analog_volts", "V"),
    ):
        stages[name]["unit"] = unit
    return stages


def _sensor_raw(sensor: Any) -> Any:
    for key in ("raw_dn", "volts", "dv", "electrons"):
        if key in getattr(sensor, "data", {}):
            return sensor.data[key]
    try:
        return sensor_get(sensor, "volts")
    except Exception:
        return None


def _sensor_digital(sensor: Any) -> Any:
    for key in ("digital_values", "dv"):
        if key in getattr(sensor, "data", {}):
            return sensor.data[key]
    try:
        return sensor_get(sensor, "digital values")
    except Exception:
        return None


def _stage_payload(values: Any, *, include_arrays: bool) -> dict[str, Any]:
    if values is None:
        return {"available": False, "shape": [], "dtype": None}
    array = np.asarray(values)
    payload: dict[str, Any] = {
        "available": bool(array.size),
        "shape": list(array.shape),
        "dtype": str(array.dtype),
    }
    if array.size:
        finite = np.asarray(array, dtype=float)
        payload.update(
            {
                "min": float(np.nanmin(finite)),
                "max": float(np.nanmax(finite)),
                "mean": float(np.nanmean(finite)),
                "std": float(np.nanstd(finite)),
            }
        )
    if include_arrays:
        payload["array"] = array.copy()
    return payload


def _faca_metrics(
    stages: Mapping[str, Mapping[str, Any]], hw_aggregate: Mapping[str, Any] | None
) -> dict[str, Any]:
    raw = dict(stages.get("sensor_raw", {}))
    rgb = dict(stages.get("ip_result", {}))
    rgb_array = np.asarray(rgb.get("array", np.empty(0)), dtype=float)
    raw_array = np.asarray(raw.get("array", np.empty(0)), dtype=float)
    raw_std = float(np.nanstd(raw_array)) if raw_array.size else raw.get("std")
    rgb_std = float(np.nanstd(rgb_array)) if rgb_array.size else rgb.get("std")
    rgb_mean = float(np.nanmean(rgb_array)) if rgb_array.size else rgb.get("mean")
    metrics: dict[str, Any] = {
        "field": {"available": bool(stages.get("oi_photons", {}).get("available", False))},
        "angle": {
            "available": False,
            "note": "CRA/field axes are recorded through attached FDTD scenario parameters.",
        },
        "color": {"rgb_mean": None if rgb_mean is None else float(rgb_mean)},
        "artifact": {
            "raw_std": None if raw_std is None else float(raw_std),
            "raw_spatial_std": None if raw_std is None else float(raw_std),
            "raw_std_note": "Single-frame spatial variation; not temporal noise or SNR.",
            "rgb_std": None if rgb_std is None else float(rgb_std),
            "rgb_clip_fraction": _clip_fraction(rgb_array) if rgb_array.size else None,
        },
        "control": dict(hw_aggregate or {}),
    }
    return metrics


def _clip_fraction(array: np.ndarray) -> float:
    values = np.asarray(array, dtype=float)
    if values.size == 0:
        return 0.0
    return float(np.mean((values <= 0.0) | (values >= 1.0)))


def _artifact_lineage_summary() -> dict[str, Any]:
    validation = camerae2e_db_validate()
    summary = camerae2e_db_summary()
    lineage = {}
    for name in (
        "fdtd_sensor_lut_active",
        "tcad_sensor_db_active",
        "lens_patents_active",
        "hwisp_parameter_profiles",
    ):
        try:
            lineage[name] = camerae2e_db_lineage(name)
        except Exception as exc:
            lineage[name] = {"error": str(exc)}
    return {"summary": summary, "validation": validation, "lineage": lineage}


def _assign_axis_value(scenario: dict[str, Any], key: str, value: Any) -> None:
    parts = str(key).split(".", 1)
    if len(parts) == 2 and parts[0] in {"sensor", "fdtd", "tcad", "hw_isp"}:
        bucket = scenario.setdefault(parts[0], {})
        if not isinstance(bucket, dict):
            bucket = {}
            scenario[parts[0]] = bucket
        if parts[0] == "hw_isp" and parts[1] != "enabled":
            bucket.setdefault("enabled", True)
        bucket[parts[1]] = value
        return
    scenario.setdefault("parameters", {})[key] = value


def _scenario_without_objects(config: Mapping[str, Any]) -> dict[str, Any]:
    payload = {}
    for key, value in config.items():
        if isinstance(value, (Camera, Scene)):
            payload[key] = {"type": type(value).__name__, "name": value.name}
        else:
            payload[key] = value
    return payload


def _deep_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        result[str(key)] = _deep_dict(item) if isinstance(item, Mapping) else item
    return result


def _safe_get(callback: Any) -> Any:
    try:
        return callback()
    except Exception:
        return None


def _safe_camera_get(camera: Camera, parameter: str) -> Any:
    try:
        return camera_get(camera, parameter)
    except Exception:
        return None


def _safe_sensor_get(sensor: Any, parameter: str) -> Any:
    try:
        return sensor_get(sensor, parameter)
    except Exception:
        return None


def _jsonable(value: Any) -> Any:
    if callable(value):
        return {"type": "scene_at", "time_unit": "seconds", "serialization": "external_callable_not_serialized"}
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
            if key not in {"array", "camera", "hw_isp_sequence"}
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if is_dataclass(value):
        return _jsonable(asdict(value))
    return value


cameraE2ERunScenario = camerae2e_run_scenario  # noqa: N816
cameraE2ERunSweep = camerae2e_run_sweep  # noqa: N816
cameraE2EFACAReport = camerae2e_faca_report  # noqa: N816
