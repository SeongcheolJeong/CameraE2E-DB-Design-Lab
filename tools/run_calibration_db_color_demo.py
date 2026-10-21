#!/usr/bin/env python3
"""Apply one spectral calibration to an explicit DB target and export a fresh replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import zipfile
from pathlib import Path

from camera_platform.sdk import PlatformClient


def run(base_url, output, source_run, candidate, condition, calibration):
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    source = {"run_id": source_run, "candidate_id": candidate}
    if condition:
        source["condition_id"] = condition
    payload = {
        "name": "맥베스 · DB QE·CCM 활성 모델",
        "source": source,
        "color_calibration_id": calibration,
        "binding": {
            "mode": "synthetic_model_transfer",
            "angular_assumption": "angle_independent",
            "green_sites": "shared_green",
            "target_camera_id": "synthetic-db-" + calibration[-12:],
        },
    }
    input_file = out / "input.json"
    if input_file.exists() and json.loads(input_file.read_text()) != payload:
        raise ValueError("Output already contains a different frozen input")
    input_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    checkpoint = out / "execution.json"
    state = (
        json.loads(checkpoint.read_text()) if checkpoint.exists() else {"status": "input_frozen"}
    )

    def save(phase=None, **data):
        if phase and phase != "failed" and state.get("error"):
            state.setdefault("recovered_errors", []).append(state.pop("error"))
        state.update(data)
        if phase:
            state["status"] = phase
        checkpoint.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    with PlatformClient(base_url, timeout=180) as client:
        try:
            model = client.post("db-design/color-models", payload)
            if state.get("model_id") not in (None, model["id"]):
                raise ValueError(
                    "Source code changed; preserve this run and choose a new output directory"
                )
            save("model_created", model_id=model["id"], profile_sha256=model["profile_sha256"])
            (out / "model.json").write_text(json.dumps(model, ensure_ascii=False, indent=2))
            active = next(
                (
                    a
                    for a in client.get("db-design/active-color-models")["items"]
                    if a["target_key"] == model["target_key"]
                ),
                None,
            )
            if not active or active["model_id"] != model["id"]:
                active = client.post(
                    "db-design/color-models/" + model["id"] + "/activate",
                    {"expected_active_model_id": active["model_id"] if active else None},
                )
            save("activated", activation_id=active["id"])
            if state.get("run_id"):
                job = client.get("db-design/color-replays/" + state["run_id"])
            else:
                job = client.post("db-design/color-replays", {"model_id": model["id"]})
                save("submitted", run_id=job["id"])
            deadline, last = time.monotonic() + 1800, None
            while job["status"] in {"queued", "running"}:
                message = (job["status"], job.get("progress"))
                if message != last:
                    print(
                        json.dumps(
                            {
                                "run_id": job["id"],
                                "status": job["status"],
                                "progress": job.get("progress"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    last = message
                if time.monotonic() > deadline:
                    raise TimeoutError("DB replay still running; checkpoint retains its run ID")
                time.sleep(3)
                job = client.get("db-design/color-replays/" + job["id"])
            if job["status"] != "succeeded":
                raise RuntimeError("DB replay did not succeed: " + str(job.get("error")))
            summary = job["result"]["summary"]
            if summary["model_id"] != model["id"]:
                raise ValueError("Replay selected another model")
            save("exporting", numerical_status=summary.get("status"))
            (out / "record.json").write_text(json.dumps(job, ensure_ascii=False, indent=2))
            (out / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
            package = client.http.get("/api/v1/db-design/color-replays/" + job["id"] + "/bundle")
            package.raise_for_status()
            package_path = out / "db-color-evidence.zip"
            package_path.write_bytes(package.content)
            with zipfile.ZipFile(package_path) as archive:
                if archive.testzip() is not None:
                    raise ValueError("Invalid evidence ZIP")
                for name, checksum in summary["files"].items():
                    data = archive.read(name)
                    if hashlib.sha256(data).hexdigest() != checksum:
                        raise ValueError("Packaged artifact differs: " + name)
                    path = (out / name).resolve()
                    if not path.is_relative_to(out):
                        raise ValueError("Packaged artifact has an unsafe path")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            if client.get("db-design/color-replays/" + job["id"]) != job:
                raise ValueError("Completed run readback changed")
            final_active = next(
                a
                for a in client.get("db-design/active-color-models")["items"]
                if a["target_key"] == model["target_key"]
            )
            if final_active["model_id"] != model["id"]:
                raise ValueError("Active model changed during the demonstration")
            save(
                "completed",
                report_url=base_url + summary["report_url"],
                lab_url=base_url + "/calibration-db-color.html?run=" + job["id"],
                files_verified=len(summary["files"]),
                package_sha256=hashlib.sha256(package.content).hexdigest(),
                physical_validation="not_established",
                independent_db_validation="not_established",
            )
            print(json.dumps(state, ensure_ascii=False), flush=True)
        except Exception as exc:
            save("failed", error=str(exc))
            raise


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://127.0.0.1:8780")
    p.add_argument("--output", required=True)
    p.add_argument("--source-run", default="run-08c20c6eb15d486f889382f516ad4966")
    p.add_argument("--candidate", default="candidate-03")
    p.add_argument("--condition", default="condition-006")
    p.add_argument("--calibration", default="color-calibration-799844fefdc479ec60391875c73046db")
    a = p.parse_args()
    run(a.base_url.rstrip("/"), a.output, a.source_run, a.candidate, a.condition, a.calibration)
