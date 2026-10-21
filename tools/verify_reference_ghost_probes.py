"""Read-only artifact checks for the prepared 12-case Reference Ghost probe demo.

Checks saved acquisition and metric provenance, not optical/physical accuracy.
No server, solver, acquisition, or source mutation is invoked. With --output,
only the requested verification JSON is written.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from pathlib import Path

import numpy as np

MODES = ("source_off", "direct_only", "with_ghost")
SHAPE = (8, 256, 256)
PIN = {
    "run_id": "run-d2105e38b3cd4f83b3550ebff21ca7fd",
    "candidate_id": None,
    "manifest_sha256": "5561e0c26de907f32b23772c864bfbab80c8c1b41f229867c113d25574d7926c",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text())


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def member(root, relative):
    path = (root / relative).resolve()
    require(path.is_relative_to(root), f"Artifact escapes run directory: {relative}")
    return path


def verify_manifest(root):
    require((root / "manifest.json").is_file(), "Run is incomplete: manifest.json is absent")
    files = read(root / "manifest.json")["files"]
    total = 0
    for relative, expected in files.items():
        path = member(root, relative)
        require(path.is_file() and file_hash(path) == expected, f"Hash mismatch: {relative}")
        total += path.stat().st_size
    for name in ("result.json", "specification.json", "prescription.json", "calibration.json"):
        require(name in files, f"Required manifest entry missing: {name}")
    return {"sha256": file_hash(root / "manifest.json"), "files": len(files), "bytes": total}


def load_arrays(path):
    with np.load(path, allow_pickle=False) as data:
        arrays = {key: data[key] for key in data.files}
    raw = arrays["raw_frames"]
    require(raw.shape == SHAPE and raw.dtype.kind in "iu", f"Invalid integer RAW: {path}")
    require(int(raw.min()) >= 0 and int(raw.max()) <= 4095, f"RAW outside 12-bit ADC range: {path}")
    require(np.any(raw[0] != raw[1]), f"No independent noise realization visible: {path}")
    for key in (
        "electron_frames",
        "raw_valid_frames",
        "valid_frames",
        "well_saturated_frames",
        "adc_saturated_frames",
    ):
        require(arrays[key].shape == SHAPE, f"Invalid {key} shape: {path}")
    require(arrays["linear_frames"].shape == (*SHAPE, 3), f"Invalid linear RGB: {path}")
    require(np.isfinite(arrays["electron_frames"]).all(), f"Nonfinite electrons: {path}")
    require(np.isfinite(arrays["linear_frames"]).all(), f"Nonfinite linear RGB: {path}")
    return arrays


def support(arrays):
    return (
        arrays["raw_valid_frames"]
        & arrays["valid_frames"]
        & ~arrays["well_saturated_frames"]
        & ~arrays["adc_saturated_frames"]
    ).all(axis=0)


def roi(bounds):
    r0, r1, c0, c1 = bounds
    require(0 <= r0 < r1 <= 256 and 0 <= c0 < c1 <= 256, "Invalid fixed chart ROI")
    return slice(r0, r1), slice(c0, c1)


def verify_metrics(case, mode, arrays, source_off):
    metrics = mode["chart_metrics"]
    current, reference = support(arrays), support(source_off)
    require(metrics["repeat_count"] == 8, "Wrong chart metric repeat count")
    if case["chart"] == "black":
        region = roi(case["chart_definition"]["measurement_roi"])
        black = metrics["black_level"]
        if current[region].all() and reference[region].all():
            excess = (
                arrays["electron_frames"].mean(axis=0) - source_off["electron_frames"].mean(axis=0)
            )[region]
            for key, expected in (
                ("mean_excess_e", excess.mean()),
                ("peak_excess_e", excess.max()),
                ("minimum_excess_e", excess.min()),
            ):
                require(
                    np.isclose(black[key], expected, rtol=1e-10, atol=1e-10),
                    f"Black excess disagrees with signed preserved electrons: {key}",
                )
        else:
            require(
                black["status"] == "unavailable" and black["mean_excess_e"] is None,
                "Invalid black support reported as a valid excess",
            )
        return {"status": metrics["status"], "mean_excess_e": black["mean_excess_e"]}
    require(len(metrics["targets"]) == 12, "Missing low-contrast target metrics")
    definitions = {t["id"]: t for t in case["chart_definition"]["targets"]}
    valid = 0
    for target in metrics["targets"]:
        definition = definitions[target["id"]]
        require(
            all(
                target[key] == definition[key]
                for key in ("object_roi", "background_roi", "declared_weber_contrast")
            ),
            "Metric mask/contrast changed from the declared chart",
        )
        obj, bg = roi(target["object_roi"]), roi(target["background_roi"])
        usable = current[obj].all() and current[bg].all()
        if not usable:
            require(
                target["status"] == "unavailable"
                and target["cnr"] is None
                and target["observed_contrast"] is None,
                "Invalid target ROI was reported as usable",
            )
        if target["status"] == "ok":
            require(
                usable and reference[obj].all() and reference[bg].all(),
                "Target comparison uses invalid source-off support",
            )
            require(
                all(
                    target[key] is not None and np.isfinite(target[key])
                    for key in ("cnr", "observed_contrast", "contrast_retention")
                ),
                "Successful target has missing metrics",
            )
            valid += 1
    require(metrics["valid_targets"] == valid, "Incorrect usable-target count")
    return {"status": metrics["status"], "valid_targets": valid, "targets": 12}


def verify(run_root, replay_root=None):
    root = Path(run_root).resolve()
    manifest = verify_manifest(root)
    result, spec = read(root / "result.json"), read(root / "specification.json")
    require(result["schema"] == "camera_reference_ghost_study_v2", "Expected v2 probe study")
    require(
        result["specification"] == spec and result["source"] == spec["source"] == PIN,
        "Specification or Reference source pin mismatch",
    )
    require(set(spec["charts"]) == {"black", "dark_contrast"}, "Expected both new charts")
    require(sorted(spec["irradiance_w_m2"]) == [1.0, 10.0], "Expected 1/10 W/m² sweep")
    require(spec["frames"] == 8 and not spec["isp"]["lsc_enabled"], "Capture/LSC mismatch")
    require(
        len(result["cases"]) == 12 and result["new_readout_frames"] == 288,
        "Expected 12 fresh cases and 288 RAW frames",
    )
    source = root.parent / PIN["run_id"]
    require(file_hash(source / "manifest.json") == PIN["manifest_sha256"], "Source pin changed")
    require(
        read(root / "calibration.json") == read(source / "calibration.json"),
        "Shared calibration differs from source",
    )
    coordinates = {field["id"]: field["xy_mm"] for field in result["fields"]}
    for probe in spec["probes"]:
        require(coordinates.get(probe["id"]) == probe["xy_mm"], "Selected coordinate mismatch")
    expected = set(
        itertools.product(
            spec["angles_deg"],
            spec["coatings"],
            coordinates,
            spec["charts"],
            spec["irradiance_w_m2"],
        )
    )
    actual, records, groups, all_hashes = set(), [], {}, {}
    frames = 0
    for case in result["cases"]:
        cid = case["id"]
        require(read(root / "cases" / cid / "case.json") == case, f"Case identity mismatch: {cid}")
        key = (
            case["angle_deg"],
            case["coating"],
            case["field_id"],
            case["chart"],
            case["irradiance_w_m2"],
        )
        require(key not in actual, "Duplicate camera/coordinate/chart/power case")
        actual.add(key)
        require(case["probe_xy_mm"] == coordinates[case["field_id"]], "Case coordinate mismatch")
        require(
            case["native_row_axis"] == "decreasing_sensor_y"
            and case["native_pixel_pitch_um"] == 2.9
            and case["native_cfa_origin"] == "independent_virtual_ROI_RGGB",
            "Native orientation/pitch/CFA contract mismatch",
        )
        require(
            tuple(mode["id"] for mode in case["modes"]) == MODES, "Missing source-off/direct/ghost"
        )
        with np.load(root / "cases" / cid / "mean-rates.npz", allow_pickle=False) as data:
            rates = {key: data[key] for key in ("background", "direct", "ghost")}
        require(
            all(
                a.shape == (256, 256) and np.isfinite(a).all() and np.min(a) >= 0
                for a in rates.values()
            ),
            "Invalid pre-noise rate arrays",
        )
        if case["chart"] == "black":
            require(not rates["background"].any(), "Black background is not zero")
        require(
            np.isclose(case["ghost_mean_e"], rates["ghost"].mean() * 0.004)
            and np.isclose(case["ghost_peak_e"], rates["ghost"].max() * 0.004),
            "Ghost electron summary disagrees with saved rate",
        )
        group = key[:-1]
        groups.setdefault(group, {})[case["irradiance_w_m2"]] = rates
        mode_records, baseline = {}, None
        for mode in case["modes"]:
            arrays = load_arrays(member(root, mode["arrays"]))
            checksum = hashlib.sha256(arrays["raw_frames"].tobytes()).hexdigest()
            require(
                checksum == case["raw_hashes"][mode["id"]], f"RAW hash mismatch: {cid}/{mode['id']}"
            )
            all_hashes[f"{cid}/{mode['id']}"] = checksum
            if mode["id"] == "source_off":
                baseline = arrays
            mode_records[mode["id"]] = verify_metrics(case, mode, arrays, baseline)
            frames += len(arrays["raw_frames"])
        records.append(
            {
                "case": cid,
                "field": case["field_id"],
                "xy_mm": case["probe_xy_mm"],
                "chart": case["chart"],
                "irradiance_w_m2": case["irradiance_w_m2"],
                "metric_validity": mode_records,
            }
        )
    require(actual == expected and frames == 288, "Incomplete case grid or RAW frame total")
    for group, powers in groups.items():
        require(set(powers) == {1.0, 10.0}, f"Power pair missing: {group}")
        for kind in ("direct", "ghost"):
            require(
                np.allclose(powers[10.0][kind], powers[1.0][kind] * 10, rtol=1e-12, atol=1e-12),
                f"Nonlinear pre-readout source scaling: {group}/{kind}",
            )
        require(
            np.array_equal(powers[1.0]["background"], powers[10.0]["background"]),
            f"Background changed with source power: {group}",
        )
    replay_report = None
    if replay_root:
        replay = Path(replay_root).resolve()
        replay_manifest = verify_manifest(replay)
        replay_result = read(replay / "result.json")
        require(
            replay_result["reprocess_of"] == root.name and replay_result["new_readout_frames"] == 0,
            "Replay recaptured or targets wrong study",
        )
        replay_hashes = {}
        for case in replay_result["cases"]:
            for mode in case["modes"]:
                with np.load(member(replay, mode["arrays"]), allow_pickle=False) as data:
                    checksum = hashlib.sha256(data["raw_frames"].tobytes()).hexdigest()
                require(checksum == case["raw_hashes"][mode["id"]], "Replay hash mismatch")
                replay_hashes[f"{case['id']}/{mode['id']}"] = checksum
        require(replay_hashes == all_hashes, "RAW changed across ISP replay")
        replay_report = {
            "root": str(replay),
            "manifest": replay_manifest,
            "preserved_raw_sets": len(replay_hashes),
            "new_readout_frames": 0,
        }
    return {
        "status": "passed",
        "run_root": str(root),
        "manifest": manifest,
        "cases": 12,
        "raw_frames": frames,
        "raw_sets": len(all_hashes),
        "source": PIN,
        "pre_readout_1_to_10_scaling_pairs": len(groups),
        "measurements": records,
        "refinement": result.get("refinement"),
        "replay": replay_report,
        "scope": "saved artifact identity, units, support and replay; no physical accuracy claim",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--replay-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = verify(args.run_root, args.replay_root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report = {"status": "failed", "run_root": str(args.run_root), "reason": str(exc)}
    serialized = json.dumps(report, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(serialized + "\n")
    print(serialized)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
