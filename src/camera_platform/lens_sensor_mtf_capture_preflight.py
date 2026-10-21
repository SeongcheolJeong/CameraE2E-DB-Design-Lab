"""Plan a same-angular-sampling source halo for Lens x Sensor MTF studies.

This is an admission diagnostic over sealed RAW and quality evidence, not an
alternative MTF estimator. A maximum across executed PSFs is a conservative
PSF bound. We propagate it through the hash-pinned strict distortion regrid,
including its bilinear sample halo. Failure of that global bound to fit never
establishes loss at a particular scored pixel. The old RAW reports do not
retain their executed mosaic integration operator, so this planner never
qualifies a physical MTF comparison or submits a recapture.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .lens_sensor_quality_study import SLOTS

SCHEMA = "camera_lens_sensor_same_sampling_mtf_recapture_preflight_v1"
# lens_sensor_raw_bridge._scene_samples currently enforces this ceiling for
# explicit scenes, including registered radiance inputs.
CURRENT_RAW_SCENE_SAMPLES_MAX = 256


def _positive(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} requires a finite positive number.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} requires a finite positive number.")
    return number


def _nonnegative(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} requires a finite nonnegative number.")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} requires a finite nonnegative number.")
    return number


def _pair(
    value: Any, label: str, *, integer: bool = False, nonnegative: bool = False,
) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} requires two row/column values.")
    if integer and any(type(item) is not int or item <= 0 for item in value):
        raise ValueError(f"{label} requires two positive integers.")
    check = _nonnegative if nonnegative else _positive
    return check(value[0], label), check(value[1], label)


def _same_spacing_fov_deg(old_fov_deg: float, old_count: int, new_count: int) -> float:
    return math.degrees(2.0 * math.atan(
        (new_count / old_count) * math.tan(math.radians(old_fov_deg) / 2.0)
    ))


def _next_power_of_two(value: int) -> int:
    return 1 << (value - 1).bit_length()


def _strict_geometry_max_inverse_factor(
    axes: Mapping[str, Any], radius_mm: float,
) -> float:
    """Bound ``np.interp(radius, distorted, ideal) / radius`` on [0, radius].

    Each segment is affine in radius, so its ratio is monotone within the
    segment. Testing its knots and the requested endpoint gives the exact
    maximum of the strict geometry operator's radial factor.
    """
    ideal = axes.get("ideal_image_height_mm")
    distorted = axes.get("distorted_image_height_mm")
    if (
        not isinstance(ideal, list) or not isinstance(distorted, list)
        or len(ideal) != len(distorted) or len(ideal) < 2
    ):
        raise ValueError("Pinned strict geometry needs matched ideal/distorted axes.")
    ideal = [_nonnegative(item, "ideal image height") for item in ideal]
    distorted = [_nonnegative(item, "distorted image height") for item in distorted]
    if (
        ideal[0] != 0 or distorted[0] != 0
        or any(b <= a for a, b in zip(ideal, ideal[1:], strict=False))
        or any(b <= a for a, b in zip(distorted, distorted[1:], strict=False))
    ):
        raise ValueError("Pinned strict geometry axes must increase from zero.")

    def interpolated_input(value: float) -> float:
        if value >= distorted[-1]:
            return ideal[-1]
        for index in range(1, len(distorted)):
            if value <= distorted[index]:
                weight = (value - distorted[index - 1]) / (
                    distorted[index] - distorted[index - 1]
                )
                return ideal[index - 1] + weight * (
                    ideal[index] - ideal[index - 1]
                )
        raise AssertionError("No strict geometry segment contains the radius.")

    candidates = [1.0]
    for output, source in zip(distorted[1:], ideal[1:], strict=True):
        if output <= radius_mm:
            candidates.append(source / output)
    if radius_mm > 0:
        candidates.append(interpolated_input(radius_mm) / radius_mm)
    return max(candidates)


def _mapped_source_half_span(
    output_half: list[float], spacing_um: float, wavelengths_nm: list[float],
    geometry: Mapping[str, Mapping[str, Any]], slot: str,
) -> tuple[list[float], dict[str, float]]:
    max_radius_mm = math.hypot(*output_half) * spacing_um / 1000.0
    factors: dict[str, float] = {}
    for wave in wavelengths_nm:
        key = f"{float(wave):g}"
        axes = geometry.get(key)
        if not isinstance(axes, Mapping):
            raise ValueError(f"{slot} lacks pinned strict geometry at {key} nm.")
        factors[key] = _strict_geometry_max_inverse_factor(axes, max_radius_mm)
    largest_factor = max(factors.values())
    # strict geometry map_coordinates(order=1, constant zero) may use
    # the adjacent source sample on either side of each query.
    source_half = [value * largest_factor + 1.0 for value in output_half]
    return source_half, factors


def _conditional_single_sample_mosaic_radius_samples(
    pitch_um: float, spacing_um: float,
) -> dict[str, Any]:
    """Describe the current *conditional* mosaic operator, without crediting it.

    For n_samples_per_pixel=1 and OCL group mode off, sensor.py applies a
    ceil(pitch/OI spacing) Gaussian with ``same`` convolution, followed by
    bilinear constant-zero interpolation. ``convolve2d(..., mode='same')``
    anchors an even kernel asymmetrically, so its largest integer offset is
    floor(kernel_size/2), not its geometric half-width. One additional OI
    sample per axis bounds interpolation beyond that finite kernel offset.
    Old RAW jobs do not pin either the subpixel count or OCL group mode.
    """
    kernel = max(1, math.ceil(pitch_um / spacing_um))
    kernel_integer_radius = kernel // 2
    return {
        "status": "conditional_current_code_not_executed_source_evidence",
        "assumptions": ["n_samples_per_pixel=1", "ocl_group_proxy.mode=off"],
        "gaussian_kernel_samples_row_col": [kernel, kernel],
        "gaussian_kernel_radius_samples_row_col": [kernel_integer_radius] * 2,
        "bilinear_interpolation_extra_bound_samples_row_col": [1.0, 1.0],
        "combined_radius_bound_samples_row_col": [kernel_integer_radius + 1.0] * 2,
        "credited_to_source_halo_or_product_mtf": False,
    }


def plan_same_sampling_mtf_recapture(
    cases: Mapping[str, Mapping[str, Any]],
    support_diagnostic: Mapping[str, Any],
    source_reports: Mapping[str, Mapping[str, Any]],
    *,
    strict_geometry_by_lens_revision: Mapping[str, Mapping[str, Mapping[str, Any]]],
    max_fov_deg_by_lens_revision: Mapping[str, float],
) -> dict[str, Any]:
    """Return a source-bound, fail-closed 2x2 recapture plan.

    The global bound covers all generated wavelength/field/azimuth PSFs. We
    expand the entire native output frame and propagate its rectangle through
    the strict distortion geometry at every source wavelength. The current
    sensor.py single-sample mosaic operator is shown only conditionally,
    because the executed one was not retained. Any unverified ISP halo is
    excluded. A suggested power-of-two window is a sizing experiment, never
    proof that a new RAW ran or MTF converged.
    """
    if set(cases) != set(SLOTS) or set(source_reports) != set(SLOTS):
        raise ValueError("Recapture preflight requires the exact crossed 2x2 slots.")
    if support_diagnostic.get("available") is not True:
        raise ValueError("Saved common-angle scored support diagnostic is unavailable.")
    per_slot = support_diagnostic.get("per_slot")
    if not isinstance(per_slot, Mapping) or set(per_slot) != set(SLOTS):
        raise ValueError("Saved scored support slots are incomplete.")

    planned: dict[str, dict[str, Any]] = {}
    largest_without_pixel = 0
    largest_conditional_default = 0
    old_scene_samples: set[tuple[int, int]] = set()
    old_fovs: set[float] = set()
    for slot in SLOTS:
        condition = cases[slot].get("conditions")
        report = source_reports[slot]
        if not isinstance(condition, Mapping) or not isinstance(report, Mapping):
            raise ValueError(f"{slot} has no sealed source conditions.")
        psf = condition.get("model_psf_source_support")
        stage = per_slot[slot].get("executed_psf_pixel_source_scene_halo")
        if (
            not isinstance(psf, Mapping)
            or psf.get("status") != "global_exact_nonzero_bound_recorded"
            or not isinstance(stage, Mapping)
            or stage.get("status") != "global_bound_evaluated"
            or stage.get("scene_samples_row_col") != psf.get("scene_samples_row_col")
            or stage.get("executed_global_max_nonzero_psf_radius_samples_row_col")
            != [(side - 1) / 2.0 for side in psf.get(
                "largest_exact_nonzero_centered_kernel_row_col", []
            )]
        ):
            raise ValueError(f"{slot} has no matched executed PSF extent and scored support.")
        scene_shape = tuple(int(item) for item in _pair(
            psf["scene_samples_row_col"], f"{slot} source scene", integer=True
        ))
        old_scene_samples.add(scene_shape)
        spacing_um = _positive(psf["oi_sample_spacing_um"], f"{slot} OI spacing")
        pitch_um = _positive(psf["sensor_pixel_pitch_um"], f"{slot} sensor pitch")
        source_fov = condition.get("angular_scene_grid_contract")
        if not isinstance(source_fov, Mapping):
            raise ValueError(f"{slot} source angular scene contract is missing.")
        fovs = (
            _positive(source_fov.get("vertical_fov_deg"), f"{slot} vertical FOV"),
            _positive(source_fov.get("horizontal_fov_deg"), f"{slot} horizontal FOV"),
        )
        old_fovs.add(fovs[1])
        point = condition.get("native_output_angular_grid", {}).get(
            "distortion_aware_point_center", {}
        )
        declared = point.get("input_scene_support") or {}
        angles = declared.get("max_abs_input_angle_deg_row_col_by_wavelength")
        if (
            declared.get("status") != "all_model_point_centers_within_source_samples"
            or not isinstance(angles, list)
            or len(angles) != len(psf.get("executed_wavelength_nm", []))
            or len(angles) == 0
        ):
            raise ValueError(f"{slot} lacks source-bound all-wavelength point positions.")
        sample_offsets = [0.0, 0.0]
        for angle_pair in angles:
            for axis, angle in enumerate(_pair(
                angle_pair, f"{slot} angle", nonnegative=True,
            )):
                if angle >= fovs[axis] / 2.0:
                    raise ValueError(f"{slot} native point center leaves the source FOV.")
                offset = (
                    scene_shape[axis] * math.tan(math.radians(angle))
                    / (2.0 * math.tan(math.radians(fovs[axis]) / 2.0))
                )
                sample_offsets[axis] = max(sample_offsets[axis], offset)
        psf_radius = _pair(
            stage["executed_global_max_nonzero_psf_radius_samples_row_col"],
            f"{slot} executed global PSF radius",
            nonnegative=True,
        )
        antialias = _pair(
            per_slot[slot]["scored_antialias_radius_native_pixels_row_col"],
            f"{slot} scored antialias radius",
            nonnegative=True,
        )
        isp_radius = stage.get("fixed_isp_radius_native_pixels")
        if isp_radius is not None:
            isp_radius = _nonnegative(isp_radius, f"{slot} fixed ISP radius")
        # Unknown ISP is excluded, never silently treated as zero evidence.
        credited_isp_radius = 0.0 if isp_radius is None else isp_radius
        ratio = pitch_um / spacing_um
        native_size = _pair(condition.get("sensor_size"), f"{slot} sensor size", integer=True)
        output_half_without_pixel = [
            (native_size[axis] - 1.0) * ratio / 2.0 + psf_radius[axis]
            + (antialias[axis] + credited_isp_radius) * ratio
            for axis in range(2)
        ]
        mosaic = _conditional_single_sample_mosaic_radius_samples(pitch_um, spacing_um)

        resource = report.get("resource_preflight")
        if (
            not isinstance(resource, Mapping)
            or resource.get("scope") != "central_bounded_strict_raytrace"
            or [resource.get("scene_samples_row"), resource.get("scene_samples_col")]
            != list(scene_shape)
            or resource.get("scene_sample_spacing_um") != spacing_um
            or resource.get("resampled_kernel_samples_row_col")
            != psf.get("resampled_kernel_samples_row_col")
        ):
            raise ValueError(f"{slot} source resource preflight differs from executed RAW.")
        revision_id = report.get("source_identities", {}).get("lens", {}).get("revision_id")
        if revision_id != report.get("specification", {}).get("lens_revision_id"):
            raise ValueError(f"{slot} Lens revision differs from sealed source specification.")
        geometry = strict_geometry_by_lens_revision.get(revision_id)
        if not isinstance(geometry, Mapping):
            raise ValueError(f"{slot} has no hash-pinned strict distortion geometry.")

        required_without_pixel, factors_without_pixel = _mapped_source_half_span(
            output_half_without_pixel, spacing_um, psf["executed_wavelength_nm"],
            geometry, slot,
        )
        output_half_conditional = [
            value + mosaic["combined_radius_bound_samples_row_col"][axis]
            for axis, value in enumerate(output_half_without_pixel)
        ]
        required_conditional, factors_conditional = _mapped_source_half_span(
            output_half_conditional, spacing_um, psf["executed_wavelength_nm"],
            geometry, slot,
        )
        if any(required_without_pixel[axis] + 1e-8 < sample_offsets[axis]
               for axis in range(2)):
            raise ValueError(f"{slot} strict geometry bound contradicts the saved point map.")
        sufficient_shape = [
            math.ceil(2.0 * value + 1.0) for value in required_without_pixel
        ]
        conditional_shape = [
            math.ceil(2.0 * value + 1.0) for value in required_conditional
        ]
        largest_without_pixel = max(largest_without_pixel, *sufficient_shape)
        largest_conditional_default = max(largest_conditional_default, *conditional_shape)
        planned[slot] = {
            "source_job_id": cases[slot]["source_job_id"],
            "lens_revision_id": revision_id,
            "scene_samples_row_col": list(scene_shape),
            "scene_fov_deg_row_col": list(fovs),
            "oi_sample_spacing_um": spacing_um,
            "all_wavelength_native_frame_max_center_offset_oi_samples_row_col": sample_offsets,
            "executed_global_psf_radius_oi_samples_row_col": list(psf_radius),
            "expanded_output_half_span_excluding_mosaic_oi_samples_row_col": (
                output_half_without_pixel
            ),
            "strict_geometry_max_inverse_factor_by_wavelength_excluding_mosaic": (
                factors_without_pixel
            ),
            "scored_antialias_radius_native_pixels_row_col": list(antialias),
            "fixed_isp_radius_native_pixels": isp_radius,
            "unidentified_isp_halo_excluded": isp_radius is None,
            "strict_geometry_source_half_span_excluding_mosaic_oi_samples_row_col": (
                required_without_pixel
            ),
            "smallest_scene_for_global_bound_excluding_mosaic_row_col": sufficient_shape,
            "conditional_default_mosaic_operator": mosaic,
            "strict_geometry_max_inverse_factor_by_wavelength_conditional_mosaic": (
                factors_conditional
            ),
            "strict_geometry_source_half_span_conditional_mosaic_row_col": (
                required_conditional
            ),
            "smallest_scene_if_conditional_default_mosaic_row_col": conditional_shape,
            "old_bounded_working_heuristic_gib": resource.get(
                "estimated_bounded_working_gib"
            ),
            "old_nonzero_input_convolutions": report.get("preview", {}).get(
                "lens_model", {}
            ).get("psf_execution", {}).get("nonzero_input_convolutions"),
        }

    if len(old_scene_samples) != 1 or len(old_fovs) != 1:
        raise ValueError("The four RAWs do not share original source dimensions and FOV.")
    source_shape = next(iter(old_scene_samples))
    original_fov = next(iter(old_fovs))
    provisional_side = _next_power_of_two(max(largest_conditional_default, 32))
    proposed_fov = _same_spacing_fov_deg(original_fov, source_shape[1], provisional_side)
    proposed_diagonal_fov = math.degrees(2.0 * math.atan(
        math.sqrt(2.0) * math.tan(math.radians(proposed_fov) / 2.0)
    ))
    for item in planned.values():
        max_fov = _positive(
            max_fov_deg_by_lens_revision[item["lens_revision_id"]],
            "hash-pinned Lens max FOV",
        )
        item["hash_pinned_lens_max_fov_deg"] = max_fov
        item["provisional_diagonal_fov_within_lens_field"] = (
            proposed_diagonal_fov <= max_fov
        )
    gib = float(1024**3)
    for slot, item in planned.items():
        kernel = source_reports[slot]["resource_preflight"][
            "resampled_kernel_samples_row_col"
        ]
        old_rows, old_cols = item["scene_samples_row_col"]
        fft_new = (provisional_side + kernel[0] - 1) * (provisional_side + kernel[1] - 1)
        fft_old = (old_rows + kernel[0] - 1) * (old_cols + kernel[1] - 1)
        pad = [2 * math.ceil((math.floor(side / 2) + 2) / 2) for side in kernel]
        item["provisional_resource_lower_bounds"] = {
            "scene_float64_three_band_cube_gib": provisional_side**2 * 3 * 8 / gib,
            "padded_output_float64_three_band_cube_gib": (
                (provisional_side + 2 * pad[0])
                * (provisional_side + 2 * pad[1]) * 3 * 8 / gib
            ),
            "one_full_convolution_real64_grid_gib": fft_new * 8 / gib,
            "one_full_convolution_grid_area_ratio_to_old": fft_new / fft_old,
            "peak_rss_or_runtime_bound": None,
        }

    return {
        "schema": SCHEMA,
        "status": "blocked_current_capture_contract_and_missing_executed_mosaic_evidence",
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "source_scene_samples_row_col": list(source_shape),
        "source_horizontal_fov_deg": original_fov,
        "current_raw_explicit_scene_max_samples_per_axis": CURRENT_RAW_SCENE_SAMPLES_MAX,
        "smallest_square_for_global_bound_excluding_mosaic": (
            largest_without_pixel
        ),
        "smallest_square_if_conditional_default_mosaic": largest_conditional_default,
        "provisional_power_of_two_scene_samples_row_col": [provisional_side] * 2,
        "provisional_same_sampling_horizontal_fov_deg": proposed_fov,
        "provisional_diagonal_fov_deg": proposed_diagonal_fov,
        "provisional_within_all_lens_fields": all(
            item["provisional_diagonal_fov_within_lens_field"]
            for item in planned.values()
        ),
        "tangent_plane_angular_sample_spacing_preserved": True,
        "can_submit_provisional_scene_through_current_raw_contract": (
            provisional_side <= CURRENT_RAW_SCENE_SAMPLES_MAX
        ),
        "executed_mosaic_operator_support_recorded": False,
        "strict_geometry_expanded_region_support_reconstructed": True,
        "large_scene_resource_qualified": False,
        "new_capture_executed": False,
        "new_mtf_estimator_convergence_test_executed": False,
        "per_slot": planned,
        "required_next_steps": [
            "Extend and resource-qualify the RAW source-scene size contract beyond 256, "
            "or implement a proven analytic slanted-edge boundary extension "
            "before PSF convolution.",
            "Persist the executed mosaic spatial integration operator: n_samples_per_pixel, "
            "pixel aperture/PD geometry, Gaussian kernel, interpolation and OCL group settings.",
            "Re-run all four hash-pinned RAWs at unchanged angular sampling with a "
            "source window large enough for wavelength/field-specific local PSF and pixel support.",
            "Re-run the same angular ROI quality Job and vary input size and angular "
            "sampling independently to check MTF estimator convergence.",
        ],
        "interpretation": (
            "The provisional dimensions propagate the whole native frame and "
            "recorded global PSF radius through pinned strict distortion geometry, "
            "conditionally using a default mosaic operator. Unrecorded pixel/ISP "
            "support and actual peak memory/runtime remain open. These dimensions "
            "are not proof of local PSF loss, physical MTF or product ordering."
        ),
    }


def require_supported_same_sampling_recapture(plan: Mapping[str, Any]) -> None:
    """Refuse a requested corrected capture until its input contract is executable."""
    if plan.get("schema") != SCHEMA:
        raise ValueError("same_sampling_preflight_invalid: unsupported plan schema.")
    if plan.get("can_submit_provisional_scene_through_current_raw_contract") is not True:
        raise ValueError(
            "same_sampling_scene_unsupported: the provisional source scene needs "
            f"{plan.get('provisional_power_of_two_scene_samples_row_col')} samples, "
            f"but explicit RAW inputs stop at {CURRENT_RAW_SCENE_SAMPLES_MAX}; "
            "widening FOV at 256 would change angular sampling."
        )
    if plan.get("provisional_within_all_lens_fields") is not True:
        raise ValueError("lens_field_unsupported: the proposed scene exceeds a pinned Lens FOV.")
    if plan.get("executed_mosaic_operator_support_recorded") is not True:
        raise ValueError(
            "mosaic_operator_unrecorded: a source-window fit cannot certify "
            "the executed pixel integration footprint."
        )
    if plan.get("large_scene_resource_qualified") is not True:
        raise ValueError(
            "large_scene_resource_unqualified: existing memory heuristics do not "
            "bound the proposed 1024-sample FFT and sensor execution."
        )
