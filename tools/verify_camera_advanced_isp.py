#!/usr/bin/env python3
"""Qualify retained native RAW replay on an externally managed local QA API/Worker.

Durable POST intents prevent duplicate uncertain work. The caller owns API and
Worker launch/restart/cleanup. Synthetic tuning and implementation comparisons
do not establish physical calibration, objective image quality, or installation.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from verify_camera_dcg import process_witness

from camera_platform import __version__
from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def scalar_lsc(grid, origin, shape):
    """Independent native-coordinate bilinear oracle, four scalar weights per pixel."""
    result = np.empty(shape, dtype=float)
    sites = np.empty(shape, dtype=np.int64)
    for y in range(shape[0]):
        for x in range(shape[1]):
            row, column = origin[0] + y, origin[1] + x
            site = 2 * (row % 2) + column % 2
            row_nodes, column_nodes = grid["row_nodes"], grid["column_nodes"]
            i = min(len(row_nodes) - 2, sum(v <= row for v in row_nodes) - 1)
            j = min(len(column_nodes) - 2, sum(v <= column for v in column_nodes) - 1)
            if i < 0 or j < 0 or row > row_nodes[-1] or column > column_nodes[-1]:
                raise ValueError("Scalar oracle refuses out-of-grid extrapolation")
            fy = (row - row_nodes[i]) / (row_nodes[i + 1] - row_nodes[i])
            fx = (column - column_nodes[j]) / (column_nodes[j + 1] - column_nodes[j])
            value = 0.0
            for dy, wy in ((0, 1 - fy), (1, fy)):
                for dx, wx in ((0, 1 - fx), (1, fx)):
                    if wy * wx and not grid["valid_mask"][i + dy][j + dx][site]:
                        raise ValueError("This qualification uses wholly valid declared LSC nodes")
                    value += wy * wx * grid["gains"][i + dy][j + dx][site]
            result[y, x], sites[y, x] = value, site
    return result, sites


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-state-dir", type=Path, required=True)
    parser.add_argument("--expected-api-pid", type=int, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if (
        address.scheme != "http"
        or address.hostname != "127.0.0.1"
        or address.port in (None, 8770)
        or address.username
        or address.password
        or address.path not in ("", "/")
        or address.query
        or address.fragment
    ):
        parser.error("Require an explicit loopback QA HTTP port; primary 8770 is forbidden")
    state, out = args.expected_state_dir.resolve(), args.output_dir.resolve()
    if "qa" not in state.name.lower() or state.name == "camera-platform":
        parser.error("An explicitly named QA state is required")
    if out == state or state in out.parents:
        parser.error("Verification evidence must be outside the state directory")
    journal = out / "http-verification.json"
    if out.exists() and not journal.exists() and any(out.iterdir()):
        parser.error("Use a fresh output directory or resume its exact existing journal")
    out.mkdir(parents=True, exist_ok=True)
    environment = environment_fingerprint()
    bindings = {
        "base_url": args.base_url,
        "state_dir": str(state),
        "source_id": args.source_id,
        "version": __version__,
        "environment": environment,
        "tools": [
            {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in (
                Path(__file__).resolve(),
                Path(__file__).resolve().with_name("verify_camera_dcg.py"),
            )
        ],
    }
    report = (
        json.loads(journal.read_text())
        if journal.exists()
        else {
            "schema": "camera_advanced_isp_http_verification_v1",
            **bindings,
            "status": "in_progress",
            "python": sys.executable,
            "checks": [],
            "operations": {},
            "artifacts": {},
            "records": {},
            "packages": {},
            "server_observations": [],
            "successful_readbacks": [],
            "physical_validation": "not_established",
            "scope": "Retained native RAW, fixed calibration/profile, separate Worker replay",
            "not_proven": [
                "physical calibration or image quality improvement",
                "vendor ISP",
                "independent installation",
                "restart until caller resumes after restart",
            ],
        }
    )
    for key, expected in bindings.items():
        if report[key] != expected:
            raise ValueError("Resume binding changed: " + key)

    def save():
        report["updated_at"] = datetime.now(UTC).isoformat()
        pending = journal.with_suffix(".tmp")
        pending.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        pending.replace(journal)

    def check(name, accepted):
        if not bool(accepted):
            raise AssertionError(name)
        if name not in report["checks"]:
            report["checks"].append(name)
        save()

    def preserve(label, value):
        path = out / (label + "-record.json")
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        report["records"][label] = {
            "id": value["id"],
            "record_sha256": digest(value),
            "path": str(path),
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        save()
        return value

    report["status"] = "in_progress"
    save()
    try:
        with PlatformClient(args.base_url, timeout=60) as client:
            health, session = client.get("health"), client.get("session")
            check(
                "exact healthy QA state and supervised Worker",
                Path(health["state_dir"]).resolve() == state
                and Path(session["state_dir"]).resolve() == state
                and health["worker"] == "running"
                and health["status"] == "ok",
            )
            check("current executable source version", session["version"] == __version__)
            report["server_observations"].append(
                process_witness(address.port, args.expected_api_pid, state)
            )
            save()

            def send(label, endpoint, payload):
                operation = report["operations"].get(label)
                if operation:
                    if (
                        operation["payload_sha256"] != digest(payload)
                        or operation["endpoint"] != endpoint
                    ):
                        raise ValueError("Changed durable POST intent: " + label)
                    if not operation.get("id"):
                        raise RuntimeError("Uncertain POST needs explicit reconciliation: " + label)
                    value = client.get(endpoint + "/" + operation["id"])
                    if (
                        operation.get("record_sha256")
                        and digest(value) != operation["record_sha256"]
                    ):
                        raise ValueError("Previously completed immutable record changed: " + label)
                    return preserve(label, value)
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
                except Exception as exc:
                    operation.update(
                        status="uncertain_post_requires_reconciliation",
                        error_type=type(exc).__name__,
                    )
                    save()
                    raise
                operation.update(id=value["id"], status="response_received")
                if endpoint != "replays":
                    operation["record_sha256"] = digest(value)
                save()
                return preserve(label, value)

            def wait(label, value):
                result = client.wait_replay(value["id"], timeout=300)
                check(label + " Worker succeeded", result["status"] == "succeeded")
                check(
                    label + " exact new execution environment", result["environment"] == environment
                )
                report["operations"][label]["record_sha256"] = digest(result)
                save()
                return preserve(label, result)

            def artifact(descriptor):
                sha, name = descriptor["sha256"], descriptor["name"]
                if (
                    len(sha) != 64
                    or any(c not in "0123456789abcdef" for c in sha)
                    or Path(name).name != name
                    or descriptor["url"] != "/api/v1/artifacts/" + sha
                ):
                    raise ValueError("Unsafe artifact descriptor")
                response = client.http.get(descriptor["url"])
                response.raise_for_status()
                content = response.content
                check(
                    "artifact bytes " + sha,
                    len(content) == descriptor["bytes"]
                    and hashlib.sha256(content).hexdigest() == sha,
                )
                target = out / "artifacts" / sha / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                report["artifacts"][sha + "/" + name] = descriptor
                save()
                return content

            def array(run, name):
                descriptors = [a for a in run["artifacts"] if a["name"] == name + ".npy"]
                if len(descriptors) != 1:
                    raise ValueError("Missing/ambiguous retained array: " + name)
                descriptor = descriptors[0]
                values = np.load(io.BytesIO(artifact(descriptor)), allow_pickle=False)
                check(
                    "array dtype/shape " + run["id"] + "/" + name,
                    values.dtype.hasobject is False
                    and str(values.dtype) == descriptor["dtype"]
                    and list(values.shape) == descriptor["shape"],
                )
                return values

            original = client.get("runs/" + args.source_id)
            old = report["records"].get("source")
            if old and old["record_sha256"] != digest(original):
                raise ValueError("Original camera run changed since the journal was created")
            preserve("source", original)
            check(
                "source is a completed single-stream camera acquisition",
                original["status"] == "succeeded"
                and original["job_kind"] == "camera_simulation"
                and original["plugin_id"] != "shared_charge_dcg_v1",
            )
            raw = array(original, "raw_frames")
            check(
                "explicit native two-frame qualification raster",
                raw.ndim == 3
                and raw.shape[0] == 2
                and 64 <= raw.shape[1] <= 512
                and 64 <= raw.shape[2] <= 512,
            )
            source_summary = original["result"].get("summary", original["result"])
            geometry = source_summary["geometry"]
            check("preserved native geometry", geometry["kind"] == "native_sensor")
            report["source_environment"] = original["environment"]
            raw_descriptor = next(a for a in original["artifacts"] if a["name"] == "raw_frames.npy")
            initial_raw_bytes = artifact(raw_descriptor)
            parameters = {"source_kind": "run", "source_id": args.source_id}
            draft = client.template("isp-calibration", **parameters)
            draft.update(
                name="Advanced ISP QA · declared asymmetric native-site calibration",
                revision="isp-qa-1",
                source_kind="estimated",
                source_uri="fixture://advanced-isp-declared-calibration",
                method_and_version="authored_four_site_lsc_noise_example:1",
            )
            draft["uncertainty"]["reason"] = (
                "Independent authored grid and noise hint; no fitting, physical calibration, "
                "or image-quality acceptance is established."
            )
            origin, shape = geometry["crop_origin"], raw.shape[-2:]
            grid = draft["payload"]["lsc"]
            grid.update(
                row_nodes=[origin[0], origin[0] + shape[0] - 1],
                column_nodes=[origin[1], origin[1] + shape[1] - 1],
                gains=[
                    [[1.0, 1.1, 1.2, 1.3], [1.8, 1.5, 1.4, 1.6]],
                    [[1.2, 1.6, 1.9, 1.5], [2.0, 2.2, 1.7, 2.4]],
                ],
                reference_roi={
                    "row_start": origin[0],
                    "row_end": origin[0] + shape[0],
                    "column_start": origin[1],
                    "column_end": origin[1] + shape[1],
                },
            )
            draft["payload"]["noise"].update(
                slope_dn=[1.0, 1.2, 1.5, 2.0], intercept_dn2=[4.0, 9.0, 16.0, 25.0]
            )
            calibration = send("calibration", "isp-calibrations", draft)
            model_draft = client.template(
                "isp-profile", version=2, calibration_id=calibration["id"], **parameters
            )
            model_draft.update(
                name="Advanced ISP QA · LSC denoise bounded sharpen", revision="isp-qa-1"
            )
            model_draft["payload"]["raw_denoise"]["strength"] = 0.65
            model_draft["payload"]["linear_sharpen"].update(
                mode="bounded_detail_v1", amount=0.4, threshold=0.002, max_delta=0.05
            )
            model = send("profile", "models", model_draft)
            baseline_draft = client.template("isp-profile", assembly_id=original["assembly_id"])
            baseline_draft.update(
                name="Advanced ISP QA · explicit unchanged v1 baseline", revision="isp-qa-v1"
            )
            for key in ("white_balance", "ccm", "tone"):
                baseline_draft["payload"][key] = model["payload"][key]
            baseline_model = send("baseline-profile", "models", baseline_draft)
            baseline = wait(
                "baseline",
                send(
                    "baseline",
                    "replays",
                    {
                        **parameters,
                        "isp_model_id": baseline_model["id"],
                        "name": "QA same RAW v1 baseline",
                    },
                ),
            )
            candidate = wait(
                "candidate",
                send(
                    "candidate",
                    "replays",
                    {**parameters, "isp_model_id": model["id"], "name": "QA same RAW advanced v2"},
                ),
            )
            summary = candidate["result"]["summary"]
            check(
                "new calibration and exact RAW frozen into replay",
                candidate["calibration_snapshot"] == calibration
                and summary["calibration_snapshot"] == calibration
                and summary["raw_sha256"] == raw_descriptor["sha256"]
                and candidate["source"]["source_snapshot"] == original,
            )
            check(
                "no physical validation or computed uncertainty claim",
                summary["physical_validation"] == "not_established"
                and summary["uncertainty"] == {"kind": "unknown"}
                and calibration["authority"] == {"physical_validation": False, "release": False},
            )
            for label, record in (("baseline", baseline), ("candidate", candidate)):
                for descriptor in record["artifacts"]:
                    content = artifact(descriptor)
                    if descriptor["media_type"] == "image/png":
                        check(label + " PNG " + descriptor["name"], content.startswith(b"\x89PNG"))
                response = client.http.get("/api/v1/replays/" + record["id"] + "/bundle")
                response.raise_for_status()
                package = out / (label + "-evidence.zip")
                package.write_bytes(response.content)
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    if label == "candidate":
                        check(
                            "bundle includes exact calibration record",
                            manifest["isp_calibration"] == calibration,
                        )
                    for descriptor in record["artifacts"]:
                        content = archive.read(
                            f"artifacts/{descriptor['sha256']}/{descriptor['name']}"
                        )
                        check(
                            label + " ZIP bytes " + descriptor["name"],
                            hashlib.sha256(content).hexdigest() == descriptor["sha256"],
                        )
                report["packages"][label] = {
                    "path": str(package),
                    "bytes": package.stat().st_size,
                    "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
                }
            check(
                "original independent calibration evidence is exported",
                all(
                    item in candidate["input_artifacts"]
                    for item in calibration["evidence_artifacts"]
                ),
            )
            baseline_rgb = array(baseline, "rgb_frames")
            check(
                "saved v2 baseline is actual separate v1 replay RGB",
                np.array_equal(array(candidate, "baseline_rgb_frames"), baseline_rgb),
            )
            black = candidate["source"]["raw_format"]["black_level_dn"]
            white = 2 ** candidate["source"]["raw_format"]["bit_depth"] - 1
            blc = (raw.astype(float) - black) / (white - black)
            check(
                "signed BLC scalar equation has no renewed analog or digital gain",
                np.array_equal(array(baseline, "black_subtracted_normalized_frames"), blc)
                and np.array_equal(array(candidate, "black_subtracted_normalized_frames"), blc),
            )
            expected_gain, site = scalar_lsc(calibration["payload"]["lsc"], origin, shape)
            check(
                "every native LSC sample matches independent scalar interpolation",
                np.allclose(array(candidate, "lsc_gain_map"), expected_gain, rtol=2e-15, atol=2e-15)
                and np.array_equal(array(candidate, "native_site_index"), site),
            )
            lsc = array(candidate, "lsc_normalized_frames")
            check(
                "LSC multiplies signed BLC exactly once",
                np.allclose(lsc, blc * expected_gain, rtol=2e-15, atol=2e-15),
            )
            noise = calibration["payload"]["noise"]
            valid = array(candidate, "noise_hint_valid_mask_frames")
            variance = (
                np.asarray(noise["slope_dn"])[site] * np.maximum(raw.astype(float) - black, 0)
                + np.asarray(noise["intercept_dn2"])[site]
            )
            expected_hint = np.where(valid, variance, 0) * expected_gain**2 / (white - black) ** 2
            check(
                "noise hint uses declared DN calibration and fixed LSC gain squared",
                np.allclose(
                    array(candidate, "noise_variance_hint_normalized_frames"),
                    expected_hint,
                    rtol=3e-15,
                    atol=3e-15,
                ),
            )
            denoised = array(candidate, "denoised_normalized_frames")
            delta = array(candidate, "sharpen_delta_frames")
            candidate_rgb = array(candidate, "rgb_frames")
            check(
                "actual denoise and sharpen change retained samples",
                np.any(denoised != lsc) and np.any(delta != 0),
            )
            check("sharpen respects declared normalized bound", np.max(np.abs(delta)) <= 0.05)
            check(
                "candidate RGB differs from same RAW fixed-tone baseline",
                candidate_rgb.shape == (*raw.shape, 3)
                and not np.array_equal(candidate_rgb, baseline_rgb),
            )
            check(
                "stage previews retain both frame identities",
                {p["frame_index"] for p in summary["stage_previews"]} == {0, 1},
            )
            for label, operation in report["operations"].items():
                check(
                    "readback exact " + label,
                    digest(client.get(operation["endpoint"] + "/" + operation["id"]))
                    == operation["record_sha256"],
                )
            check(
                "original RAW bytes and camera run remain unchanged",
                artifact(raw_descriptor) == initial_raw_bytes
                and client.get("runs/" + args.source_id) == original,
            )
            report["comparison"] = {
                "source_raw_sha256": raw_descriptor["sha256"],
                "frames": raw.shape[0],
                "native_shape": list(shape),
                "native_sites": ["R", "Gr", "Gb", "B"],
                "denoised_changed_samples": int(np.count_nonzero(denoised != lsc)),
                "sharpen_changed_samples": int(np.count_nonzero(delta)),
                "maximum_sharpen_delta": float(np.max(np.abs(delta))),
                "candidate_vs_baseline_max_rgb_delta": float(
                    np.max(np.abs(candidate_rgb - baseline_rgb))
                ),
                "quality_improvement_claim": False,
            }
            report["server_observations"].append(
                process_witness(address.port, args.expected_api_pid, state)
            )
            report["successful_readbacks"].append(datetime.now(UTC).isoformat())
            report.update(status="passed_for_declared_scope")
            report.pop("error", None)
            save()
            print(
                json.dumps(
                    {
                        "report": str(journal),
                        "status": report["status"],
                        "checks": len(report["checks"]),
                        "records": report["records"],
                    },
                    indent=2,
                )
            )
    except Exception as exc:
        report.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
        save()
        raise


if __name__ == "__main__":
    main()
