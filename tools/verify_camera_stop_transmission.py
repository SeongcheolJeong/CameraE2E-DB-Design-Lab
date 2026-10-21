#!/usr/bin/env python3
"""Run a passive-stop investigation through an isolated live API and Worker.

The journal saves every POST intent and returned identity. A repeated invocation
reads known identities and resumes their existing jobs; an uncertain POST stops
for reconciliation instead of creating a duplicate. No primary state is used.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import numpy as np

from camera_platform import __version__
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import PROVENANCE
from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient


def fields(record, contract):
    return {k: copy.deepcopy(v) for k, v in record.items() if k in contract.model_fields}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if address.hostname != "127.0.0.1" or address.port in (None, 8770):
        parser.error("An isolated explicit loopback QA port is required; primary8770 is refused")
    state = args.expected_state_dir.resolve()
    if state.name == "camera-platform":
        parser.error("Primary state is refused")
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "http-verification.json"
    report = (
        json.loads(report_path.read_text())
        if report_path.exists()
        else {
            "schema": "camera_stop_transmission_http_verification_v1",
            "status": "in_progress",
            "base_url": args.base_url,
            "state_dir": str(state),
            "version": __version__,
            "checks": [],
            "operations": {},
            "physical_validation": "not_established",
            "source_kind": "simulated",
            "total_uncertainty": {"kind": "unknown"},
        }
    )
    assert report["base_url"] == args.base_url and report["state_dir"] == str(state)
    assert report["version"] == __version__, "Resume only the same source milestone"

    def save():
        report["updated_at"] = datetime.now(UTC).isoformat()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        if name not in report["checks"]:
            report["checks"].append(name)
        save()

    with PlatformClient(args.base_url, timeout=60) as client:
        health = client.get("health")
        check(
            "isolated live Worker identity",
            Path(health["state_dir"]).resolve() == state and health["worker"] == "running",
        )
        check("source development version", client.get("session")["version"] == __version__)

        def send(label, endpoint, payload, *, collection=None):
            operations = report["operations"]
            if label in operations:
                old = operations[label]
                assert old["payload_sha256"] == digest(payload), "Changed intent: " + label
                if not old.get("id"):
                    raise RuntimeError("Reconcile uncertain POST before continuing: " + label)
                return client.get((collection or endpoint) + "/" + old["id"])
            operations[label] = {
                "endpoint": endpoint,
                "payload_sha256": digest(payload),
                "payload": payload,
                "status": "intent_saved",
            }
            save()
            result = client.post(endpoint, payload)
            operations[label].update(id=result["id"], status="response_received")
            save()
            return result

        def wait(job, kind):
            result = (
                client.wait_characterization(job["id"])
                if kind == "factory/jobs"
                else client.wait_scene_projection(job["id"])
                if kind == "scene_projections"
                else client.wait_scene_render(job["id"])
                if kind == "scene_renders"
                else client.wait(job["id"])
            )
            check(kind + " succeeded " + job["id"], result["status"] == "succeeded")
            return result

        def array(job, name):
            artifact = next(a for a in job["artifacts"] if a["name"] == name + ".npy")
            response = client.http.get(artifact["url"])
            response.raise_for_status()
            check(
                "artifact digest " + artifact["sha256"],
                hashlib.sha256(response.content).hexdigest() == artifact["sha256"],
            )
            return np.load(io.BytesIO(response.content), allow_pickle=False)

        original_id = report.get("original_assembly_id")
        if not original_id:
            original_id = client.get("assemblies")[0]["id"]
            report["original_assembly_id"] = original_id
            save()
        original = client.get("assemblies/" + original_id)
        models = {m["kind"]: m for m in [client.get("models/" + i) for i in original["model_ids"]]}
        data = fields(models["sensor"], ModelCreate)
        data.update(name="Stop QA · 명시적 128px 센서", parents=[models["sensor"]["id"]])
        data["spectral_range"] = {"min": 500, "max": 600, "unit": "nm"}
        data["payload"]["wavelength_nm"] = [500, 600]
        data["payload"]["qe"] = [[row[1], row[3]] for row in data["payload"]["qe"]]
        angular = data["payload"]["sensor_angular_response"]
        angular["relative_response"] = [[row[1], row[3]] for row in angular["relative_response"]]
        for key in ("active_width", "active_height"):
            data["properties"][key] = {"value": 0.384, "unit": "mm"}
        sensor = send("sensor", "models", data)
        light = send(
            "light",
            "scene_assets",
            {
                **PROVENANCE,
                "name": "Stop QA · 균일 분광 환경광",
                "payload": {
                    "kind": "light",
                    "distribution": "constant_environment",
                    "spectrum": {"wavelength_nm": [500, 600], "values": [0.2, 0.2]},
                    "unit": "W/m^2/sr/nm",
                    "time_model": "static",
                },
            },
        )
        scene = send(
            "scene",
            "scene_assets",
            {
                **PROVENANCE,
                "name": "Stop QA · 광량 비교 장면",
                "payload": {
                    "kind": "scene",
                    "coordinate_system": "right_handed_y_up_metres",
                    "instances": [],
                    "environment_light_id": light["id"],
                    "point_lights": [],
                },
            },
        )
        records, numeric = {}, {}
        for label in ("clear", "quarter", "radial"):
            psf_input = client.factory_template("prescription_psf_v1", request_version=2)
            psf_input["name"] = "Stop QA · " + label + " · PSF"
            mask = psf_input["request"]["prescription"]["stop_transmission"]
            mask["normalized_radius"] = [0, 0.25, 0.5, 0.75, 1]
            mask["power_transmittance"] = (
                [
                    [1 - 0.5 * r * r for r in mask["normalized_radius"]],
                    [0.9 - 0.6 * r * r for r in mask["normalized_radius"]],
                ]
                if label == "radial"
                else [[0.25 if label == "quarter" else 1.0] * 5] * 2
            )
            mask["source"]["description"] = "명시한 수동 stop 설계 · " + label
            psf = wait(send(label + "-psf", "factory/jobs", psf_input), "factory/jobs")
            check(label + " PSF numerical gates", psf["result"]["summary"]["status"] == "PROPOSED")
            request = client.factory_template("rayoptics_lens_v1", request_version=2)
            request.update(
                name="Stop QA · " + label + " · 절대 광량",
                request=psf_input["request"]["prescription"],
            )
            ray = wait(send(label + "-ray", "factory/jobs", request), "factory/jobs")
            check(
                label + " radiometry numerical gates",
                ray["result"]["summary"]["status"] == "PROPOSED",
            )
            conditions = {
                **original["conditions"],
                "f_number": {
                    "value": psf["result"]["summary"]["geometric_reference_f_number"],
                    "unit": "1",
                },
                "light_id": {"value": scene["illumination_id"], "unit": "1"},
            }
            parent = send(
                label + "-parent",
                "factory/models",
                {
                    "job_id": ray["id"],
                    "name": "Stop QA · " + label + " · 광량 응답",
                    "revision": "1",
                    "conditions": conditions,
                },
                collection="models",
            )
            lens = send(
                label + "-lens",
                "factory/models",
                {
                    "job_id": psf["id"],
                    "base_lens_model_id": parent["id"],
                    "name": "Stop QA · " + label + " · 결합 렌즈",
                    "revision": "1",
                    "conditions": conditions,
                    "condition_scope": "optical_response",
                },
                collection="models",
            )
            draft = fields(original, AssemblyCreate)
            draft.update(
                name="Stop QA · " + label + " 카메라",
                model_ids=[lens["id"], sensor["id"], models["isp"]["id"]],
                spectral_range=sensor["spectral_range"],
                conditions=conditions,
            )
            camera = send(label + "-camera", "assemblies", draft)
            check(label + " compatible camera", camera["compatibility"]["status"] == "PASS")
            if label == "clear":
                request = client.get(
                    "scene_authoring/render-template?"
                    + urlencode(
                        {
                            "scene_id": scene["id"],
                            "assembly_id": camera["id"],
                        }
                    )
                )["request"]
                request.update(name="Stop QA · 원본 분광 광휘", shape=[16, 16], samples_per_pixel=2)
                queued = send("source-render", "scene_renders", request)
                # Observe the existing scene job through its dedicated endpoint.
                source = wait(queued, "scene_renders")
                report["source_render_id"] = source["id"]
                save()
            request = client.get(
                "scene_authoring/projection-template?"
                + urlencode(
                    {
                        "source_render_id": source["id"],
                        "assembly_id": camera["id"],
                    }
                )
            )["request"]
            plan = client.preview_scene_projection(request)
            check(label + " projection ready", plan["status"] == "ready")
            projected = wait(
                send(
                    label + "-projection",
                    "scene_projections",
                    {"draft": request, "preview_sha256": plan["preview_sha256"]},
                ),
                "scene_projections",
            )
            check(
                label + " reuses radiance without renderer",
                projected["result"]["summary"]["renderer_invoked"] is False,
            )
            image = send(
                label + "-image",
                "scene_projection_publications",
                {"job_id": projected["id"], "name": "Stop QA · " + label + " 이상 영상"},
                collection="spectral_images",
            )
            test = send(
                label + "-test",
                "tests",
                client.template("spatial-test", assembly_id=camera["id"], scene_id=image["id"]),
            )
            run = wait(
                send(
                    label + "-raw",
                    "runs",
                    {
                        "assembly_id": camera["id"],
                        "test_id": test["id"],
                        "scene_id": image["id"],
                        "plugin_id": "spatial_scene_v1",
                        "parameters": {"frames": 2, "seed": 413},
                    },
                ),
                "runs",
            )
            numeric[label] = {
                "irradiance": array(projected, "spectral-image"),
                "electrons": array(run, "mean_electrons"),
                "raw": array(run, "raw_frames"),
                "psf": np.asarray(lens["payload"]["psf"]["values"]),
            }
            records[label] = {
                "lens": lens["id"],
                "camera": camera["id"],
                "projection": projected["id"],
                "run": run["id"],
            }
            (args.output / (label + "-run.json")).write_text(
                json.dumps(run, ensure_ascii=False, indent=2) + "\n"
            )
            (args.output / (label + "-lens.json")).write_text(
                json.dumps(lens, ensure_ascii=False, indent=2) + "\n"
            )
            bundle = client.http.get("/api/v1/runs/" + run["id"] + "/bundle")
            bundle.raise_for_status()
            (args.output / (label + "-evidence.zip")).write_bytes(bundle.content)
        check(
            "quarter throughput preserves PSF shape",
            np.allclose(numeric["quarter"]["psf"], numeric["clear"]["psf"], rtol=0, atol=1e-14),
        )
        for key in ("irradiance", "electrons"):
            check(
                "quarter " + key + " applies exactly once",
                np.allclose(
                    numeric["quarter"][key], numeric["clear"][key] * 0.25, rtol=1e-12, atol=1e-12
                ),
            )
        check(
            "radial mask changes PSF",
            np.max(np.abs(numeric["radial"]["psf"] - numeric["clear"]["psf"])) > 1e-5,
        )
        check(
            "actual RAW changes with stop",
            not np.array_equal(numeric["quarter"]["raw"], numeric["clear"]["raw"]),
        )
        report.update(
            status="passed",
            records=records,
            ratios={
                "quarter_electrons": float(
                    numeric["quarter"]["electrons"].sum() / numeric["clear"]["electrons"].sum()
                ),
                "radial_electrons": float(
                    numeric["radial"]["electrons"].sum() / numeric["clear"]["electrons"].sum()
                ),
            },
        )
        save()
        print(
            json.dumps(
                {"status": report["status"], "checks": len(report["checks"]), "records": records}
            )
        )


if __name__ == "__main__":
    main()
