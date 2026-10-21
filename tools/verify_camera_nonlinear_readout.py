#!/usr/bin/env python3
"""Verify v2 electrical/HDR families through an owned, isolated HTTP API and Worker.

No server is started or stopped here. POST intentions are recorded before sending;
an uncertain response is never retried automatically. --verify-only repeats exact
readback and retained-RAW calculations without new records or server mutations.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from PIL import Image
from verify_camera_dcg import process_witness

from camera_platform import nonlinear_dcg, nonlinear_hdr, nonlinear_readout, reference_hdr
from camera_platform.contracts import AssemblyCreate, SceneSequenceCreate
from camera_platform.registry import canonical, digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint

FAMILIES = {
    "hdr": ("independent_bracket_nonlinear", "hdr_bracket_v1"),
    "reference": ("reference_guarded_nonlinear", "reference_hdr_v1"),
    "dcg": ("shared_charge_nonlinear", "shared_charge_dcg_v1"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-state-dir", required=True, type=Path)
    parser.add_argument("--expected-api-pid", required=True, type=int)
    parser.add_argument("--frozen-environment", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if (
        address.scheme != "http"
        or address.hostname != "127.0.0.1"
        or address.port in {None, 8770}
        or address.path not in {"", "/"}
        or address.query
        or address.fragment
        or address.username
        or address.password
    ):
        parser.error("Use a separate explicit loopback QA URL, never primary port 8770")
    state, out = args.expected_state_dir.resolve(), args.output_dir.resolve()
    if "qa" not in state.name.lower() or out == state or state in out.parents:
        parser.error("Require an explicitly named QA state and separate report directory")
    frozen = json.loads(args.frozen_environment.read_text())["environment"]
    if environment_fingerprint() != frozen:
        raise ValueError("Current executable differs from the frozen environment")
    out.mkdir(parents=True, exist_ok=True)
    journal = out / "http-verification.json"
    if args.verify_only:
        report = json.loads(journal.read_text())
        if report["status"] != "passed":
            raise ValueError("Readback requires a previously successful complete verification")
        if report["base_url"] != args.base_url or report["state_dir"] != str(state):
            raise ValueError("Readback target differs from the preserved evidence")
        if report["environment"] != frozen:
            raise ValueError("Readback environment differs from original verification")
    else:
        if journal.exists():
            raise ValueError("Existing journal: use --verify-only or a new state/output directory")
        report = {
            "schema": "camera_nonlinear_readout_http_verification_v1",
            "status": "in_progress",
            "started_at": datetime.now(UTC).isoformat(),
            "base_url": args.base_url,
            "state_dir": str(state),
            "environment": frozen,
            "environment_sha256": digest(frozen),
            "tool": {
                "path": str(Path(__file__).resolve()),
                "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            },
            "python": sys.executable,
            "operations": {},
            "records": {},
            "checks": [],
            "artifacts": {},
            "families": {},
            "source_kind": "simulated",
            "physical_validation": "not_established",
            "total_uncertainty": "unknown",
            "objective_status": "incomplete",
            "goal_status": "paused",
            "scope": "Real API/Worker acquisition and exact retained RAW/calibration replay; "
            "not independent physical accuracy or installation qualification",
        }

    def save():
        report["updated_at"] = datetime.now(UTC).isoformat()
        temporary = journal.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        )
        temporary.replace(journal)

    def check(name, value):
        if not value:
            raise AssertionError(name)
        if name not in report["checks"]:
            report["checks"].append(name)
        save()

    def preserve(label, value):
        content = canonical(value)
        path = out / (label + ".json")
        if args.verify_only:
            check(label + " exact persisted JSON", path.read_text() == content)
        else:
            path.write_text(content)
        return value

    save()
    started = time.monotonic()
    try:
        with PlatformClient(args.base_url, timeout=120) as client:
            health = client.get("health")
            check(
                "exact isolated state and supervised Worker",
                Path(health["state_dir"]).resolve() == state and health["worker"] == "running",
            )
            report["process_witness"] = process_witness(address.port, args.expected_api_pid, state)
            save()

            def send(label, endpoint, payload):
                if args.verify_only or label in report["operations"]:
                    raise ValueError("A mutation cannot be repeated: " + label)
                operation = {
                    "endpoint": endpoint,
                    "payload": payload,
                    "payload_sha256": digest(payload),
                    "status": "intent_saved",
                }
                report["operations"][label] = operation
                save()
                try:
                    value = client.post(endpoint, payload)
                except Exception:
                    operation["status"] = "uncertain_post_requires_reconciliation"
                    save()
                    raise
                operation.update(status="response_received", id=value["id"])
                report["records"][label] = value["id"]
                save()
                return value

            def segment_probe(run):
                summary = run["result"]["summary"]

                def array(name):
                    artifact = next(a for a in run["artifacts"] if a["name"] == name + ".npy")
                    response = client.http.get(artifact["url"])
                    response.raise_for_status()
                    check(
                        "contrast probe SHA " + artifact["sha256"],
                        hashlib.sha256(response.content).hexdigest() == artifact["sha256"],
                    )
                    return np.load(io.BytesIO(response.content), allow_pickle=False)

                raw, dark = array("raw_frames"), array("calibration_dark_e")
                decoded = []
                for index in range(2):
                    cal = {
                        k: v
                        for k, v in summary["calibrations"][index].items()
                        if k not in {"frame_index", "dark_e_array"}
                    }
                    cal["dark_e"] = dark[index]
                    decoded.append(nonlinear_readout.decode_raw(raw[index], cal)["arrays"])
                model = summary["calibrations"][0]["model"]
                knot = model["charge_voltage_response"]["charge_e"][1]
                joint = decoded[0]["valid_mask"] & decoded[1]["valid_mask"]
                spanning = (
                    joint
                    & (decoded[0]["total_charge_e"] < knot)
                    & (decoded[1]["total_charge_e"] > knot)
                )
                first = decoded[0]["valid_mask"]
                charge = decoded[0]["total_charge_e"] - dark[0]
                if not first.any() or float(np.median(charge[first])) <= 0:
                    raise ValueError(
                        "Initial authored stimulus has no usable positive RAW calibration"
                    )
                return {
                    "joint_valid_pixels": int(joint.sum()),
                    "spanning_pixels": int(spanning.sum()),
                    "curve_internal_knot_e": knot,
                    "capacity_e": model["full_well_e"],
                    "first_photocharge_median_e": float(np.median(charge[first])),
                    "basis": "actual integer RAW plus bound calibration only; no simulation truth",
                }

            if not args.verify_only:
                check(
                    "fresh QA acquisition history",
                    client.get("runs") == [] and client.get("capture_programs") == [],
                )
                boot = client.get("workbench")
                original = client.get("assemblies/" + boot["assemblies"][0]["id"])
                models = [client.get("models/" + i) for i in original["model_ids"]]
                base_lens = next(m for m in models if m["kind"] == "lens")
                factory = send(
                    "factory", "factory/jobs", client.template("factory", plugin_id="prysm_psf_v1")
                )
                factory = client.wait_characterization(factory["id"], timeout=300)
                check("real PSF Factory Worker succeeded", factory["status"] == "succeeded")
                preserve("factory", factory)
                lens = send(
                    "lens",
                    "factory/models",
                    {
                        "job_id": factory["id"],
                        "base_lens_model_id": base_lens["id"],
                        "name": "비선형 전기 응답 검토 · 고정 PSF",
                        "revision": "1",
                        "conditions": original["conditions"],
                        "condition_scope": "optical_response",
                    },
                )
                draft = {k: deepcopy(original[k]) for k in AssemblyCreate.model_fields}
                draft.update(name="비선형 전기·HDR·공유 전하 설계 검토", revision="nonlinear-qa-1")
                draft["model_ids"] = [
                    lens["id"] if i == base_lens["id"] else i for i in draft["model_ids"]
                ]
                assembly = send("assembly", "assemblies", draft)
                scene = send(
                    "fixture",
                    "scene_sequences/fixture",
                    client.template("temporal-fixture", assembly_id=assembly["id"])
                    | {
                        "name": "고정 합성 영상면 · 비선형 전기 응답",
                        "shape": [16, 16],
                        "pattern": "static",
                        "knot_count": 2,
                        "irradiance_scale": 0.0003,
                    },
                )
                draft = {k: deepcopy(scene[k]) for k in SceneSequenceCreate.model_fields}
                draft.update(name="동일 영상 고정 · 순차/공유 전하 비교", revision="fixed-static-1")
                for knot in draft["payload"]["knots"]:
                    knot["spectral_image_id"] = scene["payload"]["knots"][0]["spectral_image_id"]
                scene = send("scene", "scene_sequences", draft)
                controls = client.template(
                    "control-sequence", assembly_id=assembly["id"], scene_sequence_id=scene["id"]
                )
                controls["payload"]["mode"].update(
                    shutter="global", line_time_ns=0, hidden_top_rows=0
                )
                for index, frame in enumerate(controls["payload"]["frames"]):
                    frame["applied"].update(
                        exposure_ns=(4 if index % 2 == 0 else 8) * 1_000_000, gain=1
                    )
                control = send("control", "control_sequences", controls)
                for label, (mode, plugin) in FAMILIES.items():
                    program = send(
                        label + "-program",
                        "capture_programs",
                        client.template("capture-program", assembly_id=assembly["id"], mode=mode),
                    )
                    spec = send(
                        label + "-test",
                        "tests",
                        client.template(
                            "temporal-test",
                            assembly_id=assembly["id"],
                            scene_sequence_id=scene["id"],
                            control_sequence_id=control["id"],
                            capture_program_id=program["id"],
                        ),
                    )
                    request = {
                        "assembly_id": assembly["id"],
                        "test_id": spec["id"],
                        "plugin_id": plugin,
                        "scene_sequence_id": scene["id"],
                        "control_sequence_id": control["id"],
                        "capture_program_id": program["id"],
                        "parameters": {"seed": 160017},
                    }
                    queued = send(label + "-run", "runs", request)
                    run = client.wait(queued["id"], timeout=300)
                    check(label + " real Worker succeeded", run["status"] == "succeeded")
                    report["families"][label] = {
                        "run_id": run["id"],
                        "program_id": program["id"],
                        "test_id": spec["id"],
                        "mode": mode,
                    }
                    if label == "hdr":
                        report["contrast_probe"] = segment_probe(run)
                    save()
                if report["contrast_probe"]["spanning_pixels"] == 0:
                    probe = report["contrast_probe"]
                    scale = (
                        0.0003 * (0.35 * probe["capacity_e"]) / probe["first_photocharge_median_e"]
                    )
                    if not 1e-9 <= scale <= 10:
                        raise ValueError(
                            "RAW-derived contrast stimulus exceeds bounded fixture scale"
                        )
                    contrast_scene = send(
                        "contrast-fixture",
                        "scene_sequences/fixture",
                        client.template("temporal-fixture", assembly_id=assembly["id"])
                        | {
                            "name": "RAW 역보정으로 설정한 두 응답 구간 비교",
                            "shape": [16, 16],
                            "pattern": "static",
                            "knot_count": 2,
                            "irradiance_scale": scale,
                        },
                    )
                    contrast_controls = client.template(
                        "control-sequence",
                        assembly_id=assembly["id"],
                        scene_sequence_id=contrast_scene["id"],
                    )
                    contrast_controls["payload"]["mode"].update(
                        shutter="global", line_time_ns=0, hidden_top_rows=0
                    )
                    for index, frame in enumerate(contrast_controls["payload"]["frames"]):
                        frame["applied"].update(
                            exposure_ns=(4 if index % 2 == 0 else 8) * 1_000_000, gain=1
                        )
                    contrast_control = send(
                        "contrast-control", "control_sequences", contrast_controls
                    )
                    program_id = report["families"]["hdr"]["program_id"]
                    spec = send(
                        "hdr_segments-test",
                        "tests",
                        client.template(
                            "temporal-test",
                            assembly_id=assembly["id"],
                            scene_sequence_id=contrast_scene["id"],
                            control_sequence_id=contrast_control["id"],
                            capture_program_id=program_id,
                        ),
                    )
                    queued = send(
                        "hdr_segments-run",
                        "runs",
                        {
                            "assembly_id": assembly["id"],
                            "test_id": spec["id"],
                            "plugin_id": "hdr_bracket_v1",
                            "scene_sequence_id": contrast_scene["id"],
                            "control_sequence_id": contrast_control["id"],
                            "capture_program_id": program_id,
                            "parameters": {"seed": 160017},
                        },
                    )
                    run = client.wait(queued["id"], timeout=300)
                    check("bounded contrast Worker succeeded", run["status"] == "succeeded")
                    report["contrast_probe"] = segment_probe(run)
                    report["contrast_probe"]["authored_irradiance_scale"] = scale
                    report["families"]["hdr_segments"] = {
                        "run_id": run["id"],
                        "program_id": program_id,
                        "test_id": spec["id"],
                        "mode": "independent_bracket_nonlinear",
                    }
                check(
                    "usable RAW spans both response segments",
                    report["contrast_probe"]["spanning_pixels"] > 0,
                )
                case = send(
                    "case",
                    "case_reviews",
                    {
                        "request_id": "nonlinear-readout-live-qa-v1",
                        "name": "비선형 전기 응답 설계 검토",
                        "question": ("서로 다른 전하 응답에서도 "
                                     "원본 RAW와 역보정 근거를 보존하는가?"),
                        "assembly_ids": [assembly["id"]],
                        "requirements": [
                            {
                                "key": label,
                                "description": label + " RAW·보정·합성 근거",
                                "test_id": row["test_id"],
                            }
                            for label, row in report["families"].items()
                        ],
                        "evidence": [
                            {"kind": "runs", "id": row["run_id"], "requirement_keys": [label]}
                            for label, row in report["families"].items()
                        ],
                        "decision": {
                            "status": "investigate",
                            "rationale": ("비선형 계산 경로와 원본 근거를 확인한다. "
                                          "실측 센서 적합성은 미확립이다."),
                        },
                        "unresolved": [
                            "실측 곡선·전압 잡음·독립 검증 구간",
                            "상용 설치·배포 및 물리 정확도",
                        ],
                    },
                )
                preserve("case", case)

            blob_cache = {}
            for label, row in report["families"].items():
                program = preserve(
                    label + "-program", client.get("capture_programs/" + row["program_id"])
                )
                run = preserve(label + "-run", client.get("runs/" + row["run_id"]))
                spec = preserve(label + "-test", client.get("tests/" + row["test_id"]))
                summary = run["result"]["summary"]
                check(
                    label + " exact registered program", run["capture_program_snapshot"] == program
                )
                check(
                    label + " fixed Test sample identity",
                    summary["prediction"]["sample_geometry"] == spec["stimulus"]["sample_contract"],
                )
                values = {}
                for artifact in run["artifacts"]:
                    sha = artifact["sha256"]
                    if sha not in blob_cache:
                        response = client.http.get(artifact["url"])
                        response.raise_for_status()
                        blob_cache[sha] = response.content
                    content = blob_cache[sha]
                    check(
                        "artifact SHA " + sha,
                        hashlib.sha256(content).hexdigest() == sha
                        and len(content) == artifact["bytes"],
                    )
                    report["artifacts"][sha] = {"bytes": len(content), "sha256_verified": True}
                    if artifact["name"].endswith(".npy"):
                        values[artifact["name"][:-4]] = np.load(
                            io.BytesIO(content), allow_pickle=False
                        )
                    if artifact["name"] == "sensor-rgb.png":
                        with Image.open(io.BytesIO(content)) as preview:
                            check(label + " preview decoded 16x16", preview.size == (16, 16))
                        if not args.verify_only:
                            (out / (label + "-preview.png")).write_bytes(content)
                paired = label == "dcg"
                raw = values["raw_groups" if paired else "raw_frames"]
                check(
                    label + " original integer RAW shape",
                    raw.dtype.kind in "iu"
                    and raw.shape[-2:] == (16, 16)
                    and raw.ndim == (4 if paired else 3),
                )
                calibrations = [
                    {
                        **{
                            k: v
                            for k, v in cal.items()
                            if k not in {"dark_e_array", "frame_index", "acquisition_index"}
                        },
                        "dark_e": values["calibration_dark_e"][index],
                    }
                    for index, cal in enumerate(summary["calibrations"])
                ]
                if paired:
                    replayed = [
                        nonlinear_dcg.merge_shared_charge_raw(
                            frame, cal, program["payload"]["merge_profile"]
                        )
                        for frame, cal in zip(raw, calibrations, strict=True)
                    ]
                    for key in replayed[0]["arrays"]:
                        expected = np.stack([item["arrays"][key] for item in replayed])
                        check(
                            label + " retained RAW replay " + key,
                            np.array_equal(expected, values[key], equal_nan=True),
                        )
                    valid = values["valid_mask"]
                else:
                    merge = (
                        reference_hdr.merge_reference_raw_bracket
                        if label == "reference"
                        else nonlinear_hdr.merge_raw_bracket
                    )
                    replayed = merge(
                        raw,
                        [c["acquisition"]["exposure_s"] for c in calibrations],
                        calibrations,
                        geometry=summary["geometry"],
                        profile=program["payload"]["hdr_merge"],
                    )
                    for key, expected in replayed["arrays"].items():
                        check(
                            label + " retained RAW replay " + key,
                            np.array_equal(expected, values["hdr_" + key], equal_nan=True),
                        )
                    valid = values["hdr_valid_mask"]
                check(label + " some valid calibrated output", bool(valid.any()))
                row.update(
                    raw_shape=list(raw.shape),
                    raw_dtype=str(raw.dtype),
                    valid_pixels=int(valid.sum()),
                    valid_fraction=float(valid.mean()),
                    source_kind=summary["source_kind"],
                    physical_validation=summary["physical_validation"],
                )
                if not args.verify_only:
                    bundle = client.http.get("/api/v1/runs/" + run["id"] + "/bundle")
                    bundle.raise_for_status()
                    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
                        manifest_bytes = archive.read("manifest.json")
                        check(label + " bundle exact run", json.loads(manifest_bytes)["run"] == run)
                        for artifact in run["artifacts"]:
                            content = archive.read(
                                f"artifacts/{artifact['sha256']}/{artifact['name']}"
                            )
                            check(
                                label + " bundle SHA " + artifact["name"],
                                hashlib.sha256(content).hexdigest() == artifact["sha256"],
                            )
                    (out / (label + "-bundle.zip")).write_bytes(bundle.content)
                    row["bundle_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
                save()
            case = preserve("case", client.get("case_reviews/" + report["records"]["case"]))
            evidence_ids = {e["id"] for e in case["evidence"]}
            check(
                "Case preserves all three acquisition families",
                evidence_ids == {r["run_id"] for r in report["families"].values()},
            )
            check("frozen environment unchanged", environment_fingerprint() == frozen)
            report["status"] = "passed"
            report["readback_verified"] = True
            report["verify_only_last_run"] = args.verify_only
            report["last_elapsed_s"] = time.monotonic() - started
            report["left_running_for_browser_qa"] = True
            save()
    except Exception as exc:
        report.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        save()
        raise
    print(
        json.dumps(
            {
                "status": report["status"],
                "families": report["families"],
                "case_id": report["records"]["case"],
                "checks": len(report["checks"]),
                "report": str(journal),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
