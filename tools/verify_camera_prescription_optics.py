#!/usr/bin/env python3
"""Verify prescription optics through an existing QA HTTP server and its Worker.

Creates explicit synthetic engineering records, never physical measurements.
Requires the exact existing QA state and refuses the main camera-platform state.
The report checkpoints every POST intent before sending and its returned ID
immediately. A repeated invocation observes saved IDs. An interrupted POST is
reconciled by its frozen request; an unconfirmed submission is never repeated.
This tool does not start, stop, cancel, or replace any server or Worker job.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import importlib
import io
import json
import math
import os
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import numpy as np

from camera_platform import __version__, prescription_psf
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import CONDITIONS, PROVENANCE
from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint

SCHEMA = "camera_prescription_optics_verification_v1"
STATIC_FILES = ("index.html", "app.js", "scenes.js", "workbench.js", "style.css")
PARAMETERS = {"frames": 2, "seed": 41}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def stamp():
    return datetime.now(UTC).isoformat()


def endpoint(collection, identity):
    return collection + "/" + quote(identity, safe="")


def assert_qa_state(state, workspace):
    state, workspace = Path(state).resolve(), Path(workspace).resolve()
    require(state.is_dir(), "Expected QA state directory does not exist")
    require(state != workspace / "outputs" / "camera-platform", "Refusing main platform state")
    require(
        not (state.name == "camera-platform" and state.parent.name == "outputs"),
        "Refusing main outputs/camera-platform state",
    )


def installed_binding(runtime_root):
    """Prove these already imported contracts came from the selected package.

    Import selection is intentionally not changed here: replacing sys.modules
    cannot repair references that another verifier already imported. The installed
    verifier invokes this tool in a fresh isolated Python process instead.
    """
    from start_camera_platform import verify_manifest

    root = Path(runtime_root).resolve()
    manifest = verify_manifest(root)
    names = (
        "camera_platform",
        "camera_platform.prescription_psf",
        "camera_platform.service",
        "camera_platform.contracts",
        "camera_platform.sdk",
        "camera_platform.registry",
        "pyisetcam",
    )
    modules = {name: importlib.import_module(name) for name in names}
    paths = {name: str(Path(module.__file__).resolve()) for name, module in modules.items()}
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] not in {"camera_platform", "pyisetcam"}:
            continue
        path = getattr(module, "__file__", None)
        require(
            path is not None and Path(path).resolve().is_relative_to(root),
            f"Cached module {name} is outside the selected installed runtime; "
            "use a fresh isolated Python client",
        )
    require(
        prescription_psf is modules["camera_platform.prescription_psf"]
        and environment_fingerprint is modules["camera_platform.service"].environment_fingerprint
        and ModelCreate is modules["camera_platform.contracts"].ModelCreate
        and AssemblyCreate is modules["camera_platform.contracts"].AssemblyCreate
        and PlatformClient is modules["camera_platform.sdk"].PlatformClient
        and digest is modules["camera_platform.registry"].digest,
        "Verifier retains stale development import references",
    )
    environment = environment_fingerprint()
    require(
        environment == manifest["environment"], "Installed client environment differs from manifest"
    )
    return {
        "mode": "verified_installed_package",
        "runtime_root": str(root),
        "wheel_sha256": manifest["wheel_sha256"],
        "manifest_sha256": hashlib.sha256(
            (root / "runtime-manifest.json").read_bytes()
        ).hexdigest(),
        "environment_sha256": digest(environment),
        "module_paths": paths,
    }


class Report:
    def __init__(self, output, base_url, expected_state, *, read_only=False):
        self.output = Path(output).resolve()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.output.with_suffix(self.output.suffix + ".lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Another verifier is using this report") from None
        inputs = {"base_url": base_url.rstrip("/"), "state_dir": str(expected_state.resolve())}
        if self.output.exists():
            self.data = json.loads(self.output.read_text())
            require(self.data.get("schema") == SCHEMA, "Existing report schema differs")
            require(self.data.get("inputs") == inputs, "Existing report pins different inputs")
        else:
            self.data = {
                "schema": SCHEMA,
                "inputs": inputs,
                **inputs,
                "started_at": stamp(),
                "tag": uuid.uuid4().hex[:12],
                "records": {},
                "operations": {},
                "checks": [],
                "source_kind": "simulated",
                "physical_validation": "not_established",
                "total_uncertainty": {"kind": "unknown"},
            }
        if read_only:
            require(
                self.data.get("status") == "passed" or self.data.get("first_verified_at"),
                "Read-only recheck needs a previously completed report",
            )
        if self.data.get("status") == "passed":
            self.data.setdefault("first_verified_at", self.data.get("verified_at") or stamp())
        invocation = {
            "started_at": stamp(),
            "read_only": read_only,
            "post_count": 0,
            "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        self.data.setdefault("invocations", []).append(invocation)
        self.data["latest_invocation"] = invocation
        self.data.update(status="running")
        self.data.pop("failure", None)
        self.data.pop("verified_at", None)
        self.save()

    def save(self):
        self.data["updated_at"] = stamp()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.output.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(self.data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def pin(self, key, value):
        require(key not in self.data or self.data[key] == value, f"Frozen evidence changed: {key}")
        self.data[key] = copy.deepcopy(value)
        self.save()

    def identity(self, key, identity):
        previous = self.data["records"].get(key)
        require(previous is None or previous == identity, f"Refusing to replace saved {key} ID")
        self.data["records"][key] = identity
        self.save()

    def check(self, name, condition=True):
        require(condition, name)
        if not any(c["name"] == name for c in self.data["checks"]):
            self.data["checks"].append({"name": name, "status": "passed"})
        self.save()

    def acquire(self, client, key, collection, post_path, payload, matches=None):
        """Observe, reconcile, or submit once; never retry an uncertain mutation."""
        if matches is None:
            matches = lambda record: all(  # noqa: E731
                record.get(k) == v for k, v in payload.items() if k != "force"
            )
        intent = {"collection": collection, "post_path": post_path, "payload": payload}
        previous = self.data["operations"].get(key)
        if previous is not None:
            require(previous["intent"] == intent, f"Frozen {key} request changed")
        if key in self.data["records"]:
            record = client.get(endpoint(collection, self.data["records"][key]))
        elif self.data["latest_invocation"]["read_only"]:
            raise RuntimeError(f"Read-only recheck has no saved {key} ID; refusing mutation")
        elif previous is not None:
            # The previous HTTP call may have succeeded even without a response.
            # Adopt one proven matching record, or stop without another POST.
            candidates = [r for r in client.get(collection) if matches(r)]
            require(
                len(candidates) == 1,
                f"Unconfirmed {key} POST has {len(candidates)} matches; no resubmission. "
                "Inspect the saved intent and server state before continuing.",
            )
            record = candidates[0]
            self.identity(key, record["id"])
            previous.update(status="reconciled", confirmed_at=stamp())
            self.save()
        else:
            self.data["operations"][key] = {
                "intent": copy.deepcopy(intent),
                "status": "submitting",
                "submitted_at": stamp(),
            }
            self.data["latest_invocation"]["post_count"] += 1
            self.save()
            record = client.post(post_path, payload)
            self.identity(key, record["id"])  # Checkpoint before any further check or GET.
            self.data["operations"][key].update(status="confirmed", confirmed_at=stamp())
            self.save()
        require(matches(record), f"Saved {key} record differs from the frozen request")
        return record


def fetch(client, path):
    response = client.http.get(path)
    response.raise_for_status()
    return response.content


def static_hashes(client):
    return {name: hashlib.sha256(fetch(client, "/" + name)).hexdigest() for name in STATIC_FILES}


class Artifacts:
    def __init__(self, client):
        self.client, self.cache = client, {}

    def content(self, artifact):
        checksum = artifact["sha256"]
        require(
            len(checksum) == 64 and all(c in "0123456789abcdef" for c in checksum),
            "Malformed artifact hash",
        )
        require(artifact["id"] == checksum, "Artifact ID differs from its SHA-256")
        if checksum not in self.cache:
            self.cache[checksum] = fetch(self.client, "/api/v1/artifacts/" + checksum)
        content = self.cache[checksum]
        require(len(content) == artifact["bytes"], "Artifact byte count differs")
        require(hashlib.sha256(content).hexdigest() == checksum, "Artifact SHA-256 differs")
        return content

    def array(self, record, name):
        matches = [a for a in record["artifacts"] if a["name"] == name]
        require(
            bool(matches) and len({a["sha256"] for a in matches}) == 1,
            f"Missing or ambiguous {name}",
        )
        value = np.load(io.BytesIO(self.content(matches[0])), allow_pickle=False)
        require(
            isinstance(value, np.ndarray) and np.isfinite(value).all(),
            f"Nonfinite numerical artifact: {name}",
        )
        return value

    def verify_record(self, record):
        values = {}
        for artifact in [*record.get("input_artifacts", []), *record.get("artifacts", [])]:
            self.content(artifact)
            values[artifact["name"] + ":" + artifact["sha256"]] = artifact["bytes"]
        return values


def poll(client, collection, identity, report, timeout):
    deadline = time.monotonic() + timeout
    while True:
        job = client.get(endpoint(collection, identity))
        observation = {"collection": collection, "id": identity, "status": job["status"]}
        if report.data.get("last_job_observation") != observation:
            report.data["last_job_observation"] = observation
            report.save()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            require(
                job["status"] == "succeeded",
                f"Saved job {identity} is {job['status']}: {job.get('error')}",
            )
            return job
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Observe existing {collection}/{identity} again using the same "
                f"report {report.output}; no replacement job was submitted"
            )
        time.sleep(0.5)


def fields(record, contract):
    return {k: copy.deepcopy(v) for k, v in record.items() if k in contract.model_fields}


def bundle_proof(client, collection, job, expected_jobs, expected_models, artifacts):
    content = fetch(client, "/api/v1/" + endpoint(collection, job["id"]) + "/bundle")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        keys = (
            ("run", "model_lineage", "factory_jobs")
            if collection == "runs"
            else ("replay", "source", "model_lineage", "factory_jobs")
        )
        require(
            manifest["package_hash"] == digest({k: manifest[k] for k in keys}),
            "ZIP evidence package hash differs",
        )
        require(
            manifest["run" if collection == "runs" else "replay"] == job, "ZIP frozen job differs"
        )
        jobs = {r["id"]: r for r in manifest["factory_jobs"]}
        models = {r["id"]: r for r in manifest["model_lineage"]}
        for record in expected_jobs:
            require(jobs.get(record["id"]) == record, "ZIP lost original factory evidence")
        for record in expected_models:
            require(models.get(record["id"]) == record, "ZIP lost parent model lineage")
        records = [job, *expected_jobs]
        if collection == "replays":
            records.append(manifest["source"])
        entries = {
            (a["sha256"], a["name"]): a
            for r in records
            for a in [*r.get("input_artifacts", []), *r["artifacts"]]
        }
        for (checksum, name), artifact in entries.items():
            original = artifacts.content(artifact)
            require(
                archive.read("artifacts/" + checksum + "/" + name) == original,
                f"ZIP original artifact changed: {name}",
            )
    return {
        "download_sha256": hashlib.sha256(content).hexdigest(),
        "package_hash": manifest["package_hash"],
        "artifacts_verified": len(entries),
    }


def verify(client, report, timeout, *, expected_runtime_root=None):
    if expected_runtime_root is not None:
        report.pin("runtime_binding", installed_binding(expected_runtime_root))
    state = report.data["state_dir"]
    health, session, bootstrap = (client.get(k) for k in ("health", "session", "bootstrap"))
    assert_qa_state(state, health["workspace"])
    for response in (health, session, bootstrap):
        require(response["state_dir"] == state, "Server is not the exact expected QA state")
        require(response["schema"] == "camera_platform_v1", "Unexpected server schema")
    require(health["status"] == "ok", "QA server is not healthy")
    require(
        session["version"] == bootstrap["version"] == __version__,
        "Verifier and running server versions differ",
    )
    report.pin("running_version", session["version"])
    report.pin("static_sha256", static_hashes(client))
    report.pin("environment", environment_fingerprint())
    report.data["latest_invocation"]["counts_before"] = copy.deepcopy(bootstrap["counts"])
    report.data["server"] = {"workspace": health["workspace"], "worker": health["worker"]}
    report.check("Exact QA state, main-state exclusion, version and source binding verified")

    ids, tag = report.data["records"], report.data["tag"]
    if "source_assembly" not in ids:
        models = {m["id"]: m for m in bootstrap["models"]}
        choices = [
            a
            for a in bootstrap["assemblies"]
            if a["conditions"] == CONDITIONS
            and all(models[i].get("source_uri") == PROVENANCE["source_uri"] for i in a["model_ids"])
        ]
        require(bool(choices), "QA state needs the original declared engineering fixture")
        report.identity("source_assembly", sorted(choices, key=lambda a: a["id"])[0]["id"])
    original = client.get(endpoint("assemblies", ids["source_assembly"]))
    source_models = [client.get(endpoint("models", i)) for i in original["model_ids"]]
    by_kind = {m["kind"]: m for m in source_models}
    report.pin("source_assembly_sha256", digest(original))
    report.pin("source_model_sha256", {m["id"]: digest(m) for m in source_models})
    require(by_kind["sensor"]["payload"]["pixel_pitch_um"] == 3.0, "Fixture pitch differs")

    psf_payload = client.get("templates/factory?plugin_id=prescription_psf_v1")
    require(
        psf_payload["request"] == prescription_psf.request_template(),
        "Server prescription default differs from the bound local implementation",
    )
    psf_payload.update(name="Prescription optics QA " + tag, asset_id=by_kind["lens"]["asset_id"])
    report.pin("prescription_request", psf_payload["request"])
    ri_payload = client.get("templates/factory?plugin_id=rayoptics_lens_v1")
    ri_payload.update(
        name="Prescription radiometry QA " + tag,
        asset_id=psf_payload["asset_id"],
        request=copy.deepcopy(psf_payload["request"]["prescription"]),
    )
    ri = report.acquire(client, "radiometry", "factory/jobs", "factory/jobs", ri_payload)
    ri = poll(client, "factory/jobs", ri["id"], report, timeout)
    psf = report.acquire(client, "psf", "factory/jobs", "factory/jobs", psf_payload)
    psf = poll(client, "factory/jobs", psf["id"], report, timeout)
    artifacts = Artifacts(client)
    artifact_proofs = {}
    for key, job in (("radiometry", ri), ("psf", psf)):
        require(job["environment"] == report.data["environment"], "Factory environment differs")
        require(job["result"]["summary"]["status"] == "PROPOSED", f"{key} refused its request")
        require(job["result"]["physical_validation"] == "not_established", "Physical claim changed")
        require(
            job["result"]["summary"]["uncertainty"] == {"kind": "unknown"},
            "Factory uncertainty changed",
        )
        artifact_proofs[key] = artifacts.verify_record(job)
        report.pin(key + "_summary", job["result"]["summary"])
        report.pin(key + "_record_sha256", digest(job))
    require(
        ri["request"] == psf["request"]["prescription"], "Factories used different prescriptions"
    )
    summary = psf["result"]["summary"]
    require(
        summary["propagation_phase"] == "exp(-2*pi*i*opd_nm/wavelength_nm)",
        "Prescription OPD phase convention changed",
    )
    require(
        any("transport is not solved" in s for s in summary["assumptions"]),
        "Uniform-amplitude transport assumption disappeared",
    )
    require(
        any("Jacobian" in s for s in summary["limitations"]),
        "Radiometric composition approximation disappeared",
    )
    opd = artifacts.array(psf, "fine_opd_nm.npy")
    mask = artifacts.array(psf, "fine_admitted.npy")
    require(
        opd.shape == mask.shape and mask.dtype == bool and np.max(np.abs(opd)) > 1,
        "Actual prescription OPD or clipping masks are missing",
    )
    kernel = artifacts.array(psf, "psf_kernel.npy")
    require(kernel.ndim == 5 and kernel.shape[:3] == (2, 3, 3), "Unexpected field PSF axes")
    np.testing.assert_allclose(kernel.sum(axis=(-1, -2)), 1, atol=1e-12)
    report.check("Actual Worker radiometry and signed prescription field PSF share unchanged input")
    report.check(
        "Factory original artifacts, numeric gates and physical approximation limits verified"
    )

    parent_payload = {
        "job_id": ri["id"],
        "name": "Same prescription RI QA " + tag,
        "revision": "qa-1",
        "conditions": original["conditions"],
    }
    parent = report.acquire(
        client,
        "parent_lens",
        "models",
        "factory/models",
        parent_payload,
        lambda m: (
            m.get("factory_job_id") == ri["id"]
            and m.get("name") == parent_payload["name"]
            and m.get("factory_application_conditions") == original["conditions"]
        ),
    )
    conditions = {
        **copy.deepcopy(original["conditions"]),
        "f_number": {"value": summary["geometric_reference_f_number"], "unit": "1"},
    }
    composition = {
        "job_id": psf["id"],
        "base_lens_model_id": parent["id"],
        "name": "Prescription field optics QA " + tag,
        "revision": "qa-1",
        "conditions": conditions,
        "condition_scope": "optical_response",
    }
    lens = report.acquire(
        client,
        "lens",
        "models",
        "factory/models",
        composition,
        lambda m: (
            m.get("psf_factory_job_id") == psf["id"]
            and m.get("parents") == [parent["id"]]
            and m.get("name") == composition["name"]
            and m.get("factory_application_conditions") == conditions
        ),
    )
    require(
        lens["source_kind"] == "simulated" and lens["uncertainty"] == {"kind": "unknown"},
        "Composite source or uncertainty changed",
    )
    require(lens["payload"]["psf"] == psf["result"]["model_candidate"], "Published PSF changed")
    require(lens["payload"]["field_reference_radius_mm"] == 0.75, "Physical RI radius changed")
    report.check("Published optics retain the exact radiometry parent and physical field scale")

    sensor_payload = fields(by_kind["sensor"], ModelCreate)
    sensor_payload.update(name="Declared 128px sensor QA " + tag, parents=[by_kind["sensor"]["id"]])
    wave = by_kind["sensor"]["payload"]["wavelength_nm"]
    wanted = psf["request"]["prescription"]["wavelength_nm"]
    require(
        wanted == [500, 600] and all(w in wave for w in wanted), "Fixture wavelength grid differs"
    )
    indices = [wave.index(w) for w in wanted]
    sensor_payload["spectral_range"] = {"min": 500, "max": 600, "unit": "nm"}
    sensor_payload["payload"]["wavelength_nm"] = wanted
    sensor_payload["payload"]["qe"] = [
        [row[i] for i in indices] for row in sensor_payload["payload"]["qe"]
    ]
    angular = sensor_payload["payload"]["sensor_angular_response"]
    angular["relative_response"] = [
        [row[i] for i in indices] for row in angular["relative_response"]
    ]
    for dimension in ("active_height", "active_width"):
        sensor_payload["properties"][dimension] = {"value": 0.384, "unit": "mm"}
    sensor = report.acquire(client, "sensor", "models", "models", sensor_payload)
    assembly_payload = fields(original, AssemblyCreate)
    assembly_payload.update(
        name="Prescription optics camera QA " + tag,
        model_ids=[lens["id"], sensor["id"], by_kind["isp"]["id"]],
        conditions=conditions,
        spectral_range=sensor_payload["spectral_range"],
    )
    assembly = report.acquire(client, "assembly", "assemblies", "assemblies", assembly_payload)
    require(assembly["compatibility"]["status"] == "PASS", "Assembled camera is incompatible")
    report.check(
        "Declared 128 by 128 sensor at 3 micrometres and fixed 500/600 nm assembly verified"
    )

    fixture = client.get("templates/spatial-scene?" + urlencode({"assembly_id": assembly["id"]}))
    fixture.update(name="Prescription spectral chart QA " + tag)
    require(fixture["shape"] == [64, 64], "Default fixture dimensions changed")
    image = report.acquire(
        client,
        "image",
        "spectral_images",
        "spectral_images/fixture",
        fixture,
        lambda r: (
            r.get("name") == fixture["name"]
            and r.get("conditions") == conditions
            and r.get("wavelength_nm") == wanted
        ),
    )
    require(image["sampling"]["full_sensor_shape"] == [128, 128], "Native sensor grid changed")
    require(image["sampling"]["crop_origin"] == [32, 32], "Spectral crop position changed")
    spec = client.get(
        "templates/spatial-test?"
        + urlencode({"assembly_id": assembly["id"], "scene_id": image["id"]})
    )
    spec["name"] = "Prescription RAW channel mean QA " + tag
    test = report.acquire(client, "test", "tests", "tests", spec)
    camera_payload = {
        "assembly_id": assembly["id"],
        "test_id": test["id"],
        "scene_id": image["id"],
        "plugin_id": "spatial_scene_v1",
        "parameters": PARAMETERS,
        "force": False,
    }
    camera = report.acquire(client, "run", "runs", "runs", camera_payload)
    camera = poll(client, "runs", camera["id"], report, timeout)
    require(
        camera["environment"] == report.data["environment"], "Camera Worker environment changed"
    )
    require(camera["assembly_snapshot"] == assembly, "Camera did not retain its assembly")
    require(camera["result"]["psf"]["spatial_model"] == "field_grid", "Camera bypassed field PSF")
    require(
        camera["result"]["radial_response_sampling"]["field_reference_radius_mm"] == 0.75,
        "Camera stretched physical RI coverage to sensor bounds",
    )
    require(
        any("separable approximation" in s for s in camera["result"]["limitations"]),
        "Camera omitted separable optical transport limitation",
    )
    raw, rgb = (artifacts.array(camera, name) for name in ("raw_frames.npy", "rgb_frames.npy"))
    require(raw.shape == (2, 64, 64) and raw.std() > 10, "RAW frames are missing or degenerate")
    require(rgb.shape == (2, 64, 64, 3), "RGB frame dimensions changed")
    report.check("HTTP/Worker field optics generate actual nondegenerate 64 by 64 RAW and RGB")

    isp_payload = client.get("templates/isp-profile?" + urlencode({"assembly_id": assembly["id"]}))
    isp_payload["name"] = "Explicit WB replay profile QA " + tag
    isp_payload["payload"]["white_balance"]["gains_rgb"] = [1.4, 1.0, 0.8]
    isp = report.acquire(client, "replay_isp", "models", "models", isp_payload)
    replay_payload = {
        "name": "Preserved prescription RAW QA " + tag,
        "source_kind": "run",
        "source_id": camera["id"],
        "isp_model_id": isp["id"],
        "force": False,
    }
    replay = report.acquire(client, "replay", "replays", "replays", replay_payload)
    replay = poll(client, "replays", replay["id"], report, timeout)
    require(replay["environment"] == report.data["environment"], "Replay environment changed")
    np.testing.assert_array_equal(artifacts.array(replay, "raw_frames.npy"), raw)
    require(
        not np.array_equal(artifacts.array(replay, "rgb_frames.npy"), rgb),
        "Changed WB did not change replay RGB",
    )
    report.check("Changed white balance modifies RGB while preserving every original RAW value")
    for key, record in (("camera", camera), ("replay", replay)):
        require(
            record["result"]["physical_validation"] == "not_established", "Physical claim changed"
        )
        require(record["result"]["uncertainty"] == {"kind": "unknown"}, "Total uncertainty changed")
        report.pin(key + "_record_sha256", digest(record))
    for key, record in (("image", image), ("camera", camera), ("replay", replay)):
        artifact_proofs[key] = artifacts.verify_record(record)
    report.pin("artifact_sha256", artifact_proofs)
    report.pin(
        "published_record_sha256",
        {r["id"]: digest(r) for r in (parent, lens, sensor, assembly, image, test, isp)},
    )
    report.data["bundles"] = {
        "camera": bundle_proof(
            client, "runs", camera, [ri, psf], [parent, lens, sensor], artifacts
        ),
        "replay": bundle_proof(
            client, "replays", replay, [ri, psf], [parent, lens, sensor, isp], artifacts
        ),
    }
    report.check("Camera and replay ZIPs retain both Factory originals and complete model lineage")
    report.check(
        "Every downloaded artifact and ZIP entry matches its declared SHA-256 and byte count"
    )
    report.check("Synthetic status, unknown uncertainty and physical-validation limits preserved")
    require(
        static_hashes(client) == report.data["static_sha256"],
        "Served UI changed during verification",
    )
    require(
        environment_fingerprint() == report.data["environment"], "Bound source environment changed"
    )
    require(
        client.get("session")["state_dir"] == state, "Endpoint changed state during verification"
    )
    require(
        client.get(endpoint("assemblies", original["id"])) == original, "Source assembly changed"
    )
    for model in source_models:
        require(
            client.get(endpoint("models", model["id"])) == model, "Original fixture model changed"
        )
    if expected_runtime_root is not None:
        require(
            installed_binding(expected_runtime_root) == report.data["runtime_binding"],
            "Installed package or verifier imports changed during execution",
        )
    report.data["latest_invocation"]["counts_after"] = client.get("bootstrap")["counts"]
    if report.data["latest_invocation"]["read_only"]:
        require(
            report.data["latest_invocation"]["post_count"] == 0,
            "Read-only prescription verification submitted a POST",
        )
        require(
            report.data["latest_invocation"]["counts_before"]
            == report.data["latest_invocation"]["counts_after"],
            "Read-only verification changed registry counts",
        )
        report.check("Read-only recheck preserves saved IDs and artifact hashes with zero POSTs")
    report.check("Original source records and bound executable/UI sources remain unchanged")
    report.data.update(status="passed", verified_at=stamp())
    report.data.setdefault("first_verified_at", report.data["verified_at"])
    report.save()
    return report.data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--expected-runtime-root",
        type=Path,
        help="Require already imported modules to come from this verified package",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Recheck a completed report using saved IDs only; prohibit POSTs",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180,
        help="Observation seconds per existing Worker job; never resubmits on timeout",
    )
    args = parser.parse_args()
    require(math.isfinite(args.timeout) and args.timeout > 0, "Timeout must be finite and positive")
    parsed = urlsplit(args.base_url)
    require(
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment,
        "Use an explicit loopback QA base URL",
    )
    assert_qa_state(args.expected_state, Path(__file__).resolve().parents[1])
    report = Report(args.output, args.base_url, args.expected_state, read_only=args.read_only)
    try:
        with PlatformClient(args.base_url, timeout=60) as client:
            result = verify(
                client, report, args.timeout, expected_runtime_root=args.expected_runtime_root
            )
    except BaseException as exc:
        report.data.update(
            status="timed_out" if isinstance(exc, TimeoutError) else "failed",
            failure=f"{type(exc).__name__}: {exc}",
        )
        report.save()
        exc.add_note("Saved verification IDs: " + json.dumps(report.data["records"]))
        raise
    finally:
        report.lock.close()
    print(
        json.dumps(
            {
                "status": result["status"],
                "checks": len(result["checks"]),
                "records": result["records"],
                "report": str(report.output),
            }
        )
    )


if __name__ == "__main__":
    main()
