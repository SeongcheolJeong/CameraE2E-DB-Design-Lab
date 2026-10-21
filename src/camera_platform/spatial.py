"""Native-grid spectral image / supplied intensity-PSF camera runtime.

This is a discrete incoherent image formation model with either a fixed PSF or
supplied field kernels bilinearly interpolated at each source pixel center.
The supplied image already includes on-axis lens throughput. The legacy path
applies relative illumination after its normalized PSF. An explicit opt-in
weights source cells or retained source subcells before the same PSF; the
receiver's angular QE remains a separately evaluated approximation. It is not
a geometry renderer or an MTF-to-PSF inverse.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from camera_platform import subcell_optics
from camera_platform.runtime import (
    LIGHT_M_S,
    PLANCK_J_S,
    ResponseDBRuntime,
    RuntimeRequestError,
    _issue,
    charge_collection_capabilities,
    with_sensor_effects,
)

PLUGIN_ID = "spatial_scene_v1"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_FRAME_PIXELS = 1_048_576
MAX_SPECTRAL_SAMPLES = 4_194_304
MAX_FIELD_NODES = 25
MAX_FIELD_FFT_WORK = 500_000_000
SOURCE_RI_MODE = "source_position_ri_v1"
EFFECTS = [
    "lens_psf",
    "pixel_aperture",
    "lens_relative_illumination",
    "lens_incident_angle_distribution",
    "sensor_effective_qe",
    "sensor_angular_response",
    "mean_dark_charge",
    "readout_clipping",
]


def _keys(value: Any, expected: set[str], path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{path} requires exactly {sorted(expected)}")
    return value


def _integer_pair(value: Any, path: str, low: int, high: int) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(
        isinstance(x, (int, np.integer)) and not isinstance(x, (bool, np.bool_))
        and low <= x <= high for x in value
    ):
        raise ValueError(f"{path} requires two integers in [{low},{high}]")
    return tuple(int(x) for x in value)


def _numeric_array(value: Any, ndim: int, path: str) -> np.ndarray:
    data = np.asarray(value)
    if data.ndim != ndim or data.dtype.kind not in "uif" or data.size > MAX_SPECTRAL_SAMPLES:
        raise ValueError(f"{path} requires a bounded real numeric {ndim}-D array")
    data = data.astype(np.float64, copy=False)
    if not np.isfinite(data).all() or np.any(data < 0):
        raise ValueError(f"{path} requires finite nonnegative samples")
    return data


def _source(value: Any, path: str) -> dict[str, Any]:
    data = _keys(value, {"kind", "description"}, path)
    if not isinstance(data["kind"], str) or data["kind"] not in {
        "measured", "simulated", "vendor", "estimated"
    }:
        raise ValueError(f"{path}.kind must be measured, simulated, vendor, or estimated")
    if not isinstance(data["description"], str) or not 1 <= len(data["description"]) <= 2000:
        raise ValueError(f"{path}.description requires 1..2000 characters")
    return dict(data)


def _same_wave(value: Any, wave: Any, path: str):
    axis = np.asarray(value)
    reference = np.asarray(wave)
    if (axis.dtype.kind not in "uif" or reference.dtype.kind not in "uif"
            or axis.shape != reference.shape or not np.array_equal(axis, reference)):
        raise ValueError(f"{path} must exactly match wavelength_nm; no spectral resampling")


def _field_axis(value: Any, path: str) -> np.ndarray:
    # Inspect before numeric coercion so mixed [False, 0.1] is not accepted.
    if (not isinstance(value, (list, tuple, np.ndarray))
            or np.ndim(value) != 1 or not 2 <= len(value) <= 9
            or any(isinstance(x, (bool, np.bool_)) for x in value)):
        raise ValueError(f"{path} requires 2..9 finite, strictly increasing real coordinates")
    axis = np.asarray(value)
    if axis.dtype.kind not in "uif" or not np.isfinite(axis).all():
        raise ValueError(f"{path} requires 2..9 finite, strictly increasing real coordinates")
    with np.errstate(over="ignore", invalid="ignore"):
        spacing = np.diff(axis.astype(float))
    if not np.isfinite(spacing).all() or not np.all(spacing > 0):
        raise ValueError(f"{path} requires finite, positive coordinate spacings")
    return axis.astype(float)


def _has_boolean(value):
    if isinstance(value, (bool, np.bool_)):
        return True
    if isinstance(value, np.ndarray):
        return value.dtype.kind == "b" or (value.dtype.kind == "O"
                                           and any(_has_boolean(x) for x in value.flat))
    return isinstance(value, (list, tuple)) and any(_has_boolean(x) for x in value)


def validate_psf(psf: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a standalone PSF and return detached JSON data, or raise ValueError.

    This checks the PSF's own sampling contract. Compatibility with a selected
    sensor's exact wavelength coordinates and pitch remains a runtime check.
    Values are never renormalized, resampled, or converted from an MTF.
    """
    field = isinstance(psf, Mapping) and psf.get("schema") == "field_intensity_psf_v1"
    psf = _keys(psf, {
        "schema", "wavelength_nm", "values", "sample_pitch_um", "normalization",
        "centering", "spatial_model", "boundary", "source", "sampling_model",
    } | ({"field_y_mm", "field_x_mm", "field_coordinates", "interpolation"}
         if field else set()), "psf")
    declarations = {
        "schema": "intensity_psf_v1", "normalization": "unit_sum",
        "centering": "array_center", "spatial_model": "locally_shift_invariant",
        "boundary": "zero_outside_supplied_image",
        "sampling_model": "pixel_integrated_optical_impulse",
    }
    if field:
        declarations.update(schema="field_intensity_psf_v1", spatial_model="field_grid",
                            field_coordinates="sensor_center_yx",
                            interpolation="bilinear_source_position")
    if any(psf[key] != value for key, value in declarations.items()):
        raise ValueError("PSF requires explicit unit-sum intensity kernel sampling declarations")
    wave = _numeric_array(psf["wavelength_nm"], 1, "psf.wavelength_nm")
    if (not 2 <= wave.size <= 16 or not np.all(np.diff(wave) > 0)
            or wave[0] < 200 or wave[-1] > 2500):
        raise ValueError("PSF requires 2..16 strictly increasing wavelengths within [200,2500] nm")
    pitch = psf["sample_pitch_um"]
    if (isinstance(pitch, (bool, np.bool_)) or not isinstance(pitch, (int, float, np.number))
            or np.iscomplexobj(pitch) or not np.isfinite(pitch) or not 0.1 <= pitch <= 1000):
        raise ValueError("PSF sample_pitch_um must be a finite real number in [0.1,1000]")
    if field and _has_boolean(psf["values"]):
        raise ValueError("Field PSF values require numeric samples, not booleans")
    kernels = _numeric_array(psf["values"], 5 if field else 3, "psf.values")
    if (kernels.shape[0] != wave.size
            or any(x < 1 or x > 65 or x % 2 == 0 for x in kernels.shape[-2:])):
        raise ValueError("PSF requires wavelength-first kernels with odd sides in [1,65]")
    coordinates = {}
    if field:
        axes = [_field_axis(psf[name], f"psf.{name}") for name in ("field_y_mm", "field_x_mm")]
        if (kernels.shape[1:3] != tuple(len(axis) for axis in axes)
                or len(axes[0]) * len(axes[1]) > MAX_FIELD_NODES):
            raise ValueError("PSF field dimensions must match axes with at most 25 field nodes")
        coordinates = {name: axis.tolist() for name, axis in zip(
            ("field_y_mm", "field_x_mm"), axes, strict=True)}
    if not np.allclose(kernels.sum(axis=(-2, -1)), 1, rtol=0, atol=1e-10):
        raise ValueError("Each PSF must sum to one; no automatic normalization or invented tails")
    return {
        **declarations, "wavelength_nm": wave.tolist(), "values": kernels.tolist(),
        "sample_pitch_um": float(pitch), "source": _source(psf["source"], "psf.source"),
        **coordinates,
    }


def validate_psf_coverage(psf, full, origin, shape, pitch_um):
    """Check native geometry/pitch/field coverage without allocating an image.

    This is a compatibility check for a standalone-validated PSF; it does not
    replace ``validate_psf`` or validate the kernel samples. Returns None.
    """
    full = _integer_pair(full, "full_sensor_shape", 2, 1_000_000)
    origin = _integer_pair(origin, "crop_origin", 0, 1_000_000)
    shape = _integer_pair(shape, "shape", 1, 1_000_000)
    if any(offset + side > native
           for offset, side, native in zip(origin, shape, full, strict=True)):
        raise ValueError("Scene native crop must lie inside full_sensor_shape")
    if (isinstance(pitch_um, (bool, np.bool_)) or not isinstance(pitch_um, (int, float, np.number))
            or np.iscomplexobj(pitch_um) or not np.isfinite(pitch_um)
            or not .1 <= pitch_um <= 1000 or psf["sample_pitch_um"] != pitch_um):
        raise ValueError("PSF and sensor pixel pitch must match exactly; no spatial resampling")
    if psf["schema"] != "field_intensity_psf_v1":
        return
    axes = [_field_axis(psf[name], f"psf.{name}") for name in ("field_y_mm", "field_x_mm")]
    for axis, side, offset, native in zip(axes, shape, origin, full, strict=True):
        bounds = (np.array([0, side - 1]) + offset - (native - 1) / 2) * pitch_um / 1000
        if axis[0] > bounds[0] or axis[-1] < bounds[-1]:
            raise ValueError("PSF field grid must cover every native source pixel center; "
                             "no extrapolation")


def _field_sampling(psf, rows, cols, full, origin):
    """Validate coverage and bound FFT work before allocating interpolation planes."""
    from scipy.fft import next_fast_len

    validate_psf_coverage(psf, full, origin, (rows, cols), psf["sample_pitch_um"])
    axes = [np.asarray(psf[name]) for name in ("field_y_mm", "field_x_mm")]
    positions = [(np.arange(side) + offset - (native - 1) / 2)
                 * psf["sample_pitch_um"] / 1000
                 for side, offset, native in zip((rows, cols), origin, full, strict=True)]
    shape = np.shape(psf["values"])
    fft_shape = [next_fast_len(side + kernel - 1, real=True)
                 for side, kernel in zip((rows, cols), shape[-2:], strict=True)]
    fft_samples = int(np.prod(fft_shape))
    convolutions = shape[0] * shape[1] * shape[2]
    # Three transforms per node (input, kernel and inverse), conservative even
    # when the implementation can skip zero weights or empty source planes.
    work = int(np.ceil(3 * convolutions * fft_samples * np.log2(max(fft_samples, 2))))
    if work > MAX_FIELD_FFT_WORK:
        raise ValueError("Field PSF exceeds the 500000000 FFT work-unit budget")
    return {"axes": axes, "positions": positions, "convolutions": convolutions,
            "fft_work_units": work,
            "working_array_bytes_estimate": fft_samples * 8 * 12
            + rows * cols * 8 + sum(len(a) * len(p) * 8
                                    for a, p in zip(axes, positions, strict=True))}


def _parse(request: Mapping[str, Any], *, rate_only: bool = False) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ValueError("request must be a mapping")
    required = {
        "wavelength_nm", "qe", "pixel_pitch_um", "exposure_s", "readout", "lens",
        "sensor_angular_response", "scene", "psf",
    }
    if "native_pixel_response" in request:
        required -= {"qe", "sensor_angular_response"}
        required.add("native_pixel_response")
    if "charge_collection" in request:
        required.add("charge_collection")
    optional = {"temperature_c", "frames", "seed", "noise_enabled", "isp_profile",
                "field_radiometry_mode"}
    if rate_only:
        required.remove("exposure_s")
        optional -= {"frames", "isp_profile"}
    if not required <= set(request) or set(request) - required - optional:
        raise ValueError("Unknown or missing spatial runtime fields; no output_size resampling")
    field_radiometry_mode = request.get("field_radiometry_mode")
    if ("field_radiometry_mode" in request
            and (type(field_radiometry_mode) is not str
                 or field_radiometry_mode != SOURCE_RI_MODE)):
        raise ValueError("field_radiometry_mode supports only source_position_ri_v1 opt-in")
    scene_fields = {
        "schema", "wavelength_nm", "unit", "reference_plane", "spectral_irradiance",
        "sampling", "included_effects", "source",
    }
    if isinstance(request["scene"], Mapping) and (
        "source_subcell_irradiance" in request["scene"]
        or "source_subcell_evidence" in request["scene"]
    ):
        scene_fields.update({"source_subcell_irradiance", "source_subcell_evidence"})
    scene = _keys(request["scene"], scene_fields, "scene")
    if (scene["schema"] != "spectral_ideal_image_v1" or scene["unit"] != "W/m^2/nm"
            or scene["reference_plane"] != "sensor_ideal_image_before_psf_ri"):
        raise ValueError("Scene requires spectral ideal-image irradiance in W/m^2/nm")
    if scene["included_effects"] != ["on_axis_lens_throughput"]:
        raise ValueError("Scene must include only on_axis_lens_throughput; do not double effects")
    source = _source(scene["source"], "scene.source")
    image = _numeric_array(scene["spectral_irradiance"], 3, "scene.spectral_irradiance")
    rows, cols, waves = image.shape
    subcells = None
    subcell_evidence = None
    if "source_subcell_irradiance" in scene:
        subcell_evidence = _keys(scene["source_subcell_evidence"], {
            "model", "grid_side", "radiance_sha256", "source_render_id",
            "projection_job_id",
        }, "scene.source_subcell_evidence")
        if (subcell_evidence["model"] != subcell_optics.TRANSFER
                or type(subcell_evidence["grid_side"]) is not int
                or not isinstance(subcell_evidence["radiance_sha256"], str)
                or len(subcell_evidence["radiance_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in
                       subcell_evidence["radiance_sha256"])
                or not isinstance(subcell_evidence["source_render_id"], str)
                or (subcell_evidence["projection_job_id"] is not None and not isinstance(
                    subcell_evidence["projection_job_id"], str))):
            raise ValueError("Source-subcell transfer evidence is incomplete")
        subcells = subcell_optics.validate_subcells(
            scene["source_subcell_irradiance"], image,
            subcell_evidence["grid_side"],
        )
    if not 2 <= waves <= 16 or not 8 <= rows <= 512 or not 8 <= cols <= 512:
        raise ValueError("Scene requires 8..512 rows/columns and 2..16 wavelengths")
    if rows % 2 or cols % 2:
        raise ValueError("Scene row/column counts must be even for Bayer readout")
    _same_wave(scene["wavelength_nm"], request["wavelength_nm"], "scene.wavelength_nm")
    sampling_fields = {"kind", "full_sensor_shape", "crop_origin", "pixel_pitch_um"}
    if isinstance(scene["sampling"], Mapping) and "capture_crop" in scene["sampling"]:
        sampling_fields.add("capture_crop")
    sampling = _keys(scene["sampling"], sampling_fields, "scene.sampling")
    if sampling["kind"] != "native_sensor":
        raise ValueError("Scene requires native_sensor sampling, not a representative raster")
    full = _integer_pair(sampling["full_sensor_shape"], "full_sensor_shape", 2, 1_000_000)
    origin = _integer_pair(sampling["crop_origin"], "crop_origin", 0, 1_000_000)
    if origin[0] + rows > full[0] or origin[1] + cols > full[1]:
        raise ValueError("Scene native crop must lie inside full_sensor_shape")
    if (isinstance(sampling["pixel_pitch_um"], (bool, np.bool_))
            or sampling["pixel_pitch_um"] != request["pixel_pitch_um"]):
        raise ValueError("Scene and sensor pixel pitch must match exactly")
    psf = validate_psf(request["psf"])
    _same_wave(psf["wavelength_nm"], request["wavelength_nm"], "psf.wavelength_nm")
    if (isinstance(psf["sample_pitch_um"], (bool, np.bool_))
            or psf["sample_pitch_um"] != request["pixel_pitch_um"]):
        raise ValueError("PSF and sensor pixel pitch must match exactly; no spatial resampling")
    field_psf = psf["schema"] == "field_intensity_psf_v1"
    kernels = _numeric_array(psf["values"], 5 if field_psf else 3, "psf.values")
    if kernels.shape[0] != waves:
        raise ValueError("PSF wavelength dimension must match scene spectral dimension")
    capture_crop = sampling.get("capture_crop")
    if "capture_crop" in sampling:
        capture_crop = _keys(capture_crop, {"crop_origin", "shape"},
                             "scene.sampling.capture_crop")
        capture_origin = _integer_pair(capture_crop["crop_origin"],
                                       "capture_crop.crop_origin", 0, 1_000_000)
        capture_size = _integer_pair(capture_crop["shape"], "capture_crop.shape", 8, 512)
        if any(side % 2 for side in capture_size):
            raise ValueError("capture_crop.shape requires even native Bayer sides")
        if any(start + side > native for start, side, native in
               zip(capture_origin, capture_size, full, strict=True)):
            raise ValueError("capture_crop must lie inside full_sensor_shape")
        offsets = None
        if "charge_collection" in request:
            from .charge_collection import validate_collection

            offsets = validate_collection(request["charge_collection"])["destination_offsets_rc"]
        kernel_radius = [side // 2 + (1 if subcells is not None else 0)
                         for side in kernels.shape[-2:]]
        required_support = []
        axes = zip(origin, (rows, cols), capture_origin, capture_size, full,
                   kernel_radius, strict=True)
        for axis, geometry_axis in enumerate(axes):
            source_start, source_side, capture_start, capture_side, native, radius = geometry_axis
            if offsets is None:
                required_start = max(0, capture_start - radius)
                required_end = min(native, capture_start + capture_side + radius)
            else:
                # Destination = generation source + carrier offset. Generation
                # at a required source needs optical PSF support around it.
                carrier_axis = [item[axis] for item in offsets]
                generation_start = max(0, capture_start - max(carrier_axis))
                generation_end = min(native, capture_start + capture_side - min(carrier_axis))
                required_start = min(capture_start, max(0, generation_start - radius))
                required_end = max(capture_start + capture_side,
                                   min(native, generation_end + radius))
            required_support.append((required_start, required_end))
            if source_start > required_start or source_start + source_side < required_end:
                kind = "combined PSF and carrier" if offsets is not None else "PSF"
                raise ValueError(f"Source spectral image is missing the {kind} halo required "
                                 "around capture_crop inside the physical sensor")
    else:
        capture_origin, capture_size = origin, (rows, cols)
    psf_source = psf["source"]
    input_bytes = image.nbytes + kernels.nbytes
    if subcells is not None:
        input_bytes += subcells.nbytes
    source_ri_extra_bytes = (2 * (subcells.size if subcells is not None else image.size) * 8
                             if field_radiometry_mode == SOURCE_RI_MODE else 0)
    if input_bytes + source_ri_extra_bytes > MAX_INPUT_BYTES:
        raise ValueError("Scene and PSF exceed the 64 MiB decoded input budget")
    field_sampling = _field_sampling(psf, rows, cols, full, origin) if field_psf else None
    subcell_resources = None
    if subcells is not None:
        subcell_resources = subcell_optics.preflight(
            image.shape, subcell_evidence["grid_side"], kernels,
            sampling["pixel_pitch_um"], full, origin,
            field_axes=field_sampling["axes"] if field_sampling else None,
        )
    # Reuse the existing validated spectral/angle/QE/readout contract. The
    # brightest per-band image value is a conservative sampler-budget bound.
    legacy = {
        key: value for key, value in request.items()
        if key not in {"scene", "psf", "isp_profile", "field_radiometry_mode"}
    }
    legacy["spectral_irradiance_w_m2_nm"] = (
        np.maximum(image.max(axis=(0, 1)), subcells.max(axis=(0, 1, 2, 3))).tolist()
        if subcells is not None else image.max(axis=(0, 1)).tolist()
    )
    legacy["output_size"] = [rows, cols]
    if "native_pixel_response" in request:
        legacy.update(full_sensor_shape=list(full), crop_origin=list(origin))
    if rate_only:
        # Validate optical and readout domains without inventing a one-second
        # exposure. Actual temporal charge is bounded after row integration.
        legacy["exposure_s"] = 0
        legacy["frames"] = 1
    inputs = ResponseDBRuntime()._parse(
        legacy, _validated_charge_collection_support=(
            "capture_crop" in sampling and "charge_collection" in request))
    if inputs["wave"].size != waves:
        raise ValueError("Scene spectral dimension must match wavelength_nm")
    if "field_reference_radius_mm" in inputs:
        radius_mm = _native_radius_mm((rows, cols), full, origin, inputs["pitch"])
        if radius_mm.max() > inputs["field_reference_radius_mm"]:
            raise ValueError("Lens physical radial response must cover every native receiver "
                             "pixel center; no extrapolation")
    if field_radiometry_mode == SOURCE_RI_MODE:
        side = subcells.shape[2] if subcells is not None else 1
        extent = []
        for start, count, native in zip(origin, (rows, cols), full, strict=True):
            first = start + 0.5 / side - 0.5 - (native - 1) / 2
            last = start + count - 0.5 / side - 0.5 - (native - 1) / 2
            extent.append(max(abs(first), abs(last)))
        furthest = np.hypot(*extent)
        field_limit = (inputs["field_reference_radius_mm"] * 1000 / inputs["pitch"]
                       if "field_reference_radius_mm" in inputs else
                       np.hypot((full[0] - 1) / 2, (full[1] - 1) / 2))
        if furthest > field_limit * (inputs["field"][-1] + 1e-12):
            raise ValueError("Source-position RI does not cover every native source sample")
    if rows * cols * inputs["frames"] > MAX_FRAME_PIXELS:
        raise ValueError("Spatial run exceeds 1048576 total frame-pixel limit")
    with np.errstate(over="ignore", invalid="ignore"):
        bound = (np.trapezoid(inputs["photon_density"], x=inputs["wave"])
                 * (inputs["pitch"] * 1e-6) ** 2 * inputs["exposure"]
                 * inputs["ri"].max() * (kernels.shape[1] * kernels.shape[2] if field_psf else 1)
                 * (len(inputs["charge_collection"]["destination_offsets_rc"])
                    if "charge_collection" in inputs else 1)
                 + inputs["readout"]["dark_current_e_per_s"] * inputs["exposure"])
    if not np.isfinite(bound) or bound > 1e13:
        raise ValueError("Conservative incident-charge bound exceeds 1e13 electrons per pixel")
    from camera_platform.isp import default_profile, validate_profile

    profile = request.get("isp_profile", default_profile())
    profile = validate_profile(profile)
    inputs.update(
        image=image, kernels=kernels, full=full, origin=origin, profile=profile,
        source_subcells=subcells, source_subcell_evidence=subcell_evidence,
        source_subcell_resources=subcell_resources,
        source_ri_extra_bytes=source_ri_extra_bytes,
        field_radiometry_mode=field_radiometry_mode,
        scene_source=source, psf_source=psf_source,
        psf_contract={key: value for key, value in psf.items() if key != "values"},
        field_psf=field_sampling,
        geometry={"kind": "native_sensor", "full_sensor_shape": list(full),
                  "crop_origin": list(origin)},
    )
    if "capture_crop" in sampling:
        start = tuple(c - s for c, s in zip(capture_origin, origin, strict=True))
        inputs.update(
            source_support={"kind": "native_sensor", "full_sensor_shape": list(full),
                            "crop_origin": list(origin), "shape": [rows, cols]},
            capture_support={"kind": "native_sensor", "full_sensor_shape": list(full),
                             "crop_origin": list(capture_origin), "shape": list(capture_size)},
            capture_slice=(slice(start[0], start[0] + capture_size[0]),
                           slice(start[1], start[1] + capture_size[1])),
            psf_halo_pixels=[side // 2 for side in kernels.shape[-2:]],
            required_source_support={"kind": "native_sensor",
                                     "full_sensor_shape": list(full),
                                     "crop_origin": [bound[0] for bound in required_support],
                                     "shape": [bound[1] - bound[0]
                                               for bound in required_support]},
            partial_source_collection=("charge_collection" in inputs and
                                       (origin != (0, 0) or (rows, cols) != full)),
            size=capture_size, origin=capture_origin,
            geometry={"kind": "native_sensor", "full_sensor_shape": list(full),
                      "crop_origin": list(capture_origin)},
        )
    return inputs


def _convolve(image: np.ndarray, kernels: np.ndarray, pitch_um: float):
    from scipy.signal import fftconvolve

    rows, cols, waves = image.shape
    ky, kx = kernels.shape[1] // 2, kernels.shape[2] // 2
    blurred = np.empty_like(image)
    input_flux, full_flux, cropped_flux, roundoff = [], [], [], []
    area = (pitch_um * 1e-6) ** 2
    for band in range(waves):
        full = fftconvolve(image[..., band], kernels[band], mode="full")
        # FFT roundoff can introduce tiny negative values. Reject material
        # negativity, and retain the magnitude of the documented roundoff trim.
        tolerance = max(float(image[..., band].max()), 1e-300) * 1e-12
        if not np.isfinite(full).all() or float(full.min()) < -tolerance:
            raise RuntimeError("PSF convolution produced nonphysical or nonfinite irradiance")
        roundoff.append(float(-np.minimum(full, 0).sum()) * area)
        full = np.maximum(full, 0)
        blurred[..., band] = full[ky:ky + rows, kx:kx + cols]
        input_flux.append(float(image[..., band].sum()) * area)
        full_flux.append(float(full.sum()) * area)
        cropped_flux.append(float(blurred[..., band].sum()) * area)
    diagnostics = {
        "unit": "W/nm", "input_spectral_flux": input_flux,
        "full_convolution_spectral_flux": full_flux,
        "retained_crop_spectral_flux": cropped_flux,
        "outside_crop_spectral_flux": np.maximum(np.array(full_flux) - cropped_flux, 0).tolist(),
        "fft_negative_roundoff_trimmed_spectral_flux": roundoff,
        "scope": "Before relative illumination; discrete unit-sum PSF redistribution",
    }
    if not np.allclose(full_flux, input_flux, rtol=1e-9, atol=0):
        raise RuntimeError("Full PSF convolution failed spectral flux conservation")
    return blurred, diagnostics


def _linear_field_weights(axis: np.ndarray, positions: np.ndarray) -> np.ndarray:
    """Piecewise-linear nodal basis; caller has checked exact domain coverage."""
    lower = np.clip(np.searchsorted(axis, positions, side="right") - 1, 0, len(axis) - 2)
    fraction = (positions - axis[lower]) / (axis[lower + 1] - axis[lower])
    weights = np.zeros((len(axis), len(positions)))
    weights[lower, np.arange(len(positions))] = 1 - fraction
    weights[lower + 1, np.arange(len(positions))] = fraction
    return weights


def _convolve_field(inputs: dict[str, Any]):
    from scipy.signal import fftconvolve

    image = inputs.get("_psf_source_image", inputs["image"])
    kernels, sampling = inputs["kernels"], inputs["field_psf"]
    rows, cols, waves = image.shape
    ky, kx = kernels.shape[-2] // 2, kernels.shape[-1] // 2
    y_weights, x_weights = [_linear_field_weights(axis, positions)
                           for axis, positions in zip(sampling["axes"],
                                                      sampling["positions"], strict=True)]
    blurred = np.empty_like(image)
    area = (inputs["pitch"] * 1e-6) ** 2
    input_flux, full_flux, cropped_flux, roundoff = [], [], [], []
    for band in range(waves):
        full = np.zeros((rows + 2 * ky, cols + 2 * kx))
        trimmed = 0.0
        for y_node in range(kernels.shape[1]):
            for x_node in range(kernels.shape[2]):
                # Assign each SOURCE's flux across field nodes first. Blending
                # convolution outputs at receiver coordinates is a different
                # operator and generally does not conserve source flux.
                weighted = image[..., band] * y_weights[y_node, :, None] * x_weights[x_node]
                if not np.any(weighted):
                    continue
                contribution = fftconvolve(weighted, kernels[band, y_node, x_node], mode="full")
                tolerance = max(float(weighted.max()), 1e-300) * 1e-12
                if (not np.isfinite(contribution).all()
                        or float(contribution.min()) < -tolerance):
                    raise RuntimeError("Field PSF convolution produced nonphysical irradiance")
                trimmed += float(-np.minimum(contribution, 0).sum()) * area
                full += np.maximum(contribution, 0)
        if not np.isfinite(full).all():
            raise RuntimeError("Field PSF sum produced nonfinite irradiance")
        blurred[..., band] = full[ky:ky + rows, kx:kx + cols]
        input_flux.append(float(image[..., band].sum()) * area)
        full_flux.append(float(full.sum()) * area)
        cropped_flux.append(float(blurred[..., band].sum()) * area)
        roundoff.append(trimmed)
    if (not np.isfinite([input_flux, full_flux, cropped_flux, roundoff]).all()
            or not np.allclose(full_flux, input_flux, rtol=1e-9, atol=0)):
        raise RuntimeError("Full field PSF convolution failed finite spectral flux conservation")
    return blurred, {
        "unit": "W/nm", "input_spectral_flux": input_flux,
        "full_convolution_spectral_flux": full_flux,
        "retained_crop_spectral_flux": cropped_flux,
        "outside_crop_spectral_flux": np.maximum(np.array(full_flux) - cropped_flux, 0).tolist(),
        "fft_negative_roundoff_trimmed_spectral_flux": roundoff,
        "scope": "Before relative illumination; source-position bilinear unit-sum "
                 "PSF redistribution",
    }


def _source_position_ri_input(inputs):
    """Weight ideal-image source samples by the Lens field RI, without altering evidence.

    The source's 2-D field coordinate is the native pixel center or the
    retained fractional subcell midpoint. The supplied PSF remains conditional
    and unit sum; no receiver aperture or second relative illumination follows.
    """
    subcells = inputs["source_subcells"]
    source = subcells if subcells is not None else inputs["image"]
    rows, cols = source.shape[:2]
    side = source.shape[2] if subcells is not None else 1
    source_origin = (inputs["source_support"]["crop_origin"] if "source_support" in inputs
                     else inputs["origin"])
    offsets = (np.arange(side, dtype=float) + 0.5) / side - 0.5
    yy = (source_origin[0] + np.arange(rows)[:, None] + offsets[None, :]
          - (inputs["full"][0] - 1) / 2)
    xx = (source_origin[1] + np.arange(cols)[:, None] + offsets[None, :]
          - (inputs["full"][1] - 1) / 2)
    radius = np.hypot(yy[:, None, :, None], xx[None, :, None, :])
    if "field_reference_radius_mm" in inputs:
        radius *= inputs["pitch"] / (1000 * inputs["field_reference_radius_mm"])
    else:
        radius /= np.hypot((inputs["full"][0] - 1) / 2,
                           (inputs["full"][1] - 1) / 2)
    if not np.isfinite(radius).all() or np.any(radius > inputs["field"][-1] + 1e-12):
        raise ValueError("Source-position RI does not cover every native source sample")
    radius = np.minimum(radius, inputs["field"][-1])
    if subcells is None:
        radius = radius[:, :, 0, 0]
    ri = np.stack([np.interp(radius, inputs["field"], row) for row in inputs["ri"]], axis=-1)
    weighted = source * ri
    if not np.isfinite(weighted).all() or np.any(weighted < 0):
        raise ValueError("Source-position RI produced nonphysical spectral irradiance")
    area = (inputs["pitch"] * 1e-6) ** 2 / side**2
    axes = tuple(range(source.ndim - 1))
    before = source.sum(axis=axes) * area
    after = weighted.sum(axis=axes) * area
    inputs["_source_ri_map"] = ri
    return weighted, {
        "relative_illumination_application": SOURCE_RI_MODE,
        "relative_illumination_reference": "native_source_sample_before_psf",
        "pre_ri_source_spectral_flux": before.tolist(),
        "post_ri_source_spectral_flux": after.tolist(),
        "pre_ri_source_spectral_flux_unit": "W/nm",
        "post_ri_source_spectral_flux_unit": "W/nm",
        "source_ri_min_per_wavelength": ri.min(axis=axes).tolist(),
        "source_ri_max_per_wavelength": ri.max(axis=axes).tolist(),
    }


def _source_ri_summary(inputs):
    if inputs["field_radiometry_mode"] != SOURCE_RI_MODE:
        return {}
    return {
        "field_radiometry_mode": SOURCE_RI_MODE,
        "relative_illumination_application": {
            "input_plane": "sensor_ideal_image_after_on_axis_lens_throughput_before_psf_ri",
            "source_coordinates": ("native_fractional_subcell_midpoint" if
                                   inputs["source_subcells"] is not None else
                                   "native_source_pixel_center"),
            "source_relative_illumination_map_axes": (
                ["source_row", "source_col", "subrow", "subcol", "wavelength"]
                if inputs["source_subcells"] is not None else
                ["source_row", "source_col", "wavelength"]
            ),
            "application": "Lens spectral field RI before conditional unit-sum PSF",
            "receiver_relative_illumination_map": "diagnostic_only_not_multiplied",
            "directional_qe": "receiver_position_separable_approximation",
            "physical_validation": "not_established",
        },
    }


def _convolve_inputs(inputs):
    source_image = inputs["image"]
    source_subcells = inputs["source_subcells"]
    source_origin = (inputs["source_support"]["crop_origin"] if "source_support" in inputs
                     else inputs["origin"])
    ri_evidence = None
    if inputs["field_radiometry_mode"] == SOURCE_RI_MODE:
        weighted, ri_evidence = _source_position_ri_input(inputs)
        if source_subcells is None:
            source_image = weighted
        else:
            source_subcells = weighted
    if source_subcells is not None:
        blurred, flux = subcell_optics.transfer(
            source_subcells, inputs["kernels"], inputs["pitch"],
            inputs["full"], source_origin,
            field_axes=inputs["field_psf"]["axes"] if inputs["field_psf"] else None,
        )
    elif inputs["field_psf"] is None:
        # Keep the original v1 numerical operation order and FFT path intact.
        blurred, flux = _convolve(source_image, inputs["kernels"], inputs["pitch"])
    else:
        blurred, flux = _convolve_field({**inputs, "_psf_source_image": source_image})
    if ri_evidence is not None:
        flux.update(ri_evidence)
        flux["scope"] = (
            "After source-position relative illumination; conditional unit-sum PSF "
            "redistribution before receiver angular QE"
        )
    return blurred, flux


def _convolve_capture(inputs):
    """Keep source-support convolution distinct from the receiver crop."""
    support_blurred, flux = _convolve_inputs(inputs)
    if "capture_slice" not in inputs:
        return support_blurred, flux, {}
    y, x = inputs["capture_slice"]
    capture_blurred = support_blurred[y, x].copy()
    area = (inputs["pitch"] * 1e-6) ** 2
    capture_flux = capture_blurred.sum(axis=(0, 1)) * area
    flux.update(
        retained_crop_geometry="source_support",
        source_support=inputs["source_support"],
        capture_crop=inputs["capture_support"],
        capture_spectral_flux=capture_flux.tolist(),
        capture_flux_scope=("after_source_ri_and_psf_before_receiver_angular_qe"
                            if inputs["field_radiometry_mode"] == SOURCE_RI_MODE else
                            "after_source_support_psf_before_receiver_ri"),
        outside_capture_spectral_flux=(
            np.maximum(np.asarray(flux["full_convolution_spectral_flux"]) - capture_flux, 0)
        ).tolist(),
    )
    return capture_blurred, flux, {
        "source_support_blurred_spectral_irradiance": support_blurred,
    }


def _capture_support_summary(inputs, arrays):
    if "capture_slice" not in inputs:
        return {}
    source_arrays = {"ideal_spectral_irradiance",
                     "source_relative_illumination_map",
                     "source_support_blurred_spectral_irradiance",
                     "source_support_collected_electrons",
                     "source_support_collected_photoelectron_rate",
                     "collection_generated", "collection_intrinsic_loss",
                     "collection_off_sensor_loss", "collection_outside_support_collected",
                     "collection_generated_rate", "collection_intrinsic_loss_rate",
                     "collection_off_sensor_loss_rate",
                     "collection_outside_support_collected_rate"}
    capture_arrays = {
        "blurred_spectral_irradiance", "sensor_spectral_irradiance",
        "relative_illumination_map", "radius_map", "cfa_channel_index",
        "channel_photoelectron_rate", "photoelectron_rate", "channel_electrons",
        "mean_electrons", "channel_reference_signal_dn", "reference_dn",
        "raw_frames", "rgb", "native_site_index", "sensor_radius_mm",
        "response_radius_map", "raw_temporal_variance_dn2",
        "black_subtracted_normalized_frames", "demosaiced_rgb_frames",
        "white_balanced_rgb_frames", "ccm_rgb_frames", "rgb_frames",
    }
    return {
        "optical_support": {
            "source_support": inputs["source_support"],
            "capture_crop": inputs["capture_support"],
            "required_source_support": inputs["required_source_support"],
            "psf_halo_pixels": inputs["psf_halo_pixels"],
            **({"carrier_destination_offset_bounds_rc": {
                "minimum": [min(offset[axis] for offset in
                                inputs["charge_collection"]["destination_offsets_rc"])
                            for axis in range(2)],
                "maximum": [max(offset[axis] for offset in
                                inputs["charge_collection"]["destination_offsets_rc"])
                            for axis in range(2)]}}
               if "charge_collection" in inputs else {}),
            "boundary": "zero_outside_source_support; halo complete within full_sensor_shape",
            "charge_collection": (
                ("full_sensor_generation_then_receiver_crop"
                 if inputs["source_support"]["crop_origin"] == [0, 0]
                 and inputs["source_support"]["shape"] == list(inputs["full"])
                 else "partial_source_combined_halo_then_receiver_crop")
                if "charge_collection" in inputs else "not_requested"),
            **({"source_support_collected_scope": (
                "from_supplied_sources_only; capture_crop has complete incoming "
                "combined PSF and carrier support; support boundary may be incomplete"),
                "carrier_outside_support": "tracked separately from physical off-sensor loss"}
               if inputs.get("partial_source_collection") else {}),
        },
        "array_geometry": {
            name: ("source_support" if name in source_arrays else "capture_crop")
            for name in arrays if name in source_arrays | capture_arrays
        },
    }


def _psf_summary(inputs):
    summary = {"shape": list(inputs["kernels"].shape), "normalization": "unit_sum",
               "spatial_model": "locally_shift_invariant", "centering": "array_center",
               "boundary": "zero_outside_supplied_image", "sample_pitch_um": inputs["pitch"],
               "sampling_model": "pixel_integrated_optical_impulse"}
    if inputs["field_psf"] is not None:
        summary.update({key: value for key, value in inputs["psf_contract"].items()
                        if key not in {"source", "wavelength_nm"}})
        summary.update(
            source_position_bounds_mm=[[float(p[0]), float(p[-1])]
                                       for p in inputs["field_psf"]["positions"]],
            field_axis_order=["wavelength", "field_y", "field_x", "kernel_y", "kernel_x"],
            coordinate_formula="(index + crop_origin - (full_sensor_shape - 1) / 2) "
                               "* pixel_pitch_um / 1000",
            node_convolutions=inputs["field_psf"]["convolutions"],
            fft_work_units=inputs["field_psf"]["fft_work_units"],
        )
    return summary


def _field_psf_arrays(inputs):
    if inputs["field_psf"] is None:
        return {}
    return {"psf_field_y_mm": inputs["field_psf"]["axes"][0].copy(),
            "psf_field_x_mm": inputs["field_psf"]["axes"][1].copy(),
            "psf_source_y_mm": inputs["field_psf"]["positions"][0].copy(),
            "psf_source_x_mm": inputs["field_psf"]["positions"][1].copy()}


def _native_radius_mm(shape, full, origin, pitch):
    yy = (np.arange(shape[0])[:, None] + origin[0] - (full[0] - 1) / 2) * pitch / 1000
    xx = (np.arange(shape[1])[None, :] + origin[1] - (full[1] - 1) / 2) * pitch / 1000
    return np.hypot(yy, xx)


def _radial_response_arrays(inputs):
    if "field_reference_radius_mm" not in inputs:
        return {}
    radius_mm = _native_radius_mm(inputs["size"], inputs["full"], inputs["origin"], inputs["pitch"])
    return {"sensor_radius_mm": radius_mm,
            "response_radius_map": radius_mm / inputs["field_reference_radius_mm"]}


def _radial_response_summary(inputs):
    if "field_reference_radius_mm" not in inputs:
        return {}
    return {"radial_response_sampling": {
        "field_reference_radius_mm": inputs["field_reference_radius_mm"],
        "field_radius_definition": "physical image height / field_reference_radius_mm",
        "radius_map_definition": "native radius / full sensor outermost pixel-center radius",
        "response_radius_map_definition": "sensor_radius_mm / field_reference_radius_mm",
        "coverage": "every native receiver pixel center; no extrapolation",
            "application": (
                "source-position RI before PSF; output-position angular QE remains separate"
                if inputs["field_radiometry_mode"] == SOURCE_RI_MODE else
                "output-position RI and angular QE, separate from source-position PSF"
            ),
    }}


def _native_response_arrays(inputs):
    if "native_pixel_response" not in inputs:
        return {}
    from .native_pixel_response import site_index

    sensor, lens = inputs["native_pixel_response"], inputs["directional_response"]
    arrays = {
        ("native_generation_efficiency" if "charge_collection" in inputs else "native_eqe"):
            np.asarray(sensor["values"], dtype=float),
        "native_wavelength_nm": np.asarray(sensor["wavelength_nm"], dtype=float),
        "native_theta_deg": np.asarray(sensor["theta_deg"], dtype=float),
        "native_phi_deg": np.asarray(sensor["phi_deg"], dtype=float),
        "native_site_index_tile": np.asarray(sensor["site_index_tile"], dtype=np.uint8),
        "native_site_index": site_index(inputs["size"], inputs["origin"]),
        "lens_direction_xyz": np.asarray(lens["directions_xyz"], dtype=float),
        "lens_direction_power_fraction": np.asarray(lens["conditional_weights"], dtype=float),
        "lens_on_axis_rotation_phi_deg": np.asarray(lens["on_axis_rotation_phi_deg"], dtype=float),
        "lens_direction_field_radius": np.asarray(lens["field_radius"], dtype=float),
    }
    if "charge_collection" in inputs:
        collection = inputs["charge_collection"]
        arrays.update(collection_probabilities=np.asarray(collection["probabilities"], dtype=float),
                      collection_destination_offsets_rc=np.asarray(
                          collection["destination_offsets_rc"], dtype=np.int16))
    return arrays


def _native_response_units(inputs):
    if "native_pixel_response" not in inputs:
        return {}
    return {("native_generation_efficiency" if "charge_collection" in inputs else "native_eqe"):
            "electron/incident_pixel_photon", "native_wavelength_nm": "nm",
            "native_theta_deg": "deg", "native_phi_deg": "deg",
            "native_site_index_tile": "index", "native_site_index": "index",
            "lens_direction_xyz": "1", "lens_direction_power_fraction": "1",
            "lens_on_axis_rotation_phi_deg": "deg", "lens_direction_field_radius": "1",
            **({"collection_probabilities": "1", "collection_destination_offsets_rc": "pixel"}
               if "charge_collection" in inputs else {})}


def _native_response_summary(inputs):
    if "native_pixel_response" not in inputs:
        return {}
    from .registry import digest

    sensor, lens = inputs["native_pixel_response"], inputs["directional_response"]
    summary = {"native_pixel_response": {
        "sha256": digest(sensor), "directional_response_sha256": digest(lens),
        "site_names": sensor["site_names"], "site_index_tile": sensor["site_index_tile"],
        "reference_plane": sensor["reference_plane"],
        "direction_convention": sensor["direction_convention"],
        "response_basis": sensor["response_basis"], "source": sensor["source"],
        "values_axis_order": sensor["values_axis_order"],
        "field_interpolation": "mixture of neighboring field-node joint power distributions",
        "azimuth_application": "rotate reference-field ray directions by native atan2(y,x)",
        "on_axis_integration": "explicit factorized annulus azimuth quadrature from Lens",
        "optical_effects": "absolute CFA/microlens/absorption/collection response applied once",
        "spatial_transfer": "independent_site; no inter-site redistribution kernel",
        "angular_integration_uncertainty": "unknown",
        "weighted_sample_budget": inputs["native_direction_work"],
    }}
    if "charge_collection" in inputs:
        collection = inputs["charge_collection"]
        summary["native_pixel_response"].update(
            optical_effects="CFA/microlens/absorption generation applied once; excludes collection",
            spatial_transfer="source-native-site generation followed by stochastic carrier routing")
        summary["charge_collection"] = {
            "sha256": digest(collection), "source": collection["source"],
            "schema": collection["schema"], "input_plane": collection["input_plane"],
            "output_plane": collection["output_plane"],
            "temporal_model": collection["temporal_model"], "statistics": collection["statistics"],
            "angular_assumption": collection["angular_assumption"], "regime": collection["regime"],
            "boundary": collection["boundary"],
            "crop_support": (
                ("full_sensor_generation_then_receiver_crop"
                 if inputs["source_support"]["crop_origin"] == [0, 0]
                 and inputs["source_support"]["shape"] == list(inputs["full"])
                 else "partial_source_combined_halo_then_receiver_crop")
                if "capture_slice" in inputs else "full_sensor_only"),
            "channel_tensor": "actual_destination_site_only; other hypothetical sites are zero",
            "loss_array_coordinates": "source_pixel; collected array uses destination_pixel",
            "order": "source site, wavelength routing, spectral sum, "
                     "receiver-row exposure, Poisson",
            "physical_validation": "not_established", "uncertainty": "unknown",
            "limitations": [
                "No deterministic analog charge splitting or interpixel covariance.",
                "No transport delay, high-injection field change or general TCAD solver.",
                "Direction-independent depth-marginal collection is an explicit assumption.",
                ("Partial-source support predictions are exact only within the "
                 "verified receiver crop; source-support edge collection may omit "
                 "incoming carriers and outward carriers are tracked separately."
                 if inputs.get("partial_source_collection") else
                 "Partial-sensor source support requires explicit combined optical "
                 "and carrier halo verification.")],
            **inputs.get("_collection_diagnostics", {}),
        }
    return summary


def _collection_arrays(inputs, *, rate=False):
    suffix = "_rate" if rate else ""
    return {f"collection_{key}{suffix}": value
            for key, value in inputs.get("_collection_arrays", {}).items()}


def _collection_units(inputs, *, rate=False):
    return {key: "electron/pixel/s" if rate else "electron/pixel"
            for key in _collection_arrays(inputs, rate=rate)}


def _native_effective_qe(inputs, band, response_radius, field_phi):
    """Average absolute EQE against joint power without building a full optical tensor."""
    from .lens_direction_response import iter_direction_samples
    from .native_pixel_response import evaluate

    radius, phi = response_radius.ravel(), field_phi.ravel()
    result = np.zeros((len(radius), 4), dtype=float)
    axes = inputs["field"]
    basis = _linear_field_weights(axes, radius)
    for node in range(len(axes)):
        selected = np.flatnonzero(basis[node] > 0)
        if not len(selected):
            continue
        accumulated = np.zeros((len(selected), 4), dtype=float)
        for directions, power in iter_direction_samples(
                inputs["directional_response"], band, node, chunk_size=4096):
            theta = np.degrees(np.arccos(np.clip(directions[:, 2], -1, 1)))
            ray_phi = np.degrees(np.arctan2(directions[:, 1], directions[:, 0]))
            # Floating inverse cosine roundoff at the declared boundary is not
            # permission to extrapolate the model domain.
            theta = np.minimum(theta, inputs["native_pixel_response"]["theta_deg"][-1])
            block = max(1, 131072 // len(theta))
            for start in range(0, len(selected), block):
                targets = selected[start:start + block]
                qe = evaluate(inputs["native_pixel_response"], float(inputs["wave"][band]),
                              theta[None, :], ray_phi[None, :] + phi[targets, None])
                accumulated[start:start + block] += np.einsum("pns,n->ps", qe, power)
        result[selected] += accumulated * basis[node, selected, None]
    return result.reshape((*response_radius.shape, 4))


def _spectral_sensor(inputs: dict[str, Any], blurred: np.ndarray, *, rate_only: bool = False):
    rows, cols = inputs["size"]
    yy = np.arange(rows)[:, None] + inputs["origin"][0] - (inputs["full"][0] - 1) / 2
    xx = np.arange(cols)[None, :] + inputs["origin"][1] - (inputs["full"][1] - 1) / 2
    radius = np.hypot(yy, xx) / np.hypot(
        (inputs["full"][0] - 1) / 2, (inputs["full"][1] - 1) / 2
    )
    response_radius = (_radial_response_arrays(inputs)["response_radius_map"]
                       if "field_reference_radius_mm" in inputs else radius)
    if "native_pixel_response" in inputs:
        from .native_pixel_response import site_index

        ri_map = np.stack([np.interp(response_radius, inputs["field"], row)
                           for row in inputs["ri"]], axis=-1)
        sensor_image = (blurred if inputs["field_radiometry_mode"] == SOURCE_RI_MODE
                        else blurred * ri_map)
        field_phi = np.degrees(np.arctan2(yy, xx))
        channels = np.zeros((rows, cols, 4), dtype=float)
        cfa = site_index(inputs["size"], inputs["origin"])
        collection = inputs.get("charge_collection")
        collection_totals = {key: np.zeros((rows, cols))
                             for key in (("generated", "intrinsic_loss", "off_sensor_loss")
                                         + (("outside_support_collected",)
                                            if inputs.get("partial_source_collection") else ())
                                         if collection is not None else ())}
        previous_collection = None
        previous = None
        with np.errstate(over="ignore", invalid="ignore"):
            for band, wavelength in enumerate(inputs["wave"]):
                density = sensor_image[..., band] * wavelength * 1e-9 / (PLANCK_J_S * LIGHT_M_S)
                value = density[..., None] * _native_effective_qe(
                    inputs, band, response_radius, field_phi)
                if collection is not None:
                    from .charge_collection import transport

                    source = np.take_along_axis(value, cfa[..., None], axis=-1)[..., 0]
                    source = source * (inputs["pitch"] * 1e-6) ** 2
                    routed = transport(source, float(wavelength), collection,
                                       origin=inputs["origin"],
                                       full_sensor_shape=inputs["full"])["arrays"]
                    # CFA belongs to the photon-generation site. Collection into
                    # a different well must never apply its CFA response again.
                    value = np.zeros_like(value)
                    np.put_along_axis(value, cfa[..., None], routed["collected"][..., None], -1)
                    if previous_collection is not None:
                        for key in collection_totals:
                            collection_totals[key] += (previous_collection[key] + routed[key]) * (
                                (wavelength - inputs["wave"][band-1]) / 2)
                    previous_collection = routed
                if previous is not None:
                    channels += (previous + value) * ((wavelength - inputs["wave"][band-1]) / 2)
                previous = value
            if collection is None:
                channels *= (inputs["pitch"] * 1e-6) ** 2
            if not rate_only:
                channels *= inputs["exposure"]
            if collection is not None:
                scale = 1 if rate_only else inputs["exposure"]
                inputs["_collection_arrays"] = {key: array * scale
                                                for key, array in collection_totals.items()}
                totals = {key: float(array.sum())
                          for key, array in inputs["_collection_arrays"].items()}
                totals["collected"] = float(channels.sum())
                residual = (totals["generated"] - totals["collected"]
                            - totals["intrinsic_loss"] - totals["off_sensor_loss"]
                            - totals.get("outside_support_collected", 0))
                if (not np.isfinite(list(totals.values())).all()
                        or abs(residual) > max(1e-12, totals["generated"] * 1e-12)):
                    raise ValueError(
                        "Charge collection conservation exceeds floating-point tolerance")
                inputs["_collection_diagnostics"] = {
                    "totals": totals, "totals_unit": "electron/s" if rate_only else "electron",
                    "conservation_residual": residual,
                }
        if (not np.isfinite(sensor_image).all() or not np.isfinite(channels).all()
                or np.any(channels < 0) or (not rate_only and channels.max()
                    + inputs["readout"]["dark_current_e_per_s"] * inputs["exposure"] > 1e13)):
            raise ValueError("Native site charge exceeds finite bounded sampler domain")
        mean = np.take_along_axis(channels, cfa[..., None], axis=-1)[..., 0]
        return radius, ri_map, sensor_image, channels, cfa, mean
    response = np.stack([
        np.interp(inputs["theta"], inputs["sensor_theta"], row)
        for row in inputs["response"].reshape(-1, inputs["sensor_theta"].size)
    ]).reshape(3, inputs["wave"].size, inputs["theta"].size)
    field_qe = np.einsum("wfa,cwa->fcw", inputs["weights"], response) * inputs["qe"]
    ri_map = np.stack([
        np.interp(response_radius, inputs["field"], row) for row in inputs["ri"]
    ], axis=-1)
    with np.errstate(over="ignore", invalid="ignore"):
        sensor_image = (blurred if inputs["field_radiometry_mode"] == SOURCE_RI_MODE
                        else blurred * ri_map)
        photon_density = sensor_image * (inputs["wave"] * 1e-9) / (PLANCK_J_S * LIGHT_M_S)
        channels = np.stack([
            np.trapezoid(photon_density * np.stack([
                np.interp(response_radius, inputs["field"], field_qe[:, channel, band])
                for band in range(inputs["wave"].size)
            ], axis=-1), x=inputs["wave"], axis=-1)
            for channel in range(3)
        ], axis=-1) * (inputs["pitch"] * 1e-6) ** 2
        if not rate_only:
            channels *= inputs["exposure"]
    if (not np.isfinite(sensor_image).all() or not np.isfinite(channels).all()
            or np.any(channels < 0)
            or (not rate_only and channels.max() + inputs["readout"]["dark_current_e_per_s"]
                * inputs["exposure"] > 1e13)):
        raise ValueError("Spatial charge exceeds finite bounded sampler domain")
    cfa = np.array([[0, 1], [1, 2]])[
        (np.arange(rows)[:, None] + inputs["origin"][0]) % 2,
        (np.arange(cols)[None, :] + inputs["origin"][1]) % 2,
    ]
    mean = np.take_along_axis(channels, cfa[..., None], axis=-1)[..., 0]
    return radius, ri_map, sensor_image, channels, cfa, mean


def _spectral_sensor_capture(inputs, blurred, support_arrays, *, rate_only=False):
    """Route on the entire native source grid before selecting receiver pixels."""
    if "capture_slice" not in inputs or "charge_collection" not in inputs:
        return _spectral_sensor(inputs, blurred, rate_only=rate_only)
    source_inputs = dict(inputs)
    source_inputs["size"] = inputs["image"].shape[:2]
    source_inputs["origin"] = tuple(inputs["source_support"]["crop_origin"])
    source_blurred = support_arrays["source_support_blurred_spectral_irradiance"]
    source_outputs = _spectral_sensor(source_inputs, source_blurred, rate_only=rate_only)
    inputs["_collection_arrays"] = source_inputs["_collection_arrays"]
    diagnostics = dict(source_inputs["_collection_diagnostics"])
    y, x = inputs["capture_slice"]
    source_collected = source_outputs[-1]
    capture_collected = float(source_collected[y, x].sum())
    diagnostics["capture_collected"] = capture_collected
    diagnostics["outside_capture_collected"] = (
        diagnostics["totals"]["collected"] - capture_collected
        + diagnostics["totals"].get("outside_support_collected", 0))
    if inputs.get("partial_source_collection"):
        diagnostics["collection_scope"] = "supplied_source_support_only"
        diagnostics["capture_crop_incoming_halo"] = "complete_within_full_sensor_shape"
        diagnostics["outside_capture_collected_scope"] = (
            "from_supplied_sources_only; missing sources beyond support are unmodeled")
    inputs["_collection_diagnostics"] = diagnostics
    support_arrays[("source_support_collected_photoelectron_rate" if rate_only
                    else "source_support_collected_electrons")] = source_collected.copy()
    return tuple(array[y, x].copy() for array in source_outputs)


def photoelectron_rate(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an ideal spectral image and return rates, without any readout.

    Accepts the spatial request fields except exposure_s, frames and isp_profile.
    The collected photoelectron rate excludes dark current, exposure, gain,
    clipping and random/fixed noise. No synthetic exposure is used to bound it.
    This linear optical operator is reusable at immutable timeline knots.
    """
    inputs = _parse(request, rate_only=True)
    blurred, flux, support_arrays = _convolve_capture(inputs)
    radius, ri_map, sensor_image, channels, cfa, mean = _spectral_sensor_capture(
        inputs, blurred, support_arrays, rate_only=True
    )
    arrays = {
        "blurred_spectral_irradiance": blurred, "sensor_spectral_irradiance": sensor_image,
        "relative_illumination_map": ri_map, "radius_map": radius, "cfa_channel_index": cfa,
        **({"source_relative_illumination_map": inputs["_source_ri_map"]}
           if inputs["field_radiometry_mode"] == SOURCE_RI_MODE else {}),
        "channel_photoelectron_rate": channels, "photoelectron_rate": mean,
        **support_arrays,
        **_radial_response_arrays(inputs),
        **_native_response_arrays(inputs),
        **_collection_arrays(inputs, rate=True),
    }
    summary = {
        "method_and_version": ("native_spectral_photoelectron_rate_source_ri_v1" if
                               inputs["field_radiometry_mode"] == SOURCE_RI_MODE else
                               "native_spectral_photoelectron_rate_v1"),
        "geometry": inputs["geometry"], "wavelength_nm": inputs["wave"].tolist(),
        "pixel_pitch_um": inputs["pitch"], "flux_diagnostics": flux,
        **_source_ri_summary(inputs),
        **_radial_response_summary(inputs),
        **_native_response_summary(inputs),
        "included_effects": [x for x in with_sensor_effects(EFFECTS, inputs)
                             if x not in {"mean_dark_charge", "readout_clipping"}],
        "array_units": {
            "blurred_spectral_irradiance": "W/m^2/nm", "sensor_spectral_irradiance": "W/m^2/nm",
            "relative_illumination_map": "1", "radius_map": "1", "cfa_channel_index": "index",
            **({"source_relative_illumination_map": "1"}
               if inputs["field_radiometry_mode"] == SOURCE_RI_MODE else {}),
            "channel_photoelectron_rate": "electron/pixel/s",
            "photoelectron_rate": "electron/pixel/s",
            **({"source_support_blurred_spectral_irradiance": "W/m^2/nm"}
               if support_arrays else {}),
            **({"source_support_collected_photoelectron_rate": "electron/pixel/s"}
               if "source_support_collected_photoelectron_rate" in support_arrays else {}),
            **_native_response_units(inputs),
            **_collection_units(inputs, rate=True),
        },
    }
    if "field_reference_radius_mm" in inputs:
        summary["array_units"].update(sensor_radius_mm="mm", response_radius_map="1")
    if inputs["field_psf"] is not None:
        arrays.update(psf_intensity=inputs["kernels"].copy(), **_field_psf_arrays(inputs))
        summary["psf"] = _psf_summary(inputs)
        summary["psf_source"] = inputs["psf_source"]
        summary["array_units"].update(psf_intensity="1",
                                      **{name: "mm" for name in _field_psf_arrays(inputs)})
        summary["limitations"] = [
            "Supplied PSFs interpolate at discrete source pixel centers without recentering.",
            "Kernel offsets are preserved by wavelength and field; their optical interpretation "
            "depends on the supplied source evidence.",
            "No separate general distortion-map warp or depth reprojection is implemented.",
            ("Source-position RI precedes PSF, but Lens direction/QE remains a "
             "receiver-position separable approximation."
             if inputs["field_radiometry_mode"] == SOURCE_RI_MODE else
             "PSF precedes output-position RI and angular QE as a separable approximation."),
            "Source-cell integration and optical response between field nodes are unverified.",
            "No physical accuracy or quantified model uncertainty has been established.",
        ]
    summary.update(_capture_support_summary(inputs, arrays))
    return {"summary": summary, "arrays": arrays}


def _capture(inputs: dict[str, Any], mean: np.ndarray):
    from camera_platform.precomputed_raw import capture_precomputed_raw

    phase = {(0, 0): "rggb", (0, 1): "grbg", (1, 0): "gbrg", (1, 1): "bggr"}[
        inputs["origin"][0] % 2, inputs["origin"][1] % 2
    ]
    return capture_precomputed_raw(
        mean,
        wavelength_nm=inputs["wave"],
        pixel_pitch_m=inputs["pitch"] * 1e-6,
        exposure_s=inputs["exposure"],
        cfa_phase=phase,
        readout=inputs["readout"],
        noise_enabled=inputs["noise_enabled"],
        frames=inputs["frames"],
        seed=inputs["seed"],
    )


class SpatialSceneRuntime:
    """RuntimePlugin with supplied, sampled image irradiance and intensity PSFs."""

    plugin_id = PLUGIN_ID

    def describe_capabilities(self) -> dict[str, Any]:
        import importlib.util

        collection = charge_collection_capabilities()
        collection.update(
            geometry="full or verified partial native source with receiver capture_crop",
            crop_halo="capture ROI plus carrier-source offsets and PSF radius "
                      "within full sensor; partial support requires capture_crop",
        )
        return {
            "plugin_id": self.plugin_id, "role": "runtime", "version": "1.0",
            "mode": "spatial_scene", "available": importlib.util.find_spec("pyisetcam") is not None,
            "supports": ["spectral_ideal_image", "supplied_intensity_psf", "native_sensor_crop",
                         "source_support_psf_halo_for_capture_crop",
                         "radial_spectral_angle_qe", "digital_raw", "configured_software_isp",
                         "supplied_field_psf", "bilinear_source_position_psf",
                         SOURCE_RI_MODE],
            "unsupported": ["rgb_to_spectrum", "scene_geometry", "occlusion", "mtf_inversion",
                            "depth_dependent_psf", "general_distortion_map", "depth_reprojection",
                            "motion", "hdr",
                            "azimuth_dependent_ri_or_angular_qe", "polarization",
                            "physical_validation"],
            "limits": {"max_side": 512, "max_frames": 16, "max_wavelengths": 16,
                       "max_psf_side": 65, "max_frame_pixels": MAX_FRAME_PIXELS,
                       "max_input_bytes": MAX_INPUT_BYTES, "max_field_nodes": MAX_FIELD_NODES,
                       "max_field_fft_work_units": MAX_FIELD_FFT_WORK},
            "charge_collection": collection,
            "physical_validation": "not_established",
        }

    def validate_request(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            _parse(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return [_issue("request", str(exc))]
        return []

    def _checked(self, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return _parse(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise RuntimeRequestError([_issue("request", str(exc))]) from exc

    def estimate_resources(self, request: Mapping[str, Any]) -> dict[str, Any]:
        inputs = self._checked(request)
        pixels = int(np.prod(inputs["size"]))
        estimate = {
            "cpu_threads": 1, "gpu_required": False, "execution": "in_process",
            "working_array_bytes_estimate": int(
                inputs["image"].nbytes * 8 + pixels * (inputs["frames"] * 160 + 128)
                + inputs["source_ri_extra_bytes"]
            ),
            "estimate_kind": "array_budget_not_measured_peak", "frames": inputs["frames"],
            "output_size": list(inputs["size"]), "wavelengths": int(inputs["wave"].size),
        }
        if inputs["field_psf"] is not None:
            estimate["working_array_bytes_estimate"] += (
                inputs["field_psf"]["working_array_bytes_estimate"] + inputs["kernels"].nbytes)
            estimate["field_psf_node_convolutions"] = inputs["field_psf"]["convolutions"]
            estimate["field_psf_fft_work_units"] = inputs["field_psf"]["fft_work_units"]
        if inputs["source_subcell_resources"] is not None:
            estimate["working_array_bytes_estimate"] += (
                inputs["source_subcell_resources"]["retained_source_bytes"]
                + inputs["source_subcell_resources"]["full_output_bytes"]
            )
            estimate["source_subcell_transfer"] = inputs["source_subcell_resources"]
        if inputs["field_radiometry_mode"] == SOURCE_RI_MODE:
            estimate["source_position_ri_extra_bytes"] = inputs["source_ri_extra_bytes"]
        if "native_pixel_response" in inputs:
            estimate["working_array_bytes_estimate"] += (
                64 * 1024 * 1024
                + 3 * sum(a.nbytes for a in _native_response_arrays(inputs).values())
            )
            estimate["native_direction_weighted_samples"] = inputs["native_direction_work"]
        return estimate

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        from camera_platform.isp import process_raw

        inputs = self._checked(request)
        blurred, flux, support_arrays = _convolve_capture(inputs)
        radius, ri_map, sensor_image, channels, cfa, mean = _spectral_sensor_capture(
            inputs, blurred, support_arrays)
        raw, reference, capture_metadata = _capture(inputs, mean)
        readout = inputs["readout"]
        raw_format = {
            "cfa": "RGGB", "bit_depth": readout["bit_depth"],
            "black_level_dn": readout["black_level_dn"], "response_type": "linear",
            "packing": "unpacked", "layout": "bayer", "hdr_streams": 1,
        }
        processed = process_raw(raw, raw_format, inputs["profile"], geometry=inputs["geometry"])
        dark = readout["dark_current_e_per_s"] * inputs["exposure"]
        white, black = 2 ** readout["bit_depth"] - 1, readout["black_level_dn"]
        scale = (white - black) * readout["gain"] / readout["full_well_e"]
        channel_reference = np.clip(
            black + np.minimum(channels + dark, readout["full_well_e"]) * scale, 0, white
        ) - np.clip(black + min(dark, readout["full_well_e"]) * scale, 0, white)
        native_channel_mean = [
            float(channel_reference[..., channel][cfa == channel].mean())
            for channel in range(len(inputs["channels"]))
        ]
        capacity = min(readout["full_well_e"], readout["full_well_e"] / readout["gain"])
        variance = raw.astype(float).var(axis=0, ddof=1) if inputs["frames"] > 1 else None
        noise = float(np.sqrt(variance.mean())) if variance is not None else None
        saturation = max(max(item["saturation"].values()) for item in capture_metadata)
        snr = (float(20 * np.log10(mean.mean() * scale / noise))
               if noise is not None and noise > 0 and mean.mean() > 0 and saturation <= 0.001
               else None)
        conditions = {
            "exposure_s": {"value": inputs["exposure"], "unit": "s"},
            "gain": {"value": readout["gain"], "unit": "1"},
        }
        if inputs["temperature"] is not None:
            conditions["temperature_c"] = {"value": inputs["temperature"], "unit": "degC"}
        summary = {
            "schema_version": "1.0", "plugin_id": self.plugin_id, "source_kind": "simulated",
            "physical_validation": "not_established", "uncertainty": {"kind": "unknown"},
            "engine": (
                "bounded_subcell_integrated_impulse_interpolation + pyisetcam digital "
                "readout + camera_platform.isp"
                if inputs["source_subcells"] is not None else
                "scipy.signal.fftconvolve + pyisetcam digital readout + camera_platform.isp"
            ),
            "channels": inputs["channels"], "wavelength_nm": inputs["wave"].tolist(),
            "output_size": list(inputs["size"]), "frames": inputs["frames"],
            "seed_sequence": [inputs["seed"] + index for index in range(inputs["frames"])],
            "spatial_sampling": {**inputs["geometry"], "pixel_pitch_um": inputs["pitch"],
                                 "contiguous_native_sensor_capture": True},
            "geometry": inputs["geometry"],
            **_source_ri_summary(inputs),
            **_radial_response_summary(inputs),
            **_native_response_summary(inputs),
            "scene_source": inputs["scene_source"], "psf_source": inputs["psf_source"],
            "psf": _psf_summary(inputs),
            **({"source_subcell_transfer": {
                **inputs["source_subcell_evidence"],
                **inputs["source_subcell_resources"],
                "physical_validation": "not_established",
                "continuous_position_psf_accuracy": "unknown",
            }} if inputs["source_subcells"] is not None else {}),
            "flux_diagnostics": flux, "readout": readout, "raw_format": raw_format,
            "conditions": conditions, "included_effects": with_sensor_effects(EFFECTS, inputs),
            "channel_electron_mean": ([float(channels[..., site][cfa == site].mean())
                                       for site in range(4)] if "charge_collection" in inputs
                                      else channels.mean(axis=(0, 1)).tolist()),
            "native_cfa_channel_mean_dn": native_channel_mean,
            "linear_unsaturated": bool((channels + dark < capacity).all()),
            "saturation_fraction": saturation, "capture_metadata": capture_metadata,
            "raw_temporal_noise_dn": noise, "raw_temporal_snr_db": snr,
            "temporal_snr_definition": (
                "mean expected photo-signal DN / sqrt(mean temporal variance)"
            ),
            "isp": processed["summary"],
            "prediction": {
                "metric_id": "raw.channel_mean_dn", "unit": "DN", "values": native_channel_mean,
                "axes": [{"name": "channel", "unit": "1", "values": inputs["channels"].copy()}],
                "reference_plane": "sensor_raw",
                "included_effects": with_sensor_effects(EFFECTS, inputs),
                "conditions": conditions, "source_kind": "simulated",
                "method_and_version": (
                    "continuous_expected_native_cfa_dark_corrected_source_ri_v1"
                    if inputs["field_radiometry_mode"] == SOURCE_RI_MODE else
                    "continuous_expected_native_cfa_dark_corrected_v1"
                ),
                "uncertainty": {"kind": "unknown"},
            },
            "observation_note": (
                "Spatial scenes have no radial point-response prediction; use native ROIs."
            ),
            "integration": "trapezoidal over wavelength_nm; spectral density per nm",
            "limitations": [
                "Supplied pixel-grid irradiance is an ideal-image input, not rendered 3D radiance.",
                "On-axis aperture/transmission throughput is already in the input; applied once.",
                ("Retained midpoint subcell flux is transferred separately with a bilinear "
                 "interpolation of receiver-integrated PSF impulses; the continuous-position "
                 "lens response and quadrature error remain unknown."
                 if inputs["source_subcells"] is not None else
                 "Each input cell's flux is assigned to its center as discrete source quadrature."),
                "Receiving-pixel aperture is integrated in the PSF once; no second box filter.",
                ("Recovery of aliased source content is not established."
                 if inputs["field_radiometry_mode"] == SOURCE_RI_MODE else
                 "Source-cell integration and recovery of aliased content are not established."),
                ("One PSF per wavelength across this crop; no spatially varying aberrations."
                 if inputs["field_psf"] is None else
                 "Supplied field kernels interpolate at source subcell positions; intermediate "
                 "field accuracy and depth dependence are not established."
                 if inputs["source_subcells"] is not None else
                 "Supplied field kernels interpolate at source centers; intermediate field "
                 "accuracy and depth dependence are not established."),
                "Supplied kernel offsets are preserved without recentering; interpreting them "
                "as geometric or chromatic lens shifts requires source evidence.",
                "No separate general distortion-map warp or depth reprojection is implemented.",
                ("Source-position RI precedes PSF, while Lens direction and sensor angular "
                 "QE remain a receiver-position separable approximation."
                 if inputs["field_radiometry_mode"] == SOURCE_RI_MODE else
                 "PSF precedes output-position RI and angular QE as a separable approximation."),
                ("Source RI and receiver angular QE interpolate independently over native field."
                 if inputs["field_radiometry_mode"] == SOURCE_RI_MODE else
                 "RI and angle-averaged QE interpolate independently over native sensor radius."),
                ("Source-support exterior is dark; the declared PSF halo covers the capture "
                 "crop inside the physical sensor. ISP demosaic outside the RAW crop is not "
                 "included."
                 if "capture_slice" in inputs else
                 "Image exterior is dark; full-convolution energy outside the crop is discarded."),
                "PSF support is supplied as complete unit mass; omitted optical tails are unknown.",
                "Polarization, azimuth-dependent RI/QE, crosstalk, PRNU, DSNU "
                "and motion are absent.",
                "Temperature is recorded; supplied QE/readout must already correspond to it.",
                "No physical accuracy or quantified model uncertainty has been established.",
                "Prediction averages continuous clipped dark/black-corrected expectations at CFA "
                "native samples; ADC rounding and random RAW realizations are separate artifacts.",
            ],
        }
        arrays = {
            "ideal_spectral_irradiance": inputs["image"].copy(),
            **({"source_subcell_irradiance": inputs["source_subcells"].copy()}
               if inputs["source_subcells"] is not None else {}),
            "psf_intensity": inputs["kernels"].copy(),
            "blurred_spectral_irradiance": blurred, "sensor_spectral_irradiance": sensor_image,
            **support_arrays,
            "relative_illumination_map": ri_map, "radius_map": radius, "cfa_channel_index": cfa,
            **({"source_relative_illumination_map": inputs["_source_ri_map"]}
               if inputs["field_radiometry_mode"] == SOURCE_RI_MODE else {}),
            "channel_electrons": channels, "mean_electrons": mean,
            "channel_reference_signal_dn": channel_reference, "reference_dn": reference,
            "raw_frames": raw, **processed["arrays"],
            **_field_psf_arrays(inputs),
            **_radial_response_arrays(inputs),
            **_native_response_arrays(inputs),
            **_collection_arrays(inputs),
        }
        if variance is not None:
            arrays["raw_temporal_variance_dn2"] = variance
        summary["array_units"] = {
            "ideal_spectral_irradiance": "W/m^2/nm", "psf_intensity": "1",
            "source_subcell_irradiance": "W/m^2/nm",
            "blurred_spectral_irradiance": "W/m^2/nm", "sensor_spectral_irradiance": "W/m^2/nm",
            **({"source_support_blurred_spectral_irradiance": "W/m^2/nm"}
               if support_arrays else {}),
            **({"source_support_collected_electrons": "electron/pixel"}
               if "source_support_collected_electrons" in support_arrays else {}),
            "relative_illumination_map": "1", "radius_map": "1", "cfa_channel_index": "index",
            **({"source_relative_illumination_map": "1"}
               if inputs["field_radiometry_mode"] == SOURCE_RI_MODE else {}),
            "channel_electrons": "electron/pixel", "mean_electrons": "electron/pixel",
            "channel_reference_signal_dn": "DN", "reference_dn": "DN", "raw_frames": "DN",
            "raw_temporal_variance_dn2": "DN^2", **processed["summary"]["array_units"],
            **{name: "mm" for name in _field_psf_arrays(inputs)},
            **_native_response_units(inputs),
            **_collection_units(inputs),
        }
        summary["array_units"] = {key: value for key, value in summary["array_units"].items()
                                  if key in arrays}
        summary.update(_capture_support_summary(inputs, arrays))
        if "native_pixel_response" in inputs:
            summary["limitations"] = [
                text for text in summary["limitations"]
                if not text.startswith("Polarization, azimuth-dependent")
            ] + [
                "Absolute R/Gr/Gb/B EQE and jointly weighted ray azimuth are retained; "
                "polarization, inter-site redistribution, PRNU, DSNU and motion are absent.",
                "Finite annulus quadrature and independent field-node mixture have "
                "unquantified angular/field interpolation error.",
            ]
        if "charge_collection" in inputs:
            summary["limitations"] = [text for text in summary["limitations"]
                                      if not text.startswith("Absolute R/Gr/Gb/B EQE")]
            summary["limitations"].extend(summary["charge_collection"]["limitations"])
        if "field_reference_radius_mm" in inputs:
            summary["array_units"].update(sensor_radius_mm="mm", response_radius_map="1")
        return {"summary": summary, "arrays": arrays}


def request_template() -> dict[str, Any]:
    """An authored spectral step chart, explicitly not measured data or a lens claim."""
    wave = [450, 500, 550, 600, 650]
    image = np.full((64, 64, 5), 1e-5)
    image[:, 32:, :] *= 4
    kernel = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], dtype=float) / 16
    return {
        "wavelength_nm": wave, "qe": [[0.3] * 5, [0.5] * 5, [0.2] * 5],
        "pixel_pitch_um": 4.0, "exposure_s": 0.004, "temperature_c": 25,
        "readout": {"full_well_e": 10000, "read_noise_e": 2, "dark_current_e_per_s": 0,
                    "gain": 1, "bit_depth": 12, "black_level_dn": 64},
        "lens": {"wavelength_nm": wave, "field_radius": [0, 1], "theta_deg": [0, 20],
                 "angular_weights": [[[1, 0], [0, 1]]] * 5,
                 "relative_illumination": [[1, 0.8]] * 5},
        "sensor_angular_response": {
            "theta_deg": [0, 20],
            "relative_response": [[[1, 0.5]] * 5, [[1, 1]] * 5, [[1, 0.8]] * 5],
        },
        "scene": {
            "schema": "spectral_ideal_image_v1", "wavelength_nm": wave, "unit": "W/m^2/nm",
            "reference_plane": "sensor_ideal_image_before_psf_ri",
            "spectral_irradiance": image.tolist(), "included_effects": ["on_axis_lens_throughput"],
            "sampling": {"kind": "native_sensor", "full_sensor_shape": [64, 64],
                         "crop_origin": [0, 0], "pixel_pitch_um": 4.0},
            "source": {"kind": "estimated", "description": "Authored spectral step-chart fixture"},
        },
        "psf": {
            "schema": "intensity_psf_v1", "wavelength_nm": wave,
            "values": np.repeat(kernel[None], 5, axis=0).tolist(), "sample_pitch_um": 4.0,
            "normalization": "unit_sum", "centering": "array_center",
            "spatial_model": "locally_shift_invariant", "boundary": "zero_outside_supplied_image",
            "sampling_model": "pixel_integrated_optical_impulse",
            "source": {"kind": "estimated",
                       "description": "Assumed binomial receiving-pixel hit probabilities fixture"},
        },
        "frames": 4, "seed": 223, "noise_enabled": True,
    }
