#!/usr/bin/env python3
"""Exercise timed camera inputs, real Worker, preserved RAW replay and evidence HTTP.

--base-url writes explicitly authored simulation fixtures to the selected local
platform. Omit it to use an isolated temporary API/Worker and registry.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import socket
import tempfile
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from verify_camera_platform import WORKSPACE, start, stop

from camera_platform.contracts import AssemblyCreate
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def verify(base_url, suffix):
    report = {
        "schema": "camera_temporal_http_verification_v1",
        "base_url": base_url,
        "environment": environment_fingerprint(),
        "physical_validation": "not_established",
        "checks": [],
    }

    def passed(name):
        report["checks"].append({"name": name, "status": "passed"})

    with PlatformClient(base_url, timeout=120) as client:
        boot = client.get("bootstrap")
        models = {m["id"]: m for m in boot["models"]}
        original = next(
            a
            for a in boot["assemblies"]
            if all(
                not models[i].get("psf_factory_job_id") and not models[i].get("factory_job_id")
                for i in a["model_ids"]
            )
        )
        base_lens = next(models[i] for i in original["model_ids"] if models[i]["kind"] == "lens")
        request = client.template("factory", plugin_id="prysm_psf_v1")
        request["asset_id"] = base_lens["asset_id"]
        job = client.wait_characterization(client.characterize(request)["id"])
        assert job["status"] == "succeeded" and job["result"]["summary"]["status"] == "PROPOSED"
        lens = client.post(
            "factory/models",
            {
                "job_id": job["id"],
                "base_lens_model_id": base_lens["id"],
                "conditions": original["conditions"],
                "condition_scope": "optical_response",
                "name": "시간 계산용 · 선언 동공 광학 응답",
                "revision": suffix,
            },
        )
        assert lens["valid_domain"]["exposure_s"] == base_lens["valid_domain"]["exposure_s"]
        fields = {k: deepcopy(v) for k, v in original.items() if k in AssemblyCreate.model_fields}
        fields.update(name="시간 카메라 · 합성 이동광점", revision=suffix)
        fields["model_ids"] = [
            lens["id"] if i == base_lens["id"] else i for i in fields["model_ids"]
        ]
        assembly = client.post("assemblies", fields)
        assert client.post("assemblies/check", {"assembly_id": assembly["id"]})["status"] == "PASS"
        passed("actual_psf_factory_and_optical_scope_preserve_parent_readout_bounds")
        fixture = client.template("temporal-fixture", assembly_id=assembly["id"])
        fixture.update(revision=suffix, shape=[64, 64], knot_count=65)
        scene = client.create_temporal_fixture(fixture)
        assert scene["fixture_specification"]["parameters"] == fixture
        control_document = client.template(
            "control-sequence", assembly_id=assembly["id"], scene_sequence_id=scene["id"]
        )
        control_document["revision"] = suffix
        frames = control_document["payload"]["frames"]
        for index, frame in enumerate(frames):
            frame["applied"]["exposure_ns"] = 4_000_000 if index % 2 == 0 else 8_000_000
        frames[0]["commanded"] = {
            "command_id": "exposure-command-1",
            "issued_at_ns": 0,
            "exposure_ns": 8_000_000,
            "gain": 1,
            "isp_model_id": frames[1]["applied"]["isp_model_id"],
        }
        frames[1]["applied"]["command_id"] = "exposure-command-1"
        frames[1]["sensor_readout_end_ns"] = 36_000_000
        original_text = (
            "  \r\n" + json.dumps(control_document, ensure_ascii=False, indent=2) + "\r\n"
        )
        control = client.import_control_sequence(original_text)

        def download(record, name):
            artifact = next(a for a in record["artifacts"] if a["name"] == name)
            response = client.http.get(artifact["url"])
            response.raise_for_status()
            assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
            return response.content

        assert download(control, "control-sequence-source.json") == original_text.encode()
        passed("authored_recipe_image_knots_and_verbatim_control_json_registered")
        specification = client.template(
            "temporal-test",
            assembly_id=assembly["id"],
            scene_sequence_id=scene["id"],
            control_sequence_id=control["id"],
        )
        specification["revision"] = suffix
        test = client.post("tests", specification)
        queued = client.submit_temporal(
            assembly["id"], test["id"], scene["id"], control["id"], seed=741
        )
        run = client.wait(queued["id"], timeout=180)
        assert run["status"] == "succeeded", run.get("error")
        summary = run["result"]["summary"]
        assert summary["frame_count"] == 4
        assert summary["frame_conditions"] == test["frame_conditions"]
        assert summary["clock"] == control["payload"]["clock"]
        assert summary["prediction"]["included_effects"] == test["included_effects"]
        assert summary["timing"]["frames"][1]["command_to_applied_ns"] == 20_000_000
        assert summary["timing"]["frames"][1]["transport_end_ns"] is None
        raw_bytes = download(run, "raw_frames.npy")
        raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False)
        rgb = np.load(io.BytesIO(download(run, "rgb_frames.npy")), allow_pickle=False)
        assert raw.shape == (4, 64, 64) and rgb.shape == (4, 64, 64, 3)
        assert not np.array_equal(raw[0], raw[1])
        for index, frame in enumerate(summary["frames"]):
            assert frame["applied"] == frames[index]["applied"]
            content = client.http.get(frame["preview_url"]).content
            assert hashlib.sha256(content).hexdigest() == frame["preview_artifact_id"]
        passed("http_worker_produces_actual_temporal_raw_rgb_and_explicit_frame_previews")
        cached = client.submit_temporal(
            assembly["id"], test["id"], scene["id"], control["id"], seed=741
        )
        assert cached["id"] == run["id"]
        rerun = client.wait(
            client.submit_temporal(
                assembly["id"], test["id"], scene["id"], control["id"], seed=741, force=True
            )["id"]
        )
        assert rerun["status"] == "succeeded", rerun.get("error")
        assert download(rerun, "raw_frames.npy") == raw_bytes
        passed("same_fixed_inputs_cache_and_forced_worker_replay_preserve_raw_hash")
        invalid = client.http.post(
            "/api/v1/runs",
            json={
                "assembly_id": assembly["id"],
                "test_id": test["id"],
                "plugin_id": "temporal_scene_v1",
                "scene_sequence_id": scene["id"],
                "control_sequence_id": control["id"],
                "parameters": {"exposure_ms": 3},
            },
        )
        assert invalid.status_code == 422 and "Recorded settings" in invalid.text
        passed("scalar_exposure_override_refused_before_queue")
        profile = client.template("isp-profile", assembly_id=assembly["id"])
        profile.update(name="시간 RAW 재처리 · WB 변경", revision=suffix)
        profile["payload"]["white_balance"]["gains_rgb"] = [1.3, 1.0, 0.75]
        isp = client.post("models", profile)
        replay = client.wait_replay(
            client.submit_replay("run", run["id"], isp["id"], name="같은 시간 RAW · ISP 비교")["id"]
        )
        assert replay["status"] == "succeeded", replay.get("error")
        assert download(replay, "raw_frames.npy") == raw_bytes
        changed = np.load(io.BytesIO(download(replay, "rgb_frames.npy")), allow_pickle=False)
        assert not np.array_equal(changed, rgb)
        for key in ["clock", "frame_conditions", "timing"]:
            assert replay["result"]["summary"][key] == summary[key]
        for old, new in zip(summary["frames"], replay["result"]["summary"]["frames"], strict=True):
            assert old["applied"] == new["applied"]
            assert new["replay_isp_model_id"] == isp["id"]
        passed("isp_replay_changes_rgb_but_retains_every_raw_byte_clock_and_applied_setting")
        response = client.http.get(f"/api/v1/replays/{replay['id']}/bundle")
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["source"]["id"] == run["id"]
            for record in [scene, control, *scene["image_snapshots"]]:
                for artifact in record["artifacts"]:
                    contents = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
                    assert hashlib.sha256(contents).hexdigest() == artifact["sha256"]
        passed("downloadable_evidence_contains_original_clock_control_and_every_scene_artifact")
        report.update(
            status="passed",
            verified_at=datetime.now(UTC).isoformat(),
            records={
                "assembly": assembly["id"],
                "lens": lens["id"],
                "scene": scene["id"],
                "control": control["id"],
                "test": test["id"],
                "run": run["id"],
                "rerun": rerun["id"],
                "replay": replay["id"],
                "isp": isp["id"],
            },
            raw_shape=list(raw.shape),
            raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            rgb_changed_values=int(np.count_nonzero(changed != rgb)),
            rgb_max_absolute_change=float(np.max(np.abs(changed - rgb))),
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument(
        "--report",
        type=Path,
        default=WORKSPACE / "reports/camera_platform/temporal-http-verification.json",
    )
    args = parser.parse_args()
    suffix = datetime.now(UTC).strftime("temporal-%Y%m%dT%H%M%SZ")
    if args.base_url:
        report = verify(args.base_url, suffix)
    else:
        with tempfile.TemporaryDirectory(prefix="camera-temporal-http-") as directory:
            state = Path(directory).resolve()
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            with (state / "http.log").open("ab") as log:
                process = start(state, port, log)
                try:
                    report = verify(f"http://127.0.0.1:{port}", suffix)
                finally:
                    stop(process)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": len(report["checks"]),
                "report": str(args.report),
            }
        )
    )


if __name__ == "__main__":
    main()
