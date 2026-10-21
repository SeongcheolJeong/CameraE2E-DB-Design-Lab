#!/usr/bin/env python3
"""Deliver an explicitly requested synthetic prescription example to installed primary.

Default: read-only preflight. --apply creates new records using a durable journal;
--read-only verifies a completed journal without POSTs. Never starts/stops a
process, changes a runtime pin, updates a record or retries an uncertain POST.
The QA verifier's primary-state exclusion remains unchanged and is never called
or bypassed. Shared artifact/import/poll helpers are reused with this tool's own
primary guard, journal and recipe.
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
import sqlite3
import sys
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

WORKSPACE = Path(__file__).resolve().parents[1]
SCHEMA = "camera_prescription_primary_delivery_v1"
ALLOWED_POSTS = {
    "factory/jobs",
    "factory/models",
    "models",
    "assemblies",
    "spectral_images/fixture",
    "tests",
    "runs",
    "replays",
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def stamp():
    return datetime.now(UTC).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(state, *, require_idle=False):
    database = Path(state) / "registry.sqlite"
    require(database.is_file() and not database.is_symlink(), "Primary registry is missing")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        if require_idle:
            require(
                not connection.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued','running')"
                ).fetchone(),
                "Finish existing jobs before first delivery invocation",
            )
        return {
            table: {
                str(row[key]): hashlib.sha256(
                    json.dumps(dict(row), sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                for row in connection.execute("SELECT * FROM " + table)
            }
            for table, key in (("records", "id"), ("jobs", "id"), ("audit", "seq"))
        }


def preserved(before, after):
    require(
        isinstance(before, dict) and set(before) == {"records", "jobs", "audit"},
        "Baseline needs records, jobs and audit row hashes",
    )
    for table, rows in before.items():
        require(isinstance(rows, dict) and rows, "Baseline row hash sets must be nonempty")
        require(
            all(isinstance(value, str) and len(value) == 64 for value in rows.values()),
            "Malformed baseline row checksum",
        )
        require(
            all(after.get(table, {}).get(key) == value for key, value in rows.items()),
            f"Original {table} rows changed or disappeared",
        )


def exact_primary(state, workspace):
    state, workspace = Path(state), Path(workspace)
    require(
        state.is_absolute() and state == state.resolve() and not state.is_symlink(),
        "Primary state must be a canonical absolute path",
    )
    require(workspace == WORKSPACE.resolve(), "Workspace differs from this delivery tool")
    require(state == workspace / "outputs/camera-platform", "Only exact primary state is allowed")
    require(state.is_dir(), "Primary state does not exist")


def safe_output(output, state, runtime, baseline):
    output = Path(output).absolute()
    require(
        output == output.resolve() and not output.is_symlink(),
        "Delivery output requires a canonical path without parent aliases",
    )
    require(
        output != Path(baseline).resolve()
        and not output.is_relative_to(Path(state).resolve())
        and not output.is_relative_to(Path(runtime).resolve()),
        "Delivery report must be outside baseline, primary state and installed runtime",
    )
    return output


def load_baseline(path, expected_sha, state):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "Baseline must be a regular file")
    require(sha(path) == expected_sha, "Baseline file hash differs from explicit expected SHA256")
    data = json.loads(path.read_text())
    if isinstance(data, dict) and set(data) == {"records", "jobs", "audit"}:
        # Upgrade snapshots are bare row maps. Their explicit file SHA and exact
        # match against the separately verified primary establish the binding.
        preserved(data, snapshot(state))
        return {"schema": "camera_primary_rows_snapshot_v1", "rows": data}
    require(
        data.get("schema") == "camera_prescription_release_primary_baseline_v1",
        "Unsupported primary baseline schema",
    )
    require(
        data.get("health", {}).get("state_dir") == str(state), "Baseline belongs to another state"
    )
    preserved(data.get("rows"), snapshot(state))
    return data


def bound_helpers(runtime):
    """Select package paths before any product import; existing stale imports are rejected."""
    root = Path(runtime)
    require(root.is_absolute() and root == root.resolve(), "Runtime must be canonical and absolute")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(root))
    helper = importlib.import_module("verify_camera_prescription_optics")
    helper.installed_binding(root)
    return helper, importlib.import_module("start_camera_platform")


class Journal:
    def __init__(self, output, inputs, *, read_only=False):
        self.output = Path(output).absolute()
        require(not self.output.is_symlink(), "Delivery report must not be a symbolic link")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.output.with_suffix(self.output.suffix + ".lock")
        require(not lock_path.is_symlink(), "Delivery lock must not be a symbolic link")
        self.lock = lock_path.open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.output.exists():
                self.data = json.loads(self.output.read_text())
                require(self.data.get("schema") == SCHEMA, "Existing report schema differs")
                require(self.data.get("inputs") == inputs, "Existing journal pins different inputs")
            else:
                require(not read_only, "Read-only verification requires a completed journal")
                self.data = {
                    "schema": SCHEMA,
                    "inputs": inputs,
                    "tag": uuid.uuid4().hex[:12],
                    "records": {},
                    "operations": {},
                    "checks": [],
                    "invocations": [],
                    "source_kind": "simulated",
                    "physical_validation": "not_established",
                    "total_uncertainty": {"kind": "unknown"},
                }
            if read_only:
                require(bool(self.data.get("first_verified_at")), "Journal has not completed")
            invocation = {
                "started_at": stamp(),
                "read_only": read_only,
                "post_count": 0,
                "verifier_sha256": sha(__file__),
            }
            self.data["invocations"].append(invocation)
            self.data["latest_invocation"] = invocation
            if "failure" in self.data:
                invocation["previous_failure"] = self.data.pop("failure")
            self.data["status"] = "running"
            self.save()
        except BaseException:
            self.lock.close()
            raise

    def save(self):
        with tempfile.NamedTemporaryFile(mode="w", dir=self.output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(self.data, stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(self.output)
        finally:
            temporary.unlink(missing_ok=True)

    def pin(self, name, value):
        require(
            name not in self.data or self.data[name] == value, f"Frozen evidence changed: {name}"
        )
        self.data[name] = copy.deepcopy(value)
        self.save()

    def check(self, name):
        if not any(item["name"] == name for item in self.data["checks"]):
            self.data["checks"].append({"name": name, "status": "passed"})
        self.save()

    def acquire(self, client, key, collection, post_path, payload, matches=None):
        require(
            post_path in ALLOWED_POSTS and payload.get("force") is not True,
            "Only append-only delivery endpoints without forced jobs are allowed",
        )
        if matches is None:

            def matches(record):
                return all(
                    record.get(name) == value for name, value in payload.items() if name != "force"
                )

        intent = {"collection": collection, "post_path": post_path, "payload": payload}
        operation = self.data["operations"].get(key)
        if operation is not None:
            require(operation["intent"] == intent, f"Frozen {key} intent changed")
        identity = self.data["records"].get(key)
        if identity:
            require(operation is not None, "Saved identity lacks its submission intent")
            record = client.get(collection + "/" + quote(identity, safe=""))
        elif self.data["latest_invocation"]["read_only"]:
            raise RuntimeError("Read-only invocation cannot create or adopt unsaved records")
        elif operation is not None:
            candidates = [record for record in client.get(collection) if matches(record)]
            require(
                len(candidates) == 1,
                f"Unconfirmed {key} POST has {len(candidates)} matches; no resubmission",
            )
            record = candidates[0]
            self.data["records"][key] = record["id"]
            operation["status"] = "reconciled"
            self.save()
        else:
            self.data["operations"][key] = {
                "intent": copy.deepcopy(intent),
                "status": "submitting",
                "submitted_at": stamp(),
            }
            self.data["latest_invocation"]["post_count"] += 1
            self.save()  # A lost response can never cause an automatic second POST.
            record = client.post(post_path, payload)
            self.data["records"][key] = record["id"]
            self.save()  # Save returned identity before further validation.
            self.data["operations"][key]["status"] = "confirmed"
            self.save()
        require(matches(record), f"Saved {key} record differs from intent")
        return record


class GuardedClient:
    def __init__(self, client, guard):
        self.client, self.guard, self.http = client, guard, client.http

    def get(self, path):
        return self.client.get(path)

    def post(self, path, payload):
        require(
            path in ALLOWED_POSTS and payload.get("force") is not True,
            "Delivery client refuses mutation outside append-only recipe",
        )
        self.guard()
        return self.client.post(path, payload)


def chart_sha256(fixture, wave, np):
    require(fixture["pattern"] == "spectral_chart", "Delivery supports the fixed spectral chart")
    rows, cols = fixture["shape"]
    wavelengths = np.asarray(wave)
    y, x = np.indices((rows, cols))
    reflectance = np.asarray(
        [
            np.full_like(wavelengths, 0.75, dtype=float),
            *[
                0.05 + 0.7 * np.exp(-(((wavelengths - center) / width) ** 2))
                for center, width in ((620, 45), (540, 40), (460, 35))
            ],
        ]
    )
    pixels = reflectance[((x // max(1, cols // 4)) + (y // max(1, rows // 4))) % 4]
    stream = io.BytesIO()
    np.save(
        stream, np.asarray(pixels * fixture["irradiance_scale"], dtype="<f8"), allow_pickle=False
    )
    return hashlib.sha256(stream.getvalue()).hexdigest()


def bundle_evidence(client, collection, job, factory_jobs, artifacts, helper):
    content = helper.fetch(
        client, "/api/v1/" + collection + "/" + quote(job["id"], safe="") + "/bundle"
    )
    source = job if collection == "runs" else job["source"]["source_snapshot"]
    seeds = [model["id"] for model in source["model_snapshots"]]
    if collection == "replays":
        seeds.append(job["isp_model_snapshot"]["id"])
    lineage = {}
    while seeds:
        identity = seeds.pop()
        if identity not in lineage:
            model = client.get("models/" + quote(identity, safe=""))
            lineage[identity] = model
            seeds.extend(model["parents"])
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        require(len(archive.namelist()) == len(set(archive.namelist())), "Duplicate ZIP paths")
        manifest = json.loads(archive.read("manifest.json"))
        keys = (
            ("run", "model_lineage", "factory_jobs")
            if collection == "runs"
            else ("replay", "source", "model_lineage", "factory_jobs")
        )
        require(
            manifest["package_hash"] == helper.digest({key: manifest[key] for key in keys}),
            "ZIP semantic package hash differs",
        )
        require(manifest["run" if collection == "runs" else "replay"] == job, "ZIP job differs")
        if collection == "replays":
            require(manifest["source"] == source, "ZIP source differs")
        for name, expected in (
            ("model_lineage", lineage),
            ("factory_jobs", {record["id"]: record for record in factory_jobs}),
        ):
            actual = {record["id"]: record for record in manifest[name]}
            require(
                len(actual) == len(manifest[name]) and actual == expected,
                "ZIP exact lineage differs",
            )
        entries = {
            (item["sha256"], item["name"]): item
            for record in [job, source, *factory_jobs]
            for item in [*record.get("input_artifacts", []), *record.get("artifacts", [])]
        }
        for (checksum, name), artifact in entries.items():
            require(
                archive.read("artifacts/" + checksum + "/" + name) == artifacts.content(artifact),
                "ZIP original artifact differs",
            )
    return {
        "package_hash": manifest["package_hash"],
        "artifacts_verified": len(entries),
        "lineage_model_ids": sorted(lineage),
        "factory_job_ids": sorted(record["id"] for record in factory_jobs),
        "download_sha256": hashlib.sha256(content).hexdigest(),
    }


def recipe(client, journal, helper, source_id, timeout):
    """Own primary recipe; reuse artifact checks but never invoke QA verify()."""
    q = helper
    original = client.get("assemblies/" + quote(source_id, safe=""))
    models = [
        client.get("models/" + quote(identity, safe="")) for identity in original["model_ids"]
    ]
    by_kind = {model["kind"]: model for model in models}
    require(
        original["conditions"] == q.CONDITIONS
        and all(model.get("source_uri") == q.PROVENANCE["source_uri"] for model in models),
        "Source assembly must be the original declared engineering fixture",
    )
    journal.pin("source_assembly", original)
    journal.pin("source_models", models)
    require(by_kind["sensor"]["payload"]["pixel_pitch_um"] == 3.0, "Fixture pitch changed")
    suffix = " · primary delivery " + journal.data["tag"]
    psf_request = client.get("templates/factory?plugin_id=prescription_psf_v1")
    require(
        psf_request["request"] == q.prescription_psf.request_template(),
        "Prescription template changed",
    )
    psf_request.update(name="Prescription field PSF" + suffix, asset_id=by_kind["lens"]["asset_id"])
    journal.pin("prescription_request", psf_request["request"])
    ri_request = client.get("templates/factory?plugin_id=rayoptics_lens_v1")
    ri_request.update(
        name="Same prescription radiometry" + suffix,
        asset_id=psf_request["asset_id"],
        request=copy.deepcopy(psf_request["request"]["prescription"]),
    )
    ri = journal.acquire(client, "radiometry", "factory/jobs", "factory/jobs", ri_request)
    ri = q.poll(client, "factory/jobs", ri["id"], journal, timeout)
    psf = journal.acquire(client, "psf", "factory/jobs", "factory/jobs", psf_request)
    psf = q.poll(client, "factory/jobs", psf["id"], journal, timeout)
    artifacts = q.Artifacts(client)
    for name, job in (("radiometry", ri), ("psf", psf)):
        require(
            job["environment"] == journal.data["environment"], "Factory Worker environment differs"
        )
        require(job["result"]["summary"]["status"] == "PROPOSED", "Factory refused its request")
        require(
            job["result"]["physical_validation"] == "not_established"
            and job["result"]["summary"]["uncertainty"] == {"kind": "unknown"},
            "Factory physical evidence scope changed",
        )
        journal.pin(name + "_sha256", q.digest(job))
        journal.pin(name + "_artifacts", artifacts.verify_record(job))
    require(
        ri["request"] == psf["request"]["prescription"], "Factories used different prescriptions"
    )
    summary = psf["result"]["summary"]
    require(
        summary["propagation_phase"] == "exp(-2*pi*i*opd_nm/wavelength_nm)", "OPD phase differs"
    )
    require(
        any("transport is not solved" in s for s in summary["assumptions"])
        and any("Jacobian" in s for s in summary["limitations"]),
        "Optical approximation limits missing",
    )
    kernel = artifacts.array(psf, "psf_kernel.npy")
    opd, mask = (artifacts.array(psf, name) for name in ("fine_opd_nm.npy", "fine_admitted.npy"))
    require(kernel.ndim == 5 and kernel.shape[:3] == (2, 3, 3), "Field PSF grid differs")
    require(
        opd.shape == mask.shape and mask.dtype == bool and q.np.max(q.np.abs(opd)) > 1,
        "Physical OPD samples are missing",
    )
    q.np.testing.assert_allclose(kernel.sum(axis=(-1, -2)), 1, atol=1e-12)
    journal.check("Worker computed same-prescription radiometry and signed field PSF")
    parent_request = {
        "job_id": ri["id"],
        "name": "Prescription radiometry parent" + suffix,
        "revision": "delivery-1",
        "conditions": original["conditions"],
    }
    parent = journal.acquire(
        client,
        "parent_lens",
        "models",
        "factory/models",
        parent_request,
        lambda m: (
            m.get("factory_job_id") == ri["id"]
            and m.get("name") == parent_request["name"]
            and m.get("revision") == parent_request["revision"]
            and m.get("factory_condition_scope") == "all_declared"
            and m.get("factory_application_conditions") == original["conditions"]
        ),
    )
    conditions = {
        **copy.deepcopy(original["conditions"]),
        "f_number": {"value": summary["geometric_reference_f_number"], "unit": "1"},
    }
    lens_request = {
        "job_id": psf["id"],
        "base_lens_model_id": parent["id"],
        "name": "Prescription field optics" + suffix,
        "revision": "delivery-1",
        "conditions": conditions,
        "condition_scope": "optical_response",
    }
    lens = journal.acquire(
        client,
        "lens",
        "models",
        "factory/models",
        lens_request,
        lambda m: (
            m.get("psf_factory_job_id") == psf["id"]
            and m.get("parents") == [parent["id"]]
            and m.get("name") == lens_request["name"]
            and m.get("revision") == lens_request["revision"]
            and m.get("factory_condition_scope") == lens_request["condition_scope"]
            and m.get("factory_application_conditions") == conditions
        ),
    )
    require(
        lens["source_kind"] == "simulated" and lens["uncertainty"] == {"kind": "unknown"},
        "Published lens evidence scope changed",
    )
    require(
        lens["payload"]["psf"] == psf["result"]["model_candidate"]
        and lens["payload"]["field_reference_radius_mm"] == 0.75,
        "Published optical payload differs",
    )
    sensor_request = q.fields(by_kind["sensor"], q.ModelCreate)
    sensor_request.update(name="128px sensor" + suffix, parents=[by_kind["sensor"]["id"]])
    wanted = psf["request"]["prescription"]["wavelength_nm"]
    wave = sensor_request["payload"]["wavelength_nm"]
    require(
        wanted == [500, 600] and all(w in wave for w in wanted), "Fixture wavelength grid differs"
    )
    indices = [wave.index(w) for w in wanted]
    sensor_request["spectral_range"] = {"min": 500, "max": 600, "unit": "nm"}
    sensor_request["payload"]["wavelength_nm"] = wanted
    sensor_request["payload"]["qe"] = [
        [row[i] for i in indices] for row in sensor_request["payload"]["qe"]
    ]
    angular = sensor_request["payload"]["sensor_angular_response"]
    angular["relative_response"] = [
        [row[i] for i in indices] for row in angular["relative_response"]
    ]
    for dimension in ("active_height", "active_width"):
        sensor_request["properties"][dimension] = {"value": 0.384, "unit": "mm"}
    sensor = journal.acquire(client, "sensor", "models", "models", sensor_request)
    assembly_request = q.fields(original, q.AssemblyCreate)
    assembly_request.update(
        name="Prescription optics camera" + suffix,
        model_ids=[lens["id"], sensor["id"], by_kind["isp"]["id"]],
        conditions=conditions,
        spectral_range=sensor_request["spectral_range"],
    )
    assembly = journal.acquire(client, "assembly", "assemblies", "assemblies", assembly_request)
    require(assembly["compatibility"]["status"] == "PASS", "Assembly is incompatible")
    journal.check(
        "Published parent optics and native sensor retain exact conditions and field scale"
    )
    fixture = client.get("templates/spatial-scene?" + urlencode({"assembly_id": assembly["id"]}))
    fixture["name"] = "Prescription spectral chart" + suffix
    require(fixture["shape"] == [64, 64], "Fixture dimensions differ")
    metadata = client.get("templates/spectral-image?" + urlencode({"assembly_id": assembly["id"]}))
    metadata.update(
        name=fixture["name"],
        revision=fixture["revision"],
        source_uri="fixture://authored-spectral-" + fixture["pattern"],
        license="MIT; authored synthetic image-plane fixture",
        method_and_version="native-spectral-fixture:1",
    )
    image_sha = chart_sha256(fixture, wanted, q.np)

    def matches_image(record):
        return (
            all(record.get(key) == value for key, value in metadata.items() if key != "asset_id")
            and record.get("shape") == [64, 64, len(wanted)]
            and [
                a["sha256"]
                for a in record.get("artifacts", [])
                if a["name"] == "spectral-image.npy"
            ]
            == [image_sha]
        )

    image = journal.acquire(
        client,
        "image",
        "spectral_images",
        "spectral_images/fixture",
        fixture,
        matches_image,
    )
    require(
        image["sampling"]["full_sensor_shape"] == [128, 128]
        and image["sampling"]["crop_origin"] == [32, 32],
        "Native crop contract differs",
    )
    test_request = client.get(
        "templates/spatial-test?"
        + urlencode({"assembly_id": assembly["id"], "scene_id": image["id"]})
    )
    test_request["name"] = "Prescription RAW channel mean" + suffix
    test = journal.acquire(client, "test", "tests", "tests", test_request)
    request = {
        "assembly_id": assembly["id"],
        "test_id": test["id"],
        "scene_id": image["id"],
        "plugin_id": "spatial_scene_v1",
        "parameters": q.PARAMETERS,
        "force": False,
    }
    camera = journal.acquire(client, "run", "runs", "runs", request)
    camera = q.poll(client, "runs", camera["id"], journal, timeout)
    require(
        camera["environment"] == journal.data["environment"]
        and camera["assembly_snapshot"] == assembly,
        "Camera Worker binding differs",
    )
    require(
        camera["result"]["psf"]["spatial_model"] == "field_grid"
        and camera["result"]["radial_response_sampling"]["field_reference_radius_mm"] == 0.75,
        "Camera bypassed physical field optics",
    )
    require(
        any("separable approximation" in s for s in camera["result"]["limitations"]),
        "Camera optical transport limit missing",
    )
    raw, rgb = (artifacts.array(camera, name) for name in ("raw_frames.npy", "rgb_frames.npy"))
    require(
        raw.shape == (2, 64, 64) and raw.std() > 10 and rgb.shape == (2, 64, 64, 3),
        "RAW/RGB dimensions or variation differ",
    )
    isp_request = client.get("templates/isp-profile?" + urlencode({"assembly_id": assembly["id"]}))
    isp_request["name"] = "WB replay profile" + suffix
    isp_request["payload"]["white_balance"]["gains_rgb"] = [1.4, 1.0, 0.8]
    isp = journal.acquire(client, "replay_isp", "models", "models", isp_request)
    replay_request = {
        "name": "Preserved prescription RAW" + suffix,
        "source_kind": "run",
        "source_id": camera["id"],
        "isp_model_id": isp["id"],
        "force": False,
    }
    replay = journal.acquire(client, "replay", "replays", "replays", replay_request)
    replay = q.poll(client, "replays", replay["id"], journal, timeout)
    require(replay["environment"] == journal.data["environment"], "Replay Worker binding differs")
    q.np.testing.assert_array_equal(artifacts.array(replay, "raw_frames.npy"), raw)
    require(
        not q.np.array_equal(artifacts.array(replay, "rgb_frames.npy"), rgb),
        "Replay RGB did not change",
    )
    for key, record in (("camera", camera), ("replay", replay)):
        require(
            record["result"]["physical_validation"] == "not_established"
            and record["result"]["uncertainty"] == {"kind": "unknown"},
            "Physical claim changed",
        )
        journal.pin(key + "_sha256", q.digest(record))
    journal.pin(
        "artifact_sha256",
        {
            key: artifacts.verify_record(record)
            for key, record in (("image", image), ("camera", camera), ("replay", replay))
        },
    )
    journal.pin(
        "published_sha256",
        {r["id"]: q.digest(r) for r in (parent, lens, sensor, assembly, image, test, isp)},
    )
    bundles = {
        "camera": bundle_evidence(client, "runs", camera, [ri, psf], artifacts, q),
        "replay": bundle_evidence(client, "replays", replay, [ri, psf], artifacts, q),
    }
    journal.data["latest_invocation"]["bundle_download_sha256"] = {
        key: value.pop("download_sha256") for key, value in bundles.items()
    }
    journal.pin("bundles", bundles)  # ZIP timestamps vary; semantic manifests/artifacts must not.
    journal.check(
        "Actual RAW/RGB and preserved RAW replay retain Factory originals in both evidence ZIPs"
    )
    journal.check("Synthetic evidence and unknown physical accuracy remain explicit")


def preflight(client, args, helper, launcher, baseline, *, full=True):
    state = args.expected_state
    exact_primary(state, WORKSPACE)
    binding = launcher.load_installed_runtime(state)
    require(
        binding is not None
        and binding["runtime_root"] == str(args.expected_runtime_root)
        and binding["manifest_sha256"] == args.expected_manifest_sha256,
        "Primary installed pin differs from explicitly expected runtime",
    )
    require(
        binding["manifest"]["environment"]["platform_version"] == args.expected_version,
        "Installed primary version differs from requested delivery version",
    )
    responses = [client.get(name) for name in ("health", "session", "bootstrap")]
    for response in responses:
        require(
            response.get("schema") == "camera_platform_v1"
            and response.get("state_dir") == str(state),
            "Endpoint primary identity differs",
        )
    health, session, bootstrap = responses
    require(
        health.get("workspace") == str(WORKSPACE)
        and health.get("status") == "ok"
        and health.get("worker") == "running",
        "Exact primary API/Worker is not healthy",
    )
    require(
        session.get("version")
        == bootstrap.get("version")
        == args.expected_version
        == helper.__version__,
        "Client and API versions differ",
    )
    require(
        args.source_assembly_id in baseline["rows"]["records"],
        "Source assembly is outside baseline",
    )
    preserved(baseline["rows"], snapshot(state))
    if full:
        launcher.verify_running_package(args.base_url.rstrip("/") + "/", state, binding)
        require(
            helper.installed_binding(args.expected_runtime_root)["manifest_sha256"]
            == args.expected_manifest_sha256,
            "Installed client binding differs",
        )
    return binding, bootstrap


def run(args):
    exact_primary(args.expected_state, WORKSPACE)
    args.output = safe_output(
        args.output, args.expected_state, args.expected_runtime_root, args.baseline
    )
    baseline = load_baseline(args.baseline, args.baseline_sha256, args.expected_state)
    helper, launcher = bound_helpers(args.expected_runtime_root)
    with helper.PlatformClient(args.base_url, timeout=60) as raw_client:
        binding, bootstrap = preflight(raw_client, args, helper, launcher, baseline)
        if not args.apply and not args.read_only:
            return {
                "status": "preflight_passed",
                "post_count": 0,
                "activated": False,
                "version": args.expected_version,
                "calculation_delivery": "not_run",
            }
        inputs = {
            "base_url": args.base_url.rstrip("/"),
            "state_dir": str(args.expected_state),
            "runtime_root": str(args.expected_runtime_root),
            "manifest_sha256": args.expected_manifest_sha256,
            "version": args.expected_version,
            "baseline_sha256": args.baseline_sha256,
            "source_assembly_id": args.source_assembly_id,
            "helper_sha256": sha(Path(helper.__file__)),
        }
        journal = Journal(args.output, inputs, read_only=args.read_only)
        try:
            rows = snapshot(args.expected_state, require_idle="original_rows" not in journal.data)
            if "original_rows" not in journal.data:
                journal.pin("original_rows", rows)
            preserved(journal.data["original_rows"], rows)
            journal.pin("environment", binding["manifest"]["environment"])
            journal.pin("static_sha256", helper.static_hashes(raw_client))
            journal.data["latest_invocation"]["counts_before"] = bootstrap["counts"]
            journal.save()

            def guard():
                preflight(raw_client, args, helper, launcher, baseline)
                preserved(journal.data["original_rows"], snapshot(args.expected_state))

            recipe(
                GuardedClient(raw_client, guard),
                journal,
                helper,
                args.source_assembly_id,
                args.timeout,
            )
            preflight(raw_client, args, helper, launcher, baseline)
            after = snapshot(args.expected_state)
            preserved(journal.data["original_rows"], after)
            require(
                helper.static_hashes(raw_client) == journal.data["static_sha256"],
                "Served UI changed",
            )
            require(
                helper.environment_fingerprint() == journal.data["environment"],
                "Client environment changed",
            )
            require(
                sha(args.baseline) == args.baseline_sha256, "Baseline file changed during delivery"
            )
            require(
                sha(__file__) == journal.data["latest_invocation"]["verifier_sha256"]
                and sha(helper.__file__) == journal.data["inputs"]["helper_sha256"],
                "Delivery verifier or helper changed during execution",
            )
            if args.read_only:
                require(
                    journal.data["latest_invocation"]["post_count"] == 0 and rows == after,
                    "Read-only verification observed changed rows or submitted a POST",
                )
            journal.data["latest_invocation"]["counts_after"] = raw_client.get("bootstrap")[
                "counts"
            ]
            journal.check("Original baseline and all pre-delivery registry rows remain unchanged")
            journal.data.update(status="passed", verified_at=stamp(), original_rows_preserved=True)
            journal.data.setdefault("first_verified_at", journal.data["verified_at"])
            journal.save()
            return {
                "status": "passed",
                "post_count": journal.data["latest_invocation"]["post_count"],
                "records": journal.data["records"],
                "report": str(journal.output),
            }
        except BaseException as error:
            if "original_rows" in journal.data:
                try:
                    preserved(journal.data["original_rows"], snapshot(args.expected_state))
                    journal.data["original_rows_preserved_on_failure"] = True
                except (OSError, RuntimeError, sqlite3.Error) as preservation_error:
                    journal.data["original_rows_preserved_on_failure"] = False
                    journal.data["preservation_failure"] = str(preservation_error)
            journal.data.update(
                status="timed_out" if isinstance(error, TimeoutError) else "failed",
                failure=f"{type(error).__name__}: {error}",
            )
            journal.save()
            raise
        finally:
            journal.lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in (
        "base-url",
        "expected-version",
        "expected-manifest-sha256",
        "baseline-sha256",
        "source-assembly-id",
    ):
        parser.add_argument("--" + flag, required=True)
    for flag in ("expected-state", "expected-runtime-root", "baseline", "output"):
        parser.add_argument("--" + flag, type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="Append explicitly synthetic delivery records"
    )
    mode.add_argument(
        "--read-only", action="store_true", help="Verify completed saved IDs without POSTs"
    )
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    url = urlsplit(args.base_url)
    require(
        url.scheme == "http"
        and url.hostname in {"127.0.0.1", "localhost", "::1"}
        and not url.username
        and not url.password
        and url.path in {"", "/"}
        and not url.query
        and not url.fragment,
        "Delivery requires an explicit loopback base URL",
    )
    require(math.isfinite(args.timeout) and args.timeout > 0, "Timeout must be finite and positive")
    print(json.dumps(run(args), ensure_ascii=False))


if __name__ == "__main__":
    main()
