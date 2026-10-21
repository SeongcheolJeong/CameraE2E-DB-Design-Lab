#!/usr/bin/env python3
"""Verify the exact p0014 Lens DB pair from an installed wheel and publish drafts.

The output proves source-bound research computation and SDK/API publication, not
Lens accuracy, Lens/Sensor angular coupling, or a product decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _client, _verify_wheel

from camera_platform.api import create_app
from camera_platform.registry import canonical

REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
CSV_SHA256 = "f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.state.exists() or args.output.exists():
        parser.error("State and output must be fresh so prior evidence is retained")
    package = _verify_wheel(args.installed_root, args.wheel)
    if package["version"] != "0.16.2.dev32":
        raise RuntimeError("The dev32 installed wheel is required")

    started = time.monotonic()
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed independent Worker did not start")
        specification = client.lens_db_factory_pair_template()
        specification.update(
            revision_id=REVISION,
            reviewed_csv_sha256=CSV_SHA256,
            wavelength_nm=[550.0, 650.0],
            pupil_samples=54,
            kernel_size=17,
            acknowledge_unverified_optics=True,
        )
        specification["psf_prescription_sampling"].update(
            polar_rings=24, azimuth_samples=24,
        )
        specification["radiometry_sampling"].update(
            polar_rings=40, azimuth_samples=28,
        )
        planned = client.plan_lens_db_factory_pair(specification)
        pair = client.submit_lens_db_factory_pair(
            specification, expected_plan_sha256=planned["plan_sha256"]
        )
        jobs = {}
        for label in ("radiometry", "psf"):
            identity = pair["job_ids"][label]
            lifecycle = client.wait(identity, expected_kind="characterization", timeout=300)
            job = client.get("factory/jobs/" + identity)
            if lifecycle["status"] != "succeeded" or job["status"] != "succeeded":
                raise RuntimeError(f"{label} Factory Job failed: {job.get('error')}")
            if job["result"]["summary"]["status"] != "PROPOSED":
                raise RuntimeError(f"{label} did not pass the ordinary numerical gates")
            jobs[label] = {
                "id": identity,
                "status": job["status"],
                "numerical_status": job["result"]["summary"]["status"],
                "artifact_sha256": [a["sha256"] for a in job["artifacts"]],
                "environment": job["environment"],
            }
        asset = app.state.service.registry.get("assets", pair["asset_id"])
        for descriptor in asset["lens_db_source_artifacts"]:
            downloaded = transport.get(descriptor["url"])
            downloaded.raise_for_status()
            if (len(downloaded.content) != descriptor["bytes"] or
                    hashlib.sha256(downloaded.content).hexdigest() != descriptor["sha256"]):
                raise RuntimeError("Installed source artifact readback differs")
        psf_job = client.get("factory/jobs/" + pair["job_ids"]["psf"])
        request = {
            "specification": specification,
            "expected_plan_sha256": planned["plan_sha256"],
            "asset_id": pair["asset_id"],
            "job_ids": pair["job_ids"],
            "conditions": {
                "f_number": {
                    "value": psf_job["result"]["summary"]["geometric_reference_f_number"],
                    "unit": "1",
                }
            },
            "radiometry_name": "p0014 radiometry research",
            "radiometry_revision": "1",
            "psf_name": "p0014 bounded PSF research",
            "psf_revision": "1",
            "condition_scope": "optical_response",
        }
        published = client.publish_lens_db_factory_pair(request)
        repeated = client.publish_lens_db_factory_pair(request)
        if published["model_ids"] != repeated["model_ids"]:
            raise RuntimeError("Installed pair publication retry was not idempotent")
        models = {
            label: app.state.service.registry.get("models", identity)
            for label, identity in published["model_ids"].items()
        }
        if (
            len(app.state.service.registry.list("models")) != 2
            or any(model["approval_status"] != "draft" for model in models.values())
            or models["psf"]["parents"] != [models["radiometry"]["id"]]
            or published["physical_validation"] != "not_established"
            or published["product_ranking_allowed"] is not False
            or published["commercial_release_allowed"] is not False
            or worker.poll() is not None
        ):
            raise RuntimeError("Installed draft publication or Worker state differs")
        proof = {
            "schema": "camera_lens_db_factory_pair_installed_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "source_revision_id": REVISION,
            "source_csv_sha256": CSV_SHA256,
            "plan_sha256": planned["plan_sha256"],
            "asset_id": pair["asset_id"],
            "source_artifacts": asset["lens_db_source_artifacts"],
            "jobs": jobs,
            "model_ids": published["model_ids"],
            "draft_model_lineage": {
                "radiometry_parents": models["radiometry"]["parents"],
                "psf_parents": models["psf"]["parents"],
            },
            "elapsed_s": round(time.monotonic() - started, 3),
            "physical_validation": "not_established",
            "lens_sensor_coupled_raw": False,
            "product_ranking_allowed": False,
            "commercial_release_allowed": False,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "version": package["version"],
        "wheel_sha256": package["wheel_sha256"],
        "jobs": {key: value["id"] for key, value in jobs.items()},
        "model_ids": proof["model_ids"],
        "elapsed_s": proof["elapsed_s"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
