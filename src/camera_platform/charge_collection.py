"""Linear generated-charge routing on a native sensor grid.

Each generated electron is collected at one destination or lost. Routing acts
on expected rates/charges before exposure integration and Poisson sampling, not
on sampled charge or RAW. Destination CFA factors must not be applied again.
The wavelength-dependent kernel marginalizes generation depth; its declared
direction independence is an assumption, not a general TCAD transport model.
"""

from __future__ import annotations

import math

import numpy as np

from .native_pixel_response import (
    GENERATION_SCHEMA,
    SITE_NAMES,
    site_index,
    validate_response,
)

SCHEMA = "camera_charge_collection_v1"
MAX_PIXELS = 1_048_576
MAX_GENERATED = 1e13
MAX_OFFSET = 4
MAX_OFFSETS = 81

_CONSTANTS = {
    "schema": SCHEMA,
    "site_names": list(SITE_NAMES),
    "site_index_tile": [[0, 1], [2, 3]],
    "values_axis_order": ["wavelength", "source_site", "destination_offset"],
    "interpolation": "linear_wavelength",
    "input_plane": "silicon_generated_charge",
    "output_plane": "collection_well_before_readout",
    "temporal_model": "instantaneous_memoryless",
    "boundary": "absorb_outside_full_sensor",
    "angular_assumption": "direction_independent_depth_marginal",
    "statistics": "single_destination_poisson_thinning",
    "regime": "linear_low_injection",
}
_FIELDS = set(_CONSTANTS) | {
    "pixel_pitch_um", "wavelength_nm", "destination_offsets_rc", "probabilities", "source",
}


def _number(value, name, minimum, maximum):
    if (type(value) not in (int, float) or not minimum <= value <= maximum
            or not math.isfinite(value)):
        raise ValueError(f"{name} requires a finite number in [{minimum}, {maximum}]")
    return float(value)


def _same_typed(value, expected):
    if type(value) is not type(expected):
        return False
    if isinstance(expected, list):
        return len(value) == len(expected) and all(
            _same_typed(v, e) for v, e in zip(value, expected, strict=True)
        )
    return value == expected


def validate_collection(model):
    """Return detached finite JSON with strict substochastic source probabilities.

Two to 256 wavelength nodes, four native source sites and one to 81 unique
integer offsets within +/-4 rows/columns are supported. Neither normalization
nor extrapolation is performed. Input geometry is supplied separately to
``transport`` and validated against the physical full sensor.
    """
    if not isinstance(model, dict) or set(model) != _FIELDS:
        raise ValueError("Declare the exact camera_charge_collection_v1 fields")
    for key, expected in _CONSTANTS.items():
        if not _same_typed(model[key], expected):
            raise ValueError(f"Unsupported charge collection {key}; expected {expected!r}")
    pitch = _number(model["pixel_pitch_um"], "pixel_pitch_um", 0.05, 100)
    waves = model["wavelength_nm"]
    if not isinstance(waves, list) or not 2 <= len(waves) <= 256:
        raise ValueError("wavelength_nm requires 2..256 explicit samples")
    waves = [_number(w, "wavelength_nm", 200, 2500) for w in waves]
    if any(b <= a for a, b in zip(waves[:-1], waves[1:], strict=True)):
        raise ValueError("wavelength_nm must increase strictly")
    offsets = model["destination_offsets_rc"]
    if not isinstance(offsets, list) or not 1 <= len(offsets) <= MAX_OFFSETS:
        raise ValueError("destination_offsets_rc requires 1..81 unique offsets")
    normalized_offsets = []
    for offset in offsets:
        if (not isinstance(offset, list) or len(offset) != 2
                or any(type(x) is not int or abs(x) > MAX_OFFSET for x in offset)):
            raise ValueError("Each destination offset requires two integers in [-4,4]")
        if offset in normalized_offsets:
            raise ValueError("destination_offsets_rc contains a duplicate")
        normalized_offsets.append(list(offset))
    values = model["probabilities"]
    if not isinstance(values, list) or len(values) != len(waves):
        raise ValueError("probabilities must have exact [wavelength,4,destination_offset] shape")
    normalized_values = []
    for plane in values:
        if not isinstance(plane, list) or len(plane) != 4:
            raise ValueError(
                "probabilities must have exact [wavelength,4,destination_offset] shape"
            )
        normalized_plane = []
        for row in plane:
            if not isinstance(row, list) or len(row) != len(offsets):
                raise ValueError(
                    "probabilities must have exact [wavelength,4,destination_offset] shape"
                )
            probabilities = [_number(p, "probability", 0, 1) for p in row]
            if math.fsum(probabilities) > 1:
                raise ValueError("Source collection probabilities must sum to at most 1")
            normalized_plane.append(probabilities)
        normalized_values.append(normalized_plane)
    source = model["source"]
    if (not isinstance(source, dict) or set(source) != {"kind", "description"}
            or type(source["kind"]) is not str
            or source["kind"] not in {"estimated", "simulated", "measured"}
            or type(source["description"]) is not str
            or not 1 <= len(source["description"].strip()) <= 4000):
        raise ValueError("source requires kind and a nonempty description up to 4000 characters")
    return {
        **{key: value for key, value in _CONSTANTS.items() if not isinstance(value, list)},
        "site_names": list(SITE_NAMES),
        "site_index_tile": [[0, 1], [2, 3]],
        "values_axis_order": ["wavelength", "source_site", "destination_offset"],
        "pixel_pitch_um": pitch,
        "wavelength_nm": waves,
        "destination_offsets_rc": normalized_offsets,
        "probabilities": normalized_values,
        "source": dict(source),
    }


def validate_pair(response, collection):
    """Require explicit generation response and identical native sampling/axes.

Collected-electron EQE cannot be reinterpreted as optical generation, even for
an identity collection kernel. There is no automatic conversion or division.
    """
    response = validate_response(response)
    if response["schema"] != GENERATION_SCHEMA:
        raise ValueError(
            "Charge collection requires generated-electron response, not collected EQE"
        )
    collection = validate_collection(collection)
    for key in ("site_names", "site_index_tile", "pixel_pitch_um", "wavelength_nm"):
        if response[key] != collection[key]:
            raise ValueError(f"Generation and collection {key} must match exactly")
    return response, collection


def _probabilities(model, wavelength_nm):
    if isinstance(wavelength_nm, (bool, np.bool_)) or not isinstance(
        wavelength_nm, (int, float, np.integer, np.floating)
    ):
        raise ValueError("wavelength_nm query requires a finite real scalar")
    wave = model["wavelength_nm"]
    if not wave[0] <= wavelength_nm <= wave[-1] or not np.isfinite(wavelength_nm):
        raise ValueError("Wavelength exceeds collection domain; no extrapolation")
    i = int(np.clip(np.searchsorted(wave, wavelength_nm, side="right") - 1, 0, len(wave) - 2))
    fraction = float((wavelength_nm - wave[i]) / (wave[i + 1] - wave[i]))
    low, high = np.asarray(model["probabilities"][i]), np.asarray(model["probabilities"][i+1])
    probabilities = (1 - fraction) * low + fraction * high
    # Interpolate the explicit loss outcome as well. Subtracting a rounded sum
    # of interpolated probabilities could invent tiny negative losses at unity.
    loss_low = np.array([1 - math.fsum(row) for row in low])
    loss_high = np.array([1 - math.fsum(row) for row in high])
    return probabilities, (1 - fraction) * loss_low + fraction * loss_high


def transport(generated, wavelength_nm, model, *, origin=(0, 0), full_sensor_shape=None):
    """Route expected scalar generated rates or charges over a native grid.

`generated` is HxW at ``origin`` in ``full_sensor_shape``, with the actual
source site's optical response already applied. For partial support, collection
outside the supplied array remains physical collected charge, not off-sensor
loss. Such a crop is only an exact prediction for destinations whose complete
incoming source/optical halo has been verified by the caller. Units are
preserved (e.g. electron/pixel/s/nm or electron/pixel); no RNG is used. Loss
maps index SOURCE positions, while collected indexes DESTINATION positions.
    """
    model = validate_collection(model)
    probabilities, lost = _probabilities(model, wavelength_nm)
    if (not isinstance(generated, np.ndarray) or generated.dtype.kind not in "iuf"
            or generated.ndim != 2 or not generated.size or generated.size > MAX_PIXELS):
        raise ValueError("generated requires a real HxW NumPy array of at most 1048576 pixels")
    if (not np.isfinite(generated).all() or np.any(generated < 0)
            or np.any(generated > MAX_GENERATED)):
        raise ValueError("generated must be finite nonnegative means at most 1e13 per pixel")
    values = generated.astype(np.float64, copy=True)
    if full_sensor_shape is None:
        full_sensor_shape = values.shape
    for name, pair, minimum in (("origin", origin, 0),
                                ("full_sensor_shape", full_sensor_shape, 1)):
        if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                or any(type(item) is not int or not minimum <= item <= 1_000_000
                       for item in pair)):
            raise ValueError(f"{name} requires two bounded native integers")
    if any(start + side > full for start, side, full in zip(
            origin, values.shape, full_sensor_shape, strict=True)):
        raise ValueError("Generated support must lie inside full_sensor_shape")
    partial = tuple(origin) != (0, 0) or tuple(values.shape) != tuple(full_sensor_shape)
    sites = site_index(values.shape, origin)
    collected = np.zeros_like(values)
    intrinsic_loss = values * lost[sites]
    off_sensor_loss = np.zeros_like(values)
    outside_support_collected = np.zeros_like(values) if partial else None
    height, width = values.shape
    for offset_index, (dy, dx) in enumerate(model["destination_offsets_rc"]):
        routed = values * probabilities[sites, offset_index]
        # A large offset relative to support leaves no in-support destinations;
        # the physical sensor boundary is classified separately below.
        if abs(dy) >= height or abs(dx) >= width:
            if not partial:
                off_sensor_loss += routed
                continue
        else:
            source_y = slice(max(0, -dy), min(height, height - dy))
            source_x = slice(max(0, -dx), min(width, width - dx))
            destination_y = slice(max(0, dy), min(height, height + dy))
            destination_x = slice(max(0, dx), min(width, width + dx))
            collected[destination_y, destination_x] += routed[source_y, source_x]
            # Remove in-support source contributions before classifying the
            # remaining destinations against the real physical sensor edge.
            routed[source_y, source_x] = 0
        if partial:
            destination_rows = np.arange(height) + origin[0] + dy
            destination_cols = np.arange(width) + origin[1] + dx
            on_sensor = ((destination_rows >= 0)
                         & (destination_rows < full_sensor_shape[0]))[:, None] & (
                             (destination_cols >= 0)
                             & (destination_cols < full_sensor_shape[1]))[None, :]
            outside_support_collected += np.where(on_sensor, routed, 0)
            off_sensor_loss += np.where(on_sensor, 0, routed)
        else:
            off_sensor_loss += routed
    totals = {
        "generated_total": math.fsum(values.ravel()),
        "collected_total": math.fsum(collected.ravel()),
        "intrinsic_loss_total": math.fsum(intrinsic_loss.ravel()),
        "off_sensor_loss_total": math.fsum(off_sensor_loss.ravel()),
    }
    if partial:
        totals["outside_support_collected_total"] = math.fsum(
            outside_support_collected.ravel())
    residual = totals["generated_total"] - math.fsum(
        value for key, value in totals.items() if key != "generated_total"
    )
    arrays = {"generated": values, "collected": collected,
              "intrinsic_loss": intrinsic_loss, "off_sensor_loss": off_sensor_loss}
    if partial:
        arrays["outside_support_collected"] = outside_support_collected
    return {
        "arrays": arrays,
        "diagnostics": {
            "schema": SCHEMA,
            "wavelength_nm": float(wavelength_nm),
            "native_origin": list(origin),
            "full_sensor_shape": list(full_sensor_shape),
            "geometry": ("partial_source_support" if partial else
                         "complete_native_sensor_no_crop"),
            **({"support_shape": list(values.shape),
                "outside_support_collected_coordinates": "source_pixel",
                "support_collected_warning": "Destinations near support edge may lack "
                                             "incoming sources; caller must verify target halo"}
               if partial else {}),
            "loss_map_coordinates": "source_pixel",
            "collected_map_coordinates": "destination_pixel",
            "unit": "same_as_generated_input",
            **totals,
            "conservation_residual": residual,
            "boundary": model["boundary"],
            "statistics": model["statistics"],
            "temporal_model": model["temporal_model"],
            "angular_assumption": model["angular_assumption"],
            "regime": model["regime"],
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
        },
    }
