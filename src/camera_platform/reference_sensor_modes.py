"""Bounded Reference readout comparisons with explicit same-site geometry.

These are behavioral studies, not IMX462 product modes. Same-site 2x2-plane
binning of ordinary Bayer does not make a regular half-resolution Bayer array:
site centroids alternate one and three native pitches. Rendering uses those
centroids, not an ordinary Bayer demosaic on a falsely uniform lattice.
"""

from __future__ import annotations

from copy import deepcopy

import numpy as np
from scipy.ndimage import map_coordinates, minimum_filter

from pyisetcam.bayer_demosaic import demosaic_bayer

from . import dcg, dcg_electrical, electrical

SITES = ((0, 0), (0, 1), (1, 0), (1, 1))
SITE_NAMES = ("R", "Gr", "Gb", "B")


def same_site_sum(values):
    """Sum disjoint 2x2 groups within each CFA plane, retaining leading axes."""
    values = np.asarray(values)
    if values.ndim < 2 or min(values.shape[-2:]) < 4 or any(n % 4 for n in values.shape[-2:]):
        raise ValueError("Same-site binning requires H,W divisible by four.")
    if values.dtype.kind not in "iufb":
        raise ValueError("Binning requires real numeric values.")
    values = values.astype(np.float64, copy=False)
    shape = (*values.shape[:-2], values.shape[-2] // 2, values.shape[-1] // 2)
    out = np.empty(shape, dtype=np.result_type(values.dtype, np.float64))
    for r, c in SITES:
        plane = values[..., r::2, c::2]
        out[..., r::2, c::2] = (
            plane[..., 0::2, 0::2]
            + plane[..., 0::2, 1::2]
            + plane[..., 1::2, 0::2]
            + plane[..., 1::2, 1::2]
        )
    return out


def binned_lsc(native_gain):
    """Aggregate the frozen uniform response, then invert it exactly once."""
    gains = np.asarray(native_gain, dtype=float)
    if gains.ndim != 2 or not np.isfinite(gains).all() or np.any(gains <= 0):
        raise ValueError("Native LSC gains must be finite positive HxW values.")
    return 4.0 / same_site_sum(1.0 / gains)


def binning_geometry(shape):
    h, w = shape
    same_site_sum(np.zeros((h, w)))
    yy, xx = np.indices((h // 2, w // 2))
    # Output mosaic stores alternate CFA sites; physical centroid is not 2*i.
    cy, cx = 4 * (yy // 2) + yy % 2 + 1, 4 * (xx // 2) + xx % 2 + 1
    return {
        "cfa": "RGGB",
        "native_shape": [h, w],
        "output_shape": [h // 2, w // 2],
        "contributing_pixels_per_output_site": 4,
        "sample_centers_native_row": cy.astype(float),
        "sample_centers_native_col": cx.astype(float),
        "display_centers_native_row": 0.5 + 2 * np.arange(h // 2),
        "display_centers_native_col": 0.5 + 2 * np.arange(w // 2),
        "site_plane_centroid_offsets_native": {
            name: [r + 1, c + 1] for name, (r, c) in zip(SITE_NAMES, SITES, strict=True)
        },
        "site_plane_stride_native": 4,
        "reconstruction": "bilinear_each_site_at_actual_centroids_to_regular_2_pitch_grid",
        "scope": "disjoint same-color four-pixel sums; not adjacent Bayer 2x2 average",
    }


def _inputs(mean_rate, readout, frames, exposure_s, light_scale, electrical_model, geometry):
    rate = np.asarray(mean_rate, dtype=float)
    if (
        rate.ndim != 2
        or min(rate.shape) < 4
        or any(v % 4 for v in rate.shape)
        or rate.size > 1_000_000
        or not np.isfinite(rate).all()
        or np.any(rate < 0)
    ):
        raise ValueError(
            "Mean rate requires bounded finite nonnegative RGGB HxW divisible by four."
        )
    if type(frames) is not int or not 1 <= frames <= 32:
        raise ValueError("frames must be 1..32.")
    if (
        not np.isfinite(exposure_s)
        or exposure_s <= 0
        or not np.isfinite(light_scale)
        or light_scale < 0
    ):
        raise ValueError("Exposure must be positive and light scale nonnegative.")
    model = (
        electrical.default_electrical_model(readout)
        if electrical_model is None
        else electrical.validate_electrical_model(electrical_model)
    )
    geometry = geometry or {"full_sensor_shape": list(rate.shape), "crop_origin": [0, 0]}
    full, origin = electrical._geometry(geometry, rate.shape)
    if any(v % 2 for v in origin):
        raise ValueError("The Reference adapter requires an RGGB-even crop origin.")
    return (
        rate * float(exposure_s) * float(light_scale),
        model,
        {
            "full_sensor_shape": full,
            "crop_origin": origin,
        },
    )


def _transfer(model, gain):
    adc = model["adc"]
    white = 2 ** adc["bit_depth"] - 1
    return (
        (
            model["conversion_gain_v_per_e"]
            * gain
            * white
            / adc["full_scale_v"]
            * model["digital_gain"]
        ),
        float(adc["black_level_dn"]),
        float(white),
    )


def _payload(electrons, variance, valid, well, adc, metadata, raw=None):
    result = {
        "electron_frames": np.asarray(electrons),
        "variance_e2": np.asarray(variance),
        "valid_frames": np.asarray(valid, dtype=bool),
        "well_saturated_frames": np.asarray(well, dtype=bool),
        "adc_saturated_frames": np.asarray(adc, dtype=bool),
        "metadata": metadata,
    }
    if raw is not None:
        result["raw_frames"] = np.asarray(raw)
    return result


def binning_modes(
    mean_rate,
    readout,
    *,
    exposure_s=0.004,
    light_scale=1.0,
    frames=8,
    seed=100,
    native_raw=None,
    electrical_model=None,
    charge_options=None,
    geometry=None,
    noise_enabled=True,
    temperature_c=25.0,
):
    """Acquire native/charge modes and derive digital sums from identical native RAW.

    ``electron_frames`` is signed photoelectron estimate after expected dark removal.
    Binned frames contain SUMMED electrons; rendering divides by four. Variance is
    the pre-clipping shot/read model, not a conditional variance at saturation.
    """
    photo, model, geometry = _inputs(
        mean_rate, readout, frames, exposure_s, light_scale, electrical_model, geometry
    )
    gain = float(readout["gain"])
    electrical.validate_capture_conditions(model, exposure_s, gain, temperature_c)
    full, origin = geometry["full_sensor_shape"], geometry["crop_origin"]
    prnu, dark_rate, _ = electrical._patterns(model, full, origin, photo.shape, temperature_c)
    dark = dark_rate * exposure_s
    total_mean = photo * prnu + dark
    slope, black, white = _transfer(model, gain)
    read_var = (
        model["read_noise_e"] ** 2
        + (model["post_gain_noise_v"] / (model["conversion_gain_v_per_e"] * gain)) ** 2
    )
    variance = (total_mean + read_var) if noise_enabled else np.zeros_like(photo)
    native = []
    native_well, native_adc = [], []
    supplied = native_raw is not None
    if supplied:
        native_raw = np.asarray(native_raw)
        if (
            native_raw.shape != (frames, *photo.shape)
            or native_raw.dtype.kind not in "iu"
            or np.any(native_raw < 0)
            or np.any(native_raw > white)
        ):
            raise ValueError("Preserved native RAW must match frames, shape and ADC domain.")
        native = native_raw.copy()
        # Existing RAW does not reveal true well clipping after read noise.
        native_well = native.astype(float) >= black + model["full_well_e"] * slope
        native_adc = (native <= 0) | (native >= white)
    else:
        for index in range(frames):
            captured = electrical.capture_electrons(
                photo,
                exposure_s,
                model,
                analog_gain=gain,
                temperature_c=temperature_c,
                geometry=geometry,
                noise_enabled=noise_enabled,
                seed=(seed + index) % 2**32,
            )
            arrays = captured["arrays"]
            native.append(arrays["raw_dn"])
            native_well.append(arrays["well_saturated_mask"])
            native_adc.append(
                arrays["adc_high_mask"]
                | arrays["adc_low_mask"]
                | arrays["digital_high_mask"]
                | arrays["digital_low_mask"]
            )
        native, native_well, native_adc = map(np.asarray, (native, native_well, native_adc))
    native_e = (native.astype(float) - black) / slope - dark
    raw_valid = (native > 0) & (native < white) & ~native_well
    common = {
        "exposure_s": exposure_s,
        "light_scale": light_scale,
        "cfa": "RGGB",
        "noise_enabled": noise_enabled,
        "temperature_c": temperature_c,
        "fixed_pattern": deepcopy(model["fixed_pattern"]),
        "dark_correction": "subtract_expected_dark_after_ADC; dark shot noise retained",
        "variance_scope": "pre-clipping shot/read model; no conditional or quantization variance",
        "physical_validation": "not_established",
        "product_mode": False,
        "native_geometry": geometry,
    }
    native_meta = {
        **common,
        "id": "native",
        "units": "electron/native_pixel",
        "contribution_count": 1,
        "raw_reused": supplied,
        "seed": seed,
        "well_mask_basis": "RAW-boundary proxy" if supplied else "simulator charge truth",
        "dn_per_e": slope,
        "black_level_dn": black,
        "white_level_dn": white,
    }
    modes = {
        "native": _payload(
            native_e, variance, raw_valid, native_well, native_adc, native_meta, native
        )
    }
    digital_e = same_site_sum(native_e)
    digital_meta = {
        **common,
        "id": "digital_binning",
        "units": "summed_electrons",
        "contribution_count": 4,
        "raw_reused": True,
        "source": "same native RAW; signed BLC/e conversion then same-site sum",
        "read_noise_count_per_output_site": 4,
        "well_mask_basis": native_meta["well_mask_basis"],
        "output_kind": "derived_electron_array_not_new_sensor_RAW",
    }
    modes["digital_binning"] = _payload(
        digital_e,
        same_site_sum(variance),
        same_site_sum(raw_valid) == 4,
        same_site_sum(native_well) > 0,
        same_site_sum(native_adc) > 0,
        digital_meta,
    )
    options = {
        "node_capacity_multiplier": 4.0,
        "conversion_gain_multiplier": 0.25,
        "read_noise_multiplier": 1.0,
    }
    if charge_options is not None:
        if not isinstance(charge_options, dict) or set(charge_options) - set(options):
            raise ValueError("Unknown ideal charge binning options.")
        options.update(charge_options)
    if any(
        not np.isfinite(v) or v < 0 or (v == 0 and key != "read_noise_multiplier")
        for key, v in options.items()
    ):
        raise ValueError("Charge multipliers must be positive; read noise may be zero.")
    node = deepcopy(model)
    node["full_well_e"] *= options["node_capacity_multiplier"]
    node["conversion_gain_v_per_e"] *= options["conversion_gain_multiplier"]
    node["read_noise_e"] *= options["read_noise_multiplier"]
    node = electrical.validate_electrical_model(node)
    node_slope, node_black, node_white = _transfer(node, gain)
    node_read_var = (
        node["read_noise_e"] ** 2
        + (node["post_gain_noise_v"] / (node["conversion_gain_v_per_e"] * gain)) ** 2
    )
    node_variance = (
        same_site_sum(total_mean) + node_read_var if noise_enabled else np.zeros_like(digital_e[0])
    )
    node_raw, node_well, node_adc, node_valid = [], [], [], []
    for index in range(frames):
        rng = np.random.default_rng((seed + 1_000_000 + index) % 2**32)
        charge = rng.poisson(total_mean).astype(float) if noise_enabled else total_mean.copy()
        pixel_clip = charge >= model["full_well_e"]
        node_charge = same_site_sum(np.minimum(charge, model["full_well_e"]))
        clipped = (same_site_sum(pixel_clip) > 0) | (node_charge >= node["full_well_e"])
        node_charge = np.minimum(node_charge, node["full_well_e"])
        encoded = electrical.read_delivered_charge(
            node_charge,
            node,
            analog_gain=gain,
            temperature_c=temperature_c,
            noise_enabled=noise_enabled,
            rng=rng,
        )["arrays"]
        raw = encoded["raw_dn"]
        node_raw.append(raw)
        node_well.append(clipped)
        node_adc.append(
            encoded["adc_high_mask"]
            | encoded["adc_low_mask"]
            | encoded["digital_high_mask"]
            | encoded["digital_low_mask"]
        )
        node_valid.append((raw > 0) & (raw < node_white) & ~clipped)
    node_raw = np.asarray(node_raw)
    node_e = (node_raw.astype(float) - node_black) / node_slope - same_site_sum(dark)
    modes["charge_binning"] = _payload(
        node_e,
        node_variance,
        node_valid,
        node_well,
        node_adc,
        {
            **common,
            "id": "charge_binning",
            "units": "summed_electrons",
            "contribution_count": 4,
            "raw_reused": False,
            "assumptions": options,
            "read_noise_count_per_output_site": 1,
            "seed": seed + 1_000_000,
            "dn_per_e": node_slope,
            "black_level_dn": node_black,
            "white_level_dn": node_white,
            "electrical_model": node,
            "processing_order": [
                "native PRNU/dark",
                "native shot charge",
                "native well clip",
                "same-site sum",
                "node clip",
                "one read noise",
                "gain",
                "ADC",
            ],
        },
        node_raw,
    )
    return {"modes": modes, "geometry": binning_geometry(photo.shape), "readout": model}


def dual_gain_modes(
    mean_rate,
    readout,
    *,
    exposure_s=0.004,
    light_scale=1.0,
    frames=8,
    seed=100,
    electrical_model=None,
    geometry=None,
    noise_enabled=True,
    temperature_c=25.0,
    merge_profile=None,
):
    """Reuse the existing one-population/two-path readout and RAW-only DCG merge."""
    photo, base, geometry = _inputs(
        mean_rate, readout, frames, exposure_s, light_scale, electrical_model, geometry
    )
    model = dcg_electrical.default_model(readout)
    model["shared_charge_capacity_e"] = base["full_well_e"]
    for key in (
        "fixed_pattern",
        "dark_current_e_per_s",
        "reference_temperature_c",
        "dark_current_doubling_c",
    ):
        model[key] = deepcopy(base[key])
    # Preserve the source ADC/transfer. The fourfold HCG and half-RMS noise are assumptions.
    if base["digital_gain"] != 1 or base["post_gain_noise_v"] != 0:
        raise ValueError(
            "Reference DCG first slice requires linear unit digital gain, no post-gain noise."
        )
    for path, multiplier in zip(model["paths"], (4.0, 1.0), strict=True):
        path["conversion_gain_v_per_e"] = base["conversion_gain_v_per_e"] * multiplier
        path["input_linear_ceiling_e"] = base["full_well_e"] / multiplier
        path["adc"] = deepcopy(base["adc"])
    model["read_noise"]["covariance_e2"] = [
        [base["read_noise_e"] ** 2 / 4, 0.0],
        [0.0, base["read_noise_e"] ** 2],
    ]
    model = dcg_electrical.validate_model(model)
    profile = (
        dcg.default_profile() if merge_profile is None else dcg.validate_profile(merge_profile)
    )
    collected = {
        name: {
            key: []
            for key in (
                "electron_frames",
                "variance_e2",
                "valid_frames",
                "well_saturated_frames",
                "adc_saturated_frames",
            )
        }
        for name in ("hcg", "lcg", "dcg")
    }
    for name in ("hcg", "lcg"):
        collected[name]["raw_frames"] = []
    diagnostics = {"weights": [], "gain_path_agreement": [], "shared_charge_e": []}
    calibration = None
    for index in range(frames):
        capture = dcg_electrical.capture_shared_electrons(
            photo,
            exposure_s,
            model,
            temperature_c=temperature_c,
            geometry=geometry,
            noise_enabled=noise_enabled,
            seed=(seed + index) % 2**32,
        )
        arrays, calibration = capture["arrays"], capture["calibration"]
        merged = dcg.merge_shared_charge_raw(arrays["raw_dn"], calibration, profile)
        result = merged["arrays"]
        for j, name in enumerate(("hcg", "lcg")):
            target = collected[name]
            path = calibration["paths"][j]
            target["raw_frames"].append(arrays["raw_dn"][j])
            target["electron_frames"].append(
                (arrays["raw_dn"][j].astype(float) - path["black_level_dn"]) / path["dn_per_e"]
                - calibration["dark_e"]
            )
            target["variance_e2"].append(
                arrays["mean_total_charge_e"] + calibration["read_noise_covariance_e2"][j][j]
                if noise_enabled
                else np.zeros_like(photo)
            )
            target["valid_frames"].append(result["usable_mask"][j])
            target["well_saturated_frames"].append(
                arrays["well_saturated_mask"] | arrays["path_saturated_mask"][j]
            )
            target["adc_saturated_frames"].append(
                arrays["adc_high_mask"][j] | arrays["adc_low_mask"][j]
            )
        target = collected["dcg"]
        target["electron_frames"].append(result["merged_rate"] * exposure_s)
        target["variance_e2"].append(result["shared_shot_read_variance_estimate_e2"])
        target["valid_frames"].append(result["valid_mask"])
        target["well_saturated_frames"].append(arrays["well_saturated_mask"])
        target["adc_saturated_frames"].append(
            np.all(arrays["adc_high_mask"], axis=0) | np.all(arrays["adc_low_mask"], axis=0)
        )
        diagnostics["weights"].append(result["weights"])
        diagnostics["gain_path_agreement"].append(result["gain_path_agreement_mask"])
        diagnostics["shared_charge_e"].append(arrays["shared_charge_e"])
    for name, target in collected.items():
        for key in target:
            target[key] = np.asarray(target[key])
        target["metadata"] = {
            "id": name,
            "units": "electron/native_pixel",
            "contribution_count": 1,
            "exposure_s": exposure_s,
            "light_scale": light_scale,
            "cfa": "RGGB",
            "seed": seed,
            "noise_enabled": noise_enabled,
            "temperature_c": temperature_c,
            "product_mode": False,
            "physical_validation": "not_established",
            "topology": "one shared Poisson charge; two independent read noise paths",
            "raw_reused": name == "dcg",
            "shot_noise_shared": True,
            "fixed_pattern": deepcopy(base["fixed_pattern"]),
            "variance_scope": (
                "RAW-only common-shot plus declared covariance estimate; guards are "
                "not clipping truth and conditional variance is unmodeled"
            ),
            "merge_profile": profile if name == "dcg" else None,
            "calibration_path": calibration["paths"][0 if name == "hcg" else 1]
            if name != "dcg"
            else None,
        }
    return {
        "modes": collected,
        "model": model,
        "calibration": calibration,
        "merge_summary": merged["summary"],
        "diagnostics": {k: np.asarray(v) for k, v in diagnostics.items()},
    }


def process_electrons(
    electrons,
    matrix,
    *,
    contribution_count=1,
    lsc_gain=None,
    display_white=10.0,
    gamma=2.2,
    demosaic="bilinear",
    valid=None,
    variance_e2=None,
):
    """Fixed common electron-domain display; LSC once and no frame normalization.

    Four-site sums are reconstructed at their actual centroids. Invalid samples
    remain flagged; zero fill exists only to produce a bounded preview.
    """
    values = np.asarray(electrons, dtype=float)
    transform = np.asarray(matrix, dtype=float)
    if (
        values.ndim != 2
        or min(values.shape) < 4
        or any(n % 2 for n in values.shape)
        or transform.shape != (3, 3)
        or not np.isfinite(transform).all()
        or contribution_count not in (1, 4)
        or display_white <= 0
        or gamma <= 0
    ):
        raise ValueError("Invalid electron-domain ISP input or fixed display settings.")
    mask = np.isfinite(values)
    if valid is not None:
        supplied = np.asarray(valid, dtype=bool)
        if supplied.shape != values.shape:
            raise ValueError("Validity must match electron mosaic.")
        mask &= supplied
    gains = np.ones(values.shape) if lsc_gain is None else np.asarray(lsc_gain, dtype=float)
    if gains.shape != values.shape or not np.isfinite(gains).all() or np.any(gains <= 0):
        raise ValueError("LSC must match the current mosaic and be finite positive.")
    corrected = np.where(np.isfinite(values), values, 0.0) * gains / contribution_count
    variance = None
    if variance_e2 is not None:
        variance = np.asarray(variance_e2, dtype=float)
        if variance.shape != values.shape or np.any(variance < 0):
            raise ValueError("Input variance must be nonnegative and match the electron mosaic.")
        if not np.isfinite(variance[mask]).all():
            raise ValueError("Input variance must be finite on valid electron sites.")
        variance = variance * gains**2 / contribution_count**2
    if contribution_count == 1:
        rgb = demosaic_bayer(corrected, demosaic, cfa_pattern="RGGB")
        radius = 1 if demosaic == "bilinear" else 5
        mask = minimum_filter(mask, size=radius * 2 + 1, mode="constant", cval=0).astype(bool)
    else:
        if demosaic != "bilinear":
            raise ValueError("Binned nonuniform centroids support coordinate-aware bilinear only.")
        yy, xx = np.indices(values.shape, dtype=float)
        yy, xx = 0.5 + 2 * yy, 0.5 + 2 * xx
        planes, valid_planes = [], []
        for r, c in SITES:
            coordinates = [(yy - r - 1) / 4, (xx - c - 1) / 4]
            planes.append(
                map_coordinates(
                    corrected[r::2, c::2], coordinates, order=1, mode="nearest", prefilter=False
                )
            )
            weights = map_coordinates(
                mask[r::2, c::2].astype(float),
                coordinates,
                order=1,
                mode="constant",
                cval=0,
                prefilter=False,
            )
            valid_planes.append(weights >= 1 - 1e-12)
        rgb = np.stack((planes[0], (planes[1] + planes[2]) / 2, planes[3]), axis=-1)
        mask = np.all(valid_planes, axis=0)
    linear = rgb @ transform
    display = np.clip(linear / display_white, 0, 1) ** (1 / gamma)
    return {
        "linear": linear,
        "display": display,
        "valid": mask,
        "lsc_mosaic_variance_e2_per_contributor": variance,
        "stages": {
            "electron_mosaic_per_contributor": corrected,
            "demosaiced_electrons": rgb,
            "color_linear": linear,
        },
    }


def summarize_mode(mode):
    """Temporal repeats, never spatial image texture, define measured noise."""
    frames = np.asarray(mode["electron_frames"], dtype=float)
    common = np.asarray(mode["valid_frames"], dtype=bool).all(axis=0)
    common &= np.isfinite(frames).all(axis=0)
    count = mode["metadata"]["contribution_count"]
    data = frames[:, common] / count
    signal = float(data.mean()) if data.size else None
    noise = (
        float(np.sqrt(np.var(data, axis=0, ddof=1).mean()))
        if data.size and len(frames) > 1
        else None
    )
    return {
        "mean_electrons_per_contributor": signal,
        "temporal_noise_e_per_contributor": noise,
        "snr": signal / noise if signal is not None and noise and signal >= 0 else None,
        "well_saturation_fraction": float(np.mean(mode["well_saturated_frames"])),
        "adc_saturation_fraction": float(np.mean(mode["adc_saturated_frames"])),
        "common_valid_fraction": float(common.mean()),
        "independent_acquisitions": len(frames),
        "noise_basis": "RMS pixelwise sample temporal variance on all-repeat-valid pixels",
        "noise_comparison_scope": "per contributing pixel; different spatial support retained",
    }


def replay_dual_gain(raw_frames, calibration, model, *, merge_profile=None):
    """Decode and remerge preserved paired RAW without creating any new charge.

    No simulator clipping truth is reconstructed. All saturation diagnostics in
    this adapter are explicitly RAW boundary proxies; callers may retain and
    display original capture masks separately. Shot variance is RAW inferred.
    """
    raw = np.asarray(raw_frames)
    if (
        raw.ndim != 4
        or raw.shape[1] != 2
        or raw.dtype.kind not in "iu"
        or not 1 <= raw.shape[0] <= 32
    ):
        raise ValueError("DCG replay requires integer [frames,2,H,W] paired RAW.")
    model = dcg_electrical.validate_model(model)
    selected = (
        dcg.default_profile() if merge_profile is None else dcg.validate_profile(merge_profile)
    )
    exposure = float(calibration["exposure_s"])
    dark = np.asarray(calibration["dark_e"], dtype=float)
    before = raw.tobytes(order="C")
    collected = {
        name: {
            key: []
            for key in (
                "electron_frames",
                "variance_e2",
                "valid_frames",
                "well_saturated_frames",
                "adc_saturated_frames",
            )
        }
        for name in ("hcg", "lcg", "dcg")
    }
    for name in ("hcg", "lcg"):
        collected[name]["raw_frames"] = []
    diagnostics = {"weights": [], "gain_path_agreement": []}
    for pair in raw:
        merged = dcg.merge_shared_charge_raw(pair, calibration, selected)
        arrays = merged["arrays"]
        path_high, adc_bounds = [], []
        for j, name in enumerate(("hcg", "lcg")):
            path = calibration["paths"][j]
            total = (pair[j].astype(float) - path["black_level_dn"]) / path["dn_per_e"]
            high = pair[j] >= path["linear_ceiling_dn"] - 0.5
            adc = (pair[j] <= 0) | (pair[j] >= path["white_level_dn"])
            target = collected[name]
            target["raw_frames"].append(pair[j].copy())
            target["electron_frames"].append(total - dark)
            target["variance_e2"].append(
                np.maximum(total, 0) + calibration["read_noise_covariance_e2"][j][j]
            )
            target["valid_frames"].append(arrays["usable_mask"][j])
            target["well_saturated_frames"].append(high)
            target["adc_saturated_frames"].append(adc)
            path_high.append(high)
            adc_bounds.append(adc)
        target = collected["dcg"]
        target["electron_frames"].append(arrays["merged_rate"] * exposure)
        target["variance_e2"].append(arrays["shared_shot_read_variance_estimate_e2"])
        target["valid_frames"].append(arrays["valid_mask"])
        target["well_saturated_frames"].append(np.all(path_high, axis=0))
        target["adc_saturated_frames"].append(np.all(adc_bounds, axis=0))
        diagnostics["weights"].append(arrays["weights"])
        diagnostics["gain_path_agreement"].append(arrays["gain_path_agreement_mask"])
    if raw.tobytes(order="C") != before:
        raise RuntimeError("Paired RAW was mutated during DCG replay.")
    for name, target in collected.items():
        for key in target:
            target[key] = np.asarray(target[key])
        target["metadata"] = {
            "id": name,
            "units": "electron/native_pixel",
            "contribution_count": 1,
            "exposure_s": exposure,
            "cfa": "RGGB",
            "raw_reused": True,
            "acquisition_performed": False,
            "physical_validation": "not_established",
            "product_mode": False,
            "shot_noise_shared": True,
            "fixed_pattern": deepcopy(model["fixed_pattern"]),
            "well_mask_basis": "RAW linear-ceiling proxy, not original simulator truth",
            "adc_mask_basis": "RAW endpoint proxy, not original pre-rounding ADC truth",
            "variance_scope": "RAW-inferred common shot plus declared read covariance; "
            "not post-censoring conditional variance or measured variance",
            "merge_profile": selected if name == "dcg" else None,
            "calibration_path": calibration["paths"][0 if name == "hcg" else 1]
            if name != "dcg"
            else None,
        }
    return {
        "modes": collected,
        "model": model,
        "calibration": calibration,
        "merge_summary": merged["summary"],
        "diagnostics": {k: np.asarray(v) for k, v in diagnostics.items()},
    }
