"""Run the reviewed p0014 Lens revision through both ordinary Factory Workers.

This is a research characterization replay. It does not approve a commercial
source, physical Lens/Sensor accuracy, or a camera product comparison.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from camera_platform.lens_db_factory_workflow import plan, submit, template
from camera_platform.registry import canonical, digest
from camera_platform.service import PlatformService
from camera_platform.worker import work

ROOT = Path(__file__).resolve().parents[1]
REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
CSV_SHA256 = "f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output must be new so prior verification evidence is preserved")
    service = PlatformService(args.state_dir, workspace=ROOT)
    specification = template()
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
    planned = plan(service, specification)
    started = time.monotonic()
    queued = submit(service, specification, expected_plan_sha256=planned["plan_sha256"])
    jobs = {}
    for label in ("radiometry", "psf"):
        work(service, once=True)
        job = service.registry.get_run(queued["job_ids"][label])
        jobs[label] = {
            "id": job["id"],
            "status": job["status"],
            "error": job["error"],
            "result_sha256": digest(job["result"]) if job.get("result") else None,
            "summary_status": (
                job["result"]["summary"].get("status")
                if job.get("result") and job["result"].get("summary") else None
            ),
            "model_candidate_present": bool(
                job.get("result") and job["result"].get("model_candidate")
            ),
            "artifact_sha256": [a["sha256"] for a in job.get("artifacts", [])],
        }
    asset = service.registry.get("assets", queued["asset_id"])
    source_artifacts = asset["lens_db_source_artifacts"]
    for descriptor in source_artifacts:
        assert service.registry.artifact_owner(descriptor["sha256"])[
            "descriptor"
        ] == descriptor
    result = {
        "schema": "camera_lens_db_factory_pair_verification_v1",
        "source": planned["source"],
        "plan_sha256": planned["plan_sha256"],
        "asset_id": asset["id"],
        "source_artifacts": source_artifacts,
        "physical_prescription_binding": planned["physical_prescription_binding"],
        "jobs": jobs,
        "elapsed_s": round(time.monotonic() - started, 3),
        "physical_validation": "not_established",
        "lens_sensor_coupled_raw": False,
        "product_ranking_allowed": False,
        "commercial_release_allowed": False,
        "limitation": planned["limitation"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(result) + "\n")
    print(json.dumps({"output": str(args.output), "jobs": jobs}, indent=2))
    return 0 if all(j["status"] == "succeeded" for j in jobs.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
