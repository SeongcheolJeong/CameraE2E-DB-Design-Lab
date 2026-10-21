"""Native aperture-demo measurements and bounded, declared candidate selection.

Signal and temporal noise use exactly the same Gr/Gb pixels of the uniform
chart. The selected setting is conditional on this scene, exposure, readout,
ISP and policy; it is not a lens or product optimum.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .electrical import default_electrical_model

DEFAULT_POLICY = {
    "min_snr_db": 25.0,
    "min_relative_signal": 0.1,
    "max_saturation_fraction": 0.01,
}
REQUIRED_FIELDS = ("center", "outer-ne")
CHARTS = ("resolution", "color", "uniform", "texture")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _saved_array(folder, filename, expected_sha):
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ValueError("Aperture measurement requires a local artifact filename")
    path = folder / filename
    if not isinstance(expected_sha, str) or _sha(path) != expected_sha:
        raise ValueError("Preserved aperture measurement artifact hash changed")
    array = np.load(path, allow_pickle=False)
    if array.ndim != 2 or not np.isfinite(array).all():
        raise ValueError("Aperture measurement requires finite two-dimensional arrays")
    return array


def _sfr(rendered_field):
    """Use the predeclared central edge ROI of each physical field probe."""
    resolution = rendered_field.get("tests", {}).get("resolution", {})
    site = resolution.get("sites", {}).get("center", {})
    result = site.get("processed_system", {})
    value = result.get("mtf50_cycles_per_pixel")
    reason = result.get("mtf50_reason")
    tests = rendered_field.get("tests", {})
    if "isp_lab" in tests:
        support = tests["isp_lab"].get("metric_support", {}).get("resolution", {})
        invalid = support.get("invalid_roi_ids")
        if (not isinstance(invalid, list) or "center" in invalid
                or (not support.get("eligible") and not invalid)):
            return None, "central_edge_roi_outside_common_ISP_support", site.get("roi")
    if (
        resolution.get("status") != "ok"
        or result.get("status") != "ok"
        or reason
        or not _number(value)
        or not 0 < value <= 0.5
    ):
        return None, reason or "processed_sfr50_missing_invalid_or_unavailable", site.get("roi")
    return float(value), None, site.get("roi")


def measure_field(field_root, rendered_field, *, expected_frames=8, roi_row_col=None):
    """Measure saved captures without generating RAW or refitting the ISP.

    Mean signal is expected photogenerated electrons, excluding dark, from the
    saved noise-free rate. Noise is sqrt(mean_pixel(var_repeat(RAW, ddof=1)))
    referred to electrons. Both Gr and Gb sites use the identical ROI. A
    supplied sub-ROI requires matching, mask-derived saturation evidence for
    that exact support rectangle; full-crop summaries cannot substitute.
    Saturation is a conservative upper bound over every saved chart and frame,
    not an exact union of masks; missing stage summaries remain unavailable.
    """
    root = Path(field_root)
    capture_path = root / "capture.json"
    capture = json.loads(capture_path.read_text())
    if rendered_field.get("status") != "succeeded":
        raise ValueError("Failed field cannot supply aperture selection measurements")
    field_id = rendered_field.get("id")
    if field_id not in REQUIRED_FIELDS:
        raise ValueError("Aperture comparison requires center or outer-ne")
    if type(expected_frames) is not int or expected_frames < 2:
        raise ValueError("At least two independent captures are required")
    configuration, geometry = capture["configuration"], capture["geometry"]
    if configuration.get("frames") != expected_frames:
        raise ValueError("Capture count differs from the declared aperture comparison")
    origin = geometry.get("cfa_origin", {})
    if (
        origin.get("pattern") != "RGGB"
        or origin.get("local_row") != 0
        or origin.get("local_col") != 0
    ):
        raise ValueError("Aperture measurement requires native RGGB origin [0,0]")
    shape = tuple(geometry["native_shape_row_col"])
    if len(shape) != 2 or any(type(n) is not int or n < 2 or n % 2 for n in shape):
        raise ValueError("Aperture measurement requires an even native sensor ROI")
    restricted_roi = roi_row_col is not None
    roi = [0, shape[0], 0, shape[1]] if roi_row_col is None else roi_row_col
    if (
        not isinstance(roi, (list, tuple)) or len(roi) != 4
        or any(type(v) is not int for v in roi)
        or not (0 <= roi[0] < roi[1] <= shape[0] and 0 <= roi[2] < roi[3] <= shape[1])
        or roi[1] - roi[0] < 2 or roi[3] - roi[2] < 2
    ):
        raise ValueError("Aperture measurement requires a supported rectangle inside the native ROI")
    roi = list(roi)
    support_slice = np.s_[roi[0]:roi[1], roi[2]:roi[3]]
    exposure_ms = configuration.get("exposure_ms")
    if not _number(exposure_ms) or exposure_ms <= 0:
        raise ValueError("Aperture exposure must be finite and positive")
    exposure = exposure_ms / 1000
    readout = capture["readout"]
    summary = json.loads((root / "uniform" / "capture.json").read_text())
    model = summary.get("model")
    if model != default_electrical_model(readout):
        raise ValueError("Aperture metric supports the frozen linear FPN-off Reference readout")
    if (
        summary.get("noise_enabled") is not True
        or summary.get("exposure_s") != exposure
        or summary.get("analog_gain") != readout["gain"]
    ):
        raise ValueError("Uniform readout summary disagrees with independent capture conditions")
    white = 2 ** readout["bit_depth"] - 1
    dn_per_e = (
        model["conversion_gain_v_per_e"]
        * summary["analog_gain"]
        * white
        / model["adc"]["full_scale_v"]
        * model["digital_gain"]
    )
    if not math.isfinite(dn_per_e) or dn_per_e <= 0:
        raise ValueError("Invalid electron-to-DN transfer")
    rr, cc = np.indices(shape)
    green = (rr % 2) != (cc % 2)
    if restricted_roi:
        green &= (rr >= roi[0]) & (rr < roi[1]) & (cc >= roi[2]) & (cc < roi[3])
    records = capture["captures"]
    rate = _saved_array(
        root / "uniform", "mean-electron-rate.npy", records["uniform"]["rate_sha256"]
    )
    if rate.shape != shape or (rate < 0).any():
        raise ValueError("Saved photoelectron rate does not match the native ROI")
    uniform, hashes, saturation, missing_saturation = [], [], [], []
    raw_endpoint_fraction = 0.0
    green_endpoint_fraction = 0.0
    uniform_high_saturation_fraction = 0.0
    for chart in CHARTS:
        frames = records.get(chart, {}).get("raw", [])
        if len(frames) != expected_frames:
            raise ValueError("Every aperture chart requires the declared independent captures")
        seeds = [record.get("seed") for record in frames]
        if any(type(s) is not int for s in seeds) or len(set(seeds)) != expected_frames:
            raise ValueError("Temporal noise requires distinct recorded acquisition seeds")
        if len({record.get("file") for record in frames}) != expected_frames:
            raise ValueError("Repeated RAW file cannot be counted as independent acquisition")
        for index, record in enumerate(frames):
            raw = _saved_array(root / chart, record["file"], record["sha256"])
            if (
                raw.shape != shape
                or raw.dtype.kind not in "iu"
                or raw.min() < 0
                or raw.max() > white
            ):
                raise ValueError("Saved RAW must be integer ADC codes on the common native ROI")
            raw_endpoint_fraction = max(
                raw_endpoint_fraction, float(np.mean(raw[support_slice] >= white))
            )
            if chart == "uniform":
                uniform.append(raw[green].astype(float))
                hashes.append(record["sha256"])
                green_endpoint_fraction = max(
                    green_endpoint_fraction,
                    float(np.mean((raw[green] <= 0) | (raw[green] >= white))),
                )
            if restricted_roi and record.get("supported_roi_row_col") != roi:
                raise ValueError("Per-frame saturation support differs from the measurement ROI")
            fractions = record.get(
                "supported_saturation_fractions" if restricted_roi else "saturation_fractions", {}
            )
            keys = ("well_saturated_mask", "adc_high_mask", "digital_high_mask")
            if any(not _number(fractions.get(k)) or not 0 <= fractions[k] <= 1 for k in keys):
                missing_saturation.append(f"{chart}/raw-{index:02d}")
            else:
                saturation.append(min(1.0, sum(fractions[k] for k in keys)))
                if chart == "uniform":
                    uniform_high_saturation_fraction = max(
                        uniform_high_saturation_fraction,
                        min(1.0, sum(fractions[k] for k in keys)),
                    )
    samples = np.stack(uniform) / dn_per_e
    signal = float((rate[green] * exposure).mean())
    noise = float(np.sqrt(np.mean(np.var(samples, axis=0, ddof=1))))
    snr = signal / noise if signal > 0 and noise > 0 else None
    snr_db = float(20 * np.log10(snr)) if snr is not None else None
    sfr, sfr_reason, edge_roi = _sfr(rendered_field)
    sat = None if missing_saturation else max(raw_endpoint_fraction, max(saturation))
    issues = []
    if snr_db is None:
        issues.append("nonpositive_signal_or_zero_temporal_noise")
    if green_endpoint_fraction > 0:
        issues.append("uniform_green_noise_is_censored_by_adc_endpoints")
    if uniform_high_saturation_fraction > 0:
        issues.append("uniform_noise_is_censored_by_well_or_readout_saturation")
    if sfr_reason:
        issues.append(sfr_reason)
    if missing_saturation:
        issues.append("missing_per_frame_stage_saturation_evidence")
    return {
        "field_id": field_id,
        "mean_signal_e": signal,
        "temporal_noise_e": noise,
        "snr_linear": snr,
        "snr_db": snr_db,
        "processed_sfr50_cpp": sfr,
        "processed_sfr_status": "ok" if sfr_reason is None else "unavailable",
        "processed_sfr_reason": sfr_reason,
        "saturation_fraction": sat,
        "measurement_status": "ok" if not issues else "inconclusive",
        "measurement_issues": issues,
        "snr_censored": green_endpoint_fraction > 0 or uniform_high_saturation_fraction > 0,
        "measurement": {
            "chart": "uniform",
            "repeat_count": expected_frames,
            "roi_row_col": roi,
            "sites": ["Gr", "Gb"],
            "green_pixel_count": int(green.sum()),
            "dn_per_e": dn_per_e,
            "signal_definition": "mean saved pre-noise, pre-clipping photoelectron rate "
            "* exposure; dark excluded",
            "noise_definition": "sqrt(mean_green_pixel(var_repeat(RAW_DN / DN_per_e, ddof=1))); "
            "shot, dark, read and quantization included",
            "measured_mean_minus_black_and_dark_e": float(
                (np.mean(uniform) - readout["black_level_dn"]) / dn_per_e
                - readout["dark_current_e_per_s"] * exposure
            ),
            "uniform_green_adc_endpoint_fraction": green_endpoint_fraction,
            "uniform_high_saturation_upper_bound": uniform_high_saturation_fraction,
            "saturation_definition": "maximum over all four charts and repeats of "
            "min(1, well + ADC-high + digital-high fractions), also bounded below by "
            "observed RAW-white fraction; conservative union upper bound",
            "missing_saturation_records": missing_saturation,
            "processed_sfr_roi": edge_roi,
            "processed_sfr_definition": "SFR of mean independently processed linear RGB "
            "frames, central fixed edge ROI of this field; not lens-only MTF",
            "capture_sha256": _sha(capture_path),
            "uniform_raw_sha256": hashes,
            "uniform_rate_sha256": records["uniform"]["rate_sha256"],
            "wavelength_nm": list(range(450, 651, 10)),
            "spectral_scope": "450-650nm at 10nm; native source-cell-center approximation",
        },
    }


def assess(rows, policy=None):
    """Select only fully measured candidates feasible in both declared fields."""
    limits = {**DEFAULT_POLICY, **(policy or {})}
    ranges = {
        "min_snr_db": (-20, 80),
        "min_relative_signal": (0, 1),
        "max_saturation_fraction": (0, 1),
    }
    if set(limits) != set(DEFAULT_POLICY) or any(
        not _number(v) or not ranges[k][0] <= v <= ranges[k][1] for k, v in limits.items()
    ):
        raise ValueError("Invalid declared aperture selection policy")
    ids = [r.get("candidate_id") for r in rows]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise ValueError("Aperture rows require unique candidate IDs")
    decisions, feasible = [], []
    for row in rows:
        reasons = []
        fields = row.get("fields", [])
        field_ids = [f.get("field_id") for f in fields]
        if len(field_ids) != len(REQUIRED_FIELDS) or set(field_ids) != set(REQUIRED_FIELDS):
            reasons.append("missing_or_duplicate_required_fields")
        f_number = row.get("f_number")
        if not _number(f_number) or f_number <= 0:
            reasons.append("invalid_applied_f_number")
        snrs, sfrs = [], []
        for field in fields:
            name = field.get("field_id", "unknown")
            metrics = {
                key: field.get(key)
                for key in (
                    "mean_signal_e",
                    "temporal_noise_e",
                    "snr_linear",
                    "snr_db",
                    "relative_signal",
                    "processed_sfr50_cpp",
                    "saturation_fraction",
                )
            }
            if any(not _number(value) for value in metrics.values()):
                reasons.append(name + ":missing_or_nonfinite_metric")
                continue
            if (
                metrics["mean_signal_e"] <= 0
                or metrics["temporal_noise_e"] <= 0
                or metrics["snr_linear"] <= 0
                or metrics["relative_signal"] < 0
                or not 0 < metrics["processed_sfr50_cpp"] <= 0.5
                or not 0 <= metrics["saturation_fraction"] <= 1
            ):
                reasons.append(name + ":invalid_metric_domain")
            expected_snr = metrics["mean_signal_e"] / max(metrics["temporal_noise_e"], 1e-300)
            if (
                not math.isclose(metrics["snr_linear"], expected_snr, rel_tol=1e-6)
                or expected_snr <= 0
                or not math.isclose(metrics["snr_db"], 20 * math.log10(expected_snr), abs_tol=1e-6)
            ):
                reasons.append(name + ":inconsistent_signal_noise_snr")
            if (
                field.get("measurement_status") not in (None, "ok")
                or field.get("processed_sfr_status") not in (None, "ok")
                or field.get("processed_sfr_reason")
                or field.get("snr_censored")
            ):
                reasons.append(name + ":censored_or_inconclusive_measurement")
            for key, bad in (
                ("snr_db", metrics["snr_db"] < limits["min_snr_db"]),
                ("relative_signal", metrics["relative_signal"] < limits["min_relative_signal"]),
                (
                    "saturation_fraction",
                    metrics["saturation_fraction"] > limits["max_saturation_fraction"],
                ),
            ):
                if bad:
                    reasons.append(name + ":" + key + "_constraint")
            snrs.append(metrics["snr_db"])
            sfrs.append(metrics["processed_sfr50_cpp"])
        eligible = not reasons
        decisions.append(
            {
                "candidate_id": row["candidate_id"],
                "eligible": eligible,
                "reasons": reasons,
                "worst_field_snr_db": min(snrs) if snrs else None,
                "worst_field_sfr50_cpp": min(sfrs) if sfrs else None,
            }
        )
        if eligible:
            feasible.append((min(sfrs), min(snrs), -f_number, row["candidate_id"]))
    feasible.sort(key=lambda value: (-value[0], -value[1], -value[2], value[3]))
    return {
        "policy": limits,
        "required_fields": list(REQUIRED_FIELDS),
        "recommended_candidate_id": feasible[0][3] if feasible else None,
        "eligible_candidate_ids": [item[3] for item in feasible],
        "decisions": decisions,
        "status": "selected" if feasible else "no_feasible_candidate",
        "objective": "maximize minimum processed SFR50 across center and outer-ne",
        "tie_breakers": ["higher worst-field SNR", "lower applied F-number", "candidate ID"],
        "scope": "Demo selection conditional on fixed charts, exposure, readout, ISP and policy; "
        "not a product-optimal aperture",
    }
