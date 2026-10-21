#!/usr/bin/env python3
"""Read back a completed IRCF package; this is artifact/contract verification only.

No optical solve or acquisition is repeated. Each CCM's first ISP frame is
replayed from the one preserved RAW capture to verify that binding directly.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.io import loadmat

from camera_platform import reference_ircf_job as job
from camera_platform import reference_workshop_job as workshop
from camera_platform.offline_assets import offline_asset_store
from camera_platform.reference_sensor_modes import process_electrons
from camera_platform.registry import digest
from camera_platform.service import PlatformService
from camera_platform.virtual_lab_job import sha256

PROJECT = Path(__file__).resolve().parents[1]
CASE_KEYS = ("id", "field_id", "illuminant", "chart", "filter_id", "angular_mode")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def finite_values(value, label):
    """Numbers may be finite or explicitly missing; NaN never means unavailable."""
    if isinstance(value, dict):
        for key, item in value.items():
            finite_values(item, label + "." + key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            finite_values(item, f"{label}[{index}]")
    elif isinstance(value, (int, float)):
        require(np.isfinite(value), "Nonfinite recorded metric: " + label)


def package_size(root):
    files = read(root / "manifest.json")["files"]
    sizes = [(root / path).stat() for path in files]
    return {
        "files": len(files),
        "logical_bytes": sum(s.st_size for s in sizes),
        "allocated_bytes": sum(max(s.st_size, getattr(s, "st_blocks", 0) * 512) for s in sizes),
    }


def verify_calibration(workspace, root, summary, filters):
    calibration = read(root / "calibration.json")
    assets = read(root / "spectral-assets.json")
    require(
        sha256(root / "calibration.json") == summary["calibration_sha256"],
        "Calibration hash differs from completed result",
    )
    require(calibration["asset_metadata"] == assets, "Fitting/evaluation asset bindings differ")
    require(
        assets["macbeth_used_in_fit"] is False and assets["munsell_nir_available"] is False,
        "Calibration must retain visible-only disjoint training scope",
    )
    require(
        calibration["center_matrix_applied_to_all_fields"] is True,
        "Calibration changed into field-dependent fitting",
    )
    store = offline_asset_store(workspace=workspace, source=workspace / "camerae2e_db")
    for role, record in assets["files"].items():
        require(
            sha256(store.resolve(record["file"])) == record["sha256"],
            "Spectral source asset changed: " + role,
        )
    train, evaluate = assets["files"]["munsell"], assets["files"]["macbeth"]
    require(train["sha256"] != evaluate["sha256"], "Training and evaluation share one asset")
    for record, count in ((train, 64), (evaluate, 24)):
        values = np.asarray(loadmat(store.resolve(record["file"]), simplify_cells=True)["data"])
        require(
            values.ndim == 2 and values.shape[1] == count,
            f"Reflectance asset no longer contains {count} declared samples",
        )
    require(
        set(calibration["calibrations"]) == {"baseline", *filters}, "Unexpected fitted filter set"
    )
    for name, record in calibration["calibrations"].items():
        require(
            record["training_count"] == 64 and record["evaluation_used_for_fit"] is False,
            "Fitted calibration changed training/evaluation contract: " + name,
        )
        require(
            record["nir_aware_fit"] is False and record["white_error"] < 1e-8,
            "Visible white constraint or NIR fitting scope changed: " + name,
        )
        matrix = np.asarray(record["matrix"], float)
        require(matrix.shape == (3, 3) and np.isfinite(matrix).all(), "Invalid saved CCM")
    return calibration, {
        "training_count": 64,
        "evaluation_count": 24,
        "training_sha256": train["sha256"],
        "evaluation_sha256": evaluate["sha256"],
        "evaluation_used_for_fit": False,
    }


def verify_case(root, case, specification, calibration):
    identity = case["id"]
    files = case["files"]
    expected_files = {
        "raw": "capture.npz",
        "arrays": "arrays.npz",
        "readout": "readout.json",
        "baseline_display": "baseline.png",
        "refit_display": "refit.png",
    }
    manifest = read(root / "manifest.json")["files"]
    for key, filename in expected_files.items():
        require(
            files[key] == f"cases/{identity}/{filename}" and files[key] in manifest,
            identity + ": artifact reference is outside this retained case",
        )
    require(case["ccm_reuses_same_raw"] is True, identity + ": no preserved RAW binding")
    require(case["source"] == specification["source"], identity + ": wrong source binding")
    require(case["frames"] == 8 and case["exposure_ms"] == 4, identity + ": capture changed")
    raw_path, arrays_path = root / files["raw"], root / files["arrays"]
    readout = read(root / files["readout"])
    transfer, conditions = readout["calibration"], readout["capture"]
    require(
        conditions["exposure_s"] == 0.004 and conditions["analog_gain"] == 1,
        identity + ": capture exposure/gain changed",
    )
    require(
        conditions["geometry"]["capture_shape"] == [256, 256],
        identity + ": not a native256 capture",
    )
    wanted_seeds = [(specification["seed"] + case["seed_offset"] + i) % 2**32 for i in range(8)]
    require(readout["seed_sequence"] == wanted_seeds, identity + ": seed program changed")
    with (
        np.load(raw_path, allow_pickle=False) as capture,
        np.load(arrays_path, allow_pickle=False) as data,
    ):
        raw, electrons = capture["raw_frames"], capture["electron_frames"]
        require(
            raw.dtype.kind in "iu" and raw.shape == (8, 256, 256),
            identity + ": integer native RAW contract failed",
        )
        require(
            raw.min() >= 0 and raw.max() <= transfer["white_level_dn"],
            identity + ": RAW exceeds ADC code range",
        )
        require(
            electrons.shape == raw.shape and np.isfinite(electrons).all(),
            identity + ": calibrated electron frames invalid",
        )
        decoded = (raw.astype(float) - transfer["black_level_dn"]) / transfer["dn_per_e"] - capture[
            "dark_e"
        ]
        require(np.array_equal(decoded, electrons), identity + ": RAW calibration changed")
        variance = capture["variance_e2"]
        require(
            variance.shape == (256, 256) and np.isfinite(variance).all() and np.all(variance >= 0),
            identity + ": invalid model variance",
        )
        for name in ("valid_frames", "well_saturated_frames", "adc_saturated_frames"):
            require(
                capture[name].shape == raw.shape and capture[name].dtype == bool,
                identity + ": invalid capture mask " + name,
            )
        require(
            np.array_equal(
                capture["valid_frames"],
                ~(capture["well_saturated_frames"] | capture["adc_saturated_frames"]),
            ),
            identity + ": capture validity differs from saturation masks",
        )
        planes, vis, nir = (
            data[k]
            for k in (
                "planes_electron_rate",
                "vis_planes_electron_rate",
                "nir_planes_electron_rate",
            )
        )
        for values in (planes, vis, nir):
            require(
                values.shape == (256, 256, 4) and np.isfinite(values).all() and np.all(values >= 0),
                identity + ": invalid mean site response",
            )
        require(
            np.allclose(vis + nir, planes, rtol=1e-12, atol=1e-12),
            identity + ": VIS plus NIR differs from total response",
        )
        rows, cols = np.indices((256, 256))
        sites = np.array([[0, 1], [2, 3]])[rows % 2, cols % 2]
        mosaic = np.take_along_axis(planes, sites[..., None], axis=-1)[..., 0]
        require(
            np.array_equal(data["mean_electron_rate"], mosaic),
            identity + ": RGGB mean response differs from site planes",
        )
        expected_fraction = float(nir.sum() / planes.sum()) if planes.sum() else 0.0
        expected_sites = planes.mean(axis=(0, 1)) * 0.004
        for ccm, filter_id in (("baseline", "baseline"), ("refit", case["filter_id"])):
            linear, valid = data[ccm + "_linear_frames"], data[ccm + "_valid"]
            require(
                linear.shape == (8, 256, 256, 3) and np.isfinite(linear).all(),
                identity + ": invalid linear ISP frames",
            )
            require(
                valid.shape == (256, 256) and valid.dtype == bool,
                identity + ": invalid common ISP mask",
            )
            replay = process_electrons(
                decoded[0],
                calibration["calibrations"][filter_id]["matrix"],
                lsc_gain=data["lsc_gain"],
                valid=capture["valid_frames"][0],
                variance_e2=variance,
                display_white=specification["isp"]["display_white_level"],
                gamma=specification["isp"]["tone_gamma"],
            )
            require(
                np.array_equal(replay["linear"], linear[0]),
                identity + ": CCM result does not replay from preserved RAW",
            )
            expected_png = np.uint8(np.round(np.clip(replay["display"], 0, 1) * 255))
            require(
                np.array_equal(
                    np.asarray(Image.open(root / files[ccm + "_display"])), expected_png
                ),
                identity + ": ISP PNG does not match fixed display settings",
            )
            metrics, errors = case["metrics"][ccm], case["patch_delta_e"][ccm]
            finite_values(metrics, identity + "." + ccm)
            finite_values(errors, identity + ".patch_delta_e")
            count = 24 if case["chart"] == "color" else 0
            present = [e for e in errors if e is not None]
            require(
                len(errors) == count
                and metrics["color_total_patches"] == count
                and metrics["color_valid_patches"] == len(present),
                identity + ": wrong valid/evaluated color-patch counts",
            )
            if present:
                require(
                    np.isclose(metrics["delta_e00_mean"], np.mean(present))
                    and np.isclose(metrics["delta_e00_max"], max(present)),
                    identity + ": color summary differs from retained patches",
                )
            else:
                require(
                    metrics["delta_e00_mean"] is None and metrics["delta_e00_max"] is None,
                    identity + ": missing color evidence was promoted to a score",
                )
            require(
                np.isclose(metrics["nir_fraction"], expected_fraction)
                and np.allclose(metrics["site_mean_electrons"], expected_sites),
                identity + ": spectral contribution metric differs from retained arrays",
            )
            common = valid.copy()
            common[:8] = common[-8:] = False
            common[:, :8] = common[:, -8:] = False
            if common.any():
                noise = float(np.sqrt(np.var(electrons[:, common], axis=0, ddof=1).mean()))
                require(
                    np.isclose(metrics["temporal_noise_e"], noise),
                    identity + ": temporal noise differs from repeated RAW",
                )
            else:
                require(
                    metrics["temporal_noise_e"] is None,
                    identity + ": unavailable noise was promoted to a score",
                )
        if case["filter_id"] == "baseline":
            require(
                np.array_equal(data["baseline_linear_frames"], data["refit_linear_frames"]),
                identity + ": identical baseline CCM produced different linear results",
            )
            require(
                sha256(root / files["baseline_display"]) == sha256(root / files["refit_display"]),
                identity + ": identical baseline CCM produced different PNGs",
            )
    return {
        **{key: case[key] for key in CASE_KEYS},
        "raw_sha256": sha256(raw_path),
        "new_raw_frames": 8,
        "first_frame_ccm_replays": 2,
        "metrics": case["metrics"],
    }


def verify(service, run_id):
    started = time.monotonic()
    record = job.get(service, run_id)
    require(record["status"] == "succeeded", "IRCF study is not completed")
    summary, spec = record["result"]["summary"], record["ircf_study"]
    require(
        summary["specification"] == spec and summary["source"] == spec["source"],
        "Completed result differs from submitted specification/source",
    )
    require(summary["source_code"] == record["source_code"], "Source code evidence binding changed")
    require(
        summary["raw_preserved"] and summary["ccm_reprocessing_reused_raw"],
        "Missing original capture/same-RAW processing contract",
    )
    workshop.verify_files(service, record)
    source_record = workshop.get(service, spec["source"]["run_id"])
    source_root, _ = job.source(service, spec, verify=True)
    workshop.verify_files(service, source_record)
    root = workshop._directory(service, run_id)
    require(read(root / "specification.json") == spec, "Saved specification differs from job")
    calibration, fitting = verify_calibration(service.workspace, root, summary, spec["filters"])
    wanted = job.case_specs(spec)
    require(
        summary["cases"] == [{k: item[k] for k in CASE_KEYS} for item in wanted],
        "Missing/duplicate acquisition cases or unexpected ordering",
    )
    optical = read(root / "optical-response.json")
    require(
        optical["revision"] == summary["optical_revision"]
        and sha256(root / "optics.npz") == optical["arrays_sha256"],
        "Pinned optical response differs from preserved arrays",
    )
    findings = []
    for expected in wanted:
        case = job.case_detail(service, run_id, expected["id"])
        require(all(case[key] == value for key, value in expected.items()), "Case program mismatch")
        findings.append(verify_case(root, case, spec, calibration))
    frames = sum(item["new_raw_frames"] for item in findings)
    require(frames == summary["new_raw_frames"], "Fresh acquisition frame count mismatch")
    size = package_size(root)
    require(size["logical_bytes"] == summary["package_bytes"], "Published package size changed")
    return {
        "status": "verified",
        "verification_scope": "artifact_contract_and_first_frame_isp_replay",
        "run_id": run_id,
        "manifest_sha256": summary["manifest_sha256"],
        "specification_sha256": digest(spec),
        "cases": len(findings),
        "new_raw_frames": frames,
        "first_frame_ccm_replays": len(findings) * 2,
        **size,
        "source": {**spec["source"], "verified_directory": str(source_root)},
        "color_fitting": fitting,
        "findings": findings,
        "verification_elapsed_s": time.monotonic() - started,
        "physical_validation": "not_established",
        "optics_or_acquisition_recomputed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workspace", type=Path, default=PROJECT)
    parser.add_argument(
        "--output", type=Path, default=PROJECT / "outputs/reference-ircf-lab-20261012"
    )
    args = parser.parse_args()
    if not re.fullmatch(r"run-[0-9a-f]{32}", args.run_id):
        parser.error("--run-id must identify one immutable run")
    if not (args.state_dir / "registry.sqlite").is_file():
        parser.error("--state-dir must contain an existing registry.sqlite")
    output = args.output / f"{args.run_id}-verification.json"
    try:
        result = verify(PlatformService(args.state_dir, args.workspace), args.run_id)
    except Exception as error:
        result = {
            "status": "failed",
            "run_id": args.run_id,
            "error": f"{type(error).__name__}: {error}",
            "physical_validation": "not_established",
        }
    args.output.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "findings"}, ensure_ascii=False, indent=2
        )
    )
    return 0 if result["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
