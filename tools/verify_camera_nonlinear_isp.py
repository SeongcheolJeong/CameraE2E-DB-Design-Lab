#!/usr/bin/env python3
"""Exercise charge-domain replay/Study on an isolated clone of retained dev10 evidence.

Owns no server lifecycle. Durable POST intentions prohibit uncertain retries.
Readback verifies saved JSON/artifact bytes; it is not independent physical validation.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from verify_camera_dcg import process_witness

from camera_platform import isp_v3
from camera_platform.registry import canonical, digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--api-pid", required=True, type=int)
    parser.add_argument("--source-report", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if (
        address.scheme != "http"
        or address.hostname != "127.0.0.1"
        or address.port in (None, 8770)
        or address.path not in ("", "/")
        or address.query
        or address.fragment
        or address.username
        or address.password
    ):
        parser.error("Use a separate explicit loopback QA port")
    state, out = args.state_dir.resolve(), args.output_dir.resolve()
    if "qa" not in state.name.lower() or state == out or state in out.parents:
        parser.error("Require separate explicitly named QA state and report directory")
    out.mkdir(parents=True, exist_ok=True)
    journal = out / "http-verification.json"
    binding = {
        "base_url": args.base_url,
        "state_dir": str(state),
        "environment": environment_fingerprint(),
        "source_report_sha256": hashlib.sha256(args.source_report.read_bytes()).hexdigest(),
    }
    if args.verify_only:
        report = json.loads(journal.read_text())
        if report["status"] != "passed" or any(report[k] != v for k, v in binding.items()):
            raise ValueError("Readback requires the same successful frozen verification")
    else:
        if journal.exists():
            raise ValueError("Preserve existing journal; use readback or a new QA/report directory")
        report = {
            "schema": "camera_charge_isp_http_verification_v1",
            **binding,
            "status": "in_progress",
            "operations": {},
            "records": {},
            "artifacts": {},
            "bundles": {},
            "checks": [],
            "physical_validation": "not_established",
            "scope": "Actual API/Worker and retained RAW numerical equivalence; authored models",
        }

    def save():
        report["updated_at"] = datetime.now(UTC).isoformat()
        temp = journal.with_suffix(".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        temp.replace(journal)

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        if label not in report["checks"]:
            report["checks"].append(label)
        save()

    save()
    try:
        with PlatformClient(args.base_url, timeout=120) as client:
            health = client.get("health")
            check(
                "isolated state and supervised Worker",
                Path(health["state_dir"]).resolve() == state and health["worker"] == "running",
            )
            report["process_witness"] = process_witness(address.port, args.api_pid, state)
            save()

            def preserve(label, route, record):
                data = canonical(record).encode()
                path = out / (label + ".json")
                if args.verify_only:
                    check("saved JSON " + label, path.read_bytes() == data)
                else:
                    path.write_bytes(data)
                    report["records"][label] = {
                        "id": record["id"],
                        "route": route,
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                save()
                return record

            def send(label, endpoint, payload):
                if args.verify_only or label in report["operations"]:
                    raise ValueError("No repeated mutation: " + label)
                item = {
                    "endpoint": endpoint,
                    "payload": payload,
                    "sha256": digest(payload),
                    "status": "intent_saved",
                }
                report["operations"][label] = item
                save()
                try:
                    value = client.post(endpoint, payload)
                except Exception:
                    item["status"] = "uncertain_response_requires_reconciliation"
                    save()
                    raise
                item.update(status="response_received", id=value["id"])
                save()
                return value

            def data(artifact):
                response = client.http.get(artifact["url"])
                response.raise_for_status()
                content = response.content
                check(
                    "artifact " + artifact["sha256"],
                    len(content) == artifact["bytes"]
                    and hashlib.sha256(content).hexdigest() == artifact["sha256"],
                )
                report["artifacts"][artifact["sha256"]] = {
                    "name": artifact["name"],
                    "bytes": len(content),
                }
                return content

            def array(record, name):
                entries = [a for a in record["artifacts"] if a["name"] == name + ".npy"]
                check("single array " + record["id"] + " " + name, len(entries) == 1)
                return np.load(io.BytesIO(data(entries[0])), allow_pickle=False)

            def bundle(label, route):
                response = client.http.get("/api/v1/" + route + "/bundle")
                response.raise_for_status()
                content = response.content
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    for name in archive.namelist():
                        if name.startswith("artifacts/"):
                            check(
                                "bundle member " + name,
                                hashlib.sha256(archive.read(name)).hexdigest()
                                == name.split("/")[1],
                            )
                (out / (label + ".zip")).write_bytes(content)
                report["bundles"][label] = {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "manifest_sha256": digest(manifest),
                    "bytes": len(content),
                }
                save()

            def verify_replay(job):
                source = job["source"]
                original = source["source_snapshot"]
                raw = array(
                    original,
                    "raw_groups"
                    if source["selector"]["mode"] == "shared_charge_path"
                    else "raw_frames",
                )
                if raw.ndim == 4:
                    raw = raw[:, 0 if source["selector"]["readout_id"] == "hcg" else 1]
                dark = array(original, "calibration_dark_e")
                cals = [
                    {**deepcopy(c), "dark_e": dark[i]}
                    for i, c in enumerate(source["calibration_summaries"])
                ]
                cal = job["calibration_snapshot"]
                expected = isp_v3.process_raw(
                    raw,
                    cals,
                    job["profile"],
                    geometry=source["geometry"],
                    normalization_e=source["normalization_e"],
                    calibration=cal["payload"] if cal else None,
                )
                for name, value in expected["arrays"].items():
                    np.testing.assert_array_equal(array(job, name), value)
                    check("retained numerical equality " + job["id"] + " " + name, True)
                for artifact in job["artifacts"]:
                    data(artifact)
                check(
                    "source acquisition unchanged " + original["id"],
                    client.get("runs/" + original["id"]) == original,
                )

            if args.verify_only:
                for label, row in list(report["records"].items()):
                    record = preserve(label, row["route"], client.get(row["route"]))
                    if row["route"].startswith("replays/"):
                        verify_replay(record)
                report["status"] = "passed"
                report["readback_at"] = datetime.now(UTC).isoformat()
                save()
                print(
                    json.dumps(
                        {
                            "status": "passed",
                            "scope": "saved records and replay arrays readback",
                            "check_labels": len(report["checks"]),
                        }
                    )
                )
                return
            prior = json.loads(args.source_report.read_text())
            ids = prior["records"]
            old_runs = {
                family: client.get("runs/" + ids[family + "-run"])
                for family in ("hdr", "reference", "dcg")
            }
            sources = client.replay_sources()
            choices = [("hdr", None), ("reference", None), ("dcg", "hcg"), ("dcg", "lcg")]
            for family, path in choices:
                label = family + ("-" + path if path else "")
                selector = {
                    "schema": "camera_nonlinear_raw_selection_v1",
                    "mode": "shared_charge_path" if path else "sequential_frames",
                }
                if path:
                    selector["readout_id"] = path
                original = old_runs[family]
                check(
                    "discover " + label,
                    any(
                        x["source_id"] == original["id"] and x.get("selector") == selector
                        for x in sources
                    ),
                )
                cal = None
                if label == "hdr":
                    doc = client.isp_calibration_template("run", original["id"], selector=selector)
                    doc["name"] = "QA charge LSC · authored 1.05 gain"
                    grid = doc["payload"]["lsc"]
                    grid["gains"] = (np.asarray(grid["gains"]) * 1.05).tolist()
                    cal = send("charge-calibration", "isp-calibrations", doc)
                    preserve("charge-calibration", "isp-calibrations/" + cal["id"], cal)
                doc = client.advanced_isp_template(
                    "run",
                    original["id"],
                    selector=selector,
                    calibration_id=cal["id"] if cal else None,
                )
                doc["name"] = "QA charge ISP · " + label
                doc["payload"]["raw_denoise"].update(
                    mode="local_charge_variance_filter_v1", strength=0.7
                )
                doc["payload"]["linear_sharpen"].update(
                    mode="bounded_detail_v1", amount=0.3, threshold=0
                )
                model = send(label + "-isp", "models", doc)
                preserve(label + "-isp", "models/" + model["id"], model)
                request = {
                    "schema": "camera_isp_replay_v3",
                    "name": "QA retained " + label,
                    "source_kind": "run",
                    "source_id": original["id"],
                    "selector": selector,
                    "isp_model_id": model["id"],
                    "force": False,
                }
                job = send(label + "-replay", "replays", request)
                job = client.wait_replay(job["id"], timeout=180)
                check("completed replay " + label, job["status"] == "succeeded")
                preserve(label + "-replay", "replays/" + job["id"], job)
                verify_replay(job)
                bundle(label + "-replay", "replays/" + job["id"])
            # Authored fixed-condition repeats using the same registered spectral image.
            original = old_runs["hdr"]
            control = deepcopy(prior["operations"]["control"]["payload"])
            control.update(name="QA nonlinear stationary repeats", revision="charge-isp-1")
            for frame in control["payload"]["frames"]:
                frame["applied"]["exposure_ns"] = 4_000_000
            control = send("repeat-control", "control_sequences", control)
            test = client.template(
                "temporal-test",
                assembly_id=ids["assembly"],
                scene_sequence_id=ids["scene"],
                control_sequence_id=control["id"],
                capture_program_id=ids["hdr-program"],
            )
            test = send("repeat-test", "tests", test)
            request = {
                "assembly_id": ids["assembly"],
                "test_id": test["id"],
                "scene_sequence_id": ids["scene"],
                "control_sequence_id": control["id"],
                "capture_program_id": ids["hdr-program"],
                "plugin_id": "hdr_bracket_v1",
                "parameters": {"seed": 81231},
            }
            run = send("repeat-run", "runs", request)
            run = client.wait(run["id"], timeout=180)
            check("stationary capture completed", run["status"] == "succeeded")
            preserve("repeat-run", "runs/" + run["id"], run)
            selector = {"schema": "camera_nonlinear_raw_selection_v1", "mode": "sequential_frames"}
            doc = client.advanced_isp_template("run", run["id"], selector=selector)
            doc["name"] = "QA fixed four-candidate baseline"
            doc["payload"]["raw_denoise"].update(mode="local_charge_variance_filter_v1", strength=0)
            doc["payload"]["linear_sharpen"].update(mode="bounded_detail_v1", amount=0, threshold=0)
            model = send("study-baseline", "models", doc)
            preserve("study-baseline", "models/" + model["id"], model)
            spec = client.isp_study_template("run", run["id"], None, model["id"], selector=selector)
            spec["name"] = "QA four charge-domain ISP candidates"
            spec["axes"] = [
                {"parameter": "raw_denoise.strength", "values": [0, 0.7]},
                {"parameter": "linear_sharpen.amount", "values": [0, 0.3]},
            ]
            spec["datasets"][0]["stationarity"]["description"] = (
                "Exact same spectral-image hash and fixed 4 ms acquisition; authored simulation."
            )
            plan = client.plan_isp_study(spec)
            check("four candidate plan", plan["job_count"] == 4)
            (out / "study-plan.json").write_text(canonical(plan))
            study = send(
                "study",
                "isp-studies",
                {"specification": plan["specification"], "plan_hash": plan["plan_hash"]},
            )
            analysis = client.wait_isp_study(study["id"], timeout=240)
            check("four cells completed", analysis["coverage"]["succeeded"] == 4)
            study = client.get("isp-studies/" + study["id"])
            preserve("study", "isp-studies/" + study["id"], study)
            (out / "study-analysis.json").write_text(canonical(analysis))
            noise = []
            for row in analysis["rows"]:
                metric = row["metrics"]["temporal_residual_std_e"]
                check(
                    "charge metric " + row["candidate_id"],
                    metric["status"] == "ok" and metric["unit"] == "electron_equivalent",
                )
                noise.append(metric["value"])
            check("candidate settings affect measured residuals", max(noise) > min(noise))
            chosen = next(x for x in study["candidates"] if not x["is_baseline"])
            selection = send(
                "selection",
                "isp-studies/" + study["id"] + "/selection",
                {
                    "candidate_id": chosen["candidate_id"],
                    "analysis_sha256": analysis["analysis_sha256"],
                    "rationale": "QA explicit choice to exercise independent validation; "
                    "no global optimum or physical superiority claim.",
                },
            )
            preserve("selection", "isp-selections/" + selection["id"], selection)
            bundle("study", "isp-studies/" + study["id"])
            independent_fixture = deepcopy(prior["operations"]["fixture"]["payload"])
            independent_fixture.update(
                name="QA authored held-out optical input", revision="charge-independent-1"
            )
            independent_fixture["irradiance_scale"] *= 0.97
            independent_scene = send(
                "independent-scene", "scene_sequences/fixture", independent_fixture
            )
            independent_control = deepcopy(prior["operations"]["control"]["payload"])
            independent_control.update(
                name="QA held-out fixed acquisition",
                revision="charge-independent-1",
                scene_sequence_id=independent_scene["id"],
            )
            for frame in independent_control["payload"]["frames"]:
                frame["applied"]["exposure_ns"] = 4_000_000
            independent_control = send(
                "independent-control", "control_sequences", independent_control
            )
            independent_test = client.template(
                "temporal-test",
                assembly_id=ids["assembly"],
                scene_sequence_id=independent_scene["id"],
                control_sequence_id=independent_control["id"],
                capture_program_id=ids["hdr-program"],
            )
            independent_test = send("independent-test", "tests", independent_test)
            independent_request = deepcopy(request)
            independent_request.update(
                scene_sequence_id=independent_scene["id"],
                control_sequence_id=independent_control["id"],
                test_id=independent_test["id"],
                parameters={"seed": 81232},
            )
            independent = send("independent-run", "runs", independent_request)
            independent = client.wait(independent["id"], timeout=180)
            check("independent capture completed", independent["status"] == "succeeded")
            preserve("independent-run", "runs/" + independent["id"], independent)
            validation = deepcopy(spec)
            validation.update(
                name="QA fixed selection on new charge acquisition",
                phase="independent_validation",
                axes=[],
                selection_id=selection["id"],
                parent_study_id=study["id"],
            )
            validation["datasets"][0].update(
                source_id=independent["id"],
                purpose="independent_validation",
                acquisition_group_id="independent:" + independent["id"],
            )
            validation_plan = client.plan_isp_study(validation)
            validation_study = send(
                "validation-study",
                "isp-studies",
                {
                    "specification": validation_plan["specification"],
                    "plan_hash": validation_plan["plan_hash"],
                },
            )
            validation_analysis = client.wait_isp_study(validation_study["id"], timeout=240)
            check(
                "independent fixed candidates completed",
                validation_analysis["coverage"]["succeeded"] == 2,
            )
            validation_study = client.get("isp-studies/" + validation_study["id"])
            preserve("validation-study", "isp-studies/" + validation_study["id"], validation_study)
            (out / "validation-analysis.json").write_text(canonical(validation_analysis))
            bundle("validation-study", "isp-studies/" + validation_study["id"])
            for family, original in old_runs.items():
                check(
                    "original dev10 record unchanged " + family,
                    client.get("runs/" + original["id"]) == original,
                )
            report["status"] = "passed"
            report["completed_at"] = datetime.now(UTC).isoformat()
            save()
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "check_labels": len(report["checks"]),
                        "records": len(report["records"]),
                        "artifacts": len(report["artifacts"]),
                    }
                )
            )
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        save()
        raise


if __name__ == "__main__":
    main()
