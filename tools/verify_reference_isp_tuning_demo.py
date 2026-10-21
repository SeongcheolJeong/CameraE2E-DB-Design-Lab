#!/usr/bin/env python3
"""Read-only evidence verification for prepared Reference ISP tuning jobs.

Reads Registry results through the SDK and retained lossless files. Only the
requested output directory's verification.json is written; no jobs are submitted.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

from camera_platform import reference_isp
from camera_platform import reference_isp_studies as studies
from camera_platform.reference_isp_lab import temporal_noise_power_spectrum
from camera_platform.reference_workshop_job import acquisition_configuration, fingerprint
from camera_platform.sdk import PlatformClient
from camera_platform.virtual_lab_job import sha256

LABELS = ("preview-baseline", "preview-tuned", "parameter-tuning", "saved-profile-replay")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def member(root, relative):
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), "Artifact escapes run directory: " + relative)
    return path


def directory(record):
    return (Path(record["storage_root"]) / "reference-workshop" / record["id"]).resolve()


def manifest(record):
    root = directory(record)
    summary = record["result"]["summary"]
    path = root / "manifest.json"
    require(sha256(path) == summary["manifest_sha256"], "Run manifest changed: " + record["id"])
    files = read(path)["files"]
    total = allocated = 0
    for relative, expected in files.items():
        item = member(root, relative)
        require(item.is_file() and sha256(item) == expected, "Artifact hash changed: " + str(item))
        stat = item.stat()
        total += stat.st_size
        allocated += getattr(stat, "st_blocks", 0) * 512
    require("result.json" in files and "report.html" in files, "Incomplete output manifest")
    return {
        "sha256": summary["manifest_sha256"],
        "files": len(files),
        "bytes": total,
        "allocated_bytes": allocated,
    }


def source_directory(record, sources):
    source = record["source"]
    original = sources[source["run_id"]]
    require(
        source["manifest_sha256"] == original["result"]["summary"]["manifest_sha256"],
        "Preserved source pin changed",
    )
    root = directory(original)
    return root / source["candidate_id"] if source.get("candidate_id") else root


def check_raws(root, item, source, *, preview=False):
    capture = read(source / "capture.json")
    require(
        acquisition_configuration(item["configuration"])
        == acquisition_configuration(capture["configuration"]),
        "Output acquisition configuration differs from its preserved source",
    )
    if not preview:
        field_ids = [field["id"] for field in item["fields"]]
        require(
            len(field_ids) == len(set(field_ids)) and set(field_ids) == set(capture["fields"]),
            "Full replay does not contain exactly the source capture fields",
        )
    count = 0
    for field in item["fields"]:
        require(field["status"] == "succeeded", "Failed field: " + field["id"])
        require(field["geometry"]["native_shape_row_col"] == [256, 256], "Expected native 256×256")
        original = capture["fields"][field["id"]]["captures"]
        if not preview:
            require(
                set(field["capture_files"]) == set(original),
                "Full replay does not contain exactly the source chart set",
            )
        for chart, record in field["capture_files"].items():
            wanted = original[chart]
            expected_raw = wanted["raw"][:1] if preview else wanted["raw"]
            require(record["raw"] == expected_raw, "RAW identity/seed changed")
            require(len(expected_raw) == (1 if preview else 8), "Wrong repeat count")
            require(record["rate_sha256"] == wanted["rate_sha256"], "Mean-response source changed")
            folder = root / "fields" / field["id"] / chart
            for raw in record["raw"]:
                path = folder / raw["file"]
                require(sha256(path) == raw["sha256"], "RAW does not match original capture")
                values = np.load(path, allow_pickle=False)
                require(
                    values.shape == (256, 256) and values.dtype.kind in "iu", "Invalid native RAW"
                )
                count += 1
    actual = len(list(root.glob("fields/*/*/raw-*.npy")))
    require(actual == count, "Unexpected extra or missing preserved RAW files")
    if not preview:
        source_count = sum(
            len(chart["raw"])
            for field in capture["fields"].values()
            for chart in field["captures"].values()
        )
        require(count == source_count, "Full replay RAW total differs from source acquisition")
    require(item["raw_preserved"], "Missing preserved-RAW claim")
    return count


def check_nps(root, field):
    record = field["tests"]["isp_lab"]["noise_power_spectrum"]
    require(record["status"] == "ok" and record["repeat_count"] == 8, "NPS unavailable")
    require(
        math.isclose(
            record["integrated_2d_nps_variance"],
            record["window_weighted_temporal_variance"],
            rel_tol=1e-10,
            abs_tol=1e-15,
        ),
        "2D NPS integral does not equal its temporal variance",
    )
    folder = root / "fields" / field["id"] / "uniform"
    require(read(folder / "noise-power-spectrum.json") == record, "NPS JSON/summary mismatch")
    recalculated = temporal_noise_power_spectrum(
        np.load(folder / "processed-repeats.npy", allow_pickle=False),
        np.load(folder / "comparison-valid-mask.npy", allow_pickle=False),
    )
    require(
        recalculated["status"] == "ok" and recalculated["roi"] == record["roi"],
        "NPS does not use saved common support",
    )
    require(
        math.isclose(
            recalculated["integrated_2d_nps_variance"],
            record["integrated_2d_nps_variance"],
            rel_tol=1e-5,
            abs_tol=1e-12,
        ),
        "NPS differs from saved float32 processed repeats",
    )
    return {
        "field_id": field["id"],
        "roi": record["roi"],
        "integrated_variance": record["integrated_2d_nps_variance"],
    }


def verify(output, base_url):
    receipts = read(output / "runs.json")
    latest = {
        label: next((r for r in reversed(receipts) if r["label"] == label), None)
        for label in LABELS
    }
    require(all(latest.values()), "All four prepared job receipts are required")
    code = fingerprint()
    records, sources, hashes = {}, {}, {}
    with PlatformClient(base_url, timeout=180) as client:
        for label, receipt in latest.items():
            record = client.reference_workshop_run(receipt["id"])
            require(record["status"] == "succeeded", f"{label} did not succeed")
            require(
                record["source_code"] == code, f"{label} used a different numerical implementation"
            )
            records[label] = record
            identity = record["source"]["run_id"]
            if identity not in sources:
                sources[identity] = client.reference_workshop_run(identity)
                hashes[identity] = manifest(sources[identity])
            hashes[record["id"]] = manifest(record)
    study_source = records["parameter-tuning"]["source"]
    for label, record in records.items():
        require(record["source"] == study_source, f"{label} uses a different preserved source")
        require(
            record["result"]["summary"]["source_run_id"] == study_source["run_id"],
            f"{label} summary/source run identity mismatch",
        )
    original_summary = sources[study_source["run_id"]]["result"]["summary"]
    if study_source.get("candidate_id"):
        original_summary = next(
            candidate
            for candidate in original_summary["candidates"]
            if candidate["id"] == study_source["candidate_id"]
        )
    preview_base = reference_isp.default_profile(original_summary["configuration"]["isp"])
    preview_base["lsc_enabled"] = True
    preview_base["lsc_tuning"] = {"strength": 1.0, "gain_cap": 4.0}
    preview_tuned = reference_isp.default_profile(preview_base)
    preview_tuned["raw_denoise"]["strength"] = 0.2
    preview_tuned["linear_sharpen"].update(amount=0.3, threshold=0.01, max_delta=0.05)
    preview_profiles = {"preview-baseline": preview_base, "preview-tuned": preview_tuned}
    groups = []
    for label in LABELS[:2]:
        record = records[label]
        summary, root = record["result"]["summary"], directory(record)
        source = source_directory(record, sources)
        scope = summary["preview_scope"]
        require(scope["source"] == study_source, "Preview scope/source binding mismatch")
        require(
            summary["configuration"]["isp"] == preview_profiles[label]
            and record["specification"]["isp"] == preview_profiles[label],
            f"{label} does not contain its expected preview ISP profile",
        )
        require(
            scope["field_id"] == "outer-ne" and scope["chart"] == "texture",
            "Unexpected preview target",
        )
        require(
            scope["frame_index"] == 0 and scope["processed_frame_count"] == 1,
            "Preview processed repeats",
        )
        require(scope["candidate_selection"] == "not_evaluated", "Preview claims selection")
        require(
            len(summary["fields"]) == 1 and set(summary["fields"][0]["images"]) == {"texture"},
            "Preview contains additional fields/charts",
        )
        require(
            summary["fields"][0]["metrics"]["processed_temporal_noise"] is None,
            "Single frame claims temporal noise",
        )
        count = check_raws(root, summary, source, preview=True)
        require(count == 1 and summary["acquisition_reused"], "Preview reacquired RAW")
        groups.append(
            {
                "label": label,
                "id": record["id"],
                "raw_copies": count,
                "manifest": hashes[record["id"]],
                "timing": latest[label],
                "engine_seconds": summary["performance"]["elapsed_seconds"],
            }
        )
    record = records["parameter-tuning"]
    summary, root = record["result"]["summary"], directory(record)
    source = source_directory(record, sources)
    candidates, study = summary["candidates"], summary["study"]
    require(study["source"] == study_source, "Study evaluation/source binding mismatch")
    require(
        study["recipe"] == "parameter_tuning" and len(candidates) == 12,
        "Expected actual 12-candidate tuning study",
    )
    expected = studies.profiles(study)
    require(len(expected) == len(candidates), "Declared candidate count mismatch")
    count, nps = 0, []
    for candidate, (_, profile) in zip(candidates, expected, strict=True):
        require(candidate["status"] == "succeeded", "Candidate failed: " + candidate["id"])
        require(
            candidate["configuration"]["isp"] == profile, "Candidate ISP differs from declared axes"
        )
        require(
            {f["id"] for f in candidate["fields"]} == set(studies.FIELD_IDS), "Missing study field"
        )
        require(len(candidate["fields"]) == 9, "Expected nine actual fields")
        candidate_root = root / candidate["id"]
        count += check_raws(candidate_root, candidate, source)
        nps.extend(
            {"candidate_id": candidate["id"], **check_nps(candidate_root, field)}
            for field in candidate["fields"]
        )
    baseline = candidates[0]["configuration"]["isp"]
    require(
        baseline["lsc_enabled"]
        and baseline["lsc_tuning"] == {"strength": 1.0, "gain_cap": 4.0}
        and baseline["raw_denoise"]["strength"] == 0
        and baseline["linear_sharpen"]["amount"] == 0,
        "Wrong full-LSC bypass baseline",
    )
    require(
        studies.select(candidates, study) == study["selection"],
        "Stored selection cannot be reproduced",
    )
    groups.append(
        {
            "label": "parameter-tuning",
            "id": record["id"],
            "raw_copies": count,
            "candidate_count": 12,
            "field_count": 108,
            "manifest": hashes[record["id"]],
            "timing": latest["parameter-tuning"],
            "nps_checks": nps,
        }
    )
    identity = study["selection"]["selected_candidate_id"]
    require(identity is not None, "No selected profile exists for the prepared replay")
    chosen = next(c for c in candidates if c["id"] == identity)
    saved = read(output / "saved-profile.json")
    require(
        saved["specification"] == chosen["configuration"],
        "Saved profile differs from selected candidate",
    )
    replay_record = records["saved-profile-replay"]
    replay, replay_root = replay_record["result"]["summary"], directory(replay_record)
    require(replay["configuration"]["isp"] == chosen["configuration"]["isp"], "Replay ISP changed")
    count = check_raws(replay_root, replay, source_directory(replay_record, sources))
    source_capture = read(source / "capture.json")
    expected_processed = {
        Path("fields") / field_id / chart / "processed.npy"
        for field_id, field in source_capture["fields"].items()
        for chart in field["captures"]
    }
    selected_paths = {
        path.relative_to(root / identity)
        for path in (root / identity).glob("fields/*/*/processed.npy")
    }
    replay_paths = {
        path.relative_to(replay_root) for path in replay_root.glob("fields/*/*/processed.npy")
    }
    require(
        selected_paths == replay_paths == expected_processed,
        "Selected/replayed processed paths do not exactly cover the source chart set",
    )
    for relative in sorted(expected_processed):
        require(
            sha256(root / identity / relative) == sha256(replay_root / relative),
            "Saved-profile processed array differs: " + str(relative),
        )
    processed = len(expected_processed)
    require(processed > 0, "No source processed arrays exist to compare")
    groups.append(
        {
            "label": "saved-profile-replay",
            "id": replay_record["id"],
            "raw_copies": count,
            "processed_arrays_identical": processed,
            "manifest": hashes[replay_record["id"]],
            "timing": latest["saved-profile-replay"],
        }
    )
    return {
        "status": "verified",
        "scope": "saved model RAW and ISP computation; not physical sensor validation",
        "source_code": code,
        "selection": study["selection"],
        "groups": groups,
        "source_manifests": {k: hashes[k] for k in sources},
        "output_files": sum(g["manifest"]["files"] for g in groups),
        "output_bytes": sum(g["manifest"]["bytes"] for g in groups),
        "output_allocated_bytes": sum(g["manifest"]["allocated_bytes"] for g in groups),
        "raw_copies": sum(g["raw_copies"] for g in groups),
        "raw_count_note": "Preserved copies are not independent new acquisitions",
        "unused_receipts": [
            r for r in receipts if r["id"] not in {v["id"] for v in latest.values()}
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="outputs/reference-isp-tuning-20261012")
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    try:
        result = verify(output, args.base_url)
    except Exception as error:
        result = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
    result["verification_elapsed_seconds"] = time.monotonic() - start
    (output / "verification.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(
        json.dumps(
            {
                k: result.get(k)
                for k in ("status", "error", "output_files", "output_bytes", "raw_copies")
            },
            indent=2,
        )
    )
    if result["status"] != "verified":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
