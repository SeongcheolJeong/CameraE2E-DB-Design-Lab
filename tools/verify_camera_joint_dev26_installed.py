#!/usr/bin/env python3
"""Cold dev26 wheel proof of retained RAW jobs and a refused DB OPD model."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
from verify_camera_bounded_psf_installed import _client, _verify_wheel
from verify_camera_joint_dev25_installed import _legacy_isp, _radiance, _reject_overfield

from camera_platform.api import create_app
from camera_platform.lens_db_wavefront_bridge import (
    APERTURE_HYPOTHESIS,
    characterize_db_wavefront_revision,
    db_wavefront_preflight,
)
from camera_platform.lens_patent_source_review import verify_archived_p0014_patent
from camera_platform.registry import canonical

REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
CSV_SHA256 = "f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd"


def _db_wavefront(workspace: Path, patent_html: Path) -> dict:
    source = verify_archived_p0014_patent(patent_html)
    options = {
        "catalog_root": workspace / "camerae2e_db",
        "reviewed_csv_sha256": CSV_SHA256,
        "aperture_hypothesis": APERTURE_HYPOTHESIS,
    }
    preflight = db_wavefront_preflight(REVISION, **options)
    if (
        preflight["status"] != "research_request_ready"
        or preflight["psf_request"] is None
        or preflight["directional_radiometry_preflight"]["status"] != "blocked"
        or preflight["sequential_surface_count"] != 28
        or preflight["source_semantics_review"]["terminal_numbered_row"]
        ["effective_diameter_mm"] != 50.0
        or preflight["product_ranking_allowed"] is not False
    ):
        raise RuntimeError("Installed patent/DB preflight lost a source or optical boundary")
    started = time.monotonic()
    result = characterize_db_wavefront_revision(REVISION, **options)
    summary = result["summary"]
    if (
        summary["status"] != "REFUSED"
        or summary["numerical_agreement"] is not False
        or result["model_candidate"] is not None
        or summary["max_kernel_l1_change_observed"] <= 0.03
        or summary["minimum_retained_energy_observed"] >= 0.95
        or result["product_ranking_allowed"] is not False
    ):
        raise RuntimeError("Installed OPD calculation was inappropriately accepted")
    return {
        "patent_source": source,
        "source_revision": preflight["source"],
        "source_semantics_review": preflight["source_semantics_review"],
        "paraxial_translation_diagnostic": preflight["paraxial_translation_diagnostic"],
        "directional_radiometry_preflight": preflight["directional_radiometry_preflight"],
        "opd_preflight_status": preflight["status"],
        "opd_elapsed_s": time.monotonic() - started,
        "opd_status": summary["status"],
        "opd_numerical_agreement": summary["numerical_agreement"],
        "max_kernel_l1_change_observed": summary["max_kernel_l1_change_observed"],
        "minimum_retained_energy_observed": summary["minimum_retained_energy_observed"],
        "maximum_neighbor_opd_waves_observed": summary[
            "maximum_neighbor_opd_waves_observed"
        ],
        "approved_diffraction_psf_available": False,
        "physical_validation": "not_established",
        "product_ranking_allowed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--archived-patent-html", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package = _verify_wheel(args.installed_root, args.wheel)
    if package["version"] != "0.16.2.dev26" or args.state.exists():
        raise RuntimeError("A dev26 wheel and fresh isolated Registry are required")
    app = create_app(args.state, args.workspace, start_worker=True, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        worker = app.state.worker
        if worker is None or worker.poll() is not None:
            raise RuntimeError("Installed child Worker did not start")
        radiance = _radiance(client, transport)
        overfield = _reject_overfield(client, transport)
        color = _legacy_isp(client, transport)
        wavefront = _db_wavefront(args.workspace, args.archived_patent_html)
        if worker.poll() is not None:
            raise RuntimeError("Installed child Worker exited during proof")
        proof = {
            "schema": "camerae2e_installed_joint_dev26_radiance_isp_wavefront_proof_v1",
            "package": package,
            "worker_child_pid": worker.pid,
            "registered_object_radiance_raw": radiance,
            "overfield_preflight": overfield,
            "saved_d65_raw_legacy_isp": color,
            "saved_lens_wavefront": wavefront,
            "physical_validation": "not_established",
            "product_ranking_allowed": False,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical(proof) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "version": package["version"],
        "wheel_sha256": package["wheel_sha256"],
        "radiance_raw_job_id": radiance["raw_job_id"],
        "legacy_isp_job_id": color["isp_job_id"],
        "opd_status": wavefront["opd_status"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
