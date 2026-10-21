#!/usr/bin/env python3
"""Read-only verification of completed Reference ISP Lab receipts and SSD artifacts.

Run after run_reference_isp_lab_demo.py. This does not submit jobs, refit a color
matrix, trace optics, or modify captured files. Only the verification JSON is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import binary_erosion

from camera_platform import reference_isp, reference_isp_color, reference_isp_studies
from camera_platform.reference_isp_lab import _roi_support
from camera_platform.reference_isp_scene import (
    CHART_SPECIFICATION,
    chirp_pattern,
    mosaic_from_planes,
)
from camera_platform.reference_workshop_job import acquisition_configuration, fingerprint
from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient
from camera_platform.virtual_lab_engine import XYZ_TO_RGB

REQUIRED = {
    "noise-detail": ("noise_detail", 6),
    "color-cfa-1.0": ("color_recalibration", 2),
    "color-cfa-0.9": ("color_recalibration", 2),
    "color-cfa-1.1": ("color_recalibration", 2),
    "chirp-baseline": (None, 1),
    "demosaic-baseline": ("demosaic", 3),
}
OPTIONAL = {
    "chirp-curvature": (None, 1),
    "demosaic-curvature": ("demosaic", 3),
}
CHARTS = {"resolution", "uniform", "color", "texture"}
STAGES = {
    "blc_normalized",
    "lsc_normalized",
    "denoised_normalized",
    "demosaiced_electrons",
    "color_linear",
    "sharpen_delta",
    "processed_linear",
    "display",
}


def load(path):
    return json.loads(Path(path).read_text())


def artifact(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"Artifact missing or outside run: {relative}")
    return path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value, *, nonnegative=True):
    return type(value) in (int, float) and math.isfinite(value) and (not nonnegative or value >= 0)


def audit_snapshot(state_dir):
    """Read one SQLite snapshot without invoking constructor migrations or writes."""
    path = Path(state_dir).expanduser().resolve() / "registry.sqlite"
    require(path.is_file(), "Timing Registry is unavailable: " + str(path))
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        events = [dict(row) for row in connection.execute("SELECT * FROM audit ORDER BY seq")]
        jobs = {
            row["id"]: dict(row)
            for row in connection.execute("SELECT id,status,created_at,updated_at FROM jobs")
        }
    finally:
        connection.close()
    previous = "0" * 64
    for row in events:
        row["details"] = json.loads(row["details"])
        event = {
            key: row[key] for key in ("timestamp", "action", "subject", "details", "previous_hash")
        }
        require(
            row["previous_hash"] == previous and digest(event) == row["event_hash"],
            "Registry audit hash chain verification failed",
        )
        previous = row["event_hash"]
    return {
        "path": str(path),
        "head_hash": previous,
        "events": events,
        "jobs": jobs,
        "integrity": "verified",
        "scope": "Local tamper evidence; not externally signed",
    }


def audit_timing(audit, identity):
    job = audit["jobs"][identity]
    require(job["status"] == "succeeded", "Timing requires a completed Registry job")
    events = [event for event in audit["events"] if event["subject"] == identity]
    queued = [event for event in events if event["action"] == "runs.queued"]
    running = [event for event in events if event["action"] == "runs.running"]
    succeeded = [event for event in events if event["action"] == "runs.succeeded"]
    require(
        len(queued) == len(running) == len(succeeded) == 1,
        "Timing requires one unambiguous queue, running and succeeded event",
    )
    created = datetime.fromisoformat(job["created_at"])
    queue, start, end = (
        datetime.fromisoformat(rows[0]["timestamp"]) for rows in (queued, running, succeeded)
    )
    require(created <= queue <= start <= end, "Registry timing order is inconsistent")
    return {
        "source": "hash_verified_local_registry_audit",
        "run_id": identity,
        "created_at": job["created_at"],
        "running_at": running[0]["timestamp"],
        "succeeded_at": succeeded[0]["timestamp"],
        "queue_seconds": (start - created).total_seconds(),
        "execution_wall_seconds": (end - start).total_seconds(),
        "created_to_completed_seconds": (end - created).total_seconds(),
        "audit_events": [
            {key: event[key] for key in ("seq", "timestamp", "action", "event_hash")}
            for event in (queued[0], running[0], succeeded[0])
        ],
        "includes": "worker execution, rendering, file storage and completion overhead",
        "wall_clock_is_vehicle_or_isp_hardware_latency": False,
    }


class Verifier:
    def __init__(self, client, workspace, state_dir=None):
        self.client, self.workspace = client, workspace
        self.hashes, self.records, self.sources = {}, {}, {}
        self.checks, self.groups = [], []
        self.reflectances = None
        self.source_code = fingerprint()
        self.support_masks = {}
        self.audit = audit_snapshot(
            state_dir or workspace / ("outputs/camera-platform-virtual-lab-cra-20261011")
        )

    def sha(self, path):
        """Cache only unchanged local paths; target copies are still individually read."""
        path = Path(path).resolve()
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if key not in self.hashes:
            value = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    value.update(block)
            self.hashes[key] = value.hexdigest()
        return self.hashes[key]

    def record(self, identity):
        if identity not in self.records:
            value = self.client.reference_workshop_run(identity)
            require(value["status"] == "succeeded", f"Job {identity} is {value['status']}")
            self.records[identity] = value
        return self.records[identity]

    def folder(self, record):
        folder = Path(record["storage_root"]) / "reference-workshop" / record["id"]
        require(folder.is_dir(), f"Local project storage is unavailable: {folder}")
        return folder

    def manifest(self, record, *, all_files):
        root = self.folder(record)
        path = artifact(root, "manifest.json")
        require(
            self.sha(path) == record["result"]["summary"]["manifest_sha256"],
            "Public manifest hash differs from local artifact",
        )
        files = load(path)["files"]
        logical = allocated = 0
        if all_files:
            for name, expected in files.items():
                target = artifact(root, name)
                require(self.sha(target) == expected, f"Manifest mismatch: {name}")
                stat = target.stat()
                logical += stat.st_size
                allocated += getattr(stat, "st_blocks", 0) * 512
        return {
            "files_verified": len(files) if all_files else 0,
            "logical_bytes": logical,
            "allocated_bytes": allocated,
        }

    def source(self, binding):
        key = (binding["run_id"], binding.get("candidate_id"))
        if key not in self.sources:
            record = self.record(key[0])
            self.manifest(record, all_files=False)
            summary, root = record["result"]["summary"], self.folder(record)
            require(
                summary["manifest_sha256"] == binding["manifest_sha256"],
                "Source manifest differs from study binding",
            )
            if summary.get("candidates") is not None:
                require(key[1] is not None, "Sweep source needs an exact candidate")
                summary = next(c for c in summary["candidates"] if c["id"] == key[1])
                root = root / key[1]
            require(summary.get("status", "succeeded") == "succeeded", "Source candidate failed")
            hashes = self.capture_hashes(summary, root, inspect_raw=False)
            self.sources[key] = (summary, root, hashes)
        return self.sources[key]

    def capture_hashes(self, summary, root, *, inspect_raw):
        hashes = {}
        count = 0
        for field in summary["fields"]:
            require(field["status"] == "succeeded", f"Failed native field: {field['id']}")
            folder = root / "fields" / field["id"]
            capture = load(artifact(folder, "capture.json"))
            white = 2 ** capture["readout"]["bit_depth"] - 1
            for chart, files in field["capture_files"].items():
                require(files == capture["captures"][chart], "Field capture metadata mismatch")
                require(
                    len(files["raw"]) == summary["configuration"]["frames"],
                    "RAW repeat count differs from acquisition specification",
                )
                entries = [(r["file"], r["sha256"]) for r in files["raw"]]
                entries += [
                    ("mean-electron-rate.npy", files["rate_sha256"]),
                    ("source-reference-linear.npy", files["source_reference_sha256"]),
                ]
                if "planes_sha256" in files:
                    entries.append(("planes-electron-rate.npy", files["planes_sha256"]))
                for name, expected in entries:
                    path = artifact(folder / chart, name)
                    require(self.sha(path) == expected, f"Capture hash mismatch: {path}")
                    hashes[(field["id"], chart, name)] = expected
                    if inspect_raw and name.startswith("raw-"):
                        raw = np.load(path, mmap_mode="r", allow_pickle=False)
                        require(
                            raw.shape == (256, 256)
                            and raw.dtype.kind in "iu"
                            and raw.min() >= 0
                            and raw.max() <= white,
                            "Invalid native integer RAW",
                        )
                        count += 1
        return hashes, count

    def color(self, summary, root, source_root):
        identity = summary["configuration"]["isp"].get("color_calibration_id")
        if identity is None:
            return None
        calibration = load(artifact(root, "color-calibrations/" + identity + ".json"))
        reference_isp_color.verify_calibration(calibration, source_root)
        reference_isp_color.validate_binding(calibration, source_root)
        require(calibration["id"] == identity, "Fitted color ID differs from profile")
        require(
            calibration["holdout"]["used_in_fit"] is False
            and calibration["fit"]["evaluation_raw_used"] is False
            and calibration["fit"]["frame_normalization"] is False,
            "Color evaluation data or frame normalization entered fitting",
        )
        if self.reflectances is None:
            # Loads local reflectances and checks spectral disjointness; no optical calculation.
            self.reflectances = reference_isp_color.reflectance_sets(self.workspace)
        for role, count in (("training", 64), ("holdout", 24)):
            for key, value in self.reflectances[role]["provenance"].items():
                require(calibration[role][key] == value, f"Pinned {role} asset changed: {key}")
            require(len(calibration[role]["indices_one_based"]) == count, "Patch count changed")
        basis = calibration["basis"]
        actual_white = np.asarray(basis["white_electrons_rgb"]) @ calibration["matrix_xyz"]
        require(
            np.allclose(actual_white, basis["white_xyz"], rtol=1e-10, atol=1e-10),
            "Fitted matrix violates its white constraint",
        )
        return {
            "id": identity,
            "cfa_thickness_scale": calibration["source_binding"]["cfa_thickness_scale"],
            "training_count": 64,
            "holdout_count": 24,
            "training_sha256": calibration["training"]["sha256"],
            "holdout_sha256": calibration["holdout"]["sha256"],
            "basis_sha256": digest(basis),
            "white_anchor_max_abs_error": float(np.max(np.abs(actual_white - basis["white_xyz"]))),
            "holdout_delta_e00_mean": calibration["holdout"]["delta_e00_mean"],
            "holdout_delta_e00_max": calibration["holdout"]["delta_e00_max"],
            "matrix_column_l2_norm": calibration["diagnostics"]["matrix_column_l2_norm"],
        }

    def chirp(self, field, folder):
        geometry = field["geometry"]
        full = chirp_pattern(geometry, full_support=True)
        bounds = geometry["crop_bounds_row_col"]
        r0, r1, c0, c1 = bounds if len(bounds) == 4 else [*bounds[0], *bounds[1]]
        require(
            full.shape == tuple(geometry["input_shape_row_col"])
            and min(r0, c0, full.shape[0] - r1, full.shape[1] - c1) >= 8,
            "Chirp lacks declared optical / ISP support",
        )
        require(
            np.array_equal(full[r0:r1, c0:c1], chirp_pattern(geometry)),
            "Chirp native coordinates changed with halo",
        )
        capture = load(folder.parent / "capture.json")
        target = (
            chirp_pattern(geometry)[..., None]
            * np.asarray(capture["scene_reference"]["white_xyz"])
            @ XYZ_TO_RGB.T
        )
        reference = np.load(folder / "source-reference-linear.npy", mmap_mode="r")
        require(
            np.allclose(reference, target, rtol=1e-6, atol=1e-7),
            "Chirp source reference is not the declared native analytic pattern",
        )
        planes = np.load(folder / "planes-electron-rate.npy", mmap_mode="r")
        rate = np.load(folder / "mean-electron-rate.npy", mmap_mode="r")
        require(
            planes.shape == (256, 256, 4)
            and np.isfinite(planes).all()
            and np.all(planes >= 0)
            and np.allclose(mosaic_from_planes(planes), rate, rtol=1e-12, atol=1e-12),
            "Chirp mosaic and co-located four-plane basis differ",
        )
        diagnostic = field["tests"]["isp_lab"]["demosaic"]
        require(
            diagnostic["status"] == "ok"
            and diagnostic["noise_adc_bypassed"] is True
            and finite(diagnostic["chroma_residual_rms"])
            and finite(diagnostic["luma_rmse"]),
            "Demosaic model-reference metrics unavailable or invalid",
        )
        return {
            **diagnostic,
            "planes_sha256": self.sha(folder / "planes-electron-rate.npy"),
            "source_reference_sha256": self.sha(folder / "source-reference-linear.npy"),
            "input_shape_row_col": list(full.shape),
            "optical_halo_min_pixels": min(r0, c0, full.shape[0] - r1, full.shape[1] - c1),
        }

    def candidate(self, summary, root, source=None):
        fields = summary["fields"]
        require(
            len(fields) == 9 and {f["id"] for f in fields} == set(reference_isp_studies.FIELD_IDS),
            "Expected all nine distinct native fields",
        )
        profile = reference_isp.validate_profile(summary["configuration"]["isp"])
        hashes, raw_count = self.capture_hashes(summary, root, inspect_raw=True)
        if source:
            base, source_root, source_hashes = source
            require(
                acquisition_configuration(summary["configuration"])
                == acquisition_configuration(base["configuration"]),
                "ISP study changed acquisition",
            )
            require(hashes == source_hashes[0], "ISP candidate changed source RAW or mean response")
            for name in (
                "calibration.json",
                "pixel-cell.json",
                "pixel-response.json",
                "optical-response.json",
            ):
                require(
                    self.sha(root / name) == self.sha(source_root / name),
                    f"Frozen source changed: {name}",
                )
        else:
            source_root = root
        results = {}
        for field in fields:
            require(
                field["geometry"]["native_shape_row_col"] == [256, 256]
                and field["geometry"]["pixel_pitch_um"] == 2.9
                and field["geometry"]["cfa_origin"]["pattern"] == "RGGB",
                "Native pixel or CFA geometry changed",
            )
            require(all(finite(v) for v in field["metrics"].values()), "Nonfinite field metrics")
            lab = field["tests"]["isp_lab"]
            keys = ("temporal_noise_luma", "color_noise_luma", "texture_retention")
            require(
                all(finite(lab.get(key)) for key in keys), "Missing finite ISP comparison metrics"
            )
            require(
                finite(field["tests"]["color"].get("delta_e00_mean")),
                "Invalid acquired color metric",
            )
            charts = CHARTS | (
                {"demosaic_chirp"}
                if summary["configuration"].get("chart_suite") == "isp_lab_v1"
                else set()
            )
            require(set(field["capture_files"]) == charts, "Missing declared chart")
            for chart in charts:
                folder = root / "fields" / field["id"] / chart
                stage = load(artifact(folder, "isp-stages/manifest.json"))
                require(
                    stage["profile"] == profile
                    and set(stage["files"]) == STAGES
                    and stage["raw_sha256"] == field["capture_files"][chart]["raw"][0]["sha256"],
                    "ISP stages are not bound to this first RAW and profile",
                )
                for item in stage["files"].values():
                    require(
                        self.sha(artifact(folder, item["file"])) == item["sha256"],
                        "Stage hash changed",
                    )
                mask = np.load(folder / "comparison-valid-mask.npy", mmap_mode="r")
                require(
                    mask.shape == (256, 256)
                    and mask.dtype.kind == "b"
                    and int(mask.sum()) == lab["charts"][chart]["valid_pixels"]
                    and bool(mask.any()),
                    "Empty or inconsistent comparison mask",
                )
                key = (str(source_root.resolve()), field["id"], chart)
                mask_hash = self.sha(folder / "comparison-valid-mask.npy")
                self.support_masks.setdefault(key, mask_hash)
                require(
                    self.support_masks[key] == mask_hash,
                    "Candidates from the same source have different comparison support",
                )
                support = lab["metric_support"]
                if chart in {"color", "resolution"}:
                    rois = (
                        {
                            index: patch.get("roi")
                            for index, patch in enumerate(field["tests"]["color"]["patches"])
                        }
                        if chart == "color"
                        else {
                            name: site.get("roi")
                            for name, site in field["tests"]["resolution"]["sites"].items()
                        }
                    )
                    require(
                        support[chart] == _roi_support(mask, rois),
                        "Metric support differs from its measured ROIs",
                    )
                if chart == "texture":
                    metric_mask = np.load(folder / "texture-metric-valid-mask.npy", mmap_mode="r")
                    expected_mask = binary_erosion(
                        mask, structure=np.ones((9, 9), bool), border_value=0
                    )
                    require(
                        np.array_equal(metric_mask, expected_mask)
                        and int(metric_mask.sum()) == support[chart]["valid_pixels"],
                        "Texture metric lacks its additional highpass support",
                    )
                if (
                    source
                    and (
                        source_root / "fields" / field["id"] / chart / "comparison-valid-mask.npy"
                    ).exists()
                ):
                    require(
                        self.sha(folder / "comparison-valid-mask.npy")
                        == self.sha(
                            source_root
                            / "fields"
                            / field["id"]
                            / chart
                            / "comparison-valid-mask.npy"
                        ),
                        "Same-RAW comparison masks differ",
                    )
                processed = np.load(folder / "processed.npy", mmap_mode="r")
                expected = np.round(
                    np.clip(processed / profile["display_white_level"], 0, 1)
                    ** (1 / profile["tone_gamma"])
                    * 255
                )
                display = np.asarray(Image.open(folder / "display.png"))
                require(
                    processed.shape == (256, 256, 3)
                    and np.isfinite(processed).all()
                    and display.shape == expected.shape
                    and np.max(np.abs(display.astype(float) - expected)) <= 1,
                    "Display changed fixed linear-RGB white / gamma convention",
                )
            halos = [
                max(
                    s["processed_system"]["overshoot_fraction"],
                    s["processed_system"]["undershoot_fraction"],
                )
                for s in field["tests"]["resolution"]["sites"].values()
                if s.get("processed_system", {}).get("status") == "ok"
            ]
            results[field["id"]] = {
                **{key: lab[key] for key in keys},
                "delta_e00_mean": field["tests"]["color"]["delta_e00_mean"],
                "temporal_noise_rgb": lab["temporal_noise_rgb"],
                "color_noise_rgb": lab["color_noise_rgb"],
                "max_processed_halo_fraction": max(halos) if halos else None,
                "uniform_valid_pixels": lab["charts"]["uniform"]["valid_pixels"],
                "metric_support": lab["metric_support"],
            }
            if "demosaic_chirp" in charts:
                results[field["id"]]["demosaic"] = self.chirp(
                    field, root / "fields" / field["id"] / "demosaic_chirp"
                )
                require(
                    lab["demosaic"]["method"] == profile["demosaic_method"],
                    "Demosaic method mismatch",
                )
        return {
            "id": summary["id"],
            "profile": profile,
            "fields": results,
            "raw_arrays_verified": raw_count,
            "elapsed_seconds_recorded": summary.get("performance", {}).get("elapsed_seconds"),
            "color_calibration": self.color(summary, root, source_root),
        }

    def run(self, label, receipt):
        started = time.monotonic()
        require(receipt["status"] == "succeeded", "Runner receipt is not succeeded")
        record = self.record(receipt["id"])
        require(
            record.get("source_code") == self.source_code,
            "Completed job used stale implementation; run the current demo runner again",
        )
        summary, root = record["result"]["summary"], self.folder(record)
        require(
            receipt["manifest_sha256"] == summary["manifest_sha256"],
            "Receipt manifest differs from API",
        )
        group = {
            "label": label,
            "id": record["id"],
            "manifest_sha256": summary["manifest_sha256"],
            "source": record.get("source"),
            "timing": audit_timing(self.audit, record["id"]),
            **self.manifest(record, all_files=True),
        }
        recipe, count = {**REQUIRED, **OPTIONAL}[label]
        candidates = summary.get("candidates", [summary])
        require(
            len(candidates) == count and all(c.get("status") == "succeeded" for c in candidates),
            "Unexpected or incomplete recipe candidates",
        )
        expected_scale = (
            float(label.removeprefix("color-cfa-")) if label.startswith("color-cfa-") else 1.0
        )
        require(
            all(
                c["configuration"]["pixel"]["cfa_thickness_scale"] == expected_scale
                for c in candidates
            ),
            "Demo label refers to a different CFA scale",
        )
        expected_curvature = 1.005 if label.endswith("curvature") else 1.0
        require(
            all(
                c["configuration"]["lens"]["curvature_scale"] == expected_curvature
                for c in candidates
            ),
            "Demo label refers to a different lens curvature",
        )
        source = self.source(record["source"]) if recipe else None
        if recipe:
            study = summary["study"]
            require(
                study["recipe"] == recipe and study["source"] == record["source"],
                "Study source/recipe mismatch",
            )
            expected = reference_isp_studies.profiles(study, study.get("calibration_id"))
            require(
                [c["configuration"]["isp"] for c in candidates] == [p for _, p in expected],
                "Candidate profiles differ from declared recipe",
            )
            require(
                digest(reference_isp_studies.select(candidates, study))
                == digest(study["selection"]),
                "Saved selection differs from fixed constraints and measured fields",
            )
            group["selection"] = study["selection"]
        group["candidates"] = [
            self.candidate(c, root / c["id"] if recipe else root, source) for c in candidates
        ]
        group["verification_seconds"] = time.monotonic() - started
        self.groups.append(group)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--runs", default="outputs/reference-isp-lab-delivery-20261011/runs.json")
    parser.add_argument("--output", help="Default: verification.json alongside --runs")
    parser.add_argument(
        "--state-dir",
        default="outputs/camera-platform-virtual-lab-cra-20261011",
        help="Local Registry state for read-only audited job timing",
    )
    args = parser.parse_args()
    all_receipts = load(args.runs)
    receipts = {row["label"]: row for row in all_receipts}
    labels = {**REQUIRED, **(OPTIONAL if set(OPTIONAL) <= receipts.keys() else {})}
    unused = [
        row
        for row in all_receipts
        if row["label"] not in labels or row is not receipts[row["label"]]
    ]
    started = time.monotonic()
    with PlatformClient(args.base_url, timeout=120) as client:
        verifier = Verifier(client, Path(__file__).resolve().parents[1], Path(args.state_dir))
        for label in labels:
            try:
                require(label in receipts, f"Missing receipt: {label}")
                verifier.run(label, receipts[label])
                verifier.checks.append({"name": label, "passed": True})
            except Exception as exc:
                verifier.checks.append(
                    {"name": label, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
                )
            print(
                label,
                "verified" if verifier.checks[-1]["passed"] else verifier.checks[-1]["error"],
                flush=True,
            )
    groups = verifier.groups
    failures = [check for check in verifier.checks if not check["passed"]]
    result = {
        "schema": "camera_reference_isp_lab_delivery_verification_v1",
        "status": "verified" if not failures else "failed",
        "physical_validation": "not_established",
        "required_labels": list(REQUIRED),
        "optional_lens_variant_included": set(OPTIONAL) <= labels.keys(),
        "unused_receipts": [
            {key: row.get(key) for key in ("label", "id", "status")} for row in unused
        ],
        "verified_source_code_sha256": digest(verifier.source_code),
        "timing_audit": {
            key: verifier.audit[key] for key in ("path", "head_hash", "integrity", "scope")
        },
        "checks": verifier.checks,
        "failures": failures,
        "groups": groups,
        "files_verified": sum(g["files_verified"] for g in groups),
        "unique_local_paths_hashed": len(verifier.hashes),
        "raw_arrays_verified": sum(
            c["raw_arrays_verified"] for g in groups for c in g["candidates"]
        ),
        "candidates_verified": sum(len(g["candidates"]) for g in groups),
        "fields_verified": sum(len(c["fields"]) for g in groups for c in g["candidates"]),
        "manifest_artifact_storage": {
            "logical_bytes": sum(g["logical_bytes"] for g in groups),
            "allocated_bytes": sum(g["allocated_bytes"] for g in groups),
            "scope": "target run manifest entries only; excludes source-only runs and caches",
        },
        "verification_seconds": time.monotonic() - started,
        "chirp_specification": CHART_SPECIFICATION,
        "limits": [
            "Nine local native probes; not a full product sensor mode.",
            "Stored geometric ray PSF excludes diffraction and does not establish convergence.",
            "Color holdout and chirp ground references are model diagnostics, not measurements.",
            "Repeated RAW copies and seeds are not independent scenes or products.",
            "Execution wall time is not ISP hardware latency; no universal ISP winner claimed.",
        ],
    }
    output = Path(args.output) if args.output else Path(args.runs).with_name("verification.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    print(result["status"], output, flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
