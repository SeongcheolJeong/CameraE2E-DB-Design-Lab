#!/usr/bin/env python3
"""Verify new shared-charge acquisitions through an externally managed QA API/Worker.

Require a loopback QA state and an explicit existing PSF camera. POST intentions
and identities are durable: reruns read the same records and never repeat an
uncertain POST. Server launch, restart and cleanup belong to the caller. Synthetic
readout assumptions and implementation checks do not establish physical accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shlex
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

from camera_platform import __version__
from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def paired_reference(raw, calibration, dark, profile):
    """Independent 2-path scalar equations, consuming RAW and declared calibration only."""
    r = np.asarray(calibration["read_noise_covariance_e2"], dtype=float)
    denominator = r[0, 0] + r[1, 1] - 2 * r[0, 1]
    w = (
        float(np.clip((r[1, 1] - r[0, 1]) / denominator, 0, 1))
        if denominator > 0
        else 1.0
        if profile["tie_break_stream"] == "HCG"
        else 0.0
    )
    usable = np.zeros_like(raw, dtype=bool)
    electron = np.empty(raw.shape, dtype=float)
    for index, path in enumerate(calibration["paths"]):
        guard = (
            max(
                profile["saturation_margin_dn"],
                profile["read_noise_guard_sigma"] * np.sqrt(r[index, index]) * path["dn_per_e"],
            )
            + path["rounding_bound_e"] * path["dn_per_e"]
        )
        usable[index] = (raw[index] > path["linear_floor_dn"] + guard) & (
            raw[index] < path["linear_ceiling_dn"] - guard
        )
        electron[index] = (raw[index].astype(float) - path["black_level_dn"]) / path["dn_per_e"]
    weights = np.empty(raw.shape, dtype=float)
    both = usable.all(axis=0)
    weights[0] = np.where(both, w, usable[0].astype(float))
    weights[1] = np.where(both, 1 - w, usable[1].astype(float))
    valid = usable.any(axis=0)
    mean = (weights * electron).sum(axis=0)
    merged = (mean - dark) / calibration["exposure_s"]
    read_variance = (
        weights[0] ** 2 * r[0, 0]
        + 2 * weights[0] * weights[1] * r[0, 1]
        + weights[1] ** 2 * r[1, 1]
    )
    variance = np.maximum(mean, 0) + read_variance
    merged[~valid], variance[~valid] = np.nan, np.nan
    return usable, weights, valid, merged, variance


def process_witness(port, api_pid, state):
    """Observe, never signal, the external API and its exact-state Worker child."""
    listener = subprocess.check_output(
        ["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"], text=True
    ).split()
    if set(listener) != {str(api_pid)}:
        raise ValueError("QA listener differs from the caller's expected API PID")
    output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,pgid=,command="], text=True)
    entries = []
    for line in output.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) == 4:
            entries.append(
                {
                    "pid": int(fields[0]),
                    "ppid": int(fields[1]),
                    "pgid": int(fields[2]),
                    "command": fields[3],
                }
            )
    api = next(p for p in entries if p["pid"] == api_pid)
    workers = []
    for entry in entries:
        if entry["ppid"] != api_pid:
            continue
        command = shlex.split(entry["command"])
        if "camera_platform.worker" not in command or "--state-dir" not in command:
            continue
        path = command[command.index("--state-dir") + 1]
        if Path(path).resolve() == state:
            workers.append(entry)
    if len(workers) != 1:
        raise ValueError("Expected one supervised Worker child with the exact QA state")
    return {"api": api, "worker": workers[0], "observed_at": datetime.now(UTC).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-state-dir", type=Path, required=True)
    parser.add_argument("--expected-api-pid", type=int, required=True)
    parser.add_argument("--assembly-id", required=True)
    parser.add_argument("--parent-program-id", help="Existing same-assembly DCG program lineage")
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
        parser.error("Use an explicit loopback HTTP QA port, never primary 8770")
    state, out = args.expected_state_dir.resolve(), args.output_dir.resolve()
    if "qa" not in state.name.lower() or state.name == "camera-platform":
        parser.error("An explicitly named QA state is required")
    if out == state or state in out.parents:
        parser.error("Verification output must be outside the state directory")
    out.mkdir(parents=True, exist_ok=True)
    journal = out / "http-verification.json"
    tool = {
        "path": str(Path(__file__).resolve()),
        "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    environment = environment_fingerprint()
    report = (
        json.loads(journal.read_text())
        if journal.exists()
        else {
            "schema": "camera_dcg_http_verification_v1",
            "status": "in_progress",
            "base_url": args.base_url,
            "state_dir": str(state),
            "assembly_id": args.assembly_id,
            "parent_program_id": args.parent_program_id,
            "version": __version__,
            "environment": environment,
            "tool": tool,
            "python": sys.executable,
            "checks": [],
            "operations": {},
            "artifacts": {},
            "packages": {},
            "server_observations": [],
            "successful_readbacks": [],
            "source_kind": "simulated",
            "physical_validation": "not_established",
            "scope": [
                "New shared-charge API/Worker acquisition from an explicit fixed camera",
                "Actual paired integer RAW and RAW-only correlated read-noise fusion",
                "Preserved original records and byte hashes; resume has no repeated POST",
            ],
            "not_proven": [
                "Vendor DCG circuit",
                "physical sensor accuracy",
                "independent installation",
                "restart until caller restarts and resumes this verifier",
            ],
        }
    )
    for key, expected in {
        "base_url": args.base_url,
        "state_dir": str(state),
        "assembly_id": args.assembly_id,
        "parent_program_id": args.parent_program_id,
        "version": __version__,
        "environment": environment,
        "tool": tool,
    }.items():
        if report[key] != expected:
            raise ValueError("Resume binding changed: " + key)

    def save():
        report["updated_at"] = datetime.now(UTC).isoformat()
        temporary = journal.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        )
        temporary.replace(journal)

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        if name not in report["checks"]:
            report["checks"].append(name)
        save()

    report["status"] = "in_progress"
    save()
    try:
        with PlatformClient(args.base_url, timeout=60) as client:
            health, session = client.get("health"), client.get("session")
            check(
                "exact isolated healthy state and supervised Worker",
                Path(health["state_dir"]).resolve() == state
                and Path(session["state_dir"]).resolve() == state
                and health["worker"] == "running"
                and health["status"] == "ok",
            )
            check("source version matches executable", session["version"] == __version__)
            witness = process_witness(address.port, args.expected_api_pid, state)
            report["server_observations"].append(witness)
            save()

            def preserve(label, value):
                target = out / (label + "-record.json")
                text = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
                target.write_text(text)
                return value

            def send(label, endpoint, payload, collection=None, upload=None):
                collection = collection or endpoint
                intent = (
                    payload
                    if upload is None
                    else {"metadata": payload, "image_sha256": hashlib.sha256(upload).hexdigest()}
                )
                old = report["operations"].get(label)
                if old:
                    if old["payload_sha256"] != digest(intent) or old["endpoint"] != endpoint:
                        raise ValueError("Changed saved POST intent: " + label)
                    if not old.get("id"):
                        raise RuntimeError("Uncertain POST needs reconciliation: " + label)
                    value = client.get(collection + "/" + old["id"])
                    if old.get("record_sha256") and digest(value) != old["record_sha256"]:
                        raise ValueError("Immutable saved record changed: " + label)
                    return preserve(label, value)
                operation = {
                    "endpoint": endpoint,
                    "collection": collection,
                    "payload": intent,
                    "payload_sha256": digest(intent),
                    "status": "intent_saved",
                }
                report["operations"][label] = operation
                save()
                try:
                    if upload is None:
                        value = client.post(endpoint, payload)
                    else:
                        response = client.http.post(
                            "/api/v1/" + endpoint,
                            data={"metadata": json.dumps(payload, allow_nan=False)},
                            files={"image": (label + ".npy", upload, "application/octet-stream")},
                        )
                        response.raise_for_status()
                        value = response.json()
                except Exception as exc:
                    operation.update(
                        status="uncertain_post_requires_reconciliation",
                        error_type=type(exc).__name__,
                    )
                    save()
                    raise
                operation.update(status="response_received", id=value["id"])
                if endpoint != "runs":
                    operation["record_sha256"] = digest(value)
                save()
                return preserve(label, value)

            def wait(label, value):
                result = client.wait(value["id"], timeout=300)
                check(label + " Worker completed", result["status"] == "succeeded")
                check(
                    label + " exact current execution environment",
                    result["environment"] == environment,
                )
                report["operations"][label]["record_sha256"] = digest(result)
                save()
                return preserve(label, result)

            def artifact(descriptor):
                identity, name = descriptor["sha256"], descriptor["name"]
                if (
                    len(identity) != 64
                    or any(c not in "0123456789abcdef" for c in identity)
                    or Path(name).name != name
                    or descriptor["url"] != "/api/v1/artifacts/" + identity
                ):
                    raise ValueError("Unsafe artifact descriptor")
                response = client.http.get(descriptor["url"])
                response.raise_for_status()
                content = response.content
                check(
                    "artifact SHA256 " + identity,
                    hashlib.sha256(content).hexdigest() == identity
                    and len(content) == descriptor["bytes"],
                )
                target = out / "artifacts" / identity / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                report["artifacts"][identity + "/" + name] = descriptor
                save()
                return content

            def array(run, name):
                matches = [a for a in run["artifacts"] if a["name"] == name + ".npy"]
                if len(matches) != 1:
                    raise ValueError("Ambiguous or absent array " + name)
                value = np.load(io.BytesIO(artifact(matches[0])), allow_pickle=False)
                check(
                    "array shape/type " + name,
                    list(value.shape) == matches[0]["shape"]
                    and str(value.dtype) == matches[0]["dtype"],
                )
                return value

            camera = preserve("camera", client.get("assemblies/" + args.assembly_id))
            models = [client.get("models/" + identity) for identity in camera["model_ids"]]
            sensor = next(m for m in models if m["kind"] == "sensor")
            lens = next(m for m in models if m["kind"] == "lens")
            check(
                "explicit compatible camera with published optical PSF",
                camera["compatibility"]["status"] == "PASS" and "psf" in lens["payload"],
            )
            fixed = {
                "assemblies/" + camera["id"]: digest(camera),
                **{"models/" + m["id"]: digest(m) for m in models},
            }
            if args.parent_program_id:
                parent = preserve(
                    "parent-program", client.get("capture_programs/" + args.parent_program_id)
                )
                check(
                    "parent program has the exact same camera and shared-charge topology",
                    parent["assembly_id"] == camera["id"]
                    and parent["assembly_snapshot"] == camera
                    and parent["payload"]["schema"] == "camera_dcg_capture_program_v1",
                )
                fixed["capture_programs/" + parent["id"]] = digest(parent)
            if "fixed_camera_records" in report:
                check(
                    "original camera and model records unchanged",
                    report["fixed_camera_records"] == fixed,
                )
            else:
                report["fixed_camera_records"] = fixed
            for model in models:
                preserve(model["kind"], model)
                for descriptor in model.get("response_artifacts", {}).values():
                    artifact(descriptor)
            draft = client.template(
                "capture-program", assembly_id=camera["id"], mode="shared_charge"
            )
            draft.update(
                name="DCG QA · 선언한 공통 전하와 두 선형 한계",
                revision="dcg-qa-2",
                parents=[args.parent_program_id] if args.parent_program_id else [],
            )
            model = draft["payload"]["electrical_model"]
            model["shared_charge_capacity_e"] = 3000
            model["dark_current_e_per_s"] = 0
            model["paths"][0]["input_linear_ceiling_e"] = 500
            model["paths"][1]["input_linear_ceiling_e"] = 3000
            model["source"] = {
                "kind": "estimated",
                "description": "Authored ideal shared-charge QA assumptions: shared charge "
                "capacity 3000 electron, HCG linear ceiling 500 electron, LCG linear ceiling "
                "3000 electron, and zero dark current. Conversion/ADC settings are inherited "
                "from the explicit template and paired read covariance is separately declared. "
                "These constants are not measured sensor or vendor circuit characterization.",
            }
            model["read_noise"]["covariance_e2"] = [[4, 1], [1, 16]]
            model["read_noise"]["source"] = {
                "kind": "estimated",
                "description": "Authored QA paired read covariance "
                "[[4,1],[1,16]] e2; not measured sensor read noise. Fixed 25 C path settings.",
            }
            program = send("program", "capture_programs", draft)
            runs = {}
            for label, irradiance in (("bright", 0.003), ("dark", 0.0)):
                if label == "bright":
                    image = send(
                        label + "-image",
                        "spectral_images/fixture",
                        {
                            "assembly_id": camera["id"],
                            "name": "DCG QA " + label,
                            "revision": "1",
                            "pattern": "flat",
                            "shape": [8, 8],
                            "irradiance_scale": irradiance,
                        },
                        collection="spectral_images",
                    )
                else:
                    metadata = client.template("spectral-image", assembly_id=camera["id"])
                    metadata.update(
                        name="DCG QA zero-photon input",
                        revision="1",
                        source_uri="fixture://dcg-zero-spectral-irradiance",
                        license="MIT; authored synthetic zero input",
                    )
                    metadata["sampling"]["crop_origin"] = [
                        (n - 8) // 2 for n in metadata["sampling"]["full_sensor_shape"]
                    ]
                    stream = io.BytesIO()
                    np.save(
                        stream,
                        np.zeros((8, 8, len(metadata["wavelength_nm"])), dtype="<f8"),
                        allow_pickle=False,
                    )
                    image = send(
                        label + "-image",
                        "spectral_images/import",
                        metadata,
                        collection="spectral_images",
                        upload=stream.getvalue(),
                    )
                scene_draft = client.template("temporal-scene", assembly_id=camera["id"])
                scene_draft.update(name="DCG QA static " + label, revision="1")
                for knot in scene_draft["payload"]["knots"]:
                    knot["spectral_image_id"] = image["id"]
                scene = send(label + "-scene", "scene_sequences", scene_draft)
                control_draft = client.template(
                    "control-sequence", assembly_id=camera["id"], scene_sequence_id=scene["id"]
                )
                control_draft.update(name="DCG QA one 2ms acquisition " + label, revision="1")
                control_draft["payload"]["frames"] = control_draft["payload"]["frames"][:1]
                control_draft["payload"]["mode"].update(
                    shutter="global", line_time_ns=0, hidden_top_rows=0
                )
                control_draft["payload"]["frames"][0]["applied"].update(
                    exposure_ns=2_000_000, gain=1.0
                )
                control = send(label + "-control", "control_sequences", control_draft)
                test = send(
                    label + "-test",
                    "tests",
                    client.template(
                        "temporal-test",
                        assembly_id=camera["id"],
                        scene_sequence_id=scene["id"],
                        control_sequence_id=control["id"],
                        capture_program_id=program["id"],
                    ),
                )
                payload = {
                    "assembly_id": camera["id"],
                    "scene_sequence_id": scene["id"],
                    "control_sequence_id": control["id"],
                    "test_id": test["id"],
                    "capture_program_id": program["id"],
                    "plugin_id": "shared_charge_dcg_v1",
                    "parameters": {"seed": 2029 if label == "bright" else 2031},
                }
                run = wait(label + "-run", send(label + "-run", "runs", payload))
                runs[label] = run
                summary = run["result"]["summary"]
                check(
                    label + " fixed program and paired sample authority",
                    run["capture_program_snapshot"] == program
                    and summary["prediction"]["sample_geometry"]
                    == test["stimulus"]["sample_contract"],
                )
                raw, shared, noise = [
                    array(run, name)
                    for name in (
                        "raw_groups",
                        "electrical_shared_charge_e",
                        "electrical_read_noise_e",
                    )
                ]
                check(
                    label + " one acquisition two separate RAW streams",
                    raw.shape == (1, 2, 8, 8)
                    and raw.dtype.kind in "iu"
                    and summary["acquisition_count"] == 1
                    and summary["readout_count"] == 2
                    and summary["raw_axes"] == ["acquisition", "readout", "row", "column"],
                )
                for stream, path in enumerate(program["payload"]["electrical_model"]["paths"]):
                    q = np.minimum(shared[0], path["input_linear_ceiling_e"]) + noise[0, stream]
                    adc = path["adc"]
                    black, white = adc["black_level_dn"], 2 ** adc["bit_depth"] - 1
                    codes = np.clip(
                        np.rint(
                            black
                            + q
                            * path["conversion_gain_v_per_e"]
                            * path["analog_gain"]
                            * white
                            / adc["full_scale_v"]
                        ),
                        0,
                        white,
                    )
                    expected = np.clip(
                        np.rint(black + path["digital_gain"] * (codes - black)), 0, white
                    )
                    check(
                        label + " shared charge scalar ADC path " + path["stream_id"],
                        np.array_equal(raw[0, stream], expected),
                    )
                dark_e = array(run, "calibration_dark_e")[0]
                calibration = summary["calibrations"][0]
                profile = program["payload"]["merge_profile"]
                expected = paired_reference(raw[0], calibration, dark_e, profile)
                for name, expected_array in zip(
                    (
                        "usable_mask",
                        "weights",
                        "valid_mask",
                        "merged_rate",
                        "shared_shot_read_variance_estimate_e2",
                    ),
                    expected,
                    strict=True,
                ):
                    actual = array(run, name)[0]
                    check(
                        label + " independent RAW-only " + name,
                        np.allclose(actual, expected_array, rtol=1e-12, atol=1e-8, equal_nan=True),
                    )
                valid, usable = expected[2], expected[0]
                well = array(run, "electrical_well_saturated_mask")[0]
                path_clip = array(run, "electrical_path_saturated_mask")[0]
                if label == "bright":
                    recovered = path_clip[0] & ~well & ~usable[0] & usable[1] & valid
                    check("HCG hidden clipping retains LCG-only recovery", recovered.any())
                    check("common well clipping has unrecoverable samples", (well & ~valid).any())
                    report["bright_counts"] = {
                        "lcg_only_recovery": int(recovered.sum()),
                        "common_clipped_invalid": int((well & ~valid).sum()),
                        "valid_pixels": int(valid.sum()),
                    }
                else:
                    check("dark acquisition has zero shared photocharge", not shared.any())
                    check("dark readouts retain declared temporal read noise", np.any(noise != 0))
                check(
                    label + " no oracle or physical validation claim",
                    summary["simulation_oracle"]["used_by_merge"] is False
                    and summary["physical_validation"] == "not_established",
                )
                channels = (
                    ["R", "Gr", "Gb", "B"]
                    if "native_pixel_response" in sensor["payload"]
                    else ["R", "G", "B"]
                )
                check(
                    label + " retained actual sensor site axis",
                    summary["channels"] == channels
                    and summary["prediction"]["axes"][2]["values"] == channels,
                )
                for descriptor in run["artifacts"]:
                    artifact(descriptor)
                response = client.http.get("/api/v1/runs/" + run["id"] + "/bundle")
                response.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    check(label + " package exact run", manifest["run"] == run)
                    for descriptor in run["artifacts"]:
                        member = f"artifacts/{descriptor['sha256']}/{descriptor['name']}"
                        check(
                            label + " package SHA " + descriptor["sha256"],
                            hashlib.sha256(archive.read(member)).hexdigest()
                            == descriptor["sha256"],
                        )
                package = out / (label + "-evidence.zip")
                package.write_bytes(response.content)
                report["packages"][label] = {
                    "path": str(package),
                    "sha256": hashlib.sha256(response.content).hexdigest(),
                    "bytes": len(response.content),
                }
                for acquisition in summary["acquisitions"]:
                    for readout in acquisition["readouts"]:
                        response = client.http.get(readout["preview_url"])
                        check(
                            label + " preview " + readout["stream_id"],
                            response.status_code == 200 and response.content.startswith(b"\x89PNG"),
                        )
            for key, expected in fixed.items():
                check("preserved input " + key, digest(client.get(key)) == expected)
            for label, operation in report["operations"].items():
                check(
                    "read back exact immutable " + label,
                    digest(client.get(operation["collection"] + "/" + operation["id"]))
                    == operation["record_sha256"],
                )
            report["server_observations"].append(
                process_witness(address.port, args.expected_api_pid, state)
            )
            report["records"] = {
                "assembly": camera["id"],
                "sensor": sensor["id"],
                "program": program["id"],
                **{k + "_run": v["id"] for k, v in runs.items()},
            }
            report["successful_readbacks"].append(datetime.now(UTC).isoformat())
            report["status"] = "passed_for_declared_scope"
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
