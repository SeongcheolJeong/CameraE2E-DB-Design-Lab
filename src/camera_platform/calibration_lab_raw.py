"""Bounded, explicitly declared native Bayer NPY chart observation operators."""

from __future__ import annotations

import hashlib
import io
import math

import numpy as np

from .calibration_lab_contracts import MAX_INPUT_BYTES, _integer, _number


def _load(content: bytes) -> np.ndarray:
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_INPUT_BYTES:
        raise ValueError("NPY must contain at most 64 MiB")
    stream = io.BytesIO(content)
    try:
        version = np.lib.format.read_magic(stream)
        if version == (1, 0):
            shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream)
        elif version == (2, 0):
            shape, fortran, dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError("NPY version must be 1.0 or 2.0")
    except (EOFError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid NPY header: {exc}") from exc
    if len(shape) not in (2, 3, 4) or any(not isinstance(x, int) or x < 1 for x in shape):
        raise ValueError("NPY requires [H,W], [repeat,H,W], or [level,repeat,H,W]")
    if dtype.kind not in "iu" or dtype.itemsize > 4 or fortran:
        raise ValueError(
            "NPY requires C-order integer Bayer codes, without objects or floating point"
        )
    if shape[-1] % 2 or shape[-2] % 2 or min(shape[-2:]) < 2:
        raise ValueError("RAW needs even spatial dimensions")
    if shape[-1] * shape[-2] > 4_194_304 or math.prod(shape[:-2]) > 256:
        raise ValueError("RAW exceeds 4194304 spatial pixels or 256 total frames")
    byte_count = math.prod(shape) * dtype.itemsize
    if byte_count > MAX_INPUT_BYTES or stream.tell() + byte_count != len(content):
        raise ValueError("NPY payload length does not match its bounded header")
    # Bounds are established before np.load allocates any array.
    array = np.load(io.BytesIO(content), allow_pickle=False)
    if array.ndim == 2:
        array = array[None, None]
    elif array.ndim == 3:
        array = array[None]
    return array


def _layout(meta: dict, shape: tuple) -> tuple[dict, list, list, str]:
    fmt = meta.get("raw_format", {})
    if fmt.get("cfa") != "RGGB" or fmt.get("response_type") != "linear":
        raise ValueError("RAW requires explicit linear RGGB declaration")
    bits = _integer(fmt.get("bit_depth"), "bit_depth", 1, 24)
    white = 2**bits - 1
    black = _number(fmt.get("black_level_dn"), "black_level_dn", 0, white - 1)
    geometry = meta.get("geometry", {})
    full = geometry.get("full_sensor_shape")
    # Active arrays may have odd dimensions; only the acquired CFA crop has
    # a local sampling layout. Never truncate vendor dimensions to make them even.
    if not isinstance(full, list) or len(full) != 2:
        raise ValueError("full_sensor_shape requires [height, width]")
    full = [_integer(x, "full_sensor_shape", 2, 65536) for x in full]
    origin = geometry.get("crop_origin")
    if not isinstance(origin, list) or len(origin) != 2:
        raise ValueError("RAW requires native crop_origin [row,column]")
    origin = [_integer(x, "crop_origin", 0, 65536) for x in origin]
    if any(origin[i] + shape[i] > full[i] for i in (0, 1)):
        raise ValueError("RAW crop exceeds full native sensor; resizing is unsupported")
    channel = meta.get("analysis", {}).get("channel", "G1")
    if channel not in {"R", "G1", "G2", "B"}:
        raise ValueError("analysis.channel must be R, G1, G2, or B")
    return {"white": white, "black": black}, full, origin, channel


def _plane(image: np.ndarray, channel: str, origin: list) -> np.ndarray:
    phase = {"R": (0, 0), "G1": (0, 1), "G2": (1, 0), "B": (1, 1)}[channel]
    row, col = [(phase[i] - origin[i]) % 2 for i in (0, 1)]
    return image[..., row::2, col::2]


def _rois(analysis: dict, shape: tuple) -> list:
    rois = analysis.get("rois")
    if not isinstance(rois, list) or not 1 <= len(rois) <= 512:
        raise ValueError("This chart requires 1..512 explicit analysis.rois")
    result = []
    for value in rois:
        box = value.get("box") if isinstance(value, dict) else value
        if isinstance(value, dict) and set(value) - {"box", "field_radius"}:
            raise ValueError("ROI supports box and field_radius only")
        if not isinstance(box, list) or len(box) != 4:
            raise ValueError("ROI box requires [row,column,height,width]")
        row, col, h, w = [_integer(x, "ROI box", 0, 65536) for x in box]
        if min(h, w) < 4 or row + h > shape[0] or col + w > shape[1]:
            raise ValueError("ROI requires at least 4x4 pixels inside the native crop")
        result.append(
            {
                "box": [row, col, h, w],
                **(
                    {"field_radius": value["field_radius"]}
                    if isinstance(value, dict) and "field_radius" in value
                    else {}
                ),
            }
        )
    return result


def analyze_npy(meta: dict, content: bytes) -> tuple[dict, dict, list[str]]:
    chart, analysis = meta["chart"], meta.get("analysis", {})
    if chart in {"mtf", "texture"} and any(
        declaration.get("field_coordinate_domain", "sensor_normalized") != "sensor_normalized"
        for declaration in (analysis, meta.get("observations", {}))
    ):
        raise ValueError(
            "Native RAW field coordinates are sensor_normalized; a reference-lens mapping "
            "is not established by the acquisition raster"
        )
    raw = _load(content)
    fmt, full, origin, channel = _layout(meta, raw.shape[-2:])
    if np.any(raw < 0) or np.any(raw > fmt["white"]):
        raise ValueError("RAW codes exceed declared bit depth")
    hashes = [
        hashlib.sha256(str(frame.shape).encode() + frame.astype("<u4").tobytes()).hexdigest()
        for frame in raw.reshape(-1, *raw.shape[-2:])
    ]
    plane = _plane(raw, channel, origin).astype(float)
    clipped = np.mean(plane >= fmt["white"], axis=(1, 2, 3))
    quality = {
        "method": f"native_bayer_{chart}_v1",
        "shape": list(raw.shape),
        "channel": channel,
        "crop_origin": origin,
        "native_shape": full,
        "clipped_fraction": clipped.tolist(),
        "not_fit_reasons": [],
        "physical_linearity": "declared_not_independently_verified",
    }
    if np.any(clipped > 0):
        quality["not_fit_reasons"].append(
            "ADC clipping observed; clipped samples cannot identify linear response or full-well"
        )
    if chart in {"dark", "ptc"}:
        exposures = analysis.get("exposure_s")
        if exposures is None and len(raw) == 1 and "exposure_s" in meta["conditions"]:
            exposures = [meta["conditions"]["exposure_s"]]
        if chart == "dark" and exposures is None:
            raise ValueError("Dark levels require analysis.exposure_s")
        obs = {"mean_dn": plane.mean(axis=(1, 2, 3)).tolist()}
        if exposures is not None:
            if not isinstance(exposures, list) or len(exposures) != len(raw):
                raise ValueError("analysis.exposure_s must match the number of levels")
            obs["exposure_s"] = [_number(x, "exposure_s", 1e-12, 3600) for x in exposures]
        if raw.shape[1] > 1:
            obs["variance_dn2"] = np.var(plane, axis=1, ddof=1).mean(axis=(1, 2)).tolist()
        else:
            quality["not_fit_reasons"].append(
                "At least two repeats are required for temporal variance"
            )
        if chart == "ptc":
            obs["clipped_fraction"] = clipped.tolist()
            if exposures is not None and len(raw) >= 3:
                signal = np.asarray(obs["mean_dn"]) - fmt["black"]
                fit = np.polyval(np.polyfit(exposures, signal, 1), exposures)
                residual = float(np.max(np.abs(signal - fit)) / max(float(np.ptp(signal)), 1))
                quality["exposure_linearity_residual_fraction"] = residual
                if residual > 0.03:
                    quality["not_fit_reasons"].append(
                        "Exposure response exceeds 3% diagnostic linearity residual; "
                        "select a verified linear region"
                    )
        return obs, quality, hashes
    if len(raw) != 1:
        raise ValueError("This chart accepts one acquisition level per NPY import")
    mean = raw[0].astype(float).mean(axis=0)
    corrected = mean - fmt["black"]
    if chart == "mtf":
        from .virtual_lab_metrics import slanted_edge_metrics

        if meta.get("measurement_domain", "camera_raw") != "camera_raw":
            raise ValueError("NPY MTF is camera_raw; optical and ISP MTF cannot use RAW import")
        selected = _plane(corrected, channel, origin)
        y, x, h, w = 0, 0, *mean.shape
        if "rois" in analysis:
            rois = _rois(analysis, mean.shape)
            if len(rois) != 1:
                raise ValueError("MTF import requires exactly one edge ROI per record")
            y, x, h, w = rois[0]["box"]
            selected = _plane(
                corrected[y : y + h, x : x + w], channel, [origin[0] + y, origin[1] + x]
            )
        orientation = analysis.get("orientation_deg", 0)
        if orientation not in (0, 90, 180, 270):
            raise ValueError("RAW edge orientation must be 0/180 vertical or 90/270 horizontal")
        if orientation in (90, 270):
            selected = selected.T
        result = slanted_edge_metrics(selected)
        if result["status"] != "ok":
            quality["not_fit_reasons"].append(result.get("reason", "SFR unavailable"))
            return {}, quality, hashes
        quality["frequency_note"] = (
            "CFA sublattice pitch is two native pixels; max frequency 0.25 cy/native-pixel"
        )
        quality["esf"] = result["esf"]
        quality["lsf"] = result["lsf"]
        quality["edge_slope"] = result["edge_slope"]
        quality["edge_low_dn"] = float(min(np.mean(selected[:, :3]), np.mean(selected[:, -3:])))
        quality["edge_high_dn"] = float(max(np.mean(selected[:, :3]), np.mean(selected[:, -3:])))
        quality["edge_level_domain"] = "black_subtracted_measured_DN_not_input_radiometry"
        quality["native_orientation"] = (
            "frequency along native x" if orientation in (0, 180) else "frequency along native y"
        )
        field = [
            2 * (origin[1] + x + (w - 1) / 2) / (full[1] - 1) - 1,
            2 * (origin[0] + y + (h - 1) / 2) / (full[0] - 1) - 1,
        ]
        if "field_xy" in analysis and not np.allclose(analysis["field_xy"], field, atol=1e-6):
            raise ValueError("Declared field_xy differs from the native edge ROI center")
        obs = {
            "frequency_cy_px": (np.asarray(result["frequency"]["cycles_per_pixel"]) / 2).tolist(),
            "mtf": result["sfr"],
            "orientation_deg": orientation,
            "field_xy": field,
            "field_coordinate_domain": "sensor_normalized",
        }
        return obs, quality, hashes
    if chart == "texture":
        field = [
            2 * (origin[1] + (mean.shape[1] - 1) / 2) / (full[1] - 1) - 1,
            2 * (origin[0] + (mean.shape[0] - 1) / 2) / (full[0] - 1) - 1,
        ]
        if "field_xy" in analysis and not np.allclose(analysis["field_xy"], field, atol=1e-6):
            raise ValueError("Declared field_xy differs from the native texture ROI center")
        frequencies = np.asarray(analysis.get("frequency_cy_px", []), dtype=float)
        reference = np.asarray(analysis.get("reference_amplitude", []), dtype=float)
        if (
            frequencies.ndim != 1
            or not 2 <= len(frequencies) <= 256
            or reference.shape != frequencies.shape
            or np.any(reference <= 0)
            or not np.isfinite(reference).all()
        ):
            raise ValueError(
                "Texture needs frequency_cy_px and matching positive reference_amplitude"
            )
        if (
            not np.isfinite(frequencies).all()
            or np.any(frequencies <= 0)
            or np.any(frequencies > 0.25)
        ):
            raise ValueError("RAW texture frequencies must lie in (0,0.25] cy/native-pixel")
        selected = _plane(corrected, channel, origin)
        level = float(np.mean(selected))
        if level <= 0:
            raise ValueError("Texture requires positive black-corrected mean")
        amplitude = np.abs(np.fft.fft2(selected / level - 1)) / selected.size
        fy, fx = np.meshgrid(
            np.fft.fftfreq(selected.shape[0], d=2),
            np.fft.fftfreq(selected.shape[1], d=2),
            indexing="ij",
        )
        radius = np.hypot(fy, fx)
        step = max(1 / (2 * selected.shape[0]), 1 / (2 * selected.shape[1]))
        observed = []
        for frequency in frequencies:
            mask = abs(radius - frequency) <= step / 2
            if not np.any(mask):
                raise ValueError("Texture frequency bin contains no Fourier samples")
            observed.append(float(np.mean(amplitude[mask])))
        quality["reference_requirement"] = (
            "Reference amplitudes must use identical normalization, raster, bins, "
            "and chart registration"
        )
        return (
            {
                "frequency_cy_px": frequencies.tolist(),
                "response": (np.asarray(observed) / reference).tolist(),
                "field_xy": field,
                "field_coordinate_domain": "sensor_normalized",
            },
            quality,
            hashes,
        )
    rois = _rois(analysis, mean.shape)
    values, rgb, points = [], [], []
    for roi in rois:
        y, x, h, w = roi["box"]
        patch = corrected[y : y + h, x : x + w]
        local_origin = [origin[0] + y, origin[1] + x]
        values.append(float(np.mean(_plane(patch, channel, local_origin))))
        rgb.append(
            [
                float(np.mean(_plane(patch, c, local_origin))) / (fmt["white"] - fmt["black"])
                for c in ("R", "G1", "B")
            ]
        )
        if chart == "geometry":
            # Bright fiducials on a dark background; weighted centroid is explicit.
            floor = float(np.median(np.r_[patch[0], patch[-1], patch[:, 0], patch[:, -1]]))
            weight = np.maximum(patch - floor, 0)
            if float(weight.sum()) <= 0:
                raise ValueError("Geometry ROI lacks a bright fiducial")
            yy, xx = np.indices(patch.shape)
            px = origin[1] + x + float(np.sum(xx * weight) / weight.sum())
            py = origin[0] + y + float(np.sum(yy * weight) / weight.sum())
            points.append([2 * px / (full[1] - 1) - 1, 2 * py / (full[0] - 1) - 1])
    if chart == "shading":
        radii = [roi.get("field_radius") for roi in rois]
        if any(radius is None for radius in radii) or radii[0] != 0 or values[0] <= 0:
            raise ValueError(
                "Shading ROIs require increasing field_radius starting at on-axis radius 0"
            )
        return (
            {"field_radius": radii, "relative_response": (np.asarray(values) / values[0]).tolist()},
            quality,
            hashes,
        )
    if chart == "color":
        reference = analysis.get("reference_rgb")
        if not isinstance(reference, list) or len(reference) != len(rois):
            raise ValueError("Color requires one analysis.reference_rgb row per patch ROI")
        return {"reference_rgb": reference, "measured_rgb": rgb}, quality, hashes
    if chart == "tone":
        levels = analysis.get("input_level")
        if not isinstance(levels, list) or len(levels) != len(rois):
            raise ValueError("Tone requires one analysis.input_level per ROI")
        return (
            {
                "input_level": levels,
                "output_level": (np.asarray(values) / (fmt["white"] - fmt["black"])).tolist(),
            },
            quality,
            hashes,
        )
    if chart == "geometry":
        objects = analysis.get("object_xy")
        if not isinstance(objects, list) or len(objects) != len(rois):
            raise ValueError(
                "Geometry requires one known ideal analysis.object_xy per fiducial ROI"
            )
        quality["geometry_boundary"] = (
            "Known ideal image-coordinate radial warp; "
            "camera intrinsics are not independently recovered"
        )
        return {"object_xy": objects, "image_xy": points}, quality, hashes
    raise ValueError("Unsupported RAW chart")
