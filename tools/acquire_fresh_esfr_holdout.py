"""Generate three new synthetic captures only after a server-recorded candidate freeze.

This acquisition tool never fits a model and never reads previous holdout pixels
or metrics. It reads only the frozen contract and the original development
capture metadata to reproduce the unchanged synthetic camera/lighting. The
private generator truth receipt is not uploaded as fitter input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import requests
import tifffile

from camera_platform.registry import canonical, digest


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def check_contract(contract, development):
    if contract.get("schema") != "camera_esfr_frozen_holdout_contract_v1":
        raise ValueError("A server-created frozen holdout contract is required")
    body = {k: v for k, v in contract.items() if k != "content_hash"}
    if digest(body) != contract.get("content_hash"):
        raise ValueError("Validation contract checksum mismatch")
    if len(development) != 3 or [c["id"] for c in development] != contract["development_capture_ids"]:
        raise ValueError("Exactly the three frozen development captures are required")
    if contract["required_validation_capture_count"] != 3:
        raise ValueError("Unsupported acquisition count")
    if any(c["metadata"]["role"] != "development" for c in development):
        raise ValueError("Previous validation captures must not be supplied to this generator")
    reference = development[0]["metadata"]
    for capture in development:
        for key in ("camera_id", "conditions", "raw_format", "geometry", "isp", "source_kind"):
            if capture["metadata"][key] != reference[key]:
                raise ValueError("Development acquisition conditions differ")
    if reference["source_kind"] != "synthetic" or reference.get("synthetic_generator", {}).get("method") != "full_chart_linear_rgb_gaussian_rggb_v1":
        raise ValueError("Only the known full-chart synthetic acquisition fixture is supported")
    fmt = reference["raw_format"]
    if reference["geometry"] != {"full_sensor_shape": [fmt["height"], fmt["width"]], "crop_origin": [0, 0]} or fmt["cfa"] != "RGGB":
        raise ValueError("Generator requires the unchanged full-native RGGB geometry")
    return reference


def generate(contract, development, output, *, renderer=None, capture_generator=None):
    reference = check_contract(contract, development)
    from camera_platform.esfr_chart import render_chart
    from camera_platform.esfr_demo import chart_capture
    from camera_platform.esfr_lab import chart_template

    if chart_template() != contract["chart_profile"]["chart_geometry"]:
        raise ValueError("Installed chart package differs from frozen acquisition contract")
    now = datetime.now(timezone.utc)
    if now < datetime.fromisoformat(contract["created_at"]):
        raise ValueError("Candidate freeze is in the future; no capture may be generated")
    generator = reference["synthetic_generator"]
    sigma = generator["discrete_gaussian_sigma_native_px"]
    supersample = generator["pixel_area_supersample"]
    fmt = reference["raw_format"]
    scene, rois, render_manifest = (renderer or render_chart)(shape=(fmt["height"], fmt["width"]), supersample=supersample)
    if rois != contract["rois"]:
        raise ValueError("Generated native chart ROIs differ from frozen contract")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "validation-contract.json").write_text(canonical(contract))
    token = secrets.token_hex(12)
    old_seeds = {c["metadata"]["synthetic_generator"]["seed"] for c in development}
    seeds = []
    while len(seeds) < 3:
        seed = secrets.randbits(63) | (1 << 62)
        if seed not in old_seeds and seed not in seeds:
            seeds.append(seed)
    generated_at = now.isoformat()
    rows = []
    for index, seed in enumerate(seeds):
        raw, isp = (capture_generator or chart_capture)(scene, seed, sigma)
        raw_path = output / f"fresh-validation-{index + 1}.raw"
        isp_path = output / f"fresh-validation-{index + 1}.tiff"
        raw.tofile(raw_path)
        tifffile.imwrite(isp_path, isp, photometric="rgb")
        metadata = deepcopy(reference)
        metadata.pop("parent_acquisition_ids", None)
        metadata.update(
            role="validation", session_id="fresh-validation-" + token,
            acquisition_id=f"fresh-{token}-{index + 1}", generated_at=generated_at,
            validation_contract={"id": contract["id"], "content_hash": contract["content_hash"]},
            synthetic_generator={"method": "full_chart_linear_rgb_gaussian_rggb_v1", "seed": seed,
                                 "acquisition_scope": "new_noise_realization_after_candidate_freeze",
                                 "generator_truth": "separate_private_receipt_not_fitter_input"},
        )
        metadata_path = output / f"fresh-validation-{index + 1}.json"
        metadata_path.write_text(canonical(metadata))
        rows.append({"metadata_path": metadata_path.name, "raw_path": raw_path.name, "isp_path": isp_path.name,
                     "raw_sha256": _sha(raw_path), "isp_sha256": _sha(isp_path),
                     "metadata_sha256": _sha(metadata_path), "seed": seed,
                     "session_id": metadata["session_id"], "acquisition_id": metadata["acquisition_id"]})
    if len({r["raw_sha256"] for r in rows}) != 3 or len({r["isp_sha256"] for r in rows}) != 3:
        raise ValueError("Fresh acquisitions unexpectedly have identical bytes")
    manifest = {"schema": "camera_esfr_fresh_acquisition_manifest_v1", "generated_at": generated_at,
                "validation_contract_id": contract["id"], "validation_contract_sha256": contract["content_hash"],
                "parent_study_id": contract["parent_study_id"], "candidate_sha256": contract["candidate_sha256"],
                "development_capture_ids": contract["development_capture_ids"], "captures": rows,
                "unchanged_conditions": reference["conditions"], "render_manifest": render_manifest,
                "previous_holdout_read": False, "fit_performed": False,
                "generator_truth_uploaded_to_fitter": False,
                "physical_validation": "not_established"}
    (output / "acquisition-manifest.json").write_text(canonical(manifest))
    (output / "private-generator-truth.json").write_text(canonical({
        "scope": "acquisition fixture only; never upload as calibration input",
        "discrete_gaussian_sigma_native_px": sigma, "pixel_area_supersample": supersample,
        "generator_source_sha256": _sha(Path(__file__)), "seeds": seeds}))
    return manifest


def upload(base_url, output, manifest, contract):
    bootstrap = requests.get(base_url + "/api/v1/session", timeout=60)
    bootstrap.raise_for_status()
    token = bootstrap.json().get("token")
    if not token:
        raise ValueError("Local platform session token is unavailable")
    headers = {"X-Platform-Token": token}
    ids = []
    for row in manifest["captures"]:
        output = Path(output)
        for key in ("raw", "isp", "metadata"):
            if _sha(output / row[key + "_path"]) != row[key + "_sha256"]:
                raise ValueError("Fresh acquisition changed before import: " + key)
        with (output / row["raw_path"]).open("rb") as raw, (output / row["isp_path"]).open("rb") as isp:
            response = requests.post(base_url + "/api/v1/camera-validation/esfr/captures",
                headers=headers, data={"metadata": (output / row["metadata_path"]).read_text()},
                files={"raw": (row["raw_path"], raw, "application/octet-stream"),
                       "isp": (row["isp_path"], isp, "image/tiff")}, timeout=600)
        response.raise_for_status()
        imported = response.json()
        (output / (row["metadata_path"] + ".import.json")).write_text(canonical(imported))
        ids.append(imported["id"])
    request = {"name": "Frozen candidate fresh holdout · measurement only",
               "development_ids": contract["development_capture_ids"], "validation_ids": ids,
               "fit": False, **{k: contract[k] for k in ("rois", "chart_profile", "thresholds", "baseline_sigma_native_px", "source")}}
    (Path(output) / "measurement-only-study-request.json").write_text(canonical(request))
    return request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--contract-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--generate", action="store_true", help="Explicitly authorize acquisition after candidate freeze")
    parser.add_argument("--upload", action="store_true", help="Import new original files; save but do not submit the measurement-only study request")
    args = parser.parse_args()
    if not args.generate:
        parser.error("No acquisition occurs without --generate and an existing frozen server contract")
    base = args.base_url.rstrip("/")
    response = requests.get(base + "/api/v1/camera-validation/esfr/validation-contracts/" + args.contract_id, timeout=600)
    response.raise_for_status(); contract = response.json()
    development = []
    for identity in contract["development_capture_ids"]:
        response = requests.get(base + "/api/v1/camera-validation/esfr/captures/" + identity, timeout=600)
        response.raise_for_status(); development.append(response.json())
    manifest = generate(contract, development, args.output)
    if args.upload:
        upload(base, args.output, manifest, contract)
    print(canonical({"output": str(Path(args.output).resolve()), "candidate_sha256": contract["candidate_sha256"],
                     "new_capture_count": len(manifest["captures"]), "uploaded": args.upload, "fit_performed": False}))


if __name__ == "__main__":
    main()
