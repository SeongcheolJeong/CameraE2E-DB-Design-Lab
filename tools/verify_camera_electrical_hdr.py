#!/usr/bin/env python3
"""Execute an authored HDR/LED investigation through an isolated real API/Worker."""

import argparse
import copy
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

from camera_platform.contracts import AssemblyCreate
from camera_platform.sdk import PlatformClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-state-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if address.hostname != "127.0.0.1" or address.port in {None, 8770}:
        parser.error("Use a separate explicit loopback QA port, never primary8770")
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"schema": "camera_electrical_hdr_http_verification_v1", "status": "in_progress",
              "base_url": args.base_url, "checks": [], "records": {},
              "physical_validation": "not_established", "source_kind": "simulated"}

    def save():
        (args.output / "http-verification.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report["checks"].append(name)

    with PlatformClient(args.base_url, timeout=60) as client:
        health, session = client.get("health"), client.get("session")
        check("isolated state identity", Path(health["state_dir"]).resolve()
              == args.expected_state_dir.resolve())
        check("real supervised Worker running", health["worker"] == "running")
        check("development version", session["version"] == "0.16.0.dev1")
        report.update(state_dir=health["state_dir"], version=session["version"])
        save()
        boot = client.get("workbench")
        original = client.get("assemblies/" + boot["assemblies"][0]["id"])
        base_lens = next(client.get("models/" + i) for i in original["model_ids"]
                         if client.get("models/" + i)["kind"] == "lens")
        factory = client.wait_characterization(client.characterize(
            client.template("factory", plugin_id="prysm_psf_v1"))["id"])
        check("explicit PSF factory Worker", factory["status"] == "succeeded")
        lens = client.post("factory/models", {
            "job_id": factory["id"], "base_lens_model_id": base_lens["id"],
            "name": "HDR QA · 명시적 PSF", "revision": "1",
            "conditions": original["conditions"], "condition_scope": "optical_response",
        })
        draft = {k: copy.deepcopy(v) for k, v in original.items()
                 if k in AssemblyCreate.model_fields}
        draft.update(name="HDR · LED 전기 모델 검토", revision="hdr-qa-1")
        draft["model_ids"] = [lens["id"] if i == base_lens["id"] else i
                              for i in original["model_ids"]]
        assembly = client.post("assemblies", draft)
        image = client.create_spectral_fixture({"assembly_id": assembly["id"],
            "name": "HDR 합성 분광 차트", "revision": "1", "pattern": "spectral_chart",
            "shape": [64, 64], "irradiance_scale": 0.004})
        draft = client.template("temporal-scene", assembly_id=assembly["id"])
        draft["name"] = "정적 HDR 검토 장면"
        for knot in draft["payload"]["knots"]:
            knot["spectral_image_id"] = image["id"]
        scene = client.create_scene_sequence(draft)
        draft = client.template("control-sequence", assembly_id=assembly["id"],
                                scene_sequence_id=scene["id"])
        draft["name"] = "0.5 · 2 · 8 · 16 ms 독립 순차 노출"
        draft["payload"]["mode"].update(shutter="global", line_time_ns=0, hidden_top_rows=0)
        for frame, exposure in zip(draft["payload"]["frames"],
                                    [500_000, 2_000_000, 8_000_000, 16_000_000], strict=True):
            frame["applied"]["exposure_ns"] = exposure
        control = client.create_control_sequence(draft)
        report["records"].update(assembly=assembly["id"], scene=scene["id"],
                                  control=control["id"], factory=factory["id"])
        save()

        def prepare(program_draft, label):
            program_draft["name"] = label
            program = client.create_capture_program(program_draft)
            test = client.post("tests", client.template("temporal-test",
                assembly_id=assembly["id"], scene_sequence_id=scene["id"],
                control_sequence_id=control["id"], capture_program_id=program["id"]))
            report["records"][label] = {"program": program["id"], "test": test["id"]}
            save()
            return program, test

        def run(program, test, label):
            queued = client.submit_hdr(assembly["id"], test["id"], scene["id"],
                                       control["id"], program["id"], seed=927)
            report["records"][label]["run"] = queued["id"]
            save()
            result = client.wait(queued["id"])
            check(label + " Worker completed", result["status"] == "succeeded")
            (args.output / (label + "-run.json")).write_text(
                json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            return result

        def array(result, name):
            artifact = next(a for a in result["artifacts"] if a["name"] == name + ".npy")
            response = client.http.get(artifact["url"])
            response.raise_for_status()
            check(name + " artifact digest", hashlib.sha256(response.content).hexdigest()
                  == artifact["sha256"])
            return np.load(io.BytesIO(response.content), allow_pickle=False)

        draft = client.template("capture-program", assembly_id=assembly["id"])
        program, test = prepare(draft, "hdr")
        hdr = run(program, test, "hdr")
        raw = array(hdr, "raw_frames")
        merged = array(hdr, "hdr_merged_rate")
        valid = array(hdr, "hdr_valid_mask")
        saturation = array(hdr, "electrical_well_saturated_mask")
        check("four distinct actual RAW captures", raw.shape == (4, 64, 64)
              and not np.array_equal(raw[0], raw[-1]))
        check("retained finite valid HDR", valid.any() and np.isfinite(merged[valid]).all())
        check("long-exposure saturated sites recovered from bracket",
              (saturation[-1] & valid).any())
        report["hdr"] = {"valid_pixels": int(valid.sum()),
                         "long_well_saturated_pixels": int(saturation[-1].sum()),
                         "recovered_long_saturated_pixels": int((saturation[-1] & valid).sum()),
                         "summary": hdr["result"]["summary"]["hdr_merge"]}
        bundle = client.http.get("/api/v1/runs/" + hdr["id"] + "/bundle")
        bundle.raise_for_status()
        (args.output / "hdr-evidence.zip").write_bytes(bundle.content)
        report["bundle_sha256"] = hashlib.sha256(bundle.content).hexdigest()
        draft["payload"]["waveform"] = {
            "schema": "camera_light_waveform_v1", "kind": "pwm",
            "clock_id": control["payload"]["clock"]["clock_id"],
            "period_ns": 3_000_000, "on_time_ns": 600_000, "phase_origin_ns": 0,
            "low": 0, "high": 1,
        }
        program, test = prepare(copy.deepcopy(draft), "partial-pwm-hdr")
        response = client.http.post("/api/v1/runs", json={
            "assembly_id": assembly["id"], "test_id": test["id"],
            "scene_sequence_id": scene["id"], "control_sequence_id": control["id"],
            "capture_program_id": program["id"], "plugin_id": "hdr_bracket_v1",
        })
        check("partial-period HDR preflight refused", response.status_code == 422
              and "whole-period" in response.text)
        report["partial_pwm_refusal"] = response.json()
        draft["payload"]["hdr_merge"] = None
        program, test = prepare(draft, "lfm")
        lfm = run(program, test, "lfm")
        ratio = array(lfm, "waveform_row_exposure_ratio")
        check("phase-dependent exposure response retained", np.ptp(ratio[:, 0]) > 0.05)
        check("LFM diagnostics do not claim HDR", not lfm["result"]["summary"]["hdr_merge_applied"])
        report["lfm"] = {"frame_exposure_ratios": ratio[:, 0].tolist()}
        check("no physical measurements invented", not any(
            m["source_kind"] == "measured" for m in client.get("measurements")))
        report.update(status="passed", verified_at=datetime.now(UTC).isoformat(),
                      worker=client.get("health")["worker"])
        save()
        print(json.dumps({"status": report["status"], "checks": len(report["checks"]),
                          "report": str(args.output / "http-verification.json")}))


if __name__ == "__main__":
    main()
