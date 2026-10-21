#!/usr/bin/env python3
"""Verify the public Reference workshop delivery and summarize actual evidence.

This reads completed API records and their hash-bound files on project storage.
It never reruns optics, changes artifacts, or treats a failed field as a result.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from camera_platform.reference_workshop_optics import CSV_SHA256, REVISION
from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient
from camera_platform.virtual_lab_job import sha256

REQUIRED = ("baseline", "lens-curvature", "cfa", "lsc", "lsc-denoise", "low-light")
FIELD_IDS = {
    "center",
    "mid-right",
    "mid-top",
    "mid-left",
    "mid-bottom",
    "outer-ne",
    "outer-nw",
    "outer-sw",
    "outer-se",
}
SCENES = {"resolution", "color", "uniform", "texture"}


def _load(path):
    return json.loads(Path(path).read_text())


def _path(root, relative):
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        raise ValueError("Artifact path is outside the run or missing: " + str(relative))
    return candidate


class Evidence:
    def __init__(self):
        self.checks = []
        self.groups = []
        self.metrics = {}

    def check(self, name, condition, **details):
        self.checks.append({"name": name, "passed": bool(condition), **details})

    def error(self, name, exc):
        self.check(name, False, error=f"{type(exc).__name__}: {exc}")

    def result(self):
        failures = [row for row in self.checks if not row["passed"]]
        return {
            "schema": "camera_reference_workshop_delivery_verification_v1",
            "status": "verified" if not failures else "failed",
            "physical_validation": "not_established",
            "groups": self.groups,
            "files_verified": sum(group.get("files_verified", 0) for group in self.groups),
            "raw_arrays_verified": sum(
                group.get("raw_arrays_verified", 0) for group in self.groups
            ),
            "manifest_artifact_storage": {
                "logical_bytes": sum(group.get("logical_bytes", 0) for group in self.groups),
                "allocated_bytes": sum(group.get("allocated_bytes", 0) for group in self.groups),
                "scope": "files listed in each verified run manifest; caches excluded",
            },
            "checks": self.checks,
            "failures": failures,
            "representative_metrics": self.metrics,
            "limits": [
                "Evidence of artifact integrity and implemented relative design effects only.",
                "Nine local native optical probes are not a full product sensor frame.",
                "Geometric ray PSF excludes diffraction; "
                "no product optical/electrical calibration.",
            ],
        }


def _fields(summary):
    return {field["id"]: field for field in summary["fields"]}


def _raw_hashes(summary):
    return {
        (field["id"], kind, frame["file"]): frame["sha256"]
        for field in summary["fields"]
        if field["status"] == "succeeded"
        for kind, capture in field["capture_files"].items()
        for frame in capture["raw"]
    }


def _capture_inputs(summary):
    return {
        (field["id"], kind): (capture["rate_sha256"], capture["source_reference_sha256"])
        for field in summary["fields"]
        if field["status"] == "succeeded"
        for kind, capture in field["capture_files"].items()
    }


def _representative(summary):
    fields = _fields(summary)
    return {
        "configuration": summary["configuration"],
        "calibration_id": summary["calibration"]["id"],
        "optical_revision": summary["optical_revision"],
        "performance": summary.get("performance"),
        "fields": {
            key: {
                "metrics": field.get("metrics", {}),
                "status": field["status"],
                "relative_illumination": field.get("optics", {}).get("relative_illumination"),
                "chief_ray_angle_deg": field.get("optics", {}).get("chief_ray_angle_deg"),
                "processed_noise": field.get("tests", {})
                .get("noise", {})
                .get("uniform", {})
                .get("processed"),
                "texture": field.get("tests", {}).get("texture", {}),
                "geometric_mtf_at_0_125_cycles_per_native_pixel": {
                    axis: float(
                        np.interp(
                            0.125,
                            field["optics"]["mtf"]["cycles_per_native_pixel"],
                            field["optics"]["mtf"][axis],
                        )
                    )
                    for axis in ("radial", "tangential")
                }
                if field.get("optics", {}).get("mtf")
                else None,
            }
            for key, field in fields.items()
            if key in {"center", "mid-right", "outer-ne"}
        },
    }


def _candidate(evidence, label, summary, folder):
    fields = _fields(summary)
    evidence.check(
        label + "/nine-native-fields", set(fields) == FIELD_IDS, field_ids=sorted(fields)
    )
    evidence.check(
        label + "/all-fields-computed",
        all(field["status"] == "succeeded" for field in fields.values()),
        refused=[field["id"] for field in fields.values() if field["status"] != "succeeded"],
    )
    cell = _load(folder / "pixel-cell.json")
    optical = _load(folder / "optical-response.json")
    reference = optical["reference"]
    evidence.check(
        label + "/pinned-lens-source",
        reference.get("revision_id") == REVISION
        and reference.get("reviewed_csv_sha256") == CSV_SHA256,
        revision_id=reference.get("revision_id"),
        reviewed_csv_sha256=reference.get("reviewed_csv_sha256"),
    )
    expected_surfaces = json.loads(json.dumps(reference["surfaces"]))
    first = next(
        index
        for index, surface in enumerate(expected_surfaces)
        if surface["radius_mm"] not in (None, 0)
    )
    lens = summary["configuration"]["lens"]
    expected_surfaces[first]["radius_mm"] /= lens["curvature_scale"]
    expected_surfaces[-1]["thickness_mm"] += lens["sensor_shift_um"] / 1000
    evidence.check(
        label + "/actual-prescription-matches-controls",
        optical["prescription"]["surfaces"] == expected_surfaces,
        curvature_scale=lens["curvature_scale"],
        sensor_shift_um=lens["sensor_shift_um"],
    )
    evidence.check(
        label + "/native-reviewed-pixel",
        cell["source_report"] == "DEF-2011-804"
        and cell["pixel_pitch_um"] == cell["physical_model"]["pixel_pitch_um"] == 2.9
        and cell["provenance"]["source_pixel_pitch_um"] == 2.9
        and cell["provenance"]["structural_evidence"]["available"]
        and cell["physical_model"]["optical_assumptions"]["geometry_source"]
        == "reviewed_techinsights",
    )
    raw_count, saturation = 0, {}
    for field_id, field in fields.items():
        if field["status"] != "succeeded":
            evidence.check(
                label + "/failed-field-not-filled/" + field_id,
                not field.get("images") and not field.get("metrics"),
            )
            continue
        if field["geometry"]["native_shape_row_col"] != [256, 256]:
            raise ValueError(f"{field_id}: expected 256×256 native calculation")
        if float(field["geometry"]["pixel_pitch_um"]) != 2.9:
            raise ValueError(f"{field_id}: native pitch was changed")
        if set(field["capture_files"]) != SCENES:
            raise ValueError(f"{field_id}: expected all four Lab input scenes")
        readout = cell["readout"]
        white = 2 ** readout["bit_depth"] - 1
        for kind, capture in field["capture_files"].items():
            saturation.setdefault(field_id, {})[kind] = _load(
                folder / f"fields/{field_id}/{kind}/capture.json"
            )["saturation_fractions"]
            if len(capture["raw"]) != summary["configuration"]["frames"]:
                raise ValueError(f"{field_id}/{kind}: repeated RAW count does not match")
            for frame in capture["raw"]:
                path = _path(folder, f"fields/{field_id}/{kind}/{frame['file']}")
                if sha256(path) != frame["sha256"]:
                    raise ValueError(f"{field_id}/{kind}: captured RAW hash mismatch")
                raw = np.load(path, mmap_mode="r", allow_pickle=False)
                if (
                    raw.shape != (256, 256)
                    or raw.dtype.kind not in "iu"
                    or raw.min() < 0
                    or raw.max() > white
                ):
                    raise ValueError(f"{field_id}/{kind}: invalid native integer RAW")
                raw_count += 1
            for filename, hash_key in (
                ("mean-electron-rate.npy", "rate_sha256"),
                ("source-reference-linear.npy", "source_reference_sha256"),
            ):
                path = _path(folder, f"fields/{field_id}/{kind}/{filename}")
                if sha256(path) != capture[hash_key]:
                    raise ValueError(f"{field_id}/{kind}: preserved {filename} mismatch")
            processed = np.load(folder / f"fields/{field_id}/{kind}/processed.npy")
            isp = summary["configuration"]["isp"]
            expected = np.round(
                np.clip(processed / isp["display_white_level"], 0, 1) ** (1 / isp["tone_gamma"])
                * 255
            )
            display = np.asarray(Image.open(folder / field["images"][kind]["display"]))
            if (
                display.shape != (256, 256, 3)
                or np.max(np.abs(display.astype(float) - expected)) > 1
            ):
                raise ValueError(f"{field_id}/{kind}: display does not use fixed declared range")
    evidence.check(label + "/native-raw-and-fixed-display", True, raw_arrays=raw_count)
    evidence.metrics[label] = _representative(summary)
    for field_id, metrics in evidence.metrics[label]["fields"].items():
        metrics["saturation_by_scene"] = saturation.get(field_id, {})
    return raw_count


def _verify_run(evidence, label, record):
    summary = record["result"]["summary"]
    folder = Path(record["storage_root"]) / "reference-workshop" / record["id"]
    manifest_path = folder / "manifest.json"
    if sha256(manifest_path) != summary["manifest_sha256"]:
        raise ValueError("Top-level manifest hash differs from public result")
    files = _load(manifest_path)["files"]
    total_bytes, allocated_bytes = 0, 0
    for relative, expected in files.items():
        path = _path(folder, relative)
        if sha256(path) != expected:
            raise ValueError("Manifest artifact changed: " + relative)
        file_stat = path.stat()
        total_bytes += file_stat.st_size
        allocated_bytes += file_stat.st_blocks * 512
    group = {
        "label": label,
        "id": record["id"],
        "files_verified": len(files),
        "logical_bytes": total_bytes,
        "allocated_bytes": allocated_bytes,
        "raw_arrays_verified": 0,
    }
    evidence.groups.append(group)
    evidence.check(label + "/manifest", True, files=len(files))
    candidates = summary.get("candidates")
    if candidates is None:
        group["raw_arrays_verified"] = _candidate(evidence, label, summary, folder)
    else:
        if label == "lens-grid":
            controls = {
                (
                    item["configuration"]["lens"]["curvature_scale"],
                    item["configuration"]["lens"]["sensor_shift_um"],
                )
                for item in candidates
                if item["status"] == "succeeded"
            }
            evidence.check(
                "lens-grid/nine-curvature-focus-candidates",
                len(candidates) == 9
                and controls
                == {
                    (curvature, focus)
                    for curvature in (0.995, 1.0, 1.005)
                    for focus in (-20.0, 0.0, 20.0)
                },
                computed_controls=[list(value) for value in sorted(controls)],
            )
        for item in candidates:
            tag = label + "/" + item["id"]
            if item.get("status", "succeeded") != "succeeded":
                evidence.check(tag + "/candidate-computed", False, error=item.get("error"))
                continue
            group["raw_arrays_verified"] += _candidate(evidence, tag, item, folder / item["id"])
    return folder


def _relations(evidence, records, folders):
    summaries = {key: value["result"]["summary"] for key, value in records.items()}
    baseline = summaries["baseline"]
    baseline_optical = _load(folders["baseline"] / "optical-response.json")
    changed_optical = _load(folders["lens-curvature"] / "optical-response.json")
    source, changed = (
        baseline_optical["prescription"]["surfaces"],
        changed_optical["prescription"]["surfaces"],
    )
    first = next(
        index for index, surface in enumerate(source) if surface["radius_mm"] not in (None, 0)
    )
    factor = summaries["lens-curvature"]["configuration"]["lens"]["curvature_scale"]
    original_factor = baseline["configuration"]["lens"]["curvature_scale"]
    expected = json.loads(json.dumps(source))
    expected[first]["radius_mm"] /= factor / original_factor
    evidence.check(
        "curvature/actual-prescription-edited",
        changed == expected
        and factor != original_factor
        and baseline_optical["revision"] != changed_optical["revision"],
        surface_index=first,
        baseline_radius_mm=source[first]["radius_mm"],
        changed_radius_mm=changed[first]["radius_mm"],
        curvature_scale=factor,
    )
    fields0, fields1 = _fields(baseline), _fields(summaries["lens-curvature"])
    changed_rates = [
        name
        for name in FIELD_IDS
        if fields0[name]["status"] == fields1[name]["status"] == "succeeded"
        and fields0[name]["capture_files"]["resolution"]["rate_sha256"]
        != fields1[name]["capture_files"]["resolution"]["rate_sha256"]
    ]
    evidence.check(
        "curvature/change-reaches-raw-input", bool(changed_rates), changed_fields=changed_rates
    )
    cfa = summaries["cfa"]["candidates"]
    candidates = {
        float(item["configuration"]["pixel"]["cfa_thickness_scale"]): item
        for item in cfa
        if item["status"] == "succeeded"
    }
    evidence.check("cfa/three-computed-structures", set(candidates) == {0.9, 1.0, 1.1})
    qe, means, heights, calids = {}, {}, {}, set()
    for scale, item in sorted(candidates.items()):
        root = folders["cfa"] / item["id"]
        response = _load(root / "pixel-response.json")
        cell = _load(root / "pixel-cell.json")
        index = response["wavelength_nm"].index(550)
        qe[str(scale)] = np.asarray(response["normal_qe"])[index].tolist()
        means[str(scale)] = _fields(item)["center"]["metrics"]["mean_electrons"]
        heights[str(scale)] = cell["cross_section"]["top_stack_height_um"]
        calids.add(item["calibration"]["id"])
    evidence.check(
        "cfa/absolute-response-and-mean-signal-differ",
        len(qe) == 3
        and len({digest(value) for value in qe.values()}) == 3
        and np.ptp(list(means.values())) > 0,
        qe_550nm_R_Gr_Gb_B=qe,
        center_mean_electrons=means,
    )
    evidence.check(
        "cfa/fixed-height-and-baseline-calibration",
        len(heights) == 3
        and all(np.isclose(value, 2.5) for value in heights.values())
        and calids == {baseline["calibration"]["id"]},
        top_stack_height_um=heights,
    )
    for label in ("lsc", "lsc-denoise"):
        candidate = summaries[label]
        evidence.check(
            label + "/same-raw-and-calibration",
            _raw_hashes(candidate) == _raw_hashes(baseline)
            and _capture_inputs(candidate) == _capture_inputs(baseline)
            and candidate["calibration"] == baseline["calibration"]
            and records[label].get("source", {}).get("run_id") == records["baseline"]["id"],
        )
        evidence.check(
            label + "/lsc-actually-applied",
            all(
                field.get("lsc_applied")
                for field in candidate["fields"]
                if field["status"] == "succeeded"
            ),
        )
    low = summaries["low-light"]
    factor = baseline["configuration"]["luminance_cd_m2"] / low["configuration"]["luminance_cd_m2"]
    rate_ratios = {}
    all_rates = True
    for field in baseline["fields"]:
        if field["status"] != "succeeded":
            continue
        for kind in SCENES:
            path = f"fields/{field['id']}/{kind}/mean-electron-rate.npy"
            high_array, low_array = (
                np.load(folders["baseline"] / path),
                np.load(folders["low-light"] / path),
            )
            all_rates = all_rates and bool(
                np.allclose(high_array, low_array * factor, rtol=1e-10, atol=1e-10)
            )
            if kind == "uniform":
                rate_ratios[field["id"]] = float(high_array.mean() / low_array.mean())
    evidence.check(
        "illumination/noiseless-rate-scales-before-readout",
        all_rates and factor == 10,
        declared_ratio=factor,
        observed_uniform_ratios=rate_ratios,
    )
    evidence.check(
        "illumination/raw-not-reused-as-scaled-input", _raw_hashes(baseline) != _raw_hashes(low)
    )


def _additional_relations(evidence, records, folders):
    """Check optional prepared cases by configuration and exact source identity."""
    by_id = {record["id"]: record for record in records.values()}
    baseline = records["low-light"]["result"]["summary"]
    for label, record in records.items():
        if label in REQUIRED:
            continue
        summary = record["result"]["summary"]
        source = record.get("source")
        if source:
            original = by_id[source["run_id"]]["result"]["summary"]
            if source.get("candidate_id"):
                original = next(
                    item for item in original["candidates"] if item["id"] == source["candidate_id"]
                )
            evidence.check(
                label + "/exact-source-raw-and-frozen-calibration",
                _raw_hashes(summary) == _raw_hashes(original)
                and _capture_inputs(summary) == _capture_inputs(original)
                and summary["calibration"] == original["calibration"],
                source_run_id=source["run_id"],
            )
        candidates = summary.get("candidates", [summary])
        scales = set()
        cfa_rates = []
        cfa_responses = []
        for item in candidates:
            if item.get("status", "succeeded") != "succeeded":
                continue
            configuration = item["configuration"]
            if configuration["luminance_cd_m2"] != 1000:
                continue
            tag = label + ("/" + item["id"] if "candidates" in summary else "")
            evidence.check(
                tag + "/fixed-low-light-calibration", item["calibration"] == baseline["calibration"]
            )
            if configuration["lens"]["curvature_scale"] != 1:
                original_fields, fields = _fields(baseline), _fields(item)
                differences = [
                    key
                    for key in FIELD_IDS
                    if fields[key]["status"] == original_fields[key]["status"] == "succeeded"
                    and fields[key]["capture_files"]["resolution"]["rate_sha256"]
                    != original_fields[key]["capture_files"]["resolution"]["rate_sha256"]
                ]
                evidence.check(
                    tag + "/lens-change-reaches-raw-input",
                    bool(differences),
                    changed_fields=sorted(differences),
                )
            if configuration["lens"] == baseline["configuration"]["lens"]:
                scale = configuration["pixel"]["cfa_thickness_scale"]
                scales.add(scale)
                cfa_rates.append(_fields(item)["center"]["metrics"]["mean_electrons"])
                directory = (
                    folders[label] / item["id"] if "candidates" in summary else folders[label]
                )
                response = _load(directory / "pixel-response.json")
                cfa_responses.append(digest(response["normal_qe"]))
        if scales == {0.9, 1.0, 1.1}:
            evidence.check(
                label + "/three-cfa-effects-at-low-light",
                len(set(cfa_responses)) == 3 and np.ptp(cfa_rates) > 0,
                center_mean_electrons=cfa_rates,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--runs", default="outputs/reference-workshop-delivery-20261011/runs.json")
    parser.add_argument(
        "--output", default="outputs/reference-workshop-delivery-20261011/verification.json"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Inspect saved completed API records without refreshing status.",
    )
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    evidence = Evidence()
    completed, folders = {}, {}
    try:
        records = _load(workspace / args.runs)
        latest = {record["label"]: record for record in records}
        with PlatformClient(args.base_url, timeout=120) as client:
            for label in dict.fromkeys([*REQUIRED, *latest]):
                try:
                    if label not in latest:
                        raise ValueError("Required demo group has not been submitted")
                    record = (
                        latest[label]
                        if args.offline
                        else client.reference_workshop_run(latest[label]["id"])
                    )
                    if record["status"] != "succeeded":
                        raise ValueError("Demo group is " + record["status"])
                    folders[label] = _verify_run(evidence, label, record)
                    completed[label] = record
                except Exception as exc:
                    evidence.error(label + "/verification", exc)
        if set(REQUIRED) <= set(completed):
            try:
                _relations(evidence, completed, folders)
                _additional_relations(evidence, completed, folders)
            except Exception as exc:
                evidence.error("cross-design-relations", exc)
    except Exception as exc:
        evidence.error("delivery-input", exc)
    output = evidence.result()
    output["record_source"] = "saved_api_records" if args.offline else args.base_url
    target = workspace / args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False))
    print(
        json.dumps(
            {
                "status": output["status"],
                "files_verified": output["files_verified"],
                "raw_arrays_verified": output["raw_arrays_verified"],
                "failures": output["failures"],
                "output": str(target),
            },
            indent=2,
        )
    )
    return 0 if output["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
