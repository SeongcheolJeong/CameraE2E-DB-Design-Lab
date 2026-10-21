"""Analytic PWM row integration using one pinned Lens DB x Sensor DB response.

The scene spectrum and lens are static; only a scalar ideal illumination
waveform changes in time. The saved sensor's digital readout draws integer RAW
after row-wise photoelectron integration. Timing is authored, not a measured
sensor specification or validation of a product flicker-mitigation mode.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.sensor import sensor_compute
from pyisetcam.sensor_capture import resample_sensor_capture

from .lens_catalog_bridge import geometric_lens_preview
from .lens_sensor_raw_bridge import _number
from .lens_sensor_timing_mode import mode_binding, validate_timing_mode
from .lens_sensor_timing_mode import timing_provenance as mode_provenance
from .offline_assets import offline_asset_identity
from .registry import digest
from .saved_sensor_lfm import _on_fraction, _phase_modulation
from .sensor_db_bridge import inspect_sensor_revision

SCHEMA = "camerae2e_lens_sensor_pwm_lfm_v1"
BOUNDED_SCHEMA = "camerae2e_lens_sensor_pwm_lfm_v2"
TIMED_BOUNDED_SCHEMA = "camerae2e_lens_sensor_pwm_lfm_v3"
PHASE_FRACTIONS = (0.0, 0.25, 0.5, 0.75)
LINE_TIME_S = 20e-6
MAX_PWM_CYCLES_PER_EXPOSURE = 32
MAX_LINEARITY_ERROR = 1e-6


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate_lens_sensor_pwm_lfm(
    lens_revision_id: str,
    sensor_identifier: str,
    *,
    sensor_run_id: str,
    root: str | Path,
    workspace: str | Path,
    output_dir: str | Path,
    exposure_s: tuple[float, float] | list[float] = (0.001, 0.004),
    pwm_period_s: float = 0.002,
    duty_cycle: float = 0.5,
    off_to_on_ratio: float = 0.02,
    on_luminance_cd_m2: float = 1000.0,
    sensor_size: tuple[int, int] | list[int] = (8, 8),
    seed: int = 7,
    accept_approximate_lens_radiometry: bool = False,
    psf_compute_mode: str = "precomputed",
    timing_mode: dict[str, Any] | None = None,
    timing_mode_sha256: str | None = None,
) -> dict[str, Any]:
    """Compute paired four-phase RAW at two exposures for one joint camera.

    The configured native spectral sensor response is linear in exposure and
    scalar illumination. We verify the exposure scaling with an independent
    same-optical-image computation before integrating the waveform.
    """
    from .lens_sensor_hdr_bridge import _prepare_lens_sensor_capture

    if accept_approximate_lens_radiometry is not True:
        raise ValueError("Explicit approximate lens-radiometry acceptance is required.")
    if not isinstance(exposure_s, (list, tuple)) or len(exposure_s) != 2:
        raise ValueError("Declare exactly two ascending exposure_s values.")
    exposures = [_number(value, "exposure_s", 0.0005, 0.02) for value in exposure_s]
    if not exposures[0] < exposures[1]:
        raise ValueError("LFM exposure_s values must be distinct and ascending.")
    period = _number(pwm_period_s, "pwm_period_s", 0.0005, 0.02)
    duty = _number(duty_cycle, "duty_cycle", 0.05, 0.95)
    low = _number(off_to_on_ratio, "off_to_on_ratio", 0.0, 0.5)
    luminance = _number(on_luminance_cd_m2, "on_luminance_cd_m2", 0.01, 10000.0)
    if exposures[-1] / period > MAX_PWM_CYCLES_PER_EXPOSURE:
        raise ValueError("PWM exposure exceeds the bounded number of cycles.")
    if (
        not isinstance(sensor_size, (list, tuple))
        or len(sensor_size) != 2
        or any(type(value) is not int or not 8 <= value <= 32 or value % 2
               for value in sensor_size)
    ):
        raise ValueError("sensor_size requires two even dimensions from 8 to 32.")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    size = tuple(sensor_size)
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Lens x Sensor LFM output_dir is not empty: {destination}")
    db_root = Path(root).expanduser().resolve()
    asset_identity = offline_asset_identity(workspace=workspace, source=db_root)
    source = inspect_sensor_revision(sensor_identifier, root=db_root, run_id=sensor_run_id)
    if not source["executable"] or source["native_response"]["kind"] != "db_estimate_v1":
        raise ValueError("Joint LFM requires an executable saved db_estimate_v1 sensor.")
    lens_identity = geometric_lens_preview(
        lens_revision_id, catalog_root=db_root, wavelength_nm=550.0
    )["identity"]
    prepared = _prepare_lens_sensor_capture(
        lens_revision_id, sensor_identifier, sensor_run_id=sensor_run_id,
        root=db_root, workspace=workspace, exposure_s=exposures[-1],
        luminance_cd_m2=luminance, scene_kind="uniform", sensor_size=size,
        accept_approximate_lens_radiometry=True,
        psf_compute_mode=psf_compute_mode,
    )
    if (
        prepared["lens_identity"] != lens_identity
        or prepared["sensor_revision"] != source["revision"]
        or prepared["saved_readout"] != prepared["sensor"].fields["sensor_readout"]
    ):
        raise ValueError("Joint LFM optical or saved readout binding changed.")
    if timing_mode is None:
        if timing_mode_sha256 is not None:
            raise ValueError("Timing-mode SHA requires the selected mode document.")
        line_time_s = LINE_TIME_S
        timing_provenance = "authored 20 us/line, not saved sensor timing"
    else:
        if psf_compute_mode != "bounded_streaming":
            raise ValueError("Imported timing mode requires the bounded PSF study schema.")
        expected_mode_binding = mode_binding(
            source["revision"], prepared["saved_readout"],
            {
                "profile": source["readout"]["profile"],
                "classification": source["readout"]["classification"],
            },
            list(size), exposures,
        )
        timing_mode = validate_timing_mode(
            timing_mode, expected_binding=expected_mode_binding
        )
        if (
            not isinstance(timing_mode_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", timing_mode_sha256) is None
        ):
            raise ValueError("Selected timing-mode artifact needs a SHA-256 binding.")
        line_time_s = float(timing_mode["timing"]["line_time_s"])
        timing_provenance = mode_provenance(timing_mode)
    base = np.asarray(prepared["mean_electrons"], dtype=np.float64)
    if base.shape != size or not np.isfinite(base).all() or np.any(base < 0):
        raise ValueError("Joint Lens x Sensor photoelectron array is invalid.")
    original_sensor = prepared["sensor"]
    if original_sensor.fields.get("sensor_output_mode") != "digital_raw":
        raise ValueError("Joint LFM requires native linear digital_raw output.")
    fpn_map_fields = (
        "offset_fpn_image", "gain_fpn_image",
        "column_offset_fpn", "column_gain_fpn",
    )
    if any(name in original_sensor.fields for name in fpn_map_fields):
        raise ValueError("Joint LFM expects model-generated virtual-window FPN maps.")
    saved_native_cfa = prepared["saved_metadata"]["native_cfa"]
    if (
        np.asarray(original_sensor.fields.get("pattern")).tolist()
        != saved_native_cfa["pattern"]
        or original_sensor.fields.get("filter_names") != saved_native_cfa["filter_names"]
    ):
        raise ValueError("Joint LFM runtime CFA differs from the pinned saved CFA tile.")
    # A second sensor integration uses the already computed optical image;
    # this checks exposure linearity without a second ray-trace optical solve.
    check_sensor = original_sensor.clone()
    check_sensor.fields["integration_time"] = exposures[0]
    check_sensor.fields["noise_flag"] = 0
    assets = prepared["assets"]
    with assets.bound_default(assets):
        checked = sensor_compute(check_sensor, prepared["optical_image"], seed=seed)
    independent = np.asarray(checked.data["mean_electrons"], dtype=np.float64)
    predicted = base * (exposures[0] / exposures[-1])
    linearity = float(
        np.linalg.norm(independent - predicted) / max(np.linalg.norm(independent), 1e-12)
    )
    if not math.isfinite(linearity) or linearity > MAX_LINEARITY_ERROR:
        raise ValueError("Joint lens/sensor response is not linear across LFM exposures.")
    original_readout = prepared["saved_readout"]
    frames, constants, integrated_arrays, cases = [], [], [], []
    mean_scale = duty + (1.0 - duty) * low
    for exp_index, exposure in enumerate(exposures):
        high_mean = base * (exposure / exposures[-1])
        low_mean = high_mean * low
        frame_seed = int(np.random.SeedSequence([seed, exp_index]).generate_state(1)[0])
        template = original_sensor.clone()
        template.fields["integration_time"] = exposure
        template.fields["noise_flag"] = 2
        template.data["mean_electrons"] = (high_mean * mean_scale).copy()
        baseline = resample_sensor_capture(template, seed=frame_seed)
        if baseline.fields["sensor_readout"] != original_readout:
            raise ValueError("Joint LFM baseline changed saved readout.")
        constant_raw = np.asarray(baseline.data["raw_dn"])
        if constant_raw.dtype != np.dtype("int32") or constant_raw.shape != size:
            raise ValueError("Joint LFM baseline did not produce native int32 RAW.")
        constants.append(constant_raw.copy())
        phase_raw, phase_mean, phases = [], [], []
        starts = np.arange(size[0], dtype=np.float64) * line_time_s
        for fraction in PHASE_FRACTIONS:
            phase_s = fraction * period
            on_fraction = _on_fraction(starts, exposure, period, duty, phase_s)
            integrated = low_mean + (high_mean - low_mean) * on_fraction[:, None]
            selected = baseline.clone()
            selected.data["mean_electrons"] = integrated.copy()
            captured = resample_sensor_capture(selected, seed=frame_seed)
            raw = np.asarray(captured.data["raw_dn"])
            if (
                raw.dtype != np.dtype("int32") or raw.shape != size
                or not np.array_equal(captured.data["mean_electrons"], integrated)
                or captured.fields["sensor_readout"] != original_readout
            ):
                raise ValueError("Joint LFM phase lost the native saved capture contract.")
            phase_raw.append(raw.copy())
            phase_mean.append(integrated.copy())
            phases.append({
                "phase_fraction_of_period": fraction,
                "phase_s": phase_s,
                "noise_seed": frame_seed,
                "raw_sha256": hashlib.sha256(raw.tobytes(order="C")).hexdigest(),
                "mean_raw_dn": float(np.mean(raw)),
                "row_mean_dn_range": float(np.ptp(np.mean(raw, axis=1))),
                "row_timing": {
                    "schema": (
                        "camerae2e_analytic_pwm_row_timing_v2"
                        if timing_mode is not None else "camerae2e_analytic_pwm_row_timing_v1"
                    ),
                    "frame_start_s": 0.0,
                    "rolling_shutter": True,
                    "line_time_s": line_time_s,
                    **(
                        {"timing_mode_sha256": timing_mode_sha256}
                        if timing_mode is not None else {}
                    ),
                    "row_exposure_start_s": starts.tolist(),
                    "row_exposure_end_s": (starts + exposure).tolist(),
                    "ideal_pwm_on_fraction_per_row": on_fraction.tolist(),
                    "method": "exact_piecewise_constant_row_overlap",
                },
                "sensor_capture": captured.metadata["sensor_capture"],
            })
        frames.append(np.stack(phase_raw))
        integrated_arrays.append(np.stack(phase_mean))
        modulation = _phase_modulation(
            phases, baseline.metadata["sensor_capture"],
            float(original_readout["black_level_dn"]),
        )
        cases.append({
            "exposure_s": exposure,
            "integration_method": "analytic_row_overlap_with_static_joint_optical_response",
            "constant_duty_equivalent_scale": mean_scale,
            "constant_raw_sha256": hashlib.sha256(constant_raw.tobytes()).hexdigest(),
            "constant_mean_raw_dn": float(np.mean(constant_raw)),
            **modulation,
            "maximum_row_banding_dn_range": max(item["row_mean_dn_range"] for item in phases),
            "phases": phases,
        })
    if (
        geometric_lens_preview(lens_revision_id, catalog_root=db_root, wavelength_nm=550.0)[
            "identity"
        ] != lens_identity
        or inspect_sensor_revision(sensor_identifier, root=db_root, run_id=sensor_run_id)[
            "revision"
        ] != source["revision"]
        or offline_asset_identity(workspace=workspace, source=db_root) != asset_identity
    ):
        raise ValueError("Joint LFM pinned lens, sensor or assets changed during calculation.")
    destination.mkdir(parents=True, exist_ok=True)
    npz_path = destination / "lens-sensor-lfm.npz"
    np.savez_compressed(
        npz_path,
        original_pwm_raw_dn=np.stack(frames),
        original_constant_raw_dn=np.stack(constants),
        integrated_mean_electrons=np.stack(integrated_arrays),
    )
    report = {
        "schema": SCHEMA,
        "status": "completed",
        "lens_identity": lens_identity,
        "sensor_revision": source["revision"],
        "upstream_assets": asset_identity,
        "geometry_provenance": {
            "configured_sensor_size": list(size),
            "configured_sensor_scope": "virtual_simulation_window_only",
            "saved_native_array_size": saved_native_cfa["native_size"],
            "saved_native_array_crop_origin": None,
            "saved_native_array_crop_mapping": "not_established",
            "cfa_phase_basis": "saved_native_cfa_tile_anchored_at_virtual_origin_zero",
            "saved_native_cfa": saved_native_cfa,
        },
        "source_boundary": {
            "lens_geometric_psf_used": True,
            "saved_sensor_simulation_run_used": True,
            "saved_fpn_parameters_applied_to_generated_virtual_window_map": True,
            "saved_fpn_parameter_provenance": "saved_simulation_profile_assumptions",
            "sensor_readout_profile": source["readout"]["profile"],
            "sensor_readout_classification": source["readout"]["classification"],
            "measured_fpn_map_reused": False,
            "fpn_physical_validation": "not_established",
            "lens_absolute_transmission": "unknown_unity_assumption",
            "lens_relative_illumination": "unknown_unity_assumption",
            "sensor_angular_response": "not_applied",
            "physical_validation": "not_established",
        },
        "controls": {
            "exposure_s": exposures,
            "pwm_period_s": period,
            "duty_cycle": duty,
            "off_to_on_ratio": low,
            "on_luminance_cd_m2": luminance,
            "phase_fractions_of_period": list(PHASE_FRACTIONS),
            "sensor_size": list(size),
            "line_time_s": line_time_s,
            "timing_provenance": timing_provenance,
            **(
                {
                    "timing_mode": timing_mode,
                    "timing_mode_sha256": timing_mode_sha256,
                    "timing_mode_canonical_sha256": digest(timing_mode),
                }
                if timing_mode is not None else {}
            ),
            "seed": seed,
            "saved_readout": original_readout,
            "saved_fpn_parameter_assumptions": prepared["saved_fixed_pattern"],
            "fixed_pattern_seed": original_sensor.fields.get("fixed_pattern_seed", 0),
            "noise_seed_policy": (
                "same seed for four phases and the constant-light reference "
                "within each exposure; paired sensitivity, not independent repeats"
            ),
        },
        "linearity_check": {
            "method": "independent_sensor_compute_on_reused_joint_optical_image",
            "relative_l2_error": linearity,
            "maximum_accepted_relative_l2_error": MAX_LINEARITY_ERROR,
            "status": "passed",
        },
        "scene_contract": {
            "scene_kind": "uniform_d65",
            "illumination": "scalar ideal PWM of one static Lens x Sensor optical response",
            "spatial_motion": False,
            "timing": "analytic rectangular-PWM row integration",
            "no_second_raytrace_solve": True,
        },
        "cases": cases,
        "comparison": {
            "metric": "phase_mean_black_relative_modulation_fraction",
            "eligible_cases": [
                item["exposure_s"] for item in cases
                if item["phase_modulation_status"] == "eligible"
            ],
            "interpretation": (
                "Model-conditioned phase sensitivity under declared PWM and rolling timing; "
                "not measured LFM mitigation or product ranking."
            ),
        },
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
        "limitations": [
            "Static D65 scene and scalar illumination; no spatial motion or spectral PWM shift.",
            "Lens transmission, relative illumination, diffraction and CRA remain unvalidated.",
            "The configured sensor window is not a located crop of the saved native array.",
            "FPN spatial maps are generated from saved profile assumptions and a fixed seed, "
            "not reused measured sensor maps; physical FPN accuracy is unvalidated.",
            (
                "Row timing is authored, not the saved sensor's measured timing."
                if timing_mode is None else
                "Row timing is a source-bound declaration; its numeric value and native "
                "row origin are not independently verified."
            ),
            "No sensor HDR mode, flicker-mitigation control loop or physical validation.",
        ],
    }
    if psf_compute_mode == "bounded_streaming":
        report.update(
            schema=BOUNDED_SCHEMA,
            resource_preflight=prepared["resource_preflight"],
            psf_execution=prepared["psf_execution"],
            psf_compute_mode=psf_compute_mode,
        )
        report["limitations"].append(
            "Strict geometric ray-trace PSF is cropped and streamed on demand; "
            "the complete precomputed field/angle/wavelength stack is not built. "
            "Resource preflight is a development heuristic, not a peak-memory bound."
        )
    if timing_mode is not None:
        report["schema"] = TIMED_BOUNDED_SCHEMA
        report["source_boundary"]["timing_mode_source_kind"] = (
            timing_mode["provenance"]["source_kind"]
        )
        report["source_boundary"]["timing_mode_source_document_sha256"] = (
            timing_mode["provenance"]["source_document_sha256"]
        )
        report["source_boundary"]["timing_value_verification"] = (
            "declared_not_independently_verified"
        )
    report["artifacts"] = {"npz_name": npz_path.name, "npz_sha256": _sha(npz_path)}
    json_path = destination / "lens-sensor-lfm.json"
    json_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    report["artifact_sha256"] = {"json": _sha(json_path), "npz": _sha(npz_path)}
    report["artifacts"] = {"json": str(json_path), "npz": str(npz_path)}
    return report
