"""Opt-in large Scene preflight for bounded Lens x Sensor RAW studies.

The arithmetic below is intentionally explicit about source, PSF and FFT
arrays. It is an admission estimate, not a peak-RSS proof. Large execution is
refused until a separately supervised process limit has been installed and
measured on this host; a worker thread cannot enforce that boundary.
"""

from __future__ import annotations

import math
import resource
from typing import Any

from scipy.fft import next_fast_len

from .lens_sensor_raw_bridge import bounded_psf_resource_preflight

SCHEMA = "camera_lens_sensor_large_scene_resource_preflight_v1"
# Opt-in experiment policy, not a host RAM limit or a demonstrated accurate
# Scene sampling level. The shared worker still rejects every large-scene job.
MAX_OPT_IN_SCENE_SAMPLES = 1024
# Deliberately chosen admission budget for an isolated future executor. It is
# not an enforced RSS limit and is not a measured peak-memory requirement.
OPT_IN_PROCESS_RSS_BUDGET_BYTES = 8 * 1024**3


def preflight_large_scene(
    lens_model: dict[str, Any], sensor_pitch_um: float, sensor_size: list[int],
    *, scene_horizontal_fov_deg: float, scene_samples_row_col: list[int],
) -> dict[str, Any]:
    """Describe every known large 3-band strict-streaming allocation class."""
    legacy = bounded_psf_resource_preflight(
        lens_model, sensor_pitch_um, sensor_size,
        scene_horizontal_fov_deg=scene_horizontal_fov_deg,
        scene_samples_row_col=scene_samples_row_col,
        max_scene_samples=MAX_OPT_IN_SCENE_SAMPLES,
    )
    source_rows, source_cols = scene_samples_row_col
    kernel_rows, kernel_cols = legacy["resampled_kernel_samples_row_col"]
    source_area = source_rows * source_cols
    kernel_area = kernel_rows * kernel_cols
    # Strict solver pads each axis to an even value above half the kernel.
    padding_rows = 2 * math.ceil((math.ceil(kernel_rows // 2) + 2) / 2)
    padding_cols = 2 * math.ceil((math.ceil(kernel_cols // 2) + 2) / 2)
    padded_area = (source_rows + 2 * padding_rows) * (source_cols + 2 * padding_cols)
    fft_rows = next_fast_len(source_rows + kernel_rows - 1)
    fft_cols = next_fast_len(source_cols + kernel_cols - 1)
    fft_real_bytes = fft_rows * fft_cols * 8
    fft_complex_bytes = fft_rows * (fft_cols // 2 + 1) * 16
    original_bands = 31  # pyisetcam scene_create('uniform ee') fixed 400:10:700 nm.
    arrays = {
        "initial_d65_spectral_scene_float64_bytes": source_area * original_bands * 8,
        "executed_three_band_scene_float64_bytes": source_area * 3 * 8,
        "all_band_geometry_and_mask_float64_20_planes_bytes": source_area * 20 * 8,
        "three_band_padded_output_float64_bytes": padded_area * 3 * 8,
        "four_cached_resampled_psf_float32_bytes": kernel_area * 4 * 4,
        "one_source_psf_float64_workspace_bytes": (
            int(lens_model["psf_samples_row_col"][0])
            * int(lens_model["psf_samples_row_col"][1])
            * 3 * len(lens_model["field_samples_mm"])
            * 8
        ),
        "one_full_sparse_fft_real_grid_bytes": fft_real_bytes,
        "one_full_sparse_fft_complex_grid_bytes": fft_complex_bytes,
        "fft_intermediates_policy_six_complex_plus_four_real_bytes": (
            6 * fft_complex_bytes + 4 * fft_real_bytes
        ),
    }
    # Preserve the existing empirically raised 128x-kernel heuristic, and
    # separately add large-scene and FFT terms that it omits. The policy sum
    # is conservative admission arithmetic, not a measured RSS upper bound.
    estimated = (
        round(legacy["estimated_bounded_working_gib"] * 1024**3)
        + 8 * arrays["initial_d65_spectral_scene_float64_bytes"]
        + arrays["all_band_geometry_and_mask_float64_20_planes_bytes"]
        + 3 * arrays["three_band_padded_output_float64_bytes"]
        + arrays["fft_intermediates_policy_six_complex_plus_four_real_bytes"]
    )
    rlimit_as = resource.getrlimit(resource.RLIMIT_AS)
    result = {
        "schema": SCHEMA,
        "scope": "bounded_strict_psf_opt_in_large_scene",
        "status": (
            "resource_blocked_estimate" if estimated > OPT_IN_PROCESS_RSS_BUDGET_BYTES
            else "resource_blocked_process_guard_unavailable"
        ),
        "scene_samples_row_col": list(scene_samples_row_col),
        "scene_horizontal_fov_deg": scene_horizontal_fov_deg,
        "scene_sample_spacing_um": legacy["scene_sample_spacing_um"],
        "source_psf_samples_row_col": list(lens_model["psf_samples_row_col"]),
        "resampled_psf_samples_row_col": [kernel_rows, kernel_cols],
        "padded_output_samples_row_col": [
            source_rows + 2 * padding_rows, source_cols + 2 * padding_cols
        ],
        "worst_case_sparse_fft_samples_row_col": [fft_rows, fft_cols],
        "array_classes": arrays,
        "existing_bounded_psf_heuristic_gib": legacy["estimated_bounded_working_gib"],
        "combined_admission_estimate_gib": estimated / 1024**3,
        "declared_process_rss_budget_gib": OPT_IN_PROCESS_RSS_BUDGET_BYTES / 1024**3,
        "current_rlimit_as_bytes": [int(value) for value in rlimit_as],
        "enforced_child_process_rss_guard": False,
        "hard_memory_boundary_verified": False,
        "polling_supervisor_qualification": "small_process_fault_containment_only",
        "polling_supervisor_cannot_bound_allocation_between_samples": True,
        "descendant_rss_monitoring_available_in_macos_sandbox": False,
        "reason": (
            "The known allocation-class policy estimate exceeds the opt-in RSS budget."
            if estimated > OPT_IN_PROCESS_RSS_BUDGET_BYTES else
            "No enforceable 8 GiB resident-memory boundary is verified on this host. "
            "Darwin RLIMIT_RSS aliases virtual-address RLIMIT_AS; the tested 8 GiB "
            "AS/DATA settings fail for this process while a 512 GiB setting succeeds. "
            "Parent RSS polling can miss allocation spikes and cannot inspect "
            "descendant RSS here. Do not enqueue a 1024 Scene on the shared worker."
        ),
        "estimate_is_peak_rss_upper_bound": False,
        "physical_mtf_comparison_allowed": False,
    }
    return result


def require_large_scene_resources(result: dict[str, Any]) -> None:
    # This shared-worker path has no isolated production child executor. A
    # caller-provided or test-forged status may never turn a polling observer
    # into an enforced memory boundary. Keep admission closed until such an
    # executor is implemented and separately bound to the frozen job.
    raise ValueError(
        f"resource_blocked_large_scene_executor_unavailable: "
        f"{result.get('status', 'resource_blocked')}: "
        f"{result.get('reason', 'hard process memory boundary missing')}"
    )
