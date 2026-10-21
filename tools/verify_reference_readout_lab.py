#!/usr/bin/env python3
"""Verify executed readout packages against retained RAW and source hashes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from run_reference_readout_lab import RECIPES

from camera_platform.sdk import PlatformClient
from camera_platform.virtual_lab_job import sha256


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text())


def verify_manifest(root, expected):
    require(sha256(root / "manifest.json") == expected, "Manifest binding changed")
    files = read(root / "manifest.json")["files"]
    total = allocated = 0
    for relative, checksum in files.items():
        path = (root / relative).resolve()
        require(path.is_relative_to(root.resolve()), "Manifest path escaped package")
        require(path.is_file() and sha256(path) == checksum, "Changed artifact: " + relative)
        stat = path.stat()
        total += stat.st_size
        allocated += max(stat.st_size, getattr(stat, "st_blocks", 0) * 512)
    return {"files": len(files), "logical_bytes": total, "allocated_bytes": allocated}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument(
        "--output", type=Path, default=Path("outputs/reference-readout-lab-20261012")
    )
    args = parser.parse_args()
    receipts = read(args.output / "delivery.json")["studies"]
    require(len({r["recipe"] for r in receipts}) == len(receipts), "Duplicate recipe receipts")
    verified, sources, findings = [], {}, []
    with PlatformClient(args.base_url, timeout=120) as client:
        for receipt in receipts:
            job = client.reference_sensor_mode_study(receipt["id"])
            require(job["status"] == "succeeded", "Incomplete readout study")
            summary = job["result"]["summary"]
            spec = job["sensor_mode_study"]
            require(
                job["id"] == receipt["id"] and spec["recipe"] == receipt["recipe"],
                "Receipt points to another run or recipe",
            )
            require(
                summary["manifest_sha256"] == receipt["manifest_sha256"], "Receipt manifest changed"
            )
            require(
                summary["specification"] == spec and summary["source"] == spec["source"],
                "Summary specification/source differs from submitted job",
            )
            require(summary["source_code"] == job["source_code"], "Code binding mismatch")
            recipe = RECIPES[spec["recipe"]]
            require(
                spec["fields"] == ["center", "outer-ne"]
                and spec["charts"] == recipe["charts"]
                and spec["light_scales"] == recipe["light_scales"],
                "Prepared scope mismatch",
            )
            wanted_cases = {
                (f, c, light)
                for f in spec["fields"]
                for c in spec["charts"]
                for light in spec["light_scales"]
            }
            observed_cases = [
                (c["field_id"], c["chart"], c["light_scale"]) for c in summary["cases"]
            ]
            require(
                len(observed_cases) == len(wanted_cases) and set(observed_cases) == wanted_cases,
                "Missing or duplicate field/chart/light cases",
            )
            wanted_modes = {
                "gain_budget": {"analog_gain", "digital_gain", "balanced_gain"},
                "noise_structure": {"pixel_iid", "column_noise", "cds_correlated"},
                "adc_headroom": {f"adc_{b}bit" for b in spec["readout_lab"]["adc_bits"]},
            }[spec["recipe"]]
            root = Path(job["storage_root"]) / "reference-workshop" / job["id"]
            evidence = verify_manifest(root, summary["manifest_sha256"])
            source = spec["source"]
            source_root = Path(job["storage_root"]) / "reference-workshop" / source["run_id"]
            if source["run_id"] not in sources:
                sources[source["run_id"]] = verify_manifest(source_root, source["manifest_sha256"])
            source_data = (
                source_root / source["candidate_id"] if source["candidate_id"] else source_root
            )
            require(
                summary["reused_native_raw_frames"] == 0, "Readout change cannot reuse captured RAW"
            )
            case_count, frames, ptc_checks = 0, 0, 0
            for item in summary["cases"]:
                case = client.reference_sensor_mode_case(job["id"], item["id"])
                require(
                    all(case[k] == item[k] for k in ("id", "field_id", "chart", "light_scale"))
                    and case["source_refs"]["source"] == source,
                    "Case binding mismatch",
                )
                require(
                    len(case["modes"]) == len(wanted_modes)
                    and {m["id"] for m in case["modes"]} == wanted_modes
                    and set(item["modes"]) == wanted_modes,
                    "Mode set mismatch",
                )
                rate = np.load(root / "cases" / item["id"] / "mean-electron-rate.npy")
                if case["chart"] == "dark":
                    require(not np.any(rate), "Dark input contains photoelectrons")
                elif case["chart"] == "dynamic_range":
                    require(
                        case["source_refs"]["response"]["optical_revision"]
                        == read(source_data / "optical-response.json")["revision"],
                        "Dynamic chart optical revision changed",
                    )
                else:
                    original = (
                        source_data
                        / "fields"
                        / case["field_id"]
                        / case["chart"]
                        / "mean-electron-rate.npy"
                    )
                    ref = case["source_refs"]["response"]
                    require(
                        Path(ref["path"]).resolve() == original.resolve()
                        and sha256(original) == ref["sha256"],
                        "Wrong original field/chart response",
                    )
                    require(
                        np.array_equal(rate, np.load(original) * case["light_scale"]),
                        "Case mean response differs from original times light scale",
                    )
                expected = rate * spec["exposure_ms"] / 1000
                for mode in case["modes"]:
                    with np.load(root / mode["files"]["arrays"], allow_pickle=False) as data:
                        raw, electrons = data["raw_frames"], data["electron_frames"]
                        require(
                            raw.dtype.kind in "iu" and raw.shape == (8, 256, 256),
                            "Native RAW contract failed",
                        )
                        require(
                            np.allclose(data["photo_mean_e"], expected, rtol=1e-12, atol=1e-12),
                            "Readout did not reuse the pinned mean response",
                        )
                        meta = mode["metadata"]["metadata"]
                        require(
                            meta["recipe"] == spec["recipe"]
                            and meta["id"] == mode["id"]
                            and meta["light_scale"] == case["light_scale"]
                            and meta["exposure_s"] == spec["exposure_ms"] / 1000,
                            "Mode acquisition metadata mismatch",
                        )
                        if meta["digital_gain"] == 1:
                            require(
                                not data["digital_added_saturated_frames"].any(),
                                "Unit digital gain was credited with extra clipping",
                            )
                        calibrated = (raw.astype(float) - meta["black_level_dn"]) / meta[
                            "dn_per_e"
                        ] - data["dark_e"]
                        require(
                            np.array_equal(calibrated, electrons),
                            "RAW-to-electron calibration changed",
                        )
                        require(np.all(np.isfinite(data["variance_e2"])), "Nonfinite variance")
                        require(
                            raw.min() >= 0 and raw.max() <= 2 ** meta["bit_depth"] - 1,
                            "ADC output out of range",
                        )
                        diag = mode["readout_diagnostics"]
                        for point in diag["ptc"]["points"]:
                            if point["status"] != "ok":
                                continue
                            r, c = {"R": (0, 0), "Gr": (0, 1), "Gb": (1, 0), "B": (1, 1)}[
                                point["site"]
                            ]
                            r0, r1, c0, c1 = point["roi"]
                            sample = electrons[
                                :, r0 + (r - r0) % 2 : r1 : 2, c0 + (c - c0) % 2 : c1 : 2
                            ]
                            require(
                                np.isclose(
                                    np.var(sample, axis=0, ddof=1).mean(),
                                    point["temporal_variance_e2"],
                                ),
                                "PTC variance is not temporal RAW variance",
                            )
                            ptc_checks += 1
                        frames += len(raw)
                        findings.append(
                            {
                                "recipe": spec["recipe"],
                                "case": item["id"],
                                "field": case["field_id"],
                                "chart": case["chart"],
                                "light": case["light_scale"],
                                "mode": mode["id"],
                                "noise_e": diag["temporal_noise_e"],
                                "column_rms_median_e": float(
                                    np.median(
                                        [v for v in diag["column_temporal_rms_e"] if v is not None]
                                    )
                                )
                                if any(v is not None for v in diag["column_temporal_rms_e"])
                                else None,
                                "metrics": mode["metrics"],
                            }
                        )
                case_count += 1
            require(frames == summary["new_readout_frames"], "Fresh RAW count mismatch")
            saved = client.get(
                f"reference-workshop/sensor-mode-studies/{job['id']}/profiles/{receipt['profile_id']}"
            )
            require(
                saved["manifest_sha256"] == summary["manifest_sha256"],
                "Saved profile binding changed",
            )
            selected = next(
                c
                for c in summary["cases"]
                if c["field_id"] == "outer-ne"
                and c["chart"] == recipe["chart"]
                and c["light_scale"] == recipe["light"]
            )
            require(
                saved["selection"]["case_id"] == selected["id"]
                and saved["selection"]["baseline_mode"] == recipe["compare"][0]
                and saved["selection"]["mode"] == recipe["compare"][1],
                "Saved profile does not match prepared A/B",
            )
            verified.append(
                {
                    "id": job["id"],
                    "recipe": spec["recipe"],
                    **evidence,
                    "cases": case_count,
                    "new_raw_frames": frames,
                    "ptc_checks": ptc_checks,
                    "profile_verified": True,
                }
            )
    result = {
        "status": "verified",
        "studies": verified,
        "sources": sources,
        "findings": findings,
        "physical_validation": "not_established",
    }
    (args.output / "verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    print(json.dumps({"status": result["status"], "studies": verified}, indent=2))


if __name__ == "__main__":
    main()
