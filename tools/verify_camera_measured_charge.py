#!/usr/bin/env python3
"""Actual multipart/Worker proof for imported nonlinear RAW on an owned QA server.

Owns no server lifecycle. Fixtures are authored simulated files, never measurements.
Each POST intent and response is durable; uncertain requests are never retried.
Restart readback writes a new report and cannot modify the original journal.
Explicit resume copies a failed, fully acknowledged POST prefix into a new directory.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import sqlite3
import time
import zipfile
from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from verify_camera_dcg import process_witness

from camera_platform import isp_v3
from camera_platform.evidence_package import read_bundle
from camera_platform.registry import canonical, digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def sha(content):
    return hashlib.sha256(content).hexdigest()


def original_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=3, allow_nan=False)
            .replace("\n", "\r\n") + "\r\n").encode()


def npy(array):
    stream = io.BytesIO()
    np.save(stream, array, allow_pickle=False)
    return stream.getvalue()


def artifacts(value):
    if isinstance(value, dict):
        if {"sha256", "name", "bytes"} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from artifacts(child)
    elif isinstance(value, list):
        for child in value:
            yield from artifacts(child)


def durable(path, content, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def db_snapshot(state):
    """Read physical registry rows and blob inventory, without registry initialization."""
    with sqlite3.connect((state / "registry.sqlite").as_uri() + "?mode=ro", uri=True) as conn:
        result = {}
        for table in ("records", "jobs", "audit"):
            rows = conn.execute("SELECT * FROM " + table + " ORDER BY 1,2").fetchall()
            result[table] = {"rows": len(rows), "sha256": digest(rows)}
    result["blobs"] = digest(sorted(
        (str(p.relative_to(state)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in (state / "blobs").rglob("*") if p.is_file()
    ))
    return result


class Proof:
    def __init__(self, args):
        self.args, self.state, self.out = args, args.state_dir.resolve(), args.output_dir.resolve()
        self.initial_path = self.out / "http-verification.json"
        self.binding = {
            "base_url": args.base_url.rstrip("/"), "state_dir": str(self.state),
            "environment": environment_fingerprint(),
        }
        self.cache = {}
        self.resume_operations = {}
        self.resumed_operations = set()
        self.resume_files = {}
        if args.verify_only:
            self.initial_bytes = self.initial_path.read_bytes()
            self.saved = json.loads(self.initial_bytes)
            if self.saved["status"] != "passed" or any(
                self.saved[key] != value for key, value in self.binding.items()
            ):
                raise ValueError("Readback requires the same successful frozen source and QA state")
            self.path = args.readback_output.resolve()
            if self.path.exists() or self.path == self.initial_path:
                raise ValueError("Readback requires a new separate output file")
            self.report = {
                "schema": "camera_imported_charge_http_readback_v1", **self.binding,
                "initial_report": str(self.initial_path),
                "initial_report_sha256": sha(self.initial_bytes),
                "initial_tool_sha256": self.saved["tool_sha256"],
                "checks": [], "status": "in_progress",
            }
        else:
            if self.initial_path.exists() or (self.out.exists() and any(self.out.iterdir())):
                raise ValueError("Use a new empty report directory; no implicit POST retry")
            self.path = self.initial_path
            self.report = {
                "schema": "camera_imported_charge_http_verification_v1", **self.binding,
                "status": "in_progress", "checks": [], "operations": {}, "records": {},
                "fixtures": {}, "artifacts": {}, "bundles": {}, "capture_files": {},
                "calibration_files": {}, "plans": {},
            }
            self.saved = self.report
            if args.resume_from:
                self.load_resume(args.resume_from.resolve())
        self.report.update(
            tool_sha256=sha(Path(__file__).read_bytes()),
            environment_sha256=digest(self.binding["environment"]),
            physical_validation="not_established", physical_measurement_count=0,
            numerical_scope="Same-kernel software equality; no independent physical accuracy proof",
            lifecycle_owner="caller; this tool never starts or signals API/Worker",
        )
        self.save()

    def load_resume(self, source):
        """Only acknowledged successes may be reused; uncertain mutations stop here."""
        initial_bytes = (source / "http-verification.json").read_bytes()
        prior = json.loads(initial_bytes)
        if prior["status"] != "failed" or any(
            prior[key] != value for key, value in self.binding.items()
        ):
            raise ValueError("Resume requires a failed journal for the same frozen QA state")
        for key in ("assembly_id", "test_id"):
            requested = getattr(self.args, key)
            if requested and requested != prior["selection"][key]:
                raise ValueError("Resume cannot change selected inputs: " + key)
        pins = []
        for collection in ("fixtures", "records", "artifacts", "bundles"):
            pins.extend(prior[collection].values())
        for label, operation in prior["operations"].items():
            if (operation["status"] != "response_received"
                    or not 200 <= operation.get("http_status", 0) < 300):
                raise ValueError("Resume refuses uncertain or unsuccessful POST: " + label)
            pins.extend((operation["request"], operation["response"]))
        # Verify every pin before creating any copied evidence or making any network call.
        for entry in pins:
            path = source / entry["path"]
            if not path.resolve().is_relative_to(source) or path.is_symlink():
                raise ValueError("Resume evidence path escapes its source directory")
            content = path.read_bytes()
            if sha(content) != entry["sha256"] or len(content) != entry["bytes"]:
                raise ValueError("Resume evidence has changed: " + entry["path"])
            self.resume_files[entry["path"]] = entry["sha256"]
        for label, operation in prior["operations"].items():
            request = json.loads((source / operation["request"]["path"]).read_bytes())
            value = json.loads((source / operation["response"]["path"]).read_bytes())
            if request["endpoint"] != operation["endpoint"] or not value.get("id"):
                raise ValueError("Resume requires a consistent acknowledged identity: " + label)
            if operation.get("id") not in (None, value["id"]):
                raise ValueError("Resume response identity mismatch: " + label)
            for entry in (request.get("files") or {}).values():
                if self.resume_files.get(entry["path"]) != entry["sha256"]:
                    raise ValueError("Resume upload lacks a verified fixture: " + label)
        self.resume_operations = deepcopy(prior["operations"])
        self.report = deepcopy(prior)
        self.saved = self.report
        for key in ("error", "phase", "completed_at", "check_count"):
            self.report.pop(key, None)
        self.report.update(status="in_progress", fixture_prefix=prior.get(
            "fixture_prefix", "qa-charge-" + sha(str(source).encode())[:12]), resume={
                "source_directory": str(source), "source_journal_sha256": sha(initial_bytes),
                "source_tool_sha256": prior["tool_sha256"], "reused_operations": [],
                "copied_file_sha256": dict(self.resume_files),
                "policy": "Exact requests reuse known 2xx responses; no prior POST is resent",
            })
        for relative in self.resume_files:
            durable(self.out / relative, (source / relative).read_bytes(), exclusive=True)
        durable(self.out / "resume-source-journal.json", initial_bytes, exclusive=True)

    def verify_resume_source(self):
        if not self.args.resume_from:
            return
        source = self.args.resume_from.resolve()
        self.check("original failed journal remains byte-exact", sha(
            (source / "http-verification.json").read_bytes()
        ) == self.report["resume"]["source_journal_sha256"])
        self.check("original failed evidence remains byte-exact", all(
            sha((source / relative).read_bytes()) == checksum
            for relative, checksum in self.resume_files.items()
        ))
        self.check("all prior POST responses consumed without resubmission",
                   self.resumed_operations == set(self.resume_operations))

    def save(self):
        self.report["updated_at"] = datetime.now(UTC).isoformat()
        durable(self.path, (json.dumps(self.report, ensure_ascii=False, indent=2,
                                     allow_nan=False) + "\n").encode())

    def check(self, label, condition):
        if not condition:
            raise AssertionError(label)
        if label not in self.report["checks"]:
            self.report["checks"].append(label)
        self.save()

    def progress(self, label):
        self.report["phase"] = label
        self.save()
        print(json.dumps({"phase": label, "at": datetime.now(UTC).isoformat()}), flush=True)

    def file(self, relative, content):
        if self.args.verify_only:
            raise ValueError("Readback cannot create or replace original evidence")
        path = self.out / relative
        if relative in self.resume_files:
            if (sha(content) != self.resume_files[relative]
                    or path.read_bytes() != content):
                raise ValueError("Regenerated resume evidence differs: " + relative)
            return {"path": relative, "sha256": sha(content), "bytes": len(content)}
        durable(path, content, exclusive=True)
        return {"path": relative, "sha256": sha(content), "bytes": len(content)}

    def fixture(self, label, content):
        item = self.file("fixtures/" + label, content)
        self.report["fixtures"][label] = item
        self.save()
        return item

    def preserve(self, label, route, record):
        content = canonical(record).encode()
        if self.args.verify_only:
            item = self.saved["records"][label]
            self.check("saved JSON " + label,
                       sha(content) == item["sha256"]
                       and (self.out / item["path"]).read_bytes() == content)
        else:
            item = self.file("records/" + label + ".json", content)
            self.report["records"][label] = {**item, "route": route, "id": record.get("id")}
            self.save()
        return record

    def post(self, label, endpoint, *, payload=None, files=None, status=(200, 201, 202)):
        request = {"endpoint": endpoint, "payload": payload, "files": files}
        if label in self.resume_operations and label not in self.resumed_operations:
            item = self.report["operations"][label]
            expected = (self.out / item["request"]["path"]).read_bytes()
            response = (self.out / item["response"]["path"]).read_bytes()
            if (canonical(request).encode() != expected
                    or sha(expected) != item["request"]["sha256"]
                    or sha(response) != item["response"]["sha256"]
                    or item["http_status"] not in status):
                raise ValueError("Resume request or response differs: " + label)
            value = json.loads(response)
            item.update(id=value["id"], reused_without_post=True)
            self.resumed_operations.add(label)
            self.report["resume"]["reused_operations"].append(label)
            self.check("HTTP status " + label, True)
            self.progress("reuse acknowledged POST " + label)
            return value
        if self.args.verify_only or label in self.report["operations"]:
            raise ValueError("Refusing repeated or readback POST: " + label)
        self.progress("POST " + label)
        item = {
            "request": self.file("requests/" + label + ".json", canonical(request).encode()),
            "endpoint": endpoint, "status": "intent_saved",
        }
        self.report["operations"][label] = item
        self.save()  # This journal and every uploaded byte exist before the network request.
        started = time.monotonic()
        try:
            if files:
                with ExitStack() as stack:
                    upload = {}
                    for key, entry in files.items():
                        path = self.out / entry["path"]
                        if sha(path.read_bytes()) != entry["sha256"]:
                            raise ValueError("Upload input changed after intent was saved")
                        upload[key] = (
                            path.name, stack.enter_context(path.open("rb")),
                            "application/json" if key in {"metadata_file", "evidence"}
                            else "application/octet-stream",
                        )
                    response = self.client.http.post("/api/v1/" + endpoint, files=upload)
            else:
                response = self.client.http.post("/api/v1/" + endpoint, json=payload)
        except Exception:
            item.update(status="uncertain_response_requires_manual_reconciliation",
                        elapsed_s=time.monotonic() - started)
            self.save()
            raise
        item.update(
            status="response_received", http_status=response.status_code,
            response=self.file("responses/" + label + ".json", response.content),
            elapsed_s=time.monotonic() - started,
        )
        self.save()
        self.check("HTTP status " + label, response.status_code in status)
        value = response.json()
        item["id"] = value.get("id")
        self.save()
        return value

    def readonly_post(self, label, endpoint, payload, *, expected=200, detail=None):
        before = db_snapshot(self.state)
        value = self.post(label, endpoint, payload=payload, status=(expected,))
        self.check("read-only database and blob inventory " + label,
                   db_snapshot(self.state) == before)
        if detail:
            self.check("explicit refusal " + label, detail in json.dumps(value))
        return value

    def artifact(self, artifact):
        checksum = artifact["sha256"]
        if checksum in self.cache:
            return self.cache[checksum]
        response = self.client.http.get(artifact["url"])
        response.raise_for_status()
        content = response.content
        self.check("artifact " + checksum,
                   sha(content) == checksum and len(content) == artifact["bytes"])
        if self.args.verify_only:
            entry = self.saved["artifacts"][checksum]
            self.check("saved artifact " + checksum,
                       (self.out / entry["path"]).read_bytes() == content)
        else:
            item = self.file("artifacts/" + checksum + ".bin", content)
            self.report["artifacts"][checksum] = {**item, "url": artifact["url"],
                                                 "name": artifact["name"]}
            self.save()
        self.cache[checksum] = content
        return content

    def array(self, record, name):
        matches = [a for a in record["artifacts"] if a["name"] == name + ".npy"]
        if len(matches) != 1:
            raise ValueError("Expected exactly one retained array: " + name)
        return np.load(io.BytesIO(self.artifact(matches[0])), allow_pickle=False)

    def verify_replay(self, job):
        self.progress("verify software arrays " + job["id"])
        source = job["source"]
        original = source["source_snapshot"]
        self.check("capture source schema " + job["id"],
                   source["schema"] == "camera_charge_isp_capture_source_v1"
                   and "capture_program_id" not in source and "plugin_id" not in original)
        self.check("Worker source binding " + job["id"],
                   job["environment"] == self.binding["environment"]
                   and job["status"] == "succeeded")
        raw = self.array(original, "raw-capture")
        if raw.ndim == 4:
            raw = raw[:, 0 if source["selector"]["readout_id"] == "hcg" else 1]
        rate = np.load(io.BytesIO(self.artifact(source["dark_rate_artifact"])), allow_pickle=False)
        response = source["response_calibration_snapshot"]
        origin = [a - b for a, b in zip(source["geometry"]["crop_origin"],
                                        response["geometry"]["crop_origin"], strict=True)]
        rate = rate[origin[0]:origin[0] + raw.shape[1], origin[1]:origin[1] + raw.shape[2]]
        calibrations = []
        for saved in source["calibration_summaries"]:
            cal = deepcopy(saved)
            model, acquisition = cal["model"], cal["acquisition"]
            doubling = model["dark_current_doubling_c"]
            factor = (1.0 if doubling is None else math.exp2(
                (acquisition["temperature_c"] - model["reference_temperature_c"]) / doubling))
            cal["dark_e"] = rate * factor * acquisition["exposure_s"]
            calibrations.append(cal)
        optional = job["calibration_snapshot"]
        expected = isp_v3.process_raw(
            raw, calibrations, job["profile"], geometry=source["geometry"],
            normalization_e=source["normalization_e"],
            calibration=optional["payload"] if optional else None,
        )
        for name, value in expected["arrays"].items():
            np.testing.assert_array_equal(self.array(job, name), value)
            self.check("software array equality " + job["id"] + " " + name, True)
        for artifact in job["artifacts"]:
            self.artifact(artifact)
        self.check("immutable source GET " + original["id"],
                   self.client.get("captures/" + original["id"]) == original)
        return expected["arrays"]["calibrated_photocharge_e_frames"]

    def verify_originals(self):
        artifact_names = {
            "capture_files": {
                "raw-capture.npy": "raw", "acquisition-metadata.json": "metadata_file"
            },
            "calibration_files": {"readout-calibration-metadata.json": "metadata_file",
                                  "expected-dark-rate.npy": "dark_rate",
                                  "readout-evidence.json": "evidence"},
        }
        for collection, names in artifact_names.items():
            route = "captures/" if collection == "capture_files" else "readout-calibrations/"
            for identity, files in self.saved[collection].items():
                record = self.client.get(route + identity)
                for artifact in record["artifacts"]:
                    content = self.artifact(artifact)
                    if artifact["name"] in names:
                        original = self.out / files[names[artifact["name"]]]["path"]
                        self.check("exact uploaded bytes " + identity + " " + artifact["name"],
                                   content == original.read_bytes())

    def bundle(self, label, route):
        self.progress("bundle " + label)
        manifest = self.client.get(route + "/export")
        self.preserve(label + "-export", route + "/export", manifest)
        response = self.client.http.get("/api/v1/" + route + "/bundle")
        response.raise_for_status()
        content = response.content
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            self.check("unique ZIP members " + label, len(names) == len(set(names)))
            members = {name: sha(archive.read(name)) for name in names}
            physical = sum(item.file_size for item in archive.infolist())
            header = json.loads(archive.read("manifest.json"))
            if header.get("schema") == "camera_evidence_bundle_v2":
                self.check("exact graph reconstruction " + label,
                           read_bundle(io.BytesIO(content),
                                       expected_manifest_sha256=digest(manifest)) == manifest)
            else:
                self.check("exact replay manifest " + label, header == manifest)
                required = {"artifacts/" + a["sha256"] + "/" + a["name"]
                            for a in artifacts(manifest)}
                self.check("complete replay artifact closure " + label, required <= set(names))
                for name in names:
                    if name.startswith("artifacts/"):
                        self.check("ZIP artifact " + label + " " + name,
                                   members[name] == name.split("/")[1])
        if route.startswith("isp-studies/"):
            record = manifest["study"]
            self.check("bundle within admitted capacity " + label,
                       physical <= record["resources"]["estimated_bundle_bytes"]
                       and physical <= 256 * 1024**2)
        key = "study" if route.startswith("isp-studies/") else "replay"
        self.check("bundle owns exact record " + label,
                   manifest[key] == self.client.get(route))
        self.check("bundle includes imported closure " + label,
                   bool(manifest["readout_calibrations"])
                   and bool(manifest["nonlinear_acquisition_lineage"]))
        sources = ([d["source"] for d in manifest["study"]["datasets"]]
                   if key == "study" else [manifest["replay"]["source"]])
        expected_responses = {
            response["id"] for source in sources
            for response in source["response_calibration_lineage"]["calibrations"]
        }
        self.check("bundle response ancestry identities " + label,
                   {r["id"] for r in manifest["readout_calibrations"]} == expected_responses)
        self.check("bundle owns original capture identities " + label,
                   {source["source_id"] for source in sources}
                   <= {r["id"] for r in manifest["nonlinear_acquisition_lineage"]})
        if self.args.verify_only:
            saved = self.saved["bundles"][label]
            self.check("ZIP member readback " + label,
                       members == saved["members"] and digest(manifest) == saved["manifest_sha256"])
            self.check("original ZIP retained " + label,
                       sha((self.out / saved["path"]).read_bytes()) == saved["sha256"])
        else:
            entry = self.file("bundles/" + label + ".zip", content)
            self.report["bundles"][label] = {
                **entry, "route": route, "members": members,
                "manifest_sha256": digest(manifest), "uncompressed_bytes": physical,
            }
            self.save()

    def wait_replay(self, label, queued):
        self.progress("wait Worker " + label + " " + queued["id"])
        job = self.client.wait_replay(queued["id"], timeout=900, poll_interval=1)
        self.check("Worker completed " + label, job["status"] == "succeeded")
        return self.preserve(label, "replays/" + job["id"], job)

    def wait_study(self, label, study, count):
        self.progress("wait Study " + label + " " + study["id"])
        analysis = self.client.wait_isp_study(study["id"], timeout=900, poll_interval=1)
        self.check("all Study cells succeeded " + label, analysis["coverage"]["succeeded"] == count)
        route = "isp-studies/" + study["id"]
        self.preserve(label + "-analysis", route + "/analysis", analysis)
        study = self.preserve(label, route, self.client.get(route))
        self.check("exact Study candidate count " + label, study["job_count"] == count)
        for i, cell in enumerate(study["cells"]):
            cell_route = "isp-study-cells/" + cell["job_id"]
            job = self.preserve(label + "-cell-" + str(i), cell_route, self.client.get(cell_route))
            for artifact in job["result"]["artifacts"]:
                self.artifact(artifact)
            self.check("cell retains original selected RAW " + job["id"],
                       job["result"]["raw_sha256"] == study["datasets"][0]["raw_sha256"]
                       and job["result"]["selected_array_sha256"]
                       == study["datasets"][0]["selected_array_sha256"])
        for row in analysis["rows"]:
            metric = row["metrics"]["temporal_residual_std_e"]
            self.check("electron-equivalent metric " + label + row["candidate_id"],
                       metric["status"] == "ok" and metric["unit"] == "electron_equivalent")
        return study, analysis

    def import_response(self, label, metadata):
        evidence = {
            "schema": "camera_nonlinear_response_evidence_v1", "documents": [{
                "id": identity, "role": "charge_reference",
                "source_uri": metadata["charge_reference"]["source_uri"],
                "method_and_version": metadata["charge_reference"]["method_and_version"],
                "description": "Authored HTTP fixture; no physical electron-scale certificate.",
                "tables": [],
            } for identity in metadata["charge_reference"]["evidence_document_ids"]],
        }
        files = {
            "metadata_file": self.fixture(label + ".json", original_json(metadata)),
            "dark_rate": self.fixture(label + "-dark.npy",
                                      npy(np.full(metadata["dark_rate_shape"], 2.0))),
            "evidence": self.fixture(label + "-evidence.json", original_json(evidence)),
        }
        record = self.post(label, "readout-calibrations/import", files=files)
        self.report["calibration_files"][record["id"]] = files
        self.save()
        return self.preserve(label, "readout-calibrations/" + record["id"], record)

    def import_capture(self, label, template, response, seed):
        data = deepcopy(template)
        data.update(
            name="QA authored imported " + label, source_kind="simulated",
            source_uri="fixture://measured-charge-http/" + label,
            license="Authored implementation fixture",
            method_and_version="independent-flat-codes:1",
            dataset_ids=[self.prefix + "-" + label], acquisition_group_id=self.prefix + "-" + label,
            parents=[], raw_formats=deepcopy(response["raw_formats"]),
        )
        shared = data["readout_mode"] == "shared_charge_paths"
        settings = [{
            "analog_gain": 1.0 if row["analog_gain"][0] <= 1 <= row["analog_gain"][1]
            else row["analog_gain"][0], "digital_gain": row["digital_gain"],
        } for row in response["applicability"]["readouts"]]
        data["frames"] = [{
            "frame_id": label + "-" + str(i), "raw_index": i, "frame_start_ns": None,
            "applied": {
                "exposure_ns": 4_000_000, "gain": 1.0 if shared else settings[0]["analog_gain"],
                "temperature_c": 25.0, "applied_at_ns": None, "readout_settings": settings,
            },
            "settings_evidence": {
                "status": "recorded_applied", "source_uri": "fixture://authored-applied-settings",
                "method_and_version": "authored-settings:1",
                "description": "Numerical file fixture; no physical camera was acquired.",
            },
        } for i in range(4)]
        rng = np.random.default_rng(seed)
        common = rng.integers(-8, 9, (4, 16, 16))
        raw = (np.stack((400 + common, 220 + common
                         + rng.integers(-3, 4, common.shape)), axis=1) if shared else 400 + common)
        files = {
            "metadata_file": self.fixture(label + ".json", original_json(data)),
            "raw": self.fixture(label + ".npy", npy(raw.astype(np.uint16))),
        }
        record = self.post(label, "captures/nonlinear/import", files=files)
        self.report["capture_files"][record["id"]] = files
        self.save()
        return self.preserve(label, "captures/" + record["id"], record)

    def selection(self, response, path=None):
        return {
            "schema": "camera_nonlinear_raw_selection_v2",
            "mode": "shared_charge_path" if path else "sequential_frames",
            "readout_calibration_id": response["id"], **({"readout_id": path} if path else {}),
        }

    def initial(self):
        self.prefix = self.report.setdefault(
            "fixture_prefix", "qa-charge-" + sha(str(self.out).encode())[:12])
        chosen = self.report.get("selection", {}) if self.args.resume_from else {}
        assembly_id = chosen.get("assembly_id", self.args.assembly_id)
        test_id = chosen.get("test_id", self.args.test_id)
        assemblies, tests = self.client.get("assemblies"), self.client.get("tests")
        assembly = next((r for r in assemblies if r["id"] == assembly_id), None)
        if assembly_id and assembly is None:
            raise ValueError("Explicit assembly does not exist")
        assembly = assembly or sorted(assemblies, key=lambda r: r["id"])[0]
        test = next((r for r in tests if r["id"] == test_id), None)
        if test_id and test is None:
            raise ValueError("Explicit test does not exist")
        test = test or sorted(tests, key=lambda r: r["id"])[0]
        self.preserve("input-assembly", "assemblies/" + assembly["id"], assembly)
        self.preserve("input-test", "tests/" + test["id"], test)
        sensor = next(self.client.get("models/" + identity) for identity in assembly["model_ids"]
                      if self.client.get("models/" + identity)["kind"] == "sensor")
        self.preserve("input-sensor", "models/" + sensor["id"], sensor)
        self.report["selection"] = {"assembly_id": assembly["id"], "test_id": test["id"],
                                    "sensor_model_id": sensor["id"]}
        self.save()
        responses, captures, templates, metadata = {}, {}, {}, {}
        for label, mode in (("seq", "sequential_frames"), ("paired", "shared_charge_paths")):
            template = self.client.template(
                "nonlinear-capture", assembly_id=assembly["id"], test_id=test["id"],
                readout_mode=mode,
            )
            self.fixture(label + "-unknown-template.json", original_json(template))
            self.check("capture template remains unknown " + label,
                       template["clock"]["status"] == "unknown"
                       and template["timing_mode"]["shutter"] == "unknown"
                       and template["frames"][0]["settings_evidence"]["status"] == "unknown"
                       and template["frames"][0]["applied"]["exposure_ns"] is None)
            templates[label] = template
            data = self.client.template(
                "readout-calibration", sensor_model_id=sensor["id"], mode=mode)
            self.fixture(label + "-response-template.json", original_json(data))
            data.update(name="QA authored response " + label, dark_rate_shape=[16, 16],
                        dataset_ids=[self.prefix + "-response-training-" + label])
            metadata[label] = data
            responses[label] = self.import_response(label + "-response", data)
            captures[label] = self.import_capture(
                label + "-capture", template, responses[label], 92121 if label == "seq" else 92122)
        alternative = deepcopy(metadata["seq"])
        alternative.update(name="QA alternate response for identical RAW", revision="2",
                           parents=[responses["seq"]["id"]])
        curve = alternative["electrical_model"]["charge_voltage_response"]
        curve["signal_voltage_v"] = [value * 0.9 for value in curve["signal_voltage_v"]]
        responses["alternative"] = self.import_response("alternative-response", alternative)
        profiles, jobs, charge = {}, {}, {}
        for label, family, path, response_key in (
            ("seq", "seq", None, "seq"), ("hcg", "paired", "hcg", "paired"),
            ("lcg", "paired", "lcg", "paired"), ("alternative", "seq", None, "alternative"),
        ):
            selected = self.selection(responses[response_key], path)
            capture = captures[family]
            if label == "alternative":
                model = profiles["seq"]
            else:
                draft = self.client.advanced_isp_template(
                    "capture", capture["id"], selector=selected)
                draft["name"] = "QA imported RAW ISP " + label
                draft["payload"]["raw_denoise"].update(
                    mode="local_charge_variance_filter_v1", strength=0.2)
                model = self.post(label + "-profile", "models", payload=draft)
                self.preserve(label + "-profile", "models/" + model["id"], model)
                profiles[label] = model
            queued = self.post(label + "-replay", "replays", payload={
                "schema": "camera_isp_replay_v3", "name": "QA imported " + label,
                "source_kind": "capture", "source_id": capture["id"],
                "selector": selected, "isp_model_id": model["id"], "force": False,
            })
            job = self.wait_replay(label + "-replay", queued)
            jobs[label] = job
            charge[label] = self.verify_replay(job)
            self.bundle(label + "-replay", "replays/" + job["id"])
        self.check("alternate calibration preserves same RAW and profile",
                   jobs["seq"]["source"]["raw_sha256"]
                   == jobs["alternative"]["source"]["raw_sha256"]
                   and jobs["seq"]["profile"] == jobs["alternative"]["profile"])
        self.check("alternate response changes interpretation",
                   not np.array_equal(charge["seq"], charge["alternative"], equal_nan=True))
        self.check("paired selections retain shared original exposure tokens",
                   jobs["hcg"]["source"]["acquisition_lineage"]["exposure_tokens"]
                   == jobs["lcg"]["source"]["acquisition_lineage"]["exposure_tokens"])
        selected = self.selection(responses["paired"], "hcg")
        specification = self.client.isp_study_template(
            "capture", captures["paired"]["id"], None, profiles["hcg"]["id"], selector=selected)
        specification["name"] = "QA four imported RAW candidates"
        specification["axes"] = [{"parameter": "raw_denoise.strength", "values": [0.0, 0.4, 0.8]}]
        specification["datasets"][0]["stationarity"]["description"] = (
            "Authored fixed flat scene plus independent code perturbations; declared stationarity."
        )
        plan = self.readonly_post("study-plan", "isp-studies/plan", specification)
        self.check("exact four candidate admission", plan["job_count"] == 4)
        self.report["plans"]["study"] = {
            "plan_hash": plan["plan_hash"], "resources": plan["resources"],
        }
        self.save()
        study = self.post("study", "isp-studies", payload={
            "specification": plan["specification"], "plan_hash": plan["plan_hash"],
        })
        study, analysis = self.wait_study("study", study, 4)
        chosen = next(c for c in study["candidates"] if not c["is_baseline"])
        selection = self.post("selection", "isp-studies/" + study["id"] + "/selection", payload={
            "candidate_id": chosen["candidate_id"], "analysis_sha256": analysis["analysis_sha256"],
            "rationale": "Explicit QA choice; not an optimization or physical superiority claim.",
        })
        self.preserve("selection", "isp-selections/" + selection["id"], selection)
        validation = deepcopy(specification)
        validation.update(phase="independent_validation", axes=[], selection_id=selection["id"],
                          parent_study_id=study["id"])
        validation["datasets"][0]["purpose"] = "independent_validation"
        self.readonly_post("same-RAW-refusal", "isp-studies/plan", validation,
                           expected=422, detail="overlaps")
        sibling = deepcopy(validation)
        sibling["datasets"][0]["selector"]["readout_id"] = "lcg"
        self.readonly_post("sibling-path-refusal", "isp-studies/plan", sibling,
                           expected=422, detail="selected path")
        fitted = deepcopy(metadata["paired"])
        fitted.update(name="QA explicit fit-on-target refusal", revision="fit-on-target",
                      reference_capture_ids=[captures["paired"]["id"]])
        fitted_response = self.import_response("fit-on-target-response", fitted)
        leakage = deepcopy(specification)
        leakage["datasets"][0]["selector"] = self.selection(fitted_response, "hcg")
        self.readonly_post("decoder-fit-refusal", "isp-studies/plan", leakage,
                           expected=422, detail="calibration fitting")
        holdout = self.import_capture(
            "holdout-capture", templates["paired"], responses["paired"], 92123)
        validation.update(name="QA selected ISP on independent imported capture")
        validation["datasets"][0].update(
            source_id=holdout["id"], acquisition_group_id=holdout["acquisition_group_id"])
        plan = self.readonly_post("validation-plan", "isp-studies/plan", validation)
        self.check("fixed two-cell validation admission", plan["job_count"] == 2)
        self.report["plans"]["validation"] = {
            "plan_hash": plan["plan_hash"], "resources": plan["resources"],
        }
        self.save()
        heldout = self.post("validation", "isp-studies", payload={
            "specification": plan["specification"], "plan_hash": plan["plan_hash"],
        })
        heldout, _ = self.wait_study("validation", heldout, 2)
        self.check("holdout has distinct RAW and same processing binding",
                   heldout["datasets"][0]["raw_sha256"] != study["datasets"][0]["raw_sha256"]
                   and heldout["datasets"][0]["processing_binding"]
                   == study["datasets"][0]["processing_binding"])
        self.bundle("study", "isp-studies/" + study["id"])
        self.bundle("validation", "isp-studies/" + heldout["id"])
        self.verify_originals()
        for label, entry in list(self.report["records"].items()):
            self.check("final immutable JSON " + label,
                       sha(canonical(self.client.get(entry["route"])).encode()) == entry["sha256"])
        self.report["final_db_snapshot"] = db_snapshot(self.state)
        self.save()

    def readback(self):
        self.progress("verify original saved file pins")
        for collection in ("fixtures", "records", "artifacts"):
            for label, entry in self.saved[collection].items():
                self.check("retained file " + collection + " " + label,
                           sha((self.out / entry["path"]).read_bytes()) == entry["sha256"])
        for label, operation in self.saved["operations"].items():
            for key in ("request", "response"):
                entry = operation[key]
                self.check("retained POST " + label + " " + key,
                           sha((self.out / entry["path"]).read_bytes()) == entry["sha256"])
        self.check("restart database rows and blob inventory",
                   db_snapshot(self.state) == self.saved["final_db_snapshot"])
        for label, entry in self.saved["records"].items():
            value = self.preserve(label, entry["route"], self.client.get(entry["route"]))
            if entry["route"].startswith("replays/") and not entry["route"].endswith("/export"):
                self.verify_replay(value)
        self.verify_originals()
        for entry in self.saved["artifacts"].values():
            self.artifact(entry)
        for label, entry in self.saved["bundles"].items():
            self.bundle(label, entry["route"])
        self.check("readback is read-only",
                   db_snapshot(self.state) == self.saved["final_db_snapshot"])
        self.check("initial journal remains byte-exact",
                   self.initial_path.read_bytes() == self.initial_bytes)

    def run(self):
        try:
            with PlatformClient(self.binding["base_url"], timeout=900) as client:
                self.client = client
                health = client.get("health")
                self.check("isolated healthy QA state and supervised Worker",
                           Path(health["state_dir"]).resolve() == self.state
                           and health["worker"] == "running")
                port = urlsplit(self.binding["base_url"]).port
                self.report["process_witness"] = process_witness(
                    port, self.args.api_pid, self.state)
                self.save()
                self.readback() if self.args.verify_only else self.initial()
                self.verify_resume_source()
                self.report["final_process_witness"] = process_witness(
                    port, self.args.api_pid, self.state)
                self.report.update(status="passed", completed_at=datetime.now(UTC).isoformat(),
                                   check_count=len(self.report["checks"]))
                self.save()
                print(json.dumps({"status": "passed", "checks": len(self.report["checks"]),
                                  "report": str(self.path)}), flush=True)
        except Exception as exc:
            self.report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            self.save()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--api-pid", required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--assembly-id")
    parser.add_argument("--test-id")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--readback-output", type=Path)
    parser.add_argument("--resume-from", type=Path,
                        help="Copy a failed, acknowledged POST prefix into a new output directory")
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if (address.scheme != "http" or address.hostname != "127.0.0.1"
            or address.port in (None, 8770) or address.path not in ("", "/")
            or address.query or address.fragment or address.username or address.password):
        parser.error("Use a separate explicit loopback QA port, never primary8770")
    state, out = args.state_dir.resolve(), args.output_dir.resolve()
    if ("qa" not in state.name.lower() or state == out or state in out.parents
            or out in state.parents or not (state / "registry.sqlite").is_file()):
        parser.error("Require an existing separate QA state and report directory")
    if args.verify_only != bool(args.readback_output):
        parser.error("--verify-only requires --readback-output; initial run must omit it")
    if args.resume_from:
        original = args.resume_from.resolve()
        if (args.verify_only or original == out or original in out.parents
                or out in original.parents or original == state or state in original.parents
                or not (original / "http-verification.json").is_file()):
            parser.error("Resume requires a separate original journal and a new output directory")
    if args.verify_only and (
        args.readback_output.resolve().is_relative_to(out)
        or args.readback_output.resolve().is_relative_to(state)
    ):
        parser.error("Readback report must be outside initial evidence and the QA state")
    Proof(args).run()


if __name__ == "__main__":
    main()
