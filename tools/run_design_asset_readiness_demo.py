#!/usr/bin/env python3
"""Prepare two explicit readout assumptions through the public readiness SDK.

This is an actual recapture from preserved photoelectron rates. It neither edits
the source catalog nor promotes assumptions into measured sensor specifications.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from camera_platform.sdk import PlatformClient
from camera_platform.virtual_lab_job import sha256


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def completed(client, job):
    result = client.wait(job["id"], timeout=1900, poll_interval=1)
    if result["status"] != "succeeded":
        raise RuntimeError(f"{job['id']}: {result['status']}: {result.get('error')}")
    return result


def verify_package(root, checksum):
    if sha256(root / "manifest.json") != checksum:
        raise ValueError("Package manifest changed")
    manifest = json.loads((root / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or sha256(path) != expected:
            raise ValueError("Artifact identity mismatch: " + name)
    return {
        "files": len(manifest["files"]),
        "logical_bytes": sum((root / p).stat().st_size for p in manifest["files"]),
        "allocated_bytes": sum(
            max((root / p).stat().st_size, (root / p).stat().st_blocks * 512)
            for p in manifest["files"]
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--asset", default="sensor-e71903206aefdc62550da6f5")
    parser.add_argument("--source", default="run-d2105e38b3cd4f83b3550ebff21ca7fd")
    parser.add_argument(
        "--output", type=Path, default=Path("outputs/design-readiness-delivery-20261012")
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    workspace = Path(__file__).resolve().parents[1]
    before_path = args.output / "source-before.json"
    if not before_path.exists():
        save(
            before_path,
            {
                name: sha256(workspace / name)
                for name in (
                    "camerae2e_db/catalog.sqlite",
                    "camerae2e_db/fdtd_tcad/image_sensor_db/sensor_catalog.json",
                )
            },
        )
    began = time.monotonic()
    receipt = {"schema": "camera_design_asset_demo_delivery_v1", "trials": []}
    with PlatformClient(args.base_url, timeout=180) as client:
        receipt["refresh"] = completed(client, client.submit_design_asset_refresh())["id"]
        detail = client.design_readiness(args.asset)
        save(args.output / "source-detail.json", detail)
        source = client.reference_workshop_run(args.source)
        source_root = Path(source["storage_root"]) / "reference-workshop" / args.source
        receipt["source_before"] = verify_package(
            source_root, source["result"]["summary"]["manifest_sha256"]
        )
        source_cell = json.loads((source_root / "pixel-cell.json").read_text())
        baseline = source_cell["electrical_model"]["read_noise_e"]
        for factor in (0.5, 2.0):
            started = time.monotonic()
            name = f"Readiness demo · user-assumed read noise ×{factor:g}"
            proposal = client.propose_design_asset_update(
                {
                    "asset_id": args.asset,
                    "source_run_id": args.source,
                    "name": name,
                    "changes": [
                        {
                            "field": "read_noise_e",
                            "value": baseline * factor,
                            "origin_kind": "user_assumption",
                            "reason": (
                                "Demonstrate adopted readout sensitivity from preserved pre-noise "
                                "response; not a measured product improvement."
                            ),
                        }
                    ],
                }
            )
            if not proposal["execution_supported"]:
                raise ValueError(proposal["unsupported_reasons"])
            model = client.adopt_design_asset_proposal(proposal["id"])
            if client.adopt_design_asset_proposal(proposal["id"])["id"] != model["id"]:
                raise ValueError("Repeated adoption must reuse the immutable revision")
            check = completed(client, client.submit_design_readiness_check(model["id"]))
            if not check["result"]["report"]["execution_ready"]:
                raise ValueError(check["result"]["report"]["issues"])
            job = client.submit_design_asset_trial(
                {
                    "model_revision_id": model["id"],
                    "source_run_id": args.source,
                    "name": name,
                }
            )
            print(
                json.dumps({"factor": factor, "model_id": model["id"], "job": job["id"]}),
                flush=True,
            )
            run = completed(client, job)
            summary = run["result"]["summary"]
            root = Path(run["storage_root"]) / "reference-workshop" / run["id"]
            verified = verify_package(root, summary["manifest_sha256"])
            evidence = summary["asset_trial"]
            assert evidence["new_raw_frames"] == 64
            assert evidence["original_raw_unchanged"]
            assert evidence["baseline_seeded_readout_reproduced_exactly"]
            assert evidence["applied_inputs"]["read_noise_e"] == baseline * factor
            assert all(
                c["source_rate_sha256"] == c["trial_rate_sha256"] for c in evidence["comparisons"]
            )
            assert not all(c["raw_identical"] for c in evidence["comparisons"])
            item = {
                "factor": factor,
                "run_id": run["id"],
                "proposal_id": proposal["id"],
                "model_id": model["id"],
                "check_id": check["id"],
                "manifest_sha256": summary["manifest_sha256"],
                "package": verified,
                "seconds_including_validation": time.monotonic() - started,
                "result_url": (
                    f"{args.base_url}/reference-workshop.html?run={run['id']}"
                    f"&baseline={args.source}&field=outer-ne"
                ),
                "report_url": args.base_url + summary["report_url"],
                "evidence": evidence,
            }
            receipt["trials"].append(item)
            save(args.output / "delivery.json", receipt)
            print(json.dumps({"completed": run["id"], "package": verified}), flush=True)
        receipt["source_after"] = verify_package(
            source_root, source["result"]["summary"]["manifest_sha256"]
        )
        receipt["overview"] = client.design_readiness()
        receipt["watch"] = client.design_asset_watch()
        receipt["elapsed_seconds"] = time.monotonic() - began
        receipt["physical_validation"] = "not_established"
        before = json.loads((args.output / "source-before.json").read_text())
        receipt["source_db_unchanged"] = all(sha256(workspace / p) == v for p, v in before.items())
        if not receipt["source_db_unchanged"]:
            raise ValueError("Original source database changed during delivery")
        save(args.output / "delivery.json", receipt)
        save(args.output / "final-detail.json", client.design_readiness(args.asset))


if __name__ == "__main__":
    main()
