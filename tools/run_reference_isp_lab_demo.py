#!/usr/bin/env python3
"""Prepare the Reference ISP Lab through the public SDK; resume completed jobs."""

from __future__ import annotations

import argparse
import copy
import json
import time
from pathlib import Path

from camera_platform import reference_isp
from camera_platform.reference_workshop_job import fingerprint
from camera_platform.sdk import PlatformClient

BASELINE = "run-34aadb199e4d416bb0ba390ccf79636c"
CFA = "run-c07bf344f52848be9727c122ccde8257"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--output", default="outputs/reference-isp-lab-delivery-20261011/runs.json")
    parser.add_argument(
        "--include-lens-variant",
        action="store_true",
        help="Additionally acquire curvature x1.005 and compare demosaic",
    )
    args = parser.parse_args()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    records = json.loads(target.read_text()) if target.exists() else []
    code = fingerprint()
    with PlatformClient(args.base_url, timeout=120) as client:
        baseline = client.reference_workshop_run(BASELINE)
        source = baseline["result"]["summary"]["configuration"]

        def save():
            target.write_text(json.dumps(records, indent=2, ensure_ascii=False))

        def finish(label, submit):
            last = next((r for r in reversed(records) if r["label"] == label), None)
            job = client.reference_workshop_run(last["id"]) if last else None
            if (
                job is None
                or job["status"] not in {"queued", "running", "succeeded"}
                or job["source_code"] != code
            ):
                job = submit()
                records.append({"label": label, "id": job["id"], "status": job["status"]})
                last = records[-1]
                save()
            print(label, job["id"], job["status"], flush=True)
            before = time.monotonic()
            previous_stage = None
            while job["status"] in {"queued", "running"}:
                time.sleep(5)
                job = client.reference_workshop_run(job["id"])
                stage = job.get("progress", {}).get("stage")
                if stage and stage != previous_stage:
                    print(label, stage, flush=True)
                    previous_stage = stage
                if time.monotonic() - before > 7260:
                    raise TimeoutError("Study still running: " + job["id"])
            last["status"] = job["status"]
            if job["status"] != "succeeded":
                last["error"] = job.get("error")
                save()
                raise RuntimeError(job.get("error"))
            result = job["result"]["summary"]
            failures = [c for c in result.get("candidates", []) if c["status"] != "succeeded"]
            if failures:
                last["candidate_failures"] = failures
                save()
                raise RuntimeError(failures)
            last.update(
                url=args.base_url + "/reference-workshop.html?run=" + job["id"] + "&field=outer-ne",
                successful_candidates=result.get("successful_candidates"),
                selection=result.get("study", {}).get("selection"),
                manifest_sha256=result["manifest_sha256"],
            )
            save()
            return job

        finish("noise-detail", lambda: client.submit_reference_isp_study(BASELINE, "noise_detail"))
        finish(
            "color-cfa-1.0",
            lambda: client.submit_reference_isp_study(BASELINE, "color_recalibration"),
        )
        for label, candidate in (("0.9", "candidate-00"), ("1.1", "candidate-02")):
            finish(
                "color-cfa-" + label,
                lambda candidate=candidate: client.submit_reference_isp_study(
                    CFA, "color_recalibration", source_candidate_id=candidate
                ),
            )
        advanced = reference_isp.default_profile(source["isp"])
        advanced["lsc_enabled"] = True
        spec = {
            **copy.deepcopy(source),
            "name": "Reference ISP Lab · analytic chirp",
            "chart_suite": "isp_lab_v1",
            "isp": advanced,
        }
        capture = finish("chirp-baseline", lambda: client.submit_reference_workshop(spec))
        finish(
            "demosaic-baseline",
            lambda: client.submit_reference_isp_study(capture["id"], "demosaic"),
        )
        if args.include_lens_variant:
            changed = copy.deepcopy(spec)
            changed["name"] += " · curvature ×1.005"
            changed["lens"]["curvature_scale"] = 1.005
            changed_capture = finish(
                "chirp-curvature", lambda: client.submit_reference_workshop(changed)
            )
            finish(
                "demosaic-curvature",
                lambda: client.submit_reference_isp_study(changed_capture["id"], "demosaic"),
            )
    print("Prepared", len(records), "job receipts:", target, flush=True)


if __name__ == "__main__":
    main()
