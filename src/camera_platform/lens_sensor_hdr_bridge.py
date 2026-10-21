"""Static geometric Lens DB × saved Sensor DB integer-RAW HDR bracket.

This pure bridge returns arrays and a report; it does not persist a Job. It
uses one pinned lens, one frozen sensor/readout, and one static D65 chart. The
saved sensor's photoelectron response is checked for exposure linearity at the
same optical image before independent short/long RAW readouts are sampled.
Current lens radiometry is unknown and explicitly accepted as a unity proxy.
Only native 2x2 RGGB DB-estimate snapshots fit the existing RAW HDR merger.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.camera import camera_create, camera_get, camera_set
from pyisetcam.lens_patents import lens_patent_raytrace_optics
from pyisetcam.scene import scene_adjust_luminance, scene_interpolate_w, scene_set
from pyisetcam.sensor import sensor_compute_noise_free
from pyisetcam.sensor_capture import resample_sensor_capture
from pyisetcam.sensor_comparison import _make_scene
from pyisetcam.sensor_simulation_db import (
    _digest,
    sensor_simulation_db_config,
    sensor_simulation_db_get,
)
from pyisetcam.system_faca import camerae2e_run_scenario

from . import hdr
from .lens_catalog_bridge import _asset, _revision_source, geometric_lens_preview
from .lens_sensor_raw_bridge import (
    WAVELENGTHS_NM,
    _number,
    bounded_psf_resource_preflight,
    require_bounded_psf_budget,
)
from .offline_assets import offline_asset_identity, offline_asset_store
from .saved_sensor_hdr import _FPN_FIELDS, _calibration, _disable_fixed_pattern, _exposures
from .sensor_db_bridge import inspect_sensor_revision

SCHEMA = "camerae2e_lens_sensor_static_hdr_v1"
BOUNDED_SCHEMA = "camerae2e_lens_sensor_static_hdr_v2"
LIMITATION = (
    "Static independently reset RAW bracket from one pinned geometric Lens DB "
    "PSF and one saved DB-estimate sensor. Lens spectral transmission and "
    "relative illumination are unknown unity placeholders; diffraction, "
    "pixel-integrated PSF accuracy and lens-CRA-dependent sensor response "
    "are not established. The configured sensor window is a virtual mosaic "
    "with saved CFA phase at origin zero, not a located crop of the saved "
    "native array. Saved PRNU/DSNU/column FPN are explicitly disabled "
    "for the scalar RAW HDR merger. Joint LFM keeps saved FPN, so HDR and LFM "
    "do not share identical executed readout conditions. A model-known FPN "
    "HDR merger is future work and is not implied by this proxy. The scene "
    "and optical image are held "
    "constant while a checked linear photoelectron rate is scaled across "
    "exposures. No exposure timestamps, motion/deghosting, PWM/LFM, native "
    "Quad Bayer fusion, measured calibration or product ranking is claimed."
)
BOUNDED_LIMITATION = (
    LIMITATION + " The strict ray-trace PSF is generated and cropped on demand "
    "without a complete precomputed field/angle/wavelength stack. This changes "
    "the execution strategy, not the optical model or validation status."
)


def _identity(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _array_sha(value: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(value).tobytes()).hexdigest()


def _prepare_lens_sensor_capture(
    lens_revision_id: str,
    sensor_identifier: str,
    *,
    sensor_run_id: str,
    root: str | Path,
    workspace: str | Path,
    exposure_s: float,
    scene_kind: str = "slanted_edge",
    sensor_size: Sequence[int] = (16, 16),
    luminance_cd_m2: float = 1000.0,
    seed: int = 7,
    accept_approximate_lens_radiometry: bool = False,
    require_native_rggb: bool = False,
    psf_compute_mode: str = "precomputed",
) -> dict[str, Any]:
    """Prepare one pinned static lens/sensor mean before dark and readout.

    The returned Sensor contains a 2D mean_electrons plane at ``exposure_s``.
    It is a simulator state, not a sampled RAW image. Consumers must check
    their own CFA, timing and response-linearity contracts before sampling.
    """
    if accept_approximate_lens_radiometry is not True:
        raise ValueError("Explicit acceptance of approximate lens radiometry is required.")
    if psf_compute_mode not in {"precomputed", "bounded_streaming"}:
        raise ValueError("psf_compute_mode must be precomputed or bounded_streaming.")
    if not isinstance(lens_revision_id, str) or not lens_revision_id.strip():
        raise ValueError("Choose an explicit Lens DB revision ID.")
    if not isinstance(sensor_identifier, str) or not sensor_identifier.strip():
        raise ValueError("Choose an explicit saved sensor identifier.")
    if not isinstance(sensor_run_id, str) or not sensor_run_id.strip():
        raise ValueError("Choose an explicit saved sensor run ID.")
    reference_exposure = _number(exposure_s, "exposure_s", 1e-6, 1.0)
    if scene_kind not in {"uniform", "slanted_edge"}:
        raise ValueError("scene_kind must be uniform or slanted_edge.")
    if (
        not isinstance(sensor_size, (tuple, list)) or len(sensor_size) != 2
        or any(type(v) is not int or v < 8 or v > 32 or v % 2 for v in sensor_size)
    ):
        raise ValueError("sensor_size needs two even integer dimensions from 8 to 32.")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    luminance = _number(luminance_cd_m2, "luminance_cd_m2", 0.01, 10000.0)
    size = (int(sensor_size[0]), int(sensor_size[1]))
    db_root = Path(root).expanduser().resolve()

    # These checks run before expensive optics. An HDR caller requests the
    # stricter native-RGGB gate; temporal callers retain their actual CFA.
    sensor_source = inspect_sensor_revision(
        sensor_identifier, root=db_root, run_id=sensor_run_id
    )
    if not sensor_source["executable"]:
        raise ValueError(f"Saved sensor run cannot execute: {sensor_source['reason']}")
    if sensor_source["native_response"]["kind"] != "db_estimate_v1":
        raise ValueError(
            "Joint lens/sensor capture supports db_estimate_v1 only; physical/FDTD sensors "
            "need a matched lens pupil and CRA contract."
        )
    selected = sensor_source["revision"]
    saved = sensor_simulation_db_get(
        selected["report_code"], root=db_root, run_id=selected["run_id"]
    )
    metadata = saved["metadata"]
    if _digest(metadata["config"]) != selected["config_sha256"]:
        raise ValueError("Saved sensor config differs from its pinned run.")
    native_cfa = metadata.get("native_cfa") or {}
    model = metadata["model_snapshot"]
    native_size = native_cfa.get("native_size")
    if (
        native_cfa.get("pattern_index_base") != 1
        or native_cfa.get("filter_names") != model.get("filter_names")
        or native_cfa.get("pixel_pitch_um") != model.get("pixel_pitch_um")
        or not isinstance(native_size, list)
        or len(native_size) != 2
        or any(type(value) is not int or value <= 0 or value % 2 for value in native_size)
    ):
        raise ValueError("Saved sensor native CFA differs from its model snapshot.")
    if require_native_rggb and (
        native_cfa.get("pattern") != [[1, 2], [2, 3]]
        or native_cfa.get("filter_names") != ["red", "green", "blue"]
        or model.get("cfa_pattern") != "rggb_bayer"
    ):
        raise ValueError(
            "Joint lens HDR requires an exact native 2x2 RGGB saved CFA; "
            "Quad Bayer and other mosaics need a CFA-aware HDR merger."
        )
    saved_fpn = (metadata.get("readout_profile") or {}).get("fixed_pattern") or {}
    if set(saved_fpn) != set(_FPN_FIELDS) or any(
        type(value) not in (int, float) or not math.isfinite(float(value))
        for value in saved_fpn.values()
    ):
        raise ValueError("Saved capture requires explicit finite four-field FPN provenance.")
    scenario = sensor_simulation_db_config(
        selected["report_code"], root=db_root, run_id=selected["run_id"]
    )["scenario"]
    if scenario.get("sensor_output_mode") != "digital_raw":
        raise ValueError("Saved sensor needs a digital_raw output contract.")
    if scenario.get("sensor_illumination") is not None:
        raise ValueError("Saved angular illumination cannot be reused with a new lens.")

    lens = geometric_lens_preview(lens_revision_id, catalog_root=db_root)
    resource_preflight = None
    if psf_compute_mode == "bounded_streaming":
        resource_preflight = bounded_psf_resource_preflight(
            lens["model"], sensor_source["native_response"]["pixel_pitch_um"], size
        )
        require_bounded_psf_budget(resource_preflight)
    revision, snapshot, lens_db, source_sha = _revision_source(lens_revision_id, db_root)
    simulation_id, psf_path, validation, _ = _asset(
        revision, snapshot, lens_db, db_root
    )
    optics = lens_patent_raytrace_optics(
        simulation_id, db_path=lens_db, psf_dir=psf_path.parent,
        allow_approximate_radiometry=True,
    )
    ray = optics["raytrace"]
    if (
        ray.get("asset_sha256") != lens["identity"]["asset_sha256"]
        or ray.get("source_prescription_sha256") != validation["source_prescription_sha256"]
        or ray.get("psf_kind") != "geometric"
        or ray.get("diffraction_included") is not False
        or ray.get("radiometry_assumption_accepted") is not True
        or ray.get("radiometry_status") == "validated"
    ):
        raise ValueError("Loaded lens optics differ from the pinned geometric proxy.")
    for table_name in ("geometry", "relative_illumination", "psf"):
        if not np.allclose(
            np.asarray(ray[table_name]["wavelength_nm"], dtype=float),
            WAVELENGTHS_NM, rtol=0, atol=1e-9,
        ):
            raise ValueError("Lens optical tables must contain exactly 450/550/650 nm.")
    transmission = optics.get("transmittance", {})
    relative = ray["relative_illumination"]
    if (
        transmission.get("status") != "unknown"
        or not np.array_equal(np.asarray(transmission.get("scale")), np.ones(3))
        or relative.get("status") != "unknown"
        or not np.array_equal(
            np.asarray(relative["function"]),
            np.ones_like(np.asarray(relative["function"])),
        )
    ):
        raise ValueError("Lens radiometry differs from the declared unity proxy.")

    pitch_m = _number(
        sensor_source["native_response"]["pixel_pitch_um"],
        "pixel_pitch_um", 0.1, 100.0,
    ) * 1e-6
    focal_m = _number(optics["focal_length_m"], "lens_focal_length_m", 1e-5, 1.0)
    horizontal_extent_m = 1.2 * size[1] * pitch_m
    horizontal_fov_deg = math.degrees(
        2 * math.atan(horizontal_extent_m / (2 * focal_m))
    )
    vertical_fov_deg = math.degrees(
        2 * math.atan(horizontal_extent_m * size[0] / size[1] / (2 * focal_m))
    )
    diagonal_fov_deg = math.degrees(2 * math.atan(math.hypot(
        math.tan(math.radians(horizontal_fov_deg) / 2),
        math.tan(math.radians(vertical_fov_deg) / 2),
    )))
    if diagonal_fov_deg > float(ray["max_fov_deg"]):
        raise ValueError("Sensor scene exceeds the saved lens PSF field of view.")
    object_distance_m = float(ray["object_distance_m"])
    if not object_distance_m > 0:
        raise ValueError("Lens object distance is invalid.")

    assets_identity = offline_asset_identity(workspace=workspace, source=db_root)
    assets = offline_asset_store(workspace=workspace, source=db_root)
    with assets.bound_default(assets):
        scene, scene_geometry = _make_scene(
            scene_kind, size, horizontal_fov_deg, assets
        )
        scene_interpolate_w(scene, WAVELENGTHS_NM, asset_store=assets)
        scene_adjust_luminance(scene, luminance, asset_store=assets)
        scene_set(scene, "distance", object_distance_m)
        if math.degrees(2 * math.atan(math.hypot(
            math.tan(math.radians(scene.fields["fov_deg"]) / 2),
            math.tan(math.radians(scene.fields["vfov_deg"]) / 2),
        ))) > float(ray["max_fov_deg"]):
            raise ValueError("Generated scene exceeds the saved lens PSF field of view.")
        scene_hash = _array_sha(np.asarray(scene.data["photons"], dtype="<f8"))
        camera = camera_create(asset_store=assets)
        camera_set(camera, "optics", optics)
        if psf_compute_mode == "bounded_streaming":
            camera.fields["oi"].fields["psf_compute_mode"] = "bounded_streaming"
        current_scenario = {
            **scenario,
            "name": "lens_sensor_static_hdr_bracket",
            "sensor": {
                **scenario.get("sensor", {}),
                "size": list(size),
                "integration_time": reference_exposure,
                "noise_flag": 0,
            },
            "sensor_resize": False,
        }
        initial = camerae2e_run_scenario(
            current_scenario, scene=scene, camera=camera, asset_store=assets,
            sensor_output_mode="digital_raw", include_arrays=False, seed=seed,
        )
        computed = initial["camera"]
        cached = camera_get(computed, "sensor")
        optical_image = camera_get(computed, "oi")
        psf_execution = None
        if psf_compute_mode == "bounded_streaming":
            psf_execution = optical_image.fields.get("psf_streaming")
            if (
                not isinstance(psf_execution, dict)
                or psf_execution.get("mode") != "strict_bounded_streaming"
                or psf_execution.get("complete_precomputed_stack") is not False
                or type(psf_execution.get("peak_cached_kernels")) is not int
                or not 0 <= psf_execution["peak_cached_kernels"] <= 4
            ):
                raise ValueError("Joint capture did not execute bounded strict PSF streaming.")
        binding = cached.metadata.get("sensor_simulation_db") or {}
        if (
            binding.get("run_id") != selected["run_id"]
            or binding.get("report_code") != selected["report_code"]
            or binding.get("result_sha256") != selected["result_sha256"]
            or cached.fields["sensor_readout"] != model["readout"]
            or optical_image.fields["optics"].get("raytrace", {}).get("asset_sha256")
            != lens["identity"]["asset_sha256"]
        ):
            raise ValueError("Executed camera lost its pinned lens/sensor/readout binding.")
        mean_first = np.asarray(cached.data["mean_electrons"], dtype=float)
        if (
            mean_first.shape != size or not np.isfinite(mean_first).all()
            or np.any(mean_first < 0)
        ):
            raise ValueError("Saved sensor returned invalid mean photoelectrons.")
    prepared = {
        "sensor": cached,
        "mean_electrons": mean_first.copy(),
        "optical_image": optical_image,
        "scene": scene,
        "lens_identity": lens["identity"],
        "sensor_revision": selected,
        "saved_readout": model["readout"],
        "saved_metadata": metadata,
        "saved_fixed_pattern": saved_fpn,
        "sensor_source": sensor_source,
        "lens_source_db_sha256": source_sha,
        "upstream_assets": assets_identity,
        "scene_geometry": scene_geometry,
        "scene_photons_sha256": scene_hash,
        "horizontal_fov_deg": horizontal_fov_deg,
        "object_distance_m": object_distance_m,
        "luminance_cd_m2": luminance,
        "sensor_size": size,
        "assets": assets,
        "db_root": db_root,
    }
    if psf_compute_mode == "bounded_streaming":
        prepared.update(
            resource_preflight=resource_preflight,
            psf_compute_mode=psf_compute_mode,
            psf_execution=psf_execution,
        )
    return prepared


def evaluate_lens_sensor_hdr_bracket(
    lens_revision_id: str,
    sensor_identifier: str,
    *,
    sensor_run_id: str,
    root: str | Path,
    workspace: str | Path,
    exposure_s: Sequence[float] = (0.0005, 0.008),
    scene_kind: str = "slanted_edge",
    sensor_size: Sequence[int] = (16, 16),
    luminance_cd_m2: float = 1000.0,
    seed: int = 7,
    accept_approximate_lens_radiometry: bool = False,
    psf_compute_mode: str = "precomputed",
) -> dict[str, Any]:
    """Capture two original int32 RAW frames and fuse observable codes only.

    ``arrays`` contains both exact RAW frames and prefixed HDR diagnostic
    arrays. ``report`` is JSON-serializable and includes source/effect,
    readout, FPN, seed and timing limits. Fusion never consumes the separately
    retained model photoelectron-rate diagnostic.
    """
    exposures = _exposures(exposure_s)
    if len(exposures) != 2:
        raise ValueError("Joint lens × sensor HDR currently requires exactly two exposures.")
    prepared = _prepare_lens_sensor_capture(
        lens_revision_id,
        sensor_identifier,
        sensor_run_id=sensor_run_id,
        root=root,
        workspace=workspace,
        exposure_s=exposures[0],
        scene_kind=scene_kind,
        sensor_size=sensor_size,
        luminance_cd_m2=luminance_cd_m2,
        seed=seed,
        accept_approximate_lens_radiometry=accept_approximate_lens_radiometry,
        require_native_rggb=True,
        psf_compute_mode=psf_compute_mode,
    )
    cached = prepared["sensor"]
    mean_first = prepared["mean_electrons"]
    optical_image = prepared["optical_image"]
    assets = prepared["assets"]
    size = prepared["sensor_size"]
    model = prepared["saved_metadata"]["model_snapshot"]
    selected = prepared["sensor_revision"]
    sensor_source = prepared["sensor_source"]
    lens = {"identity": prepared["lens_identity"]}
    source_sha = prepared["lens_source_db_sha256"]
    assets_identity = prepared["upstream_assets"]
    scene_geometry = prepared["scene_geometry"]
    scene_hash = prepared["scene_photons_sha256"]
    horizontal_fov_deg = prepared["horizontal_fov_deg"]
    object_distance_m = prepared["object_distance_m"]
    luminance = prepared["luminance_cd_m2"]
    saved_fpn = prepared["saved_fixed_pattern"]
    saved_native_cfa = prepared["saved_metadata"]["native_cfa"]
    db_root = prepared["db_root"]
    with assets.bound_default(assets):
        photoelectron_rate = mean_first / exposures[0]
        optical = cached.clone()
        optical.fields.pop("sensor_output_mode", None)
        optical.fields.pop("sensor_readout", None)
        optical.metadata.pop("sensor_capture", None)
        optical.fields["integration_time"] = exposures[1]
        long_mean_sensor = sensor_compute_noise_free(optical, optical_image, seed=seed)
        conversion_gain = float(
            long_mean_sensor.fields["pixel"]["conversion_gain_v_per_electron"]
        )
        long_mean = np.asarray(long_mean_sensor.data["volts"], dtype=float) / conversion_gain
        if long_mean.shape != size or not np.isfinite(long_mean).all():
            raise ValueError("Long-exposure saved sensor mean is not finite or aligned.")
        expected_long = photoelectron_rate * exposures[1]
        linearity_error = float(
            np.max(np.abs(long_mean - expected_long))
            / max(1.0, float(np.max(np.abs(expected_long))))
        )
        if not math.isfinite(linearity_error) or linearity_error > 1e-8:
            raise ValueError("Saved sensor photoelectron response is not exposure-linear.")

        raw_frames: list[np.ndarray] = []
        captures: list[dict[str, Any]] = []
        calibrations: list[dict[str, Any]] = []
        original_fpn: dict[str, Any] | None = None
        for index, exposure in enumerate(exposures):
            selected_sensor = cached.clone()
            selected_sensor.fields["integration_time"] = exposure
            selected_sensor.data["mean_electrons"] = (
                photoelectron_rate * exposure
            ).copy()
            selected_sensor, before_fpn = _disable_fixed_pattern(selected_sensor)
            if original_fpn is None:
                original_fpn = before_fpn
            elif before_fpn != original_fpn:
                raise ValueError("Saved FPN configuration changed across bracket frames.")
            frame_seed = int(np.random.SeedSequence([seed, index]).generate_state(1)[0])
            captured = resample_sensor_capture(selected_sensor, seed=frame_seed)
            capture = captured.metadata["sensor_capture"]
            frame = np.asarray(captured.data["raw_dn"])
            if (
                frame.dtype != np.dtype("int32") or frame.shape != size
                or capture["noise_flag"] != 2
                or capture["exposure_time_s"] != exposure
                or capture["readout"] != model["readout"]
            ):
                raise ValueError("Bracket frame lost native RAW/readout/exposure binding.")
            raw_frames.append(frame.copy())
            calibrations.append(_calibration(capture, size))
            captures.append({
                "index": index,
                "exposure_s": exposure,
                "noise_seed": frame_seed,
                "original_raw_sha256": _array_sha(frame),
                "sensor_capture": capture,
            })
    assert original_fpn is not None
    raw_bracket = np.stack(raw_frames)
    # The 8..32 image is a newly configured simulation sensor, with the
    # saved RGGB tile anchored at virtual origin zero. This is *not* a known
    # crop coordinate within the saved model's native_size array.
    geometry = {
        "kind": "native_sensor",
        "full_sensor_shape": list(size),
        "crop_origin": [0, 0],
    }
    merged = hdr.merge_raw_bracket(
        raw_bracket, exposures, calibrations,
        geometry=geometry, profile=hdr.default_profile(),
    )
    if merged["summary"]["input_array_bytes_sha256"] != _array_sha(raw_bracket):
        raise ValueError("HDR merger did not consume the original RAW bracket.")
    if (
        _identity(inspect_sensor_revision(
            sensor_identifier, root=db_root, run_id=sensor_run_id
        )) != _identity(sensor_source)
        or geometric_lens_preview(lens_revision_id, catalog_root=db_root)[
            "identity"
        ] != lens["identity"]
        or offline_asset_identity(workspace=workspace, source=db_root) != assets_identity
    ):
        raise ValueError("Pinned lens, sensor or upstream assets changed during HDR capture.")

    arrays = {
        "original_raw_dn": raw_bracket,
        "model_photoelectron_rate": photoelectron_rate.copy(),
        **{f"hdr_{key}": value for key, value in merged["arrays"].items()},
    }
    report = {
        "schema": SCHEMA,
        "status": "completed" if merged["summary"]["valid_pixels"] else "inconclusive",
        "scope": "static_geometric_lens_saved_sensor_rggb_hdr_proxy",
        "lens_identity": lens["identity"],
        "sensor_revision": selected,
        "upstream_assets": assets_identity,
        "geometry_provenance": {
            "merger_configured_sensor_geometry": geometry,
            "merger_geometry_scope": "virtual_configured_sensor_only",
            "configured_sensor_size": list(size),
            "saved_native_array_size": saved_native_cfa["native_size"],
            "saved_native_array_crop_origin": None,
            "saved_native_array_crop_mapping": "not_established",
            "cfa_phase_basis": "saved_rggb_tile_anchored_at_virtual_origin_zero",
        },
        "source_boundary": {
            "lens_source_db_sha256": source_sha,
            "geometric_psf": True,
            "lens_absolute_radiometry": "unknown_unity_transmission_assumption",
            "lens_relative_illumination": "unknown_unity_assumption",
            "lens_cra_to_sensor": "not_connected",
            "sensor_saved_snapshot_used": True,
            "sensor_model_kind": "db_estimate_v1",
            "native_cfa": "2x2_RGGB_only",
            "sensor_readout_classification": sensor_source["readout"]["classification"],
            "physical_validation": "not_established",
        },
        "scene": {
            "kind": f"{scene_kind}_d65",
            "luminance_cd_m2": luminance,
            "wavelength_nm": list(WAVELENGTHS_NM),
            "horizontal_fov_deg": horizontal_fov_deg,
            "object_distance_m": (
                "infinity" if math.isinf(object_distance_m) else object_distance_m
            ),
            "geometry": scene_geometry,
            "photons_sha256": scene_hash,
        },
        "controls": {
            "exposure_s": exposures,
            "sensor_size": list(size),
            "seed": seed,
            "timing": "static_scene_sequential_independent_reset_no_timestamps",
            "first_exposure_oi_reused": True,
            "photoelectron_rate_linearity_relative_error": linearity_error,
            "fixed_pattern_mode": "zero_fpn_override",
            "joint_lfm_readout_comparability": (
                "different_fpn_conditions_lfm_retains_saved_fpn"
            ),
            "saved_fixed_pattern": {
                key: float(saved_fpn[key]) for key in _FPN_FIELDS
            },
            "executed_fixed_pattern": {key: 0.0 for key in _FPN_FIELDS},
            "saved_sensor_fpn_before_override": original_fpn,
            "readout": model["readout"],
        },
        "captures": captures,
        "fusion": merged["summary"],
        "arrays": {
            "original_raw_dn": {
                "shape": list(raw_bracket.shape),
                "dtype": "int32",
                "unit": "DN",
                "sha256_c_order_bytes": _array_sha(raw_bracket),
            },
            "model_photoelectron_rate": {
                "shape": list(photoelectron_rate.shape),
                "unit": "electron/pixel/s",
                "purpose": "linearity_diagnostic_not_fusion_input",
            },
        },
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
            "motion_or_deghosting_evaluated": False,
            "lfm_evaluated": False,
        },
        "limitation": LIMITATION,
    }
    if psf_compute_mode == "bounded_streaming":
        report.update(
            schema=BOUNDED_SCHEMA,
            resource_preflight=prepared["resource_preflight"],
            psf_execution=prepared["psf_execution"],
            psf_compute_mode=psf_compute_mode,
            limitation=BOUNDED_LIMITATION,
        )
    return {"report": report, "arrays": arrays}
