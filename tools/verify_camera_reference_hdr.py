#!/usr/bin/env python3
"""Exercise constant-light reference fusion and motion/PWM RAW through real HTTP/Worker."""

import argparse
import hashlib
import io
import json
import zipfile
from copy import deepcopy
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
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.hostname != "127.0.0.1" or url.port in {None, 8770}:
        parser.error("Use a separate explicit QA loopback port")
    args.output.mkdir(exist_ok=True, parents=True)
    report_path = args.output / "http-verification.json"
    report = (json.loads(report_path.read_text()) if args.verify_only else {
        "schema": "camera_reference_hdr_http_v1", "status": "in_progress",
        "checks": [], "records": {}, "source_kind": "simulated",
        "physical_validation": "not_established", "total_uncertainty": "unknown",
    })

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def check(name, value):
        if not value:
            raise AssertionError(name)
        report["checks"].append(name)

    with PlatformClient(args.base_url, timeout=90) as client:
        health = client.get("health")
        check("isolated state identity", Path(health["state_dir"]).resolve()
              == args.expected_state_dir.resolve())
        check("separate supervised Worker", health["worker"] == "running")
        report.update(base_url=args.base_url, state_dir=health["state_dir"],
                      version=client.get("session")["version"])
        if not args.verify_only:
            if report_path.exists():
                raise RuntimeError("Existing evidence: use --verify-only or a new output/state")
            boot = client.get("workbench")
            original = client.get("assemblies/" + boot["assemblies"][0]["id"])
            models = [client.get("models/" + i) for i in original["model_ids"]]
            base_lens = next(m for m in models if m["kind"] == "lens")
            factory = client.wait_characterization(client.characterize(
                client.template("factory", plugin_id="prysm_psf_v1"))["id"])
            check("PSF Factory succeeded", factory["status"] == "succeeded")
            lens = client.post("factory/models", {
                "job_id": factory["id"], "base_lens_model_id": base_lens["id"],
                "name": "Reference HDR optical response", "revision": "1",
                "conditions": original["conditions"], "condition_scope": "optical_response",
            })
            draft = {k: deepcopy(original[k]) for k in AssemblyCreate.model_fields}
            draft.update(name="움직임 · 기준 프레임 제한 합성 검토", revision="reference-hdr-1")
            draft["model_ids"] = [lens["id"] if i == base_lens["id"] else i
                                  for i in draft["model_ids"]]
            assembly = client.post("assemblies", draft)
            scene = client.create_temporal_fixture(
                client.template("temporal-fixture", assembly_id=assembly["id"])
                | {"name": "이동하는 광점 · 합성 영상면", "shape": [32, 48],
                   "pattern": "moving_spot", "knot_count": 3, "irradiance_scale": 0.04}
            )
            controls = client.template("control-sequence", assembly_id=assembly["id"],
                                       scene_sequence_id=scene["id"])
            controls["payload"]["mode"].update(shutter="global", line_time_ns=0,
                                               hidden_top_rows=0)
            for index, frame in enumerate(controls["payload"]["frames"]):
                frame["applied"]["exposure_ns"] = (4 if index % 2 == 0 else 8) * 1_000_000
            control = client.create_control_sequence(controls)
            report["records"].update(assembly=assembly["id"], scene=scene["id"],
                                      control=control["id"], factory=factory["id"])
            save()

            def run_program(draft, label, plugin):
                draft["name"] = label
                program = client.create_capture_program(draft)
                test = client.post("tests", client.template(
                    "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
                    control_sequence_id=control["id"], capture_program_id=program["id"],
                ))
                queued = client.submit(
                    assembly["id"], test["id"], plugin_id=plugin, scene_sequence_id=scene["id"],
                    control_sequence_id=control["id"], capture_program_id=program["id"],
                    parameters={"seed": 273},
                )
                report["records"][label] = {"program": program["id"], "test": test["id"],
                                               "run": queued["id"]}
                save()
                run = client.wait(queued["id"])
                check(label + " Worker succeeded", run["status"] == "succeeded")
                save()
                return run

            draft = client.template("capture-program", assembly_id=assembly["id"],
                                    mode="reference_guarded")
            reference = run_program(draft, "reference", "reference_hdr_v1")
            draft["payload"]["hdr_merge"]["reference_frame_index"] = 2
            run_program(draft, "reference_2", "reference_hdr_v1")
            # Identical acquisition without fusion must retain identical RAW.
            base = client.template("capture-program", assembly_id=assembly["id"])
            base["payload"]["electrical_model"] = deepcopy(draft["payload"]["electrical_model"])
            base["payload"]["hdr_merge"] = None
            run_program(base, "acquisition_only", "hdr_bracket_v1")
            for phase in (0, 500_000):
                base["payload"]["waveform"] = {
                    "schema": "camera_light_waveform_v1", "kind": "pwm",
                    "clock_id": control["payload"]["clock"]["clock_id"],
                    "period_ns": 1_000_000, "on_time_ns": 500_000,
                    "phase_origin_ns": phase, "low": 0.0, "high": 1.0,
                }
                run_program(base, "pwm_" + str(phase), "hdr_bracket_v1")
            draft["payload"]["waveform"] = deepcopy(base["payload"]["waveform"])
            response = client.http.post("/api/v1/capture_programs", json=draft)
            check("moving PWM fusion refused at registration", response.status_code == 422)
            summary = reference["result"]["summary"]
            report["reference_summary"] = summary["hdr_merge"]
            case = client.post("case_reviews", {
                "request_id": "reference-hdr-qa-case-v1", "name": "움직임과 LED 위상 조사",
                "question": "변화가 감지된 영역을 기준 노출로 제한하고 위상 의존성을 보존하는가?",
                "assembly_ids": [assembly["id"]],
                "requirements": [{"key": "raw", "description": "고정된 원본 RAW와 조건",
                                  "test_id": report["records"]["reference"]["test"]}],
                "evidence": [{"kind": "runs", "id": reference["id"],
                              "requirement_keys": ["raw"]}],
                "decision": {"status": "investigate", "rationale":
                             "합성 계산 경로 확인. 실제 카메라 보정·움직임 검증은 남아 있다."},
                "unresolved": ["정렬·기준 포화 영역 복원·PWM 합성·실물 불확실도"],
            })
            report["records"]["case"] = case["id"]
            save()

        runs = {}
        arrays = {}
        for label, row in report["records"].items():
            if not isinstance(row, dict) or "run" not in row:
                continue
            run = client.get("runs/" + row["run"])
            check(label + " persisted completed run", run["status"] == "succeeded")
            runs[label] = run
            values = {}
            for artifact in run["artifacts"]:
                content = client.http.get(artifact["url"]).content
                check(label + " SHA " + artifact["name"],
                      hashlib.sha256(content).hexdigest() == artifact["sha256"])
                if artifact["name"].endswith(".npy"):
                    values[artifact["name"][:-4]] = np.load(io.BytesIO(content), allow_pickle=False)
            arrays[label] = values
            target = args.output / (label + "-run.json")
            encoded = json.dumps(run, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            if args.verify_only:
                check(label + " restart exact full run", target.read_text() == encoded)
            else:
                target.write_text(encoded)
        a, b = arrays["reference"], arrays["reference_2"]
        check("reference selection preserves original RAW", np.array_equal(a["raw_frames"],
                                                                         b["raw_frames"]))
        check("fusion does not alter RAW acquisition", np.array_equal(a["raw_frames"],
                                arrays["acquisition_only"]["raw_frames"]))
        check("reference choice changes fused radiometry", not np.array_equal(
            a["hdr_merged_rate"], b["hdr_merged_rate"], equal_nan=True))
        p, q = arrays["pwm_0"], arrays["pwm_500000"]
        check("integer-period PWM exposure unchanged", np.array_equal(
            p["waveform_row_effective_s"], q["waveform_row_effective_s"]))
        check("moving PWM phase changes actual RAW", not np.array_equal(p["raw_frames"],
                                                                      q["raw_frames"]))
        check("moving PWM phase changes actual RGB", not np.array_equal(p["rgb_frames"],
                                                                      q["rgb_frames"]))
        report["observations"] = {
            "raw_shape": list(a["raw_frames"].shape),
            "pwm_changed_raw_samples": int(np.sum(p["raw_frames"] != q["raw_frames"])),
            "pwm_changed_rgb_samples": int(np.sum(p["rgb_frames"] != q["rgb_frames"])),
        }
        case = client.get("case_reviews/" + report["records"]["case"])
        check("case preserves run evidence", case["snapshots"]["evidence"][0]["snapshot"]
              == runs["reference"])
        response = client.http.get("/api/v1/runs/" + runs["reference"]["id"] + "/bundle")
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest_bytes = archive.read("manifest.json")
            manifest = json.loads(manifest_bytes)
            check("portable bundle preserves exact run", manifest["run"] == runs["reference"])
            for artifact in runs["reference"]["artifacts"]:
                content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
                check("bundle SHA " + artifact["name"],
                      hashlib.sha256(content).hexdigest() == artifact["sha256"])
        bundle_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if args.verify_only:
            check("restart exact bundle manifest", bundle_hash == report["bundle_manifest_sha256"])
        else:
            report["bundle_manifest_sha256"] = bundle_hash
            (args.output / "reference-bundle.zip").write_bytes(response.content)
        report["status"] = "passed"
        report["restart_verified"] = args.verify_only
        save()
    print(json.dumps({"status": report["status"], "checks": len(report["checks"]),
                      "observations": report["observations"]}))


if __name__ == "__main__":
    main()
