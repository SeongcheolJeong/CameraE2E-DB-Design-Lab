"""Small spectral scan and native 24-patch RAW fixture; never physical evidence."""

from __future__ import annotations

import base64
import io

import numpy as np

from .registry import digest


def template(seed=20261017):
    from pyisetcam.assets import AssetStore

    from .calibration_spectral import derive_qe, profile_sha256

    store = AssetStore.default()
    wave = np.arange(380.0, 781.0, 10.0)
    _, reflectance = store.load_reflectances("macbethChart.mat", wave_nm=wave)
    _, illuminant = store.load_illuminant("D65", wave_nm=wave)
    qe = np.column_stack(
        [
            0.65 * np.exp(-0.5 * ((wave - 615) / 42) ** 2),
            0.70 * np.exp(-0.5 * ((wave - 540) / 35) ** 2),
            0.58 * np.exp(-0.5 * ((wave - 450) / 30) ** 2),
        ]
    )
    # This is an authored sensor, not a measured commercial sensor specification.
    qe = np.maximum(qe, 0.0001)
    black, gain, reference_e = 64.0, 0.5, 2000.0
    conditions = {
        "camera_id": f"spectral-synthetic-{seed}",
        "mode_id": "linear-rggb-12",
        "gain": 1.0,
        "temperature_c": 25.0,
    }
    provenance = {
        "calibration_id": "synthetic-radiometry-v1",
        "session_id": f"scan-{seed}",
        "acquisition_id": f"scan-{seed}",
        "instrument_id": "synthetic-monochromator",
    }
    scan = {
        "schema": "camera_qe_scan_v1",
        "wavelength_nm": wave.tolist(),
        "channels": ["R", "G", "B"],
        "mean_dn": (black + 3000 * gain * qe).tolist(),
        "dark_dn": np.full_like(qe, black).tolist(),
        "dn_per_e": gain,
        "exposure_s": [0.01] * len(wave),
        "bandwidth_nm": 5.0,
        "adc_white_dn": 4095.0,
        "clipped_fraction": np.zeros_like(qe).tolist(),
        "dark_clipped_fraction": np.zeros_like(qe).tolist(),
        "reference_plane": "sensor_surface",
        "conditions": conditions,
        "source_kind": "synthetic",
        "provenance": provenance,
        "included_effects": ["cfa", "microlens", "photodiode"],
        "incident_photons_per_pixel": [3000.0] * len(wave),
        "radiometry": {
            "kind": "absolute_calibrated",
            "calibration_id": "synthetic-radiometry-v1",
            "unit": "photons/pixel",
        },
    }
    baseline = derive_qe(scan)
    parent = baseline["profile_sha256"]
    baseline["qe"] = (qe * [0.80, 1.10, 0.70]).tolist()
    baseline["method"] = "authored_baseline_v1"
    baseline["provenance"].update(
        parent_profile_sha256=parent, baseline_reason="Authored spectral baseline mismatch"
    )
    baseline["profile_sha256"] = profile_sha256(baseline)
    # Convert energy SPD shape into photon shape; amplitude is an authored dose.
    photon_spd = illuminant * wave
    photon_spd = photon_spd / photon_spd.mean() * 20.0
    dose = reflectance.T * photon_spd
    electrons = np.trapezoid(dose[:, :, None] * qe[None, :, :], wave, axis=1)
    true_ccm = np.array([[1.30, -0.12, -0.08], [-0.10, 1.24, -0.08], [0.03, -0.10, 1.20]])
    wb = [1.1, 1.0, 1.2]
    target = (electrons / reference_e * wb) @ true_ccm
    spd_hash = digest({"wavelength_nm": wave.tolist(), "energy_spd": illuminant.tolist()})
    originals = {}
    ccm = []
    rois = [[r * 24 + 2, c * 24 + 2, 20, 20] for r in range(4) for c in range(6)]
    for index, role in enumerate(("training", "validation")):
        rng = np.random.default_rng(seed + 100 + index)
        raw = np.full((1, 8, 96, 144), black, dtype=np.uint16)
        for patch, (y, x, _, _) in enumerate(rois):
            y, x = y - 2, x - 2
            rr, cc = np.indices((24, 24))
            sites = np.where(
                (rr % 2 == 0) & (cc % 2 == 0), 0, np.where((rr % 2 == 1) & (cc % 2 == 1), 2, 1)
            )
            charge = rng.poisson(electrons[patch][sites], size=(8, 24, 24))
            noise = rng.normal(0, 3.0, size=charge.shape)
            raw[0, :, y : y + 24, x : x + 24] = np.rint(
                np.clip(black + gain * (charge + noise), 0, 4095)
            ).astype(np.uint16)
        stream = io.BytesIO()
        np.save(stream, raw, allow_pickle=False)
        originals[role] = {
            "base64": base64.b64encode(stream.getvalue()).decode(),
            "cfa": "RGGB",
            "bit_depth": 12,
            "crop_origin": [0, 0],
            "rois": rois,
        }
        ccm.append(
            {
                "camera_rgb": [],
                "target_rgb": target.tolist(),
                "target_space": "camera_linear_rgb",
                "white_balance": wb,
                "exposure_scale": 1.0,
                "session_id": f"{role}-{seed}",
                "acquisition_id": f"{role}-{seed}",
                "source_kind": "synthetic",
                "illuminant_id": "D65-380-780nm",
                "illuminant_spd_sha256": spd_hash,
                "conditions": conditions,
                "whitepoint": "D65",
                "normalization": "fixed_reference_electrons",
                "input_domain": "camera_linear_rgb",
                "clipped_patch_indices": [],
                "source_artifact_sha256": "0" * 64,
                "normalization_metadata": {
                    "black_level_dn": black,
                    "dn_per_e": gain,
                    "reference_white_e": reference_e,
                    "formula": "(native_mean_dn-black_level_dn)/dn_per_e/reference_white_e",
                },
            }
        )
    ccm[0]["baseline_matrix"] = np.eye(3).tolist()
    ccm[1]["thresholds"] = {"max_rmse": 0.006}
    raw_validation = {
        "schema": "camera_spectral_raw_validation_v1",
        "wavelength_nm": wave.tolist(),
        "photon_spectral_dose": dose.tolist(),
        "photon_dose_unit": "photons/pixel/nm",
        "measured_raw_rgb_dn": [],
        "black_level_dn": black,
        "dn_per_e": gain,
        "adc_white_dn": 4095.0,
        "exposure_s": 0.01,
        "conditions": conditions,
        "source_kind": "synthetic",
        "normalization": "none",
        "clipped_fraction": [],
        "provenance": {
            **provenance,
            "session_id": f"validation-{seed}",
            "acquisition_id": f"validation-{seed}",
        },
        "reference_plane": "sensor_surface",
        "dose_includes": ["lens", "filters"],
        "radiometry": {
            "kind": "absolute_calibrated",
            "calibration_id": "synthetic-radiometry-v1",
            "unit": "photons/pixel/nm",
        },
        "tolerance": {"channel_rmse_dn": [3.0, 3.0, 3.0], "max_abs_dn": 8.0},
    }
    return {
        "schema": "camera_spectral_color_task_v1",
        "name": "파장별 QE → 맥베스 24패치 RAW → ISP CCM · 합성 검증",
        "qe_scan": scan,
        "baseline_qe": baseline,
        "raw_validation": raw_validation,
        "ccm_training": ccm[0],
        "ccm_validation": ccm[1],
        "raw_originals": originals,
        "source_notes": {
            "seed": seed,
            "chart": "ISETCam Macbeth 24 spectral reflectances",
            "spectral_band_nm": [380, 780],
            "band_scope": "visible subset only; no UV/NIR claim",
            "reflectance_sha256": digest(reflectance.tolist()),
            "illuminant_sha256": spd_hash,
            "asset_snapshot": str(store._sha),
            "target": "Authored synthetic camera linear ISP output; not measured colour accuracy",
            "limitations": (
                "Same-model synthetic evidence; no instrument or physical-camera validation"
            ),
        },
    }
