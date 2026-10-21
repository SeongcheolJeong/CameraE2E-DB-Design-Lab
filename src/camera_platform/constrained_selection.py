"""Finite camera-design selection with explicit limits and a held-out boundary.

These are offline, model-conditioned design decisions, not RAW-only camera
controls or physical approval. Minimum signal is not a substitute for photon
information: a separate lower-tail input-referred SNR criterion is applied.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import math

import numpy as np


@dataclass(frozen=True)
class SelectionLimits:
    minimum_green_dn: float = 120.0
    minimum_green_snr_p10: float = 10.0
    maximum_well_fraction: float = 0.01
    maximum_white_fraction: float = 0.01
    maximum_exposure_ms: float = 4.0

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} requires a finite nonnegative number")
        if self.maximum_exposure_ms <= 0:
            raise ValueError("Exposure bound must be positive")
        if max(self.maximum_well_fraction, self.maximum_white_fraction) > 1:
            raise ValueError("Saturation fractions must not exceed one")


def summarize_output(output: dict) -> dict:
    """Summarize the response chain, preserving sampled versus predicted meaning.

    SNR uses generated photocharge and the declared independent-reset readout
    model; it is NOT estimated from spatial image variation. Poisson photo/dark,
    input read noise, post-gain voltage noise, and quantization are included.
    FPN, model-parameter uncertainty and interpixel covariance are not included.
    Clipping is assessed independently and cannot increase candidate feasibility.
    """
    arrays, summary = output["arrays"], output["summary"]
    model = summary["electrical"]["model"]
    if model["fixed_pattern"]["prnu_sigma"] or model["fixed_pattern"]["dsnu_e_per_s"]:
        raise ValueError("This predicted SNR requires zero declared fixed-pattern variance")
    photo = np.asarray(arrays["mean_photocharge_e"], dtype=float)
    raw = np.asarray(arrays["raw_dn"])
    sites = np.asarray(arrays["cfa_channel_index"])
    if photo.ndim != 2 or photo.shape != raw.shape or sites.shape != raw.shape:
        raise ValueError("Charge, RAW and native CFA coordinates must match")
    native = len(summary["site_metrics"]) == 4
    green = np.isin(sites, [1, 2]) if native else sites == 1
    if not green.any() or not np.isfinite(photo).all() or np.any(photo < 0):
        raise ValueError("Finite nonnegative photocharge and green sites are required")
    gain = summary["analog_gain"]
    adc = model["adc"]
    white, black = 2 ** adc["bit_depth"] - 1, adc["black_level_dn"]
    volts_per_e = model["conversion_gain_v_per_e"] * gain
    adc_dn_per_e = volts_per_e * white / adc["full_scale_v"]
    dark = np.asarray(arrays["dark_current_e_per_s_map"]) * summary["exposure_s"]
    variance = (photo + dark + model["read_noise_e"] ** 2
                + (model["post_gain_noise_v"] / volts_per_e) ** 2
                + 1 / (12 * adc_dn_per_e ** 2))
    if model["digital_gain"] != 1:
        variance = variance + 1 / (12 * (adc_dn_per_e * model["digital_gain"]) ** 2)
    snr = np.divide(photo, np.sqrt(variance), out=np.zeros_like(photo), where=variance > 0)
    if not np.isfinite(snr).all():
        raise ValueError("Predicted SNR is nonfinite")
    return {
        "green_dn": float(np.mean(raw[green].astype(float) - black)),
        "green_snr_p10": float(np.quantile(snr[green], 0.1)),
        "green_photocharge_mean_e": float(photo[green].mean()),
        "mean_photocharge_e": float(photo.mean()),
        "well_fraction": float(np.mean(arrays["well_saturated_mask"])),
        "adc_high_before_rounding_fraction": float(np.mean(arrays["adc_high_mask"])),
        "white_fraction": float(np.mean(raw == white)),
        "raw_min_dn": int(raw.min()), "raw_max_dn": int(raw.max()),
        "snr_basis": "predicted_photo_dark_read_postgain_quantization_not_measured",
    }


def violations(metrics: dict, limits: SelectionLimits) -> list[str]:
    """Inclusive engineering limits; missing/nonfinite observations are errors."""
    bounds = {
        "green_dn": (limits.minimum_green_dn, None, "minimum_signal"),
        "green_snr_p10": (limits.minimum_green_snr_p10, None, "minimum_snr"),
        "well_fraction": (None, limits.maximum_well_fraction, "well_saturation"),
        "white_fraction": (None, limits.maximum_white_fraction, "raw_clipping"),
        "exposure_ms": (None, limits.maximum_exposure_ms, "exposure_time"),
    }
    failed = []
    for key, (low, high, reason) in bounds.items():
        value = metrics.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Missing or invalid decision metric: " + key)
        if key.endswith("fraction") and value > 1:
            raise ValueError("Invalid observed fraction: " + key)
        if (low is not None and value < low) or (high is not None and value > high):
            failed.append(reason)
    return failed


def select_candidate(candidates: list[dict], observations: list[dict], *,
                     training_scenario_ids: list[str], limits: SelectionLimits) -> dict:
    """Select using the complete training grid only; never consume held-out rows.

    Primary objective: maximize the worst scenario's green SNR p10. Ties prefer
    shorter exposure, lower gain, then lexical identity. No infeasible candidate
    is promoted and a no-feasible result leaves selected_candidate_id as None.
    """
    if not training_scenario_ids or len(set(training_scenario_ids)) != len(training_scenario_ids):
        raise ValueError("Require unique, predeclared training scenarios")
    by_id = {c["candidate_id"]: c for c in candidates}
    if not candidates or len(by_id) != len(candidates):
        raise ValueError("Require unique candidate identities")
    cells = {}
    for row in observations:
        if row.get("split") != "train" or row.get("scenario_id") not in training_scenario_ids:
            raise ValueError("Held-out or undeclared rows cannot enter selection")
        key = row.get("candidate_id"), row["scenario_id"]
        if key[0] not in by_id or key in cells:
            raise ValueError("Unknown or duplicated training cell")
        candidate = by_id[key[0]]
        if row.get("exposure_ms") != candidate["exposure_ms"] or row.get("gain") != candidate["gain"]:
            raise ValueError("Observation settings differ from the candidate")
        cells[key] = row
    ranked, reason_counts = [], Counter()
    for identity, candidate in by_id.items():
        exposure, gain = candidate.get("exposure_ms"), candidate.get("gain")
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (exposure, gain)):
            raise ValueError("Candidate exposure and gain must be finite and positive")
        if exposure > limits.maximum_exposure_ms:
            if any(k[0] == identity for k in cells):
                raise ValueError("Do not acquire preexcluded exposure candidates")
            item = {**candidate, "feasible": False, "failures": ["exposure_time"],
                    "evaluated_scenarios": 0, "worst_green_snr_p10": None,
                    "failed_scenarios": {"exposure_time": "preexcluded_without_runtime"}}
        else:
            rows = []
            for scenario in training_scenario_ids:
                if (identity, scenario) not in cells:
                    raise ValueError("A candidate is missing a required training scenario")
                rows.append(cells[identity, scenario])
            failed = {r["scenario_id"]: violations(r, limits) for r in rows if violations(r, limits)}
            reasons = sorted({f for values in failed.values() for f in values})
            item = {**candidate, "feasible": not reasons, "failures": reasons,
                    "failed_scenarios": failed, "evaluated_scenarios": len(rows),
                    "worst_green_snr_p10": min(r["green_snr_p10"] for r in rows),
                    "minimum_green_dn": min(r["green_dn"] for r in rows),
                    "maximum_well_fraction": max(r["well_fraction"] for r in rows),
                    "maximum_white_fraction": max(r["white_fraction"] for r in rows)}
        reason_counts.update(item["failures"])
        ranked.append(item)
    feasible = sorted([r for r in ranked if r["feasible"]], key=lambda r: (
        -r["worst_green_snr_p10"], r["exposure_ms"], r["gain"], r["candidate_id"]))
    return {"selected_candidate_id": feasible[0]["candidate_id"] if feasible else None,
            "status": "selected_on_training_only" if feasible else "no_feasible_candidate",
            "limits": asdict(limits), "feasible_count": len(feasible),
            "candidate_count": len(candidates), "candidates": ranked,
            "feasible_ranking": [r["candidate_id"] for r in feasible],
            "failure_counts_nonexclusive": dict(reason_counts),
            "training_scenario_ids": list(training_scenario_ids),
            "objective": "maximize_worst_green_SNR_p10_then_shorter_exposure_lower_gain_id",
            "automatic_approval": False, "physical_validation": "not_established"}
