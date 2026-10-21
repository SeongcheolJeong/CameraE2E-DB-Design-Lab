#!/usr/bin/env python3
"""Prepare and verify the source-bound Reference Pixel Design Lab through the SDK.

Uses existing acquisition and color-study artifacts; never copies native RAW.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from importlib.resources import files
from pathlib import Path

import numpy as np
from verify_reference_isp_lab_demo import audit_snapshot, audit_timing

from camera_platform.reference_pixel_job import fingerprint
from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient


def load(path):
    return json.loads(Path(path).read_text())


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def pinned_specification():
    return json.loads(
        files("camera_platform").joinpath("data/reference_pixel_study.json").read_text()
    )


def source_pins(specification):
    pins = {}
    for candidate in specification["sources"]:
        for id_key, hash_key in (
            ("source_run_id", "source_manifest_sha256"),
            ("color_run_id", "color_manifest_sha256"),
        ):
            identity, checksum = candidate[id_key], candidate[hash_key]
            if identity in pins and pins[identity] != checksum:
                raise ValueError("Conflicting source manifest pins")
            pins[identity] = checksum
    return pins


def verify_result(record, specification, storage_root, state_dir):
    """Verify new small artifacts and exact source manifests without duplicating source files."""
    if record["status"] != "succeeded":
        raise ValueError("Pixel study is not complete")
    root = (Path(storage_root) / "reference-workshop" / record["id"]).resolve()
    summary = record["result"]["summary"]
    if record["source_code"] != fingerprint() or summary["source_code"] != fingerprint():
        raise ValueError("Pixel diagnostic code changed after this result was submitted")
    if summary["source_specification_sha256"] != digest(specification):
        raise ValueError("Result is bound to a different Pixel study specification")
    if (
        summary.get("raw_preserved") is not True
        or summary.get("raw_copied") is not False
        or summary.get("new_acquisitions") != 0
    ):
        raise ValueError("Pixel study must preserve source RAW without copying or recapturing it")
    if sha(root / "manifest.json") != summary["manifest_sha256"]:
        raise ValueError("Result manifest differs from the public record")
    manifest = load(root / "manifest.json")["files"]
    logical = allocated = 0
    for name, checksum in manifest.items():
        path = (root / name).resolve()
        if any(part.startswith("._") for part in Path(name).parts):
            raise ValueError("Export manifest must exclude filesystem metadata sidecars")
        if not path.is_relative_to(root) or not path.is_file() or sha(path) != checksum:
            raise ValueError("Missing or changed Pixel Lab artifact: " + name)
        if path.suffix.lower() in {".npy", ".npz"} or path.name.startswith("raw-"):
            raise ValueError("Pixel comparison unexpectedly copied native numerical capture files")
        stat = path.stat()
        logical += stat.st_size
        allocated += getattr(stat, "st_blocks", 0) * 512
    measurements = []
    expected_fields = {
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
    for pin in specification["sources"]:
        candidate = load(root / "candidates" / (pin["id"] + ".json"))
        if (
            candidate["id"] != pin["id"]
            or candidate["cfa_thickness_scale"] != pin["cfa_thickness_scale"]
        ):
            raise ValueError("Saved diagnostic candidate does not match its source pin")
        if {field["id"] for field in candidate["fields"]} != expected_fields:
            raise ValueError("Pixel diagnostic package must report all nine optical fields")
        if (
            candidate["source_refs"]["acquisition"]["manifest_sha256"]
            != pin["source_manifest_sha256"]
        ):
            raise ValueError("Candidate acquisition pin differs from its requested source")
        if candidate["source_refs"]["color"]["manifest_sha256"] != pin["color_manifest_sha256"]:
            raise ValueError("Candidate color-study pin differs from its requested source")
        wave, site = candidate["wavelength_nm"].index(550), candidate["sites"].index("Gr")
        for name in ("normal", "table", "delta_normal", "delta_table"):
            if not np.isfinite(candidate["qe"][name]).all():
                raise ValueError("Non-finite saved QE diagnostic")
        if candidate["id"] == specification["baseline_candidate_id"]:
            if np.any(candidate["qe"]["delta_normal"]) or np.any(candidate["qe"]["delta_table"]):
                raise ValueError("Baseline QE delta must be exactly zero")
        for field in candidate["fields"]:
            row = {"candidate_id": pin["id"], "field_id": field["id"], "status": field["status"]}
            if field["status"] == "available":
                for key in ("normal_qe", "chief_qe", "pupil_qe"):
                    if not np.isfinite(field[key]).all():
                        raise ValueError("Non-finite saved field response")
                    row[key + "_550nm_Gr"] = field[key][wave][site]
                color = field["color_comparison"]
                row["color_status"] = color["status"]
                for arm in ("frozen", "munsell"):
                    values = color.get(arm, {})
                    row[arm] = {
                        key: values.get(key)
                        for key in ("delta_e00_mean", "color_noise_luma", "uniform_noise_luma")
                    }
                    row[arm]["eligible"] = (
                        values.get("metric_support", {}).get("color", {}).get("eligible")
                    )
            else:
                row["reason"] = field.get("reason")
            measurements.append(row)
    sources = []
    for identity, checksum in source_pins(specification).items():
        path = Path(storage_root) / "reference-workshop" / identity / "manifest.json"
        if sha(path) != checksum:
            raise ValueError("Pinned source manifest changed: " + identity)
        sources.append({"run_id": identity, "manifest_sha256": checksum})
    audit = audit_snapshot(state_dir)
    return {
        "schema": "camera_reference_pixel_lab_verification_v1",
        "status": "verified",
        "run_id": record["id"],
        "manifest_sha256": summary["manifest_sha256"],
        "specification_sha256": digest(specification),
        "source_manifests": sources,
        "files_verified": len(manifest),
        "candidates": len(specification["sources"]),
        "fields_reported": len(measurements),
        "measurements": measurements,
        "new_raw_copies": 0,
        "storage": {
            "logical_bytes": logical,
            "allocated_bytes": allocated,
            "scope": "new Pixel Lab manifest only; existing acquisition/color data excluded",
        },
        "timing": audit_timing(audit, record["id"]),
        "audit_head_hash": audit["head_hash"],
        "physical_validation": "not_established",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument(
        "--output", default="outputs/reference-pixel-lab-delivery-20261011/runs.json"
    )
    parser.add_argument("--state-dir", default="outputs/camera-platform-virtual-lab-cra-20261011")
    parser.add_argument("--timeout", type=float, default=7260)
    parser.add_argument(
        "--comparison-spec", help="Optional JSON to save through the comparison SDK"
    )
    args = parser.parse_args()
    output = Path(args.output)
    specification = pinned_specification()
    checksum, code = digest(specification), fingerprint()
    receipts = load(output) if output.exists() else []
    write(output.with_name("specification.json"), specification)
    with PlatformClient(args.base_url, timeout=120) as client:
        catalog = client.get("reference-workshop/pixel-studies")
        if digest(catalog["template"]) != checksum:
            raise ValueError(
                "Server template differs from the packaged pinned Pixel Lab specification"
            )
        previous = next(
            (r for r in reversed(receipts) if r.get("specification_sha256") == checksum), None
        )
        job = client.reference_workshop_run(previous["id"]) if previous else None
        if (
            job is None
            or job["status"] not in {"queued", "running", "succeeded"}
            or job.get("source_code") != code
        ):
            job = client.submit_reference_pixel_study(specification)
            previous = {
                "id": job["id"],
                "status": job["status"],
                "specification_sha256": checksum,
                "source_code_sha256": digest(code),
            }
            receipts.append(previous)
            write(output, receipts)
        print("Reference Pixel Lab", job["id"], job["status"], flush=True)
        started = time.monotonic()
        if job["status"] in {"queued", "running"}:
            try:
                job = client.wait_reference_workshop(
                    job["id"], timeout=args.timeout, poll_interval=2
                )
            except TimeoutError:
                previous["status"] = "running_or_queued"
                write(output, receipts)
                raise
        previous.update(status=job["status"], runner_wait_seconds=time.monotonic() - started)
        if job["status"] != "succeeded":
            previous["error"] = job.get("error")
            write(output, receipts)
            raise RuntimeError(job.get("error") or job["status"])
        summary = job["result"]["summary"]
        previous.update(
            manifest_sha256=summary["manifest_sha256"],
            url=args.base_url.rstrip("/")
            + "/reference-workshop.html?pixel_study="
            + job["id"]
            + "&pixel_candidate=candidate-00&field=outer-ne",
            report_url=args.base_url.rstrip("/")
            + "/api/v1/reference-workshop/runs/"
            + job["id"]
            + "/files/report.html",
            bundle_url=args.base_url.rstrip("/")
            + "/api/v1/reference-workshop/runs/"
            + job["id"]
            + "/bundle",
        )
        write(output, receipts)
        verification = verify_result(job, specification, job["storage_root"], args.state_dir)
        write(output.with_name("verification.json"), verification)
        write(output.with_name("result.json"), summary)
        if args.comparison_spec:
            comparison = client.save_reference_pixel_comparison(
                job["id"], load(args.comparison_spec)
            )
            write(output.with_name("comparison.json"), comparison)
        write(
            output.with_name("delivery.json"),
            {
                "schema": "camera_reference_pixel_lab_delivery_v1",
                "status": "verified",
                **previous,
                "verification": verification,
                "comparison_saved": bool(args.comparison_spec),
            },
        )
        print(
            json.dumps(
                {
                    "run_id": job["id"],
                    "report": previous["report_url"],
                    "files": verification["files_verified"],
                    "logical_bytes": verification["storage"]["logical_bytes"],
                    "execution_seconds": verification["timing"]["execution_wall_seconds"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
