#!/usr/bin/env python3
"""Verify stored-radiance reuse through HTTP and the running Worker.

This explicit creation tool requires an exact state directory, a completed source
render and an existing target assembly. Throughput 0.4 is a synthetic assumption,
never a measured lens property. No models, assemblies or renderer jobs are made.
The report checkpoints job IDs before polling. Reusing the same report resumes
those IDs; a timeout never cancels or replaces a submitted job.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import math
import os
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode

import numpy as np

from camera_platform import SCHEMA_VERSION, __version__
from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient

SCHEMA = "camera_scene_projection_verification_v1"
PROJECTION_PLUGIN = "stored_spectral_radiance_projection_v1"
STATIC_FILES = ("index.html", "app.js", "scenes.js", "workbench.js", "style.css")
PARAMETERS = {"frames": 2, "seed": 931}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def stamp():
    return datetime.now(UTC).isoformat()


class Report:
    def __init__(
        self, output=None, *, base_url, expected_state, source_render, assembly, checkpoint=None
    ):
        self.output = Path(output) if output is not None else None
        self.checkpoint = checkpoint
        inputs = {
            "base_url": base_url.rstrip("/"),
            "state_dir": str(Path(expected_state).resolve()),
            "source_render_id": source_render,
            "assembly_id": assembly,
        }
        if self.output is not None and self.output.exists():
            self.data = json.loads(self.output.read_text())
            require(self.data.get("schema") == SCHEMA, "Existing report has another schema")
            require(self.data.get("inputs") == inputs, "Existing report pins different inputs")
        else:
            self.data = {
                "schema": SCHEMA,
                "inputs": inputs,
                **inputs,
                "started_at": stamp(),
                "checks": [],
                "records": {},
                "physical_validation": "not_established",
                "source_kind": "simulated",
                "total_uncertainty": {"kind": "unknown"},
            }
        self.data.update(status="running", last_started_at=stamp())
        self.data.pop("failure", None)
        self.data.pop("verified_at", None)
        self.save()

    def save(self):
        self.data["updated_at"] = stamp()
        if self.checkpoint is not None:
            self.checkpoint(copy.deepcopy(self.data))
        if self.output is None:
            return
        self.output.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(self.data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.output.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def pin(self, key, value):
        if key in self.data:
            require(self.data[key] == value, f"Frozen report value changed: {key}")
        else:
            self.data[key] = copy.deepcopy(value)
        self.save()

    def record(self, key, identity):
        previous = self.data["records"].get(key)
        require(previous is None or previous == identity, f"Refusing to replace saved {key} ID")
        self.data["records"][key] = identity
        self.save()

    def check(self, name, condition=True):
        require(condition, name)
        if not any(item["name"] == name for item in self.data["checks"]):
            self.data["checks"].append({"name": name, "status": "passed"})
        self.save()


def endpoint(kind, identity):
    return kind + "/" + quote(identity, safe="")


def fetched(client, path):
    response = client.http.get(path)
    response.raise_for_status()
    return response.content


def static_hashes(client):
    return {name: hashlib.sha256(fetched(client, "/" + name)).hexdigest() for name in STATIC_FILES}


def artifact_bytes(client, artifact):
    checksum = artifact["sha256"]
    require(
        len(checksum) == 64 and all(c in "0123456789abcdef" for c in checksum),
        "Artifact SHA-256 is malformed",
    )
    require(artifact["id"] == checksum, "Artifact ID differs from its checksum")
    content = fetched(client, "/api/v1/artifacts/" + checksum)
    require(len(content) == artifact["bytes"], "Artifact byte count changed")
    require(hashlib.sha256(content).hexdigest() == checksum, "Artifact checksum changed")
    return content


def named(record, name, *, unique=True):
    matches = [a for a in record["artifacts"] if a["name"] == name]
    require(
        bool(matches) and (not unique or len(matches) == 1),
        f"Expected {'one' if unique else 'a'} {name} artifact",
    )
    return matches[0]


def array(client, artifact):
    value = np.load(io.BytesIO(artifact_bytes(client, artifact)), allow_pickle=False)
    require(
        isinstance(value, np.ndarray) and np.isfinite(value).all(),
        "Expected a finite numeric NPY array",
    )
    return value


def poll(client, kind, identity, report, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(endpoint(kind, identity))
        current = {"collection": kind, "id": identity, "status": job["status"]}
        if report.data.get("last_job_observation") != current:
            report.data["last_job_observation"] = current
            report.save()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            require(
                job["status"] == "succeeded",
                f"{kind}/{identity} {job['status']}: {job.get('error')}",
            )
            return job
        time.sleep(0.5)
    raise TimeoutError(
        f"{kind}/{identity} remains active. Keep this existing job ID; "
        + (
            f"rerun with the same arguments and {report.output} to resume it."
            if report.output is not None
            else "resume polling this ID without submitting a replacement."
        )
    )


def camera_bundle_records(client, job):
    """Resolve the full expected lineage independently of the downloaded ZIP."""
    snapshots = {model["id"]: model for model in job.get("model_snapshots", [])}
    require(
        len(snapshots) == len(job.get("model_snapshots", [])),
        "Camera has duplicate frozen model snapshots",
    )
    pending, lineage = list(snapshots), {}
    while pending:
        identity = pending.pop()
        if identity in lineage:
            continue
        model = client.get(endpoint("models", identity))
        require(model["id"] == identity, "Model lookup returned another identity")
        if identity in snapshots:
            require(model == snapshots[identity], "Frozen Camera model changed")
        lineage[identity] = model
        pending.extend(model["parents"])
    factory_ids = sorted({
        model[key]
        for model in lineage.values()
        for key in ("factory_job_id", "psf_factory_job_id")
        if model.get(key)
    })
    factories = [client.get(endpoint("factory/jobs", identity)) for identity in factory_ids]
    require(
        [factory["id"] for factory in factories] == factory_ids,
        "Factory lookup returned another identity",
    )
    return [lineage[identity] for identity in sorted(lineage)], factories


def bundle_proof(client, kind, job, originals):
    content = fetched(client, "/api/v1/" + endpoint(kind, job["id"]) + "/bundle")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "ZIP has duplicate semantic paths")
        manifest = json.loads(archive.read("manifest.json"))
        require(manifest.get("schema") == SCHEMA_VERSION, "ZIP manifest schema changed")
        require(manifest.get("run") == job, "ZIP manifest differs from the completed job")
        legacy_type = {
            "spectral_scene_projection": "scene_projection_evidence_package",
            "spectral_scene_render": "scene_render_evidence_package",
        }.get(job.get("job_kind"))
        if legacy_type is not None:
            require(manifest.get("type") == legacy_type, "ZIP evidence type changed")
            require(
                "model_lineage" not in manifest and "factory_jobs" not in manifest,
                "Projection/Render ZIP contains unbound Camera lineage",
            )
            package_hash, lineage, factories = digest(job), [], []
        else:
            require(manifest.get("type") == "run_evidence_package", "ZIP evidence type changed")
            lineage, factories = camera_bundle_records(client, job)
            require(manifest.get("model_lineage") == lineage, "ZIP Camera model lineage changed")
            require(manifest.get("factory_jobs") == factories, "ZIP Camera Factory lineage changed")
            package_hash = digest({
                "run": job, "model_lineage": lineage, "factory_jobs": factories,
            })
        require(manifest.get("package_hash") == package_hash, "ZIP manifest hash changed")
        artifacts = {
            (a["sha256"], a["name"]): a
            for a in [
                *originals, *job["artifacts"], *job.get("input_artifacts", []),
                *(a for factory in factories
                  for a in [*factory.get("input_artifacts", []), *factory["artifacts"]]),
            ]
        }
        expected_paths = {"artifacts/" + checksum + "/" + name for checksum, name in artifacts}
        require(
            {name for name in names if name.startswith("artifacts/")} == expected_paths,
            "ZIP original artifact paths changed or are incomplete",
        )
        for (checksum, name), artifact in artifacts.items():
            saved = archive.read("artifacts/" + checksum + "/" + name)
            require(
                len(saved) == artifact["bytes"] and hashlib.sha256(saved).hexdigest() == checksum,
                f"ZIP original bytes changed: {name}",
            )
    return {
        "sha256": hashlib.sha256(content).hexdigest(), "package_hash": package_hash,
        "artifacts_verified": len(artifacts), "models_verified": len(lineage),
        "factory_jobs_verified": len(factories),
    }


def _verify(client, report, *, timeout=180, reference=None):
    inputs, ids = report.data["inputs"], report.data["records"]
    bootstrap, session, health = (client.get(k) for k in ("bootstrap", "session", "health"))
    for response in (bootstrap, session, health):
        require(response["state_dir"] == inputs["state_dir"], "Destination state differs")
        require(response["schema"] == "camera_platform_v1", "Unexpected server schema")
    require(health["status"] == "ok", "Server is not healthy")
    openapi = json.loads(fetched(client, "/openapi.json"))
    require(
        session["version"] == bootstrap["version"] == openapi["info"]["version"] == __version__,
        "Running server and verifier package versions differ",
    )
    require(
        "/api/v1/scene_projections" in openapi["paths"],
        "Running server does not expose stored-radiance projection",
    )
    report.pin("running_version", session["version"])
    report.pin("static_sha256", static_hashes(client))
    report.data["server"] = {"workspace": health["workspace"], "worker": health["worker"]}
    report.check("Exact state, running package version and public projection API verified")

    source = client.get(endpoint("scene_renders", inputs["source_render_id"]))
    target = client.get(endpoint("assemblies", inputs["assembly_id"]))
    require(
        source["status"] == "succeeded"
        and source["plugin_id"] == "mitsuba_fixed_wavelength_scene_v1",
        "Source must be a completed fixed-wavelength Mitsuba render",
    )
    source_assembly = client.get(endpoint("assemblies", source["assembly_id"]))
    require(source["assembly_snapshot"] == source_assembly, "Original assembly snapshot changed")
    report.pin("source_render_sha256", digest(source))
    report.pin("source_assembly_sha256", digest(source_assembly))
    report.pin("target_assembly_sha256", digest(target))
    report.pin("source_environment", source["environment"])
    if reference is not None:
        require(
            reference["source_render_sha256"] == digest(source)
            and reference["source_assembly_sha256"] == digest(source_assembly)
            and reference["target_assembly_sha256"] == digest(target),
            "Repeat inputs differ from the original verified evidence",
        )
        require(
            reference["static_sha256"] == report.data["static_sha256"],
            "Repeat uses different served product files",
        )
    report.record("source_render", source["id"])
    report.record("source_assembly", source_assembly["id"])
    report.record("assembly", target["id"])
    report.record("scene", source["scene_asset_id"])
    if "baseline" not in report.data:
        report.pin(
            "baseline",
            {
                "counts": bootstrap["counts"],
                "scene_render_ids": sorted(r["id"] for r in bootstrap["scene_renders"]),
                "model_hashes": {m["id"]: m["content_hash"] for m in bootstrap["models"]},
            },
        )
    for artifact in [*source["input_artifacts"], *source["artifacts"]]:
        artifact_bytes(client, artifact)
    report.check("Completed source, immutable camera snapshots and every original byte verified")

    before = client.get("workbench")["counts"]
    template = client.get(
        "scene_authoring/projection-template?"
        + urlencode(
            {
                "source_render_id": source["id"],
                "assembly_id": target["id"],
            }
        )
    )
    draft = copy.deepcopy(template["request"])
    require(
        draft["source_render_id"] == source["id"] and draft["assembly_id"] == target["id"],
        "Template changes the selected source or target",
    )
    require(
        all(v is None for v in draft["projection"]["on_axis_transmittance"]["values"]),
        "Unknown template throughput was silently supplied",
    )
    draft["name"] = ("Stored radiance HTTP verification · " + source["id"] + " · " + target["id"])[
        :200
    ]
    draft["force"] = reference is not None
    projection = draft["projection"]
    projection["on_axis_transmittance"]["values"] = [
        0.4 for _ in projection["on_axis_transmittance"]["wavelength_nm"]
    ]
    projection.update(
        source_kind="estimated",
        source_uri="fixture://radiance-projection-verifier/throughput-0.4",
        method_and_version="synthetic-throughput-assumption:1",
        physical_instance=None,
        license="Synthetic verification assumption; not a measured lens transmission",
        parents=[],
        approximation_uncertainty="unknown",
    )
    report.pin("draft", draft)
    preview = client.preview_scene_projection(draft)
    require(preview["status"] == "ready", f"Projection is not ready: {preview['issues']}")
    require(
        preview["preview_sha256"]
        == digest({k: v for k, v in preview.items() if k != "preview_sha256"}),
        "Preview checksum does not bind its full result",
    )
    require(preview["source_snapshot_sha256"] == digest(source), "Preview pins another source")
    require(
        preview["target_assembly_sha256"] == target["content_hash"],
        "Preview pins another target camera",
    )
    require(
        preview["physical_validation"] == "not_established"
        and preview["total_uncertainty"] == {"kind": "unknown"},
        "Interface readiness must not claim physical accuracy",
    )
    report.pin("preview_sha256", preview["preview_sha256"])
    report.check(
        "Explicit synthetic throughput passes a nonmutating frozen preview",
        client.get("workbench")["counts"] == before,
    )

    if "projection" not in ids:
        queued = client.project_scene_radiance(preview)
        report.record("projection", queued["id"])
    projected = poll(client, "scene_projections", ids["projection"], report, timeout)
    require(
        projected["plugin_id"] == PROJECTION_PLUGIN
        and projected["source_render_snapshot"] == source
        and projected["assembly_snapshot"] == target,
        "Projection changed frozen inputs",
    )
    require(
        all(projected["request"].get(k) == v for k, v in draft.items() if k != "force"),
        "Saved projection uses a different request",
    )
    require(
        digest(projected["environment"]) == preview["environment_sha256"],
        "Worker environment differs from the inspected preview",
    )
    require(
        projected["environment"]["platform_version"] == report.data["running_version"],
        "Worker package version differs from the running server",
    )
    report.pin("environment", projected["environment"])
    if reference is not None:
        require(
            projected["id"] != reference["records"]["projection"],
            "Forced repeat reused the completed projection job",
        )
        require(
            projected["environment"] == reference["environment"],
            "Repeated projection Worker environment changed",
        )
    summary = projected["result"]["summary"]
    require(
        summary["radiance_reused"] is True and summary["renderer_invoked"] is False,
        "Projection did not use the stored-radiance path",
    )
    require(
        projected["result"]["physical_validation"] == "not_established"
        and projected["result"]["total_uncertainty"] == {"kind": "unknown"}
        and "not_model_or_total_uncertainty" in summary["mc_standard_error"],
        "Projection overstates physical validation or Monte Carlo uncertainty",
    )
    report.check("Worker reuses the frozen radiance without invoking the renderer")

    source_l = named(source, "spectral-radiance.npy")
    source_e = named(source, "spectral-image.npy")
    source_mc = named(source, "radiance-mc-standard-error.npy")
    new_e = named(projected, "spectral-image.npy", unique=False)
    new_mc = named(projected, "irradiance-mc-standard-error.npy")
    require(
        projected["artifacts"][0] == new_e, "New irradiance must precede the old named artifact"
    )
    radiance, old_e, mc = (array(client, a) for a in (source_l, source_e, source_mc))
    irradiance, projected_mc = (array(client, a) for a in (new_e, new_mc))
    expected_shape = (*source["request"]["shape"], len(source["request"]["wavelength_nm"]))
    require(
        all(a.shape == expected_shape for a in (radiance, old_e, mc, irradiance, projected_mc)),
        "Projection changed native crop or spectral shape",
    )
    scale = (
        np.asarray(projection["on_axis_transmittance"]["values"])
        * np.pi
        / (4 * projection["f_number"] ** 2)
    )
    expected_e, expected_mc = radiance * scale, mc * scale
    np.testing.assert_allclose(irradiance, expected_e, rtol=1e-12, atol=0)
    np.testing.assert_allclose(projected_mc, expected_mc, rtol=1e-12, atol=0)
    retained = {(a["sha256"], a["name"]) for a in projected["artifacts"]}
    require(
        all((a["sha256"], a["name"]) in retained for a in source["artifacts"]),
        "Projection lost an original semantic artifact path",
    )
    for artifact in projected["artifacts"]:
        artifact_bytes(client, artifact)
    report.data["artifact_sha256"] = {
        "original_radiance": source_l["sha256"],
        "original_irradiance": source_e["sha256"],
        "original_mc_standard_error": source_mc["sha256"],
        "new_irradiance": new_e["sha256"],
        "new_mc_standard_error": new_mc["sha256"],
    }
    report.data["numerics"] = {
        "shape": list(expected_shape),
        "irradiance_unit": "W/m^2/nm",
        "irradiance_max_absolute_error": float(np.max(np.abs(irradiance - expected_e))),
        "mc_standard_error_max_absolute_error": float(np.max(np.abs(projected_mc - expected_mc))),
        "irradiance_changed": new_e["sha256"] != source_e["sha256"],
        "mc_standard_error_scope": summary["mc_standard_error"],
    }
    report.check("New irradiance and scaled MC standard error match pi L T / (4 N squared)")

    image = client.publish_scene_projection(projected["id"], projected["name"][:180] + " · input")
    report.record("image", image["id"])
    published_counts = client.get("workbench")["counts"]
    require(
        client.publish_scene_projection(projected["id"], projected["name"][:180] + " · input")
        == image,
        "Repeated publication changed the immutable image",
    )
    require(
        client.get("workbench")["counts"] == published_counts,
        "Repeated publication created another record",
    )
    require(
        image["source_render_snapshot"] == source
        and image["projection_job_snapshot"] == projected
        and "render_job_snapshot" not in image
        and image["projection_lens_model_id"] == projected["lens_snapshot"]["id"]
        and image["conditions"] == target["conditions"]
        and image["artifacts"][0]["sha256"] == new_e["sha256"]
        and image["physical_validation"] == "not_established",
        "Publication lost source/target lineage or selected the old irradiance",
    )
    require(
        client.get(endpoint("assets", image["asset_id"])) == image["asset_snapshot"],
        "Published image has no matching immutable derived asset",
    )
    report.check("Idempotent publication keeps the original source and new target lens separate")

    specification = client.get(
        "templates/spatial-test?"
        + urlencode(
            {
                "assembly_id": target["id"],
                "scene_id": image["id"],
            }
        )
    )

    def matches(test):
        return all(test.get(k) == v for k, v in specification.items())

    if "test" in ids:
        test = client.get(endpoint("tests", ids["test"]))
        require(matches(test), "Saved Test differs from the complete spatial contract")
    else:
        test = next((t for t in client.get("tests") if matches(t)), None)
        if test is None:
            test = client.post("tests", specification)
        report.record("test", test["id"])
    report.pin("parameters", PARAMETERS)
    if "run" not in ids:
        queued = client.submit(
            target["id"],
            test["id"],
            plugin_id="spatial_scene_v1",
            scene_id=image["id"],
            parameters=PARAMETERS,
            force=reference is not None,
        )
        report.record("run", queued["id"])
    camera = poll(client, "runs", ids["run"], report, timeout)
    require(
        camera["assembly_snapshot"] == target
        and camera["test_snapshot"] == test
        and camera["scene_snapshot"] == image
        and camera["parameters"] == PARAMETERS,
        "Camera job changed the selected target, Test or projected image",
    )
    require(camera["environment"] == projected["environment"], "Camera Worker environment changed")
    require(
        camera["result"]["prediction"]["uncertainty"] == {"kind": "unknown"},
        "Synthetic RAW must not establish physical uncertainty",
    )
    raw, rgb = (named(camera, name) for name in ("raw_frames.npy", "rgb_frames.npy"))
    require(
        array(client, raw).shape == (PARAMETERS["frames"], *expected_shape[:2]),
        "Actual RAW shape differs from the frozen crop",
    )
    require(
        array(client, rgb).shape == (PARAMETERS["frames"], *expected_shape[:2], 3),
        "Actual RGB shape differs from the frozen crop",
    )
    report.data["artifact_sha256"].update(raw=raw["sha256"], rgb=rgb["sha256"])
    report.check("Matching Test and target camera produce verified actual RAW and RGB bytes")

    report.data["bundles"] = {
        "projection": bundle_proof(client, "scene_projections", projected, source["artifacts"]),
        "camera": bundle_proof(
            client, "runs", camera, [*source["artifacts"], *projected["artifacts"]]
        ),
    }
    report.check("Projection and Camera ZIPs retain every original and new semantic artifact path")
    final = client.get("bootstrap")
    require(
        final["state_dir"] == inputs["state_dir"]
        and final["version"] == report.data["running_version"],
        "Server destination or package version changed during verification",
    )
    require(static_hashes(client) == report.data["static_sha256"], "Served product source changed")
    baseline = report.data["baseline"]
    require(
        sorted(r["id"] for r in final["scene_renders"]) == baseline["scene_render_ids"],
        "A new renderer job appeared during stored-radiance verification",
    )
    require(
        {m["id"]: m["content_hash"] for m in final["models"]} == baseline["model_hashes"],
        "Model revisions were added or changed",
    )
    require(
        client.get(endpoint("scene_renders", source["id"])) == source
        and client.get(endpoint("assemblies", target["id"])) == target
        and client.get(endpoint("assemblies", source_assembly["id"])) == source_assembly,
        "Source render or original/target camera changed",
    )
    require(
        final["counts"]["assemblies"] == baseline["counts"]["assemblies"],
        "Verification created another camera assembly",
    )
    report.data["final_counts"] = final["counts"]
    report.check(
        "No new renderer, model or assembly; original evidence and served source unchanged"
    )
    if reference is not None:
        require(
            camera["id"] != reference["records"]["run"],
            "Forced repeat reused the completed Camera job",
        )
        require(
            report.data["artifact_sha256"] == reference["artifact_sha256"],
            "Forced projection or Camera execution changed the verified numeric bytes",
        )
        report.data["repeated_from"] = copy.deepcopy(reference["records"])
        report.check("Forced new projection and Camera jobs reproduce all original artifact hashes")
    report.data.update(status="passed", verified_at=stamp())
    report.save()
    return report.data


def run_verified(client, report, *, timeout=180, reference=None):
    try:
        return _verify(client, report, timeout=timeout, reference=reference)
    except BaseException as exc:
        report.data.update(
            status="timed_out" if isinstance(exc, TimeoutError) else "failed",
            failure=f"{type(exc).__name__}: {exc}",
        )
        report.save()
        exc.add_note("Saved verification IDs: " + json.dumps(report.data["records"]))
        raise


def verify(client, expected_state, source_render_id, assembly_id, *, checkpoint=None):
    """Create/reuse public HTTP jobs; checkpoint(report) receives IDs before polling."""
    report = Report(
        base_url=str(client.http.base_url),
        expected_state=expected_state,
        source_render=source_render_id,
        assembly=assembly_id,
        checkpoint=checkpoint,
    )
    return run_verified(client, report)


def repeat(client, expected_state, proof, *, checkpoint=None):
    """Force new projection and Camera jobs and prove identical numerical artifacts."""
    require(
        proof.get("schema") == SCHEMA and proof.get("status") == "passed",
        "Repeat requires a completed projection verification report",
    )
    require(
        proof["state_dir"] == str(Path(expected_state).resolve()),
        "Repeat proof belongs to another state directory",
    )
    require(proof["parameters"] == PARAMETERS, "Repeat parameters differ from this verifier")
    report = Report(
        base_url=str(client.http.base_url),
        expected_state=expected_state,
        source_render=proof["records"]["source_render"],
        assembly=proof["records"]["assembly"],
        checkpoint=checkpoint,
    )
    return run_verified(client, report, reference=proof)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("--expected-state", type=Path, required=True)
    parser.add_argument("--source-render", required=True)
    parser.add_argument("--assembly", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--timeout",
        type=float,
        default=180,
        help="Seconds to observe each saved Worker job before resumable timeout",
    )
    args = parser.parse_args()
    require(math.isfinite(args.timeout) and args.timeout > 0, "Timeout must be finite and positive")
    report = Report(
        args.output,
        base_url=args.base_url,
        expected_state=args.expected_state,
        source_render=args.source_render,
        assembly=args.assembly,
    )
    try:
        with PlatformClient(args.base_url, timeout=120) as client:
            result = run_verified(client, report, timeout=args.timeout)
    except BaseException as exc:
        report.data.update(
            status="timed_out" if isinstance(exc, TimeoutError) else "failed",
            failure=f"{type(exc).__name__}: {exc}",
        )
        report.save()
        raise
    print(
        json.dumps(
            {
                "status": result["status"],
                "checks": len(result["checks"]),
                "records": result["records"],
                "report": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
