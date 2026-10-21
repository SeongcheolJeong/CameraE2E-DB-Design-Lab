"""Bounded temporal quadrature from actual instantaneous 3D scene renders.

Each sample is a registered Mitsuba render of the same immutable scene at one
exact simulation-clock time. The existing temporal camera then integrates the
piecewise-linear *irradiance* between these samples over each native row window.
That approximation can be refined; finite samples and Monte Carlo noise do not
provide an error bound for occlusion between samples.
"""

from __future__ import annotations

import copy

import numpy as np

from . import imaging, periodic_light, scene_motion, scene_render, sequences
from .registry import canonical, digest
from .timing import MAX_TIME_NS

MAX_RENDER_SAMPLES = 65


def plan_times(start_ns: int, end_ns: int, max_gap_ns: int, *,
               required_times_ns: tuple[int, ...] = ()) -> list[int]:
    """Include exposure-critical times and fill each gap with exact integer ns.

    ``max_gap_ns`` is a requested numerical resolution, not a claim that this
    resolution resolves every occlusion event. Callers must compare refinements.
    """
    for label, value in (("start_ns", start_ns), ("end_ns", end_ns),
                         ("max_gap_ns", max_gap_ns)):
        if type(value) is not int:
            raise ValueError(f"{label} must be an exact integer nanosecond value")
    if (not -MAX_TIME_NS <= start_ns < end_ns <= MAX_TIME_NS
            or end_ns - start_ns > MAX_TIME_NS
            or not 1 <= max_gap_ns <= MAX_TIME_NS):
        raise ValueError("Temporal render span and maximum gap are invalid")
    required = []
    for value in required_times_ns:
        if type(value) is not int or not start_ns <= value <= end_ns:
            raise ValueError("Required render times must be exact ns within the span")
        required.append(value)
    anchors = sorted({start_ns, end_ns, *required})
    count = 1 + sum((right - left + max_gap_ns - 1) // max_gap_ns
                    for left, right in zip(anchors, anchors[1:], strict=False))
    if count > MAX_RENDER_SAMPLES:
        raise ValueError(f"Temporal rendering exceeds {MAX_RENDER_SAMPLES} samples")
    times = [anchors[0]]
    for left, right in zip(anchors, anchors[1:], strict=False):
        steps = (right - left + max_gap_ns - 1) // max_gap_ns
        times.extend(left + ((right - left) * index) // steps
                     for index in range(1, steps + 1))
    assert all(0 < b - a <= max_gap_ns for a, b in zip(times, times[1:], strict=False))
    return times


def submit_renders(service, base_request: dict, times_ns: list[int]) -> list[dict]:
    """Queue bounded real 3D samples, preserving the original scene and clock."""
    times = _times(times_ns)
    if any(key in base_request for key in ("render_time_ns", "force")):
        raise ValueError("Base render request must leave render_time_ns and force unset")
    clock = base_request.get("render_clock")
    if not isinstance(clock, dict):
        raise ValueError("Temporal renders require an explicit relative scene clock")
    scene = service.registry.get("scene_assets", base_request["scene_asset_id"])
    if scene["payload"].get("kind") != "scene":
        raise ValueError("Temporal render source must be a registered 3D Scene")
    for time_ns in times:
        scene_motion.resolve({**base_request, "render_time_ns": time_ns}, scene)
    return [scene_render.submit(service, {**base_request, "render_time_ns": time_ns})
            for time_ns in times]


def publish_sequence(service, render_job_ids: list[str], *, name: str,
                     revision: str = "1") -> dict:
    """Publish one timed sequence only from homogeneous completed 3D renders."""
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValueError("Temporal scene needs a nonempty name of at most 200 characters")
    if not isinstance(revision, str) or not revision or len(revision) > 80:
        raise ValueError("Temporal scene revision must be nonempty and at most 80 characters")
    if not isinstance(render_job_ids, list) or not 2 <= len(render_job_ids) <= MAX_RENDER_SAMPLES:
        raise ValueError("Temporal scene needs 2..65 render job IDs")
    if any(not isinstance(identity, str) or not identity for identity in render_job_ids):
        raise ValueError("Temporal render IDs must be nonempty strings")
    jobs = [scene_render.get_job(service, identity) for identity in render_job_ids]
    if any(job["status"] != "succeeded" for job in jobs):
        raise ValueError("Every temporal 3D render must complete before publication")
    times = _times([job["request"].get("render_time_ns") for job in jobs])
    shape = jobs[0]["request"]["shape"]
    wavelength_count = len(jobs[0]["request"]["wavelength_nm"])
    if int(np.prod(shape)) * wavelength_count * len(jobs) > 64_000_000:
        raise ValueError("Temporal render series exceeds 64 million wavelength-pixel samples")
    base = _base_request(jobs[0]["request"])
    backend = jobs[0]["backend"]
    environment = jobs[0]["environment"]
    for job in jobs:
        request = job["request"]
        scene_render._verify_motion_request(service, job)
        if (_base_request(request) != base or job["backend"] != backend
                or job["environment"] != environment):
            raise ValueError("Temporal renders must share one frozen 3D scene and camera setup")
        if request["scene"]["payload"].get("clock") != request["render_clock"]:
            raise ValueError("Temporal samples require the scene's frozen clock")
    images = [scene_render.publish(service, {
        "job_id": job["id"],
        "name": f"3D temporal render {job['id']} · {time_ns} ns",
        "revision": "1",
    }) for job, time_ns in zip(jobs, times, strict=True)]
    scene = jobs[0]["request"]["scene"]
    return sequences.create_sequence(service, "scene_sequences", {
        "source_kind": "simulated",
        "source_uri": "scene-render-series://" + digest(render_job_ids),
        "license": scene["license"],
        "method_and_version": "registered_3d_temporal_quadrature_v1",
        "physical_instance": None,
        "parents": [],
        "name": name,
        "revision": revision,
        "fixture_specification": {
            "schema": "registered_3d_temporal_quadrature_v1",
            "render_job_ids": render_job_ids,
            "render_times_ns": times,
            "maximum_sample_gap_ns": max(b - a for a, b in zip(times, times[1:], strict=False)),
            "integrand": "rendered_3d_spectral_irradiance_before_sensor_optics",
            "integration": "piecewise_linear_irradiance_over_native_row_exposure",
            "inter_sample_occlusion_error": "unknown; compare a refined registered series",
            "physical_validation": "not_established",
        },
        "payload": {
            "schema": "timed_spectral_scene_v1",
            "clock": copy.deepcopy(jobs[0]["request"]["render_clock"]),
            "interpolation": "linear_irradiance",
            "knots": [{"time_ns": time_ns, "spectral_image_id": image["id"]}
                      for time_ns, image in zip(times, images, strict=True)],
        },
    })


def compare_refinement(service, coarse_sequence_id: str,
                       fine_sequence_id: str, *, windows_ns=None) -> dict:
    """Measure a nested-grid exposure-integral change; never call it an error bound."""
    coarse = service.registry.get("scene_sequences", coarse_sequence_id)
    fine = service.registry.get("scene_sequences", fine_sequence_id)
    if any(not isinstance(record.get("fixture_specification"), dict)
           or record["fixture_specification"].get("schema")
           != "registered_3d_temporal_quadrature_v1" for record in (coarse, fine)):
        raise ValueError("Refinement requires two registered 3D temporal sequences")
    coarse_knots, fine_knots = coarse["payload"]["knots"], fine["payload"]["knots"]
    if coarse["payload"]["clock"] != fine["payload"]["clock"]:
        raise ValueError("Refinement series clocks differ")
    coarse_times = [item["time_ns"] for item in coarse_knots]
    fine_times = [item["time_ns"] for item in fine_knots]
    if coarse_times[0] != fine_times[0] or coarse_times[-1] != fine_times[-1]:
        raise ValueError("Refinement series exposure spans differ")
    if not set(coarse_times) <= set(fine_times):
        raise ValueError("Fine sequence must retain all coarse render times")
    if len(fine_knots) <= len(coarse_knots):
        raise ValueError("Fine sequence must add real intermediate 3D samples")
    coarse_records = _verified_series(service, coarse)
    fine_records = _verified_series(service, fine)
    fine_by_time = dict(zip(fine_times, fine_records, strict=True))
    for time_ns, record in zip(coarse_times, coarse_records, strict=True):
        original = record["render_job_snapshot"]
        retained = fine_by_time[time_ns]["render_job_snapshot"]
        if original != retained:
            raise ValueError("Fine sequence must retain exact coarse render jobs")
    if _base_request(coarse_records[0]["render_job_snapshot"]["request"]) != _base_request(
        fine_records[0]["render_job_snapshot"]["request"]
    ):
        raise ValueError("Refinement series must share one frozen 3D scene and camera setup")
    if windows_ns is None:
        windows_ns = [(coarse_times[0], coarse_times[-1])]
    if (not isinstance(windows_ns, (list, tuple)) or not 1 <= len(windows_ns) <= 1024):
        raise ValueError("Refinement needs 1..1024 explicit exposure windows")
    if int(np.prod(fine_records[0]["shape"])) * len(fine_times) * len(windows_ns) > 32_000_000:
        raise ValueError("Refinement exposure-window comparison exceeds its work limit")
    for window in windows_ns:
        if (not isinstance(window, (list, tuple)) or len(window) != 2
                or any(type(value) is not int for value in window)
                or not coarse_times[0] <= window[0] < window[1] <= coarse_times[-1]):
            raise ValueError("Refinement exposure window lies outside both scene timelines")
    coarse_values = _values(service, coarse_records)
    fine_values = _values(service, fine_records)
    coarse_integral = _window_integral(coarse_times, coarse_values,
                                       coarse_times[0], coarse_times[-1])
    fine_integral = _window_integral(fine_times, fine_values,
                                     fine_times[0], fine_times[-1])
    delta = np.abs(fine_integral - coarse_integral)
    reference = float(np.sum(np.abs(fine_integral)))
    maximum_window_l1 = 0.0
    maximum_window_pixel = 0.0
    for window in windows_ns:
        coarse_window = _window_integral(coarse_times, coarse_values, *window)
        fine_window = _window_integral(fine_times, fine_values, *window)
        window_delta = np.abs(fine_window - coarse_window)
        maximum_window_l1 = max(maximum_window_l1, float(np.sum(window_delta)))
        maximum_window_pixel = max(maximum_window_pixel, float(np.max(window_delta)))
    return {
        "schema": "registered_3d_refinement_diagnostic_v1",
        "coarse_sequence_id": coarse_sequence_id,
        "fine_sequence_id": fine_sequence_id,
        "coarse_sample_count": len(coarse_times),
        "fine_sample_count": len(fine_times),
        "integral_unit": "J/m^2/nm",
        "absolute_l1_change": float(np.sum(delta)),
        "relative_l1_change": float(np.sum(delta) / reference) if reference else None,
        "maximum_absolute_change": float(np.max(delta)),
        "exposure_window_count": len(windows_ns),
        "maximum_window_l1_change": maximum_window_l1,
        "maximum_window_pixel_change": maximum_window_pixel,
        "interpretation": "nested-grid change only; not an occlusion-error bound",
        "physical_validation": "not_established",
    }


def _pwm_window_integral(times, values, row_local, start_ns, end_ns, waveform):
    """Integrate the selected 3D Light's linear knot basis under authored PWM."""
    integral = np.zeros_like(values[0][row_local], dtype=np.float64)
    for index, (left, right) in enumerate(zip(times, times[1:], strict=False)):
        weight_left, weight_right = periodic_light.integrate_linear_basis(
            waveform, start_ns, end_ns, left, right,
        )
        if weight_left or weight_right:
            integral += (values[index][row_local] * weight_left
                         + values[index + 1][row_local] * weight_right)
    return integral


def _change_metrics(coarse, fine):
    delta = np.abs(fine - coarse)
    reference = float(np.sum(np.abs(fine)))
    return {
        "coarse_sample_sum": float(np.sum(coarse)),
        "fine_sample_sum": float(np.sum(fine)),
        "absolute_l1_change": float(np.sum(delta)),
        "relative_l1_change": float(np.sum(delta) / reference) if reference else None,
        "maximum_absolute_change": float(np.max(delta)),
    }


def compare_dual_light_refinement(
    service,
    coarse_modulated_sequence_id: str,
    coarse_ambient_sequence_id: str,
    fine_modulated_sequence_id: str,
    fine_ambient_sequence_id: str,
    *,
    waveform: dict,
    row_windows_ns: list[dict],
) -> dict:
    """Observed row-integral change from nested *real 3D* two-Light samples.

    This reuses the registered single-component refinement preflight, then
    integrates one Light with its authored PWM and the other with unit level.
    No continuous-occlusion or LED-waveform accuracy bound follows from it.
    """
    identities = (
        coarse_modulated_sequence_id, coarse_ambient_sequence_id,
        fine_modulated_sequence_id, fine_ambient_sequence_id,
    )
    if len(set(identities)) != 4:
        raise ValueError("Dual-light refinement requires four distinct sequences")
    coarse_pair = scene_render.verify_light_component_sequences(
        service, coarse_modulated_sequence_id, coarse_ambient_sequence_id,
    )
    fine_pair = scene_render.verify_light_component_sequences(
        service, fine_modulated_sequence_id, fine_ambient_sequence_id,
    )
    if (coarse_pair["scene_asset_id"] != fine_pair["scene_asset_id"]
            or coarse_pair["whole_scene_illumination_id"]
            != fine_pair["whole_scene_illumination_id"]
            or coarse_pair["light_asset_ids"] != fine_pair["light_asset_ids"]):
        raise ValueError("Dual-light refinements must retain the same two frozen Light assets")
    coarse_times = coarse_pair["render_times_ns"]
    fine_times = fine_pair["render_times_ns"]
    if (coarse_times[0] != fine_times[0] or coarse_times[-1] != fine_times[-1]
            or not set(coarse_times) <= set(fine_times)
            or len(fine_times) <= len(coarse_times)):
        raise ValueError("Fine dual-light times must strictly refine coarse render times")
    sequence_records = [service.registry.get("scene_sequences", identity)
                        for identity in identities]
    clocks = [record["payload"]["clock"] for record in sequence_records]
    if any(clock != clocks[0] for clock in clocks[1:]):
        raise ValueError("Dual-light refinement requires one exact simulation clock")
    checked_waveform = periodic_light.validate_waveform(waveform)
    if (checked_waveform["kind"] != "pwm"
            or checked_waveform["clock_id"] != clocks[0]["clock_id"]):
        raise ValueError("Dual-light refinement requires matching authored PWM clock")
    if (not isinstance(row_windows_ns, list) or not 1 <= len(row_windows_ns) <= 1024):
        raise ValueError("Dual-light refinement needs 1..1024 native row windows")
    first_job = scene_render.get_job(service, sequence_records[0][
        "fixture_specification"]["render_job_ids"][0])
    origin = first_job["request"]["sampling"]["crop_origin"]
    rows, cols = first_job["request"]["shape"]
    bands = len(first_job["request"]["wavelength_nm"])
    if (rows * cols * bands * 2 * (len(coarse_times) + len(fine_times)) > 8_000_000
            or len(row_windows_ns) * cols * bands * len(fine_times) > 8_000_000):
        raise ValueError("Dual-light refinement exceeds its bounded sample work")
    windows = []
    for entry in row_windows_ns:
        if not isinstance(entry, dict) or set(entry) != {"row", "start_ns", "end_ns"}:
            raise ValueError("Each row window requires exact row/start_ns/end_ns")
        row, start, end = entry["row"], entry["start_ns"], entry["end_ns"]
        if (any(type(value) is not int for value in (row, start, end))
                or not origin[0] <= row < origin[0] + rows
                or not coarse_times[0] <= start < end <= coarse_times[-1]):
            raise ValueError("Native row window lies outside frozen crop or render span")
        windows.append((row, row - origin[0], start, end))
    # Existing refinement checks exact shared coarse job snapshots and pinned
    # scene/camera/Backend before either component's arrays are read here.
    component_checks = [
        compare_refinement(service, coarse_id, fine_id)
        for coarse_id, fine_id in (
            (coarse_modulated_sequence_id, fine_modulated_sequence_id),
            (coarse_ambient_sequence_id, fine_ambient_sequence_id),
        )
    ]
    arrays = [_values(service, _verified_series(service, record))
              for record in sequence_records]
    windows_result = []
    for native_row, local_row, start, end in windows:
        modulated_coarse = _window_integral(
            coarse_times, [value[local_row] for value in arrays[0]], start, end,
        )
        ambient_coarse = _window_integral(
            coarse_times, [value[local_row] for value in arrays[1]], start, end,
        )
        modulated_fine = _window_integral(
            fine_times, [value[local_row] for value in arrays[2]], start, end,
        )
        ambient_fine = _window_integral(
            fine_times, [value[local_row] for value in arrays[3]], start, end,
        )
        gated_coarse = _pwm_window_integral(
            coarse_times, arrays[0], local_row, start, end, checked_waveform,
        )
        gated_fine = _pwm_window_integral(
            fine_times, arrays[2], local_row, start, end, checked_waveform,
        )
        windows_result.append({
            "row_native": native_row,
            "start_ns": start,
            "end_ns": end,
            "modulated_component_ungated": _change_metrics(
                modulated_coarse, modulated_fine,
            ),
            "ambient_component": _change_metrics(ambient_coarse, ambient_fine),
            "modulated_component_pwm_gated": _change_metrics(gated_coarse, gated_fine),
            "combined_pwm_plus_ambient": _change_metrics(
                gated_coarse + ambient_coarse, gated_fine + ambient_fine,
            ),
        })
    result = {
        "schema": "registered_3d_dual_light_pwm_refinement_v1",
        "sequence_ids": list(identities),
        "scene_asset_id": coarse_pair["scene_asset_id"],
        "light_asset_ids": coarse_pair["light_asset_ids"],
        "clock": clocks[0],
        "coarse_times_ns": coarse_times,
        "fine_times_ns": fine_times,
        "coarse_pair_sha256": coarse_pair["timelines_sha256"],
        "fine_pair_sha256": fine_pair["timelines_sha256"],
        "waveform": checked_waveform,
        "component_full_frame_checks": component_checks,
        "integral_unit": "J/m^2/nm at each native source pixel and sampled wavelength",
        "row_windows": windows_result,
        "interpretation": (
            "observed nested-grid difference only; not a continuous-motion, "
            "occlusion, finite-Monte-Carlo, or physical accuracy bound"
        ),
        "physical_validation": "not_established",
    }
    result["diagnostic_sha256"] = digest(result)
    return result


def _times(values):
    if (not isinstance(values, list) or not 2 <= len(values) <= MAX_RENDER_SAMPLES
            or any(type(value) is not int or not -MAX_TIME_NS <= value <= MAX_TIME_NS
                   for value in values)
            or any(b <= a for a, b in zip(values, values[1:], strict=False))
            or values[-1] - values[0] > MAX_TIME_NS):
        raise ValueError("Temporal samples need 2..65 strictly increasing exact ns times")
    return list(values)


def _base_request(request):
    mutable = copy.deepcopy(request)
    for key in ("name", "render_time_ns", "resolved_scene", "resolved_pose",
                "scene_motion_evidence"):
        mutable.pop(key, None)
    return canonical(mutable)


def _verified_series(service, sequence):
    images = sequences._images(service, sequence["payload"])
    spec = sequence["fixture_specification"]
    jobs = spec.get("render_job_ids")
    times = spec.get("render_times_ns")
    if not isinstance(jobs, list) or len(jobs) != len(images):
        raise ValueError("Temporal render series job IDs differ from its images")
    if times != [item["time_ns"] for item in sequence["payload"]["knots"]]:
        raise ValueError("Temporal render series times differ from its images")
    if (spec.get("maximum_sample_gap_ns") != max(
        right - left for left, right in zip(times, times[1:], strict=False)
    ) or spec.get("integrand") != "rendered_3d_spectral_irradiance_before_sensor_optics"
            or spec.get("integration")
            != "piecewise_linear_irradiance_over_native_row_exposure"
            or spec.get("physical_validation") != "not_established"
            or sequence["source_kind"] != "simulated"
            or sequence["source_uri"] != "scene-render-series://" + digest(jobs)
            or sequence["method_and_version"] != "registered_3d_temporal_quadrature_v1"
            or sequence["payload"]["interpolation"] != "linear_irradiance"):
        raise ValueError("Temporal render series meaning differs from frozen 3D quadrature")
    base = None
    for record, job_id in zip(images, jobs, strict=True):
        source = record.get("render_job_snapshot")
        if (not isinstance(source, dict) or source.get("id") != job_id
                or source != scene_render.get_job(service, job_id)):
            raise ValueError("Temporal series lacks its exact registered 3D render")
        scene_render._verify_motion_request(service, source)
        current = _base_request(source["request"])
        if base is None:
            base = current
        elif current != base:
            raise ValueError("Temporal series mixes 3D scene or camera setups")
    return images


def _values(service, images):
    values = []
    for record in images:
        artifacts = [item for item in record["artifacts"]
                     if item["name"] == "spectral-image.npy"]
        if not artifacts or any(item["sha256"] != artifacts[0]["sha256"]
                                for item in artifacts[1:]):
            raise ValueError("Temporal image has missing or ambiguous irradiance")
        content = service.registry.blob_path(artifacts[0]["sha256"]).read_bytes()
        value = imaging.load_spectral_npy(content)
        if list(value.shape) != record["shape"]:
            raise ValueError("Temporal image irradiance differs from its declared shape")
        values.append(value)
    return values


def _window_integral(times, values, start_ns, end_ns):
    integral = np.zeros_like(values[0], dtype=np.float64)
    for index, (left, right) in enumerate(zip(times, times[1:], strict=False)):
        low, high = max(left, start_ns), min(right, end_ns)
        if high <= low:
            continue
        left_fraction = (low - left) / (right - left)
        right_fraction = (high - left) / (right - left)
        low_value = values[index] * (1 - left_fraction) + values[index + 1] * left_fraction
        high_value = values[index] * (1 - right_fraction) + values[index + 1] * right_fraction
        integral += (low_value + high_value) * ((high - low) * 0.5e-9)
    return integral
