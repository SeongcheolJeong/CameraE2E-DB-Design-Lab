#!/usr/bin/env python3
"""Prepare single-frame previews and a finite same-RAW ISP tuning demonstration."""

from __future__ import annotations

import argparse
import copy
import json
import time
from pathlib import Path

from camera_platform import reference_isp
from camera_platform.reference_workshop_job import fingerprint
from camera_platform.sdk import PlatformClient

SOURCE = "run-34aadb199e4d416bb0ba390ccf79636c"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument("--source", default=SOURCE)
    parser.add_argument("--output", default="outputs/reference-isp-tuning-20261012")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    receipt_path = root / "runs.json"
    records = json.loads(receipt_path.read_text()) if receipt_path.exists() else []
    code = fingerprint()

    def save():
        receipt_path.write_text(json.dumps(records, indent=2, ensure_ascii=False))

    with PlatformClient(args.base_url, timeout=180) as client:
        source = client.reference_workshop_run(args.source)
        profile = reference_isp.default_profile(source["result"]["summary"]["configuration"]["isp"])
        profile["lsc_enabled"] = True
        profile["lsc_tuning"] = {"strength": 1.0, "gain_cap": 4.0}

        def finish(label, submit):
            item = next((r for r in reversed(records) if r["label"] == label), None)
            job = client.reference_workshop_run(item["id"]) if item else None
            if (job is None or job["status"] not in {"queued", "running", "succeeded"}
                    or job["source_code"] != code):
                start = time.monotonic()
                job = submit()
                item = {"label": label, "id": job["id"], "status": job["status"],
                        "submission_seconds": time.monotonic() - start}
                records.append(item)
                save()
            print(label, job["id"], job["status"], flush=True)
            start = time.monotonic()
            last = None
            while job["status"] in {"queued", "running"}:
                time.sleep(3)
                job = client.reference_workshop_run(job["id"])
                stage = job.get("progress", {}).get("stage")
                if stage != last:
                    print(label, stage, flush=True)
                    last = stage
                if time.monotonic() - start > 7260:
                    raise TimeoutError(job["id"])
            item["status"] = job["status"]
            item.setdefault("observed_wait_seconds", time.monotonic() - start)
            (root / (label + ".json")).write_text(json.dumps(job, indent=2, ensure_ascii=False))
            if job["status"] != "succeeded":
                item["error"] = job.get("error")
                save()
                raise RuntimeError(item)
            summary = job["result"]["summary"]
            item.update(manifest_sha256=summary["manifest_sha256"],
                        performance=summary.get("performance"),
                        preview_scope=summary.get("preview_scope"),
                        selection=summary.get("study", {}).get("selection"))
            if any(c["status"] != "succeeded" for c in summary.get("candidates", [])):
                save()
                raise RuntimeError("Incomplete candidate set: " + job["id"])
            save()
            return job

        finish("preview-baseline", lambda: client.preview_reference_isp(
            args.source, profile, field_id="outer-ne", chart="texture"))
        altered = copy.deepcopy(profile)
        altered["raw_denoise"]["strength"] = 0.2
        altered["linear_sharpen"].update(amount=0.3, threshold=0.01, max_delta=0.05)
        finish("preview-tuned", lambda: client.preview_reference_isp(
            args.source, altered, field_id="outer-ne", chart="texture"))
        study = finish("parameter-tuning", lambda: client.submit_reference_isp_study(
            args.source, "parameter_tuning", isp=profile,
            name="ISP tuning · native LSC × RAW NR × bounded detail"))
        summary = study["result"]["summary"]
        selected = summary["study"]["selection"]["selected_candidate_id"]
        if selected:
            chosen = next(c for c in summary["candidates"] if c["id"] == selected)
            revision = client.create_reference_revision(
                chosen["configuration"], name="ISP tuning · saved evaluated profile")
            (root / "saved-profile.json").write_text(json.dumps(revision, indent=2))
            finish("saved-profile-replay", lambda: client.replay_reference_workshop(
                args.source, revision["specification"]["isp"],
                name="ISP tuning · saved profile same-RAW replay"))
        delivery = {
            "source_run_id": args.source,
            "study_run_id": study["id"],
            "selected_candidate_id": selected,
            "url": args.base_url + "/reference-workshop.html?run=" + study["id"]
                + "&candidate=" + (selected or "candidate-00")
                + "&baseline=" + study["id"] + "&baseline_candidate=candidate-00"
                + "&field=outer-ne&chart=texture&stage=isp&editor=isp",
            "selection": summary["study"]["selection"],
            "records": [next(r for r in reversed(records) if r["label"] == label)
                        for label in ["preview-baseline", "preview-tuned", "parameter-tuning"]
                        + (["saved-profile-replay"] if selected else [])],
            "history_receipts": "runs.json",
            "scope": "same preserved model RAW; not measured ISP calibration",
        }
        (root / "delivery.json").write_text(json.dumps(delivery, indent=2, ensure_ascii=False))
        print(delivery["url"], flush=True)


if __name__ == "__main__":
    main()
