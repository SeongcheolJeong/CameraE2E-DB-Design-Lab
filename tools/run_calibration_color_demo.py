#!/usr/bin/env python3
"""Execute the complete QE/RAW/CCM synthetic workflow and export its frozen report."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import zipfile
from pathlib import Path

from camera_platform.calibration_color_demo import template
from camera_platform.sdk import PlatformClient


def run(base_url, output, seed=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    inputs = output / "input.json"
    if inputs.exists():
        payload = json.loads(inputs.read_text())
        if seed is not None and seed != payload["source_notes"]["seed"]:
            raise ValueError("Output already contains another frozen dataset")
    else:
        payload = template(seed if seed is not None else secrets.randbits(31))
        inputs.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    witness = {
        "schema": "camera_spectral_color_execution_v1",
        "status": "input_frozen",
        "physical_validation": "not_established",
        "base_url": base_url,
        "input_file_sha256": hashlib.sha256(inputs.read_bytes()).hexdigest(),
    }
    checkpoint = output / "execution.json"

    def save():
        checkpoint.write_text(json.dumps(witness, ensure_ascii=False, indent=2) + "\n")

    save()
    try:
        with PlatformClient(base_url, timeout=120) as client:
            record = client.post("calibration-lab/color-calibrations", payload)
            witness.update(
                status="report_created", record_id=record["id"], numerical_status=record["status"]
            )
            save()
            (output / "record.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
            for name, checksum in record["files"].items():
                response = client.http.get(
                    f"/api/v1/calibration-lab/color-calibrations/{record['id']}/files/{name}"
                )
                response.raise_for_status()
                if hashlib.sha256(response.content).hexdigest() != checksum:
                    raise ValueError("Export hash mismatch: " + name)
                (output / name).write_bytes(response.content)
            package = client.http.get(record["bundle_url"])
            package.raise_for_status()
            path = output / "calibration-evidence.zip"
            path.write_bytes(package.content)
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None:
                    raise ValueError("Invalid evidence ZIP")
                for name, checksum in record["files"].items():
                    if hashlib.sha256(archive.read(name)).hexdigest() != checksum:
                        raise ValueError("Packaged hash mismatch: " + name)
            if client.get("calibration-lab/color-calibrations/" + record["id"]) != record:
                raise ValueError("Saved task readback differs from executed record")
        witness.update(
            status="completed",
            files=record["files"],
            report_url=base_url + record["report_url"],
            lab_url=base_url + "/calibration-color.html?calibration=" + record["id"],
            package_sha256=hashlib.sha256(package.content).hexdigest(),
            raw_metrics=record["raw_validation"]["candidate"]["metrics"],
            ccm_metrics=record["ccm"]["validation"]["comparison"]["metrics"],
            raw_unchanged_by_ccm=record["raw_unchanged_by_ccm"],
        )
        save()
        print(
            json.dumps(
                {k: witness[k] for k in ("status", "record_id", "numerical_status", "report_url")},
                ensure_ascii=False,
            )
        )
        return witness
    except Exception as exc:
        witness.update(status="failed", error=str(exc))
        save()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    run(args.base_url.rstrip("/"), args.output, args.seed)
