"""Reproduce the pinned p0014 patent-source and research OPD diagnostic."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from camera_platform.lens_db_wavefront_bridge import (
    APERTURE_HYPOTHESIS,
    characterize_db_wavefront_revision,
    db_wavefront_preflight,
)
from camera_platform.lens_patent_source_review import verify_archived_p0014_patent

REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
CSV_SHA256 = "f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", type=Path, required=True)
    parser.add_argument("--archived-patent-html", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = verify_archived_p0014_patent(args.archived_patent_html)
    kwargs = {
        "catalog_root": args.catalog_root,
        "reviewed_csv_sha256": CSV_SHA256,
        "aperture_hypothesis": APERTURE_HYPOTHESIS,
    }
    preflight = db_wavefront_preflight(REVISION, **kwargs)
    if preflight["status"] != "research_request_ready":
        raise RuntimeError(f"Pinned OPD preflight failed: {preflight['blockers']}")
    if preflight["directional_radiometry_preflight"]["status"] != "blocked":
        raise RuntimeError("Pinned directional-radiometry boundary changed")
    start = time.monotonic()
    result = characterize_db_wavefront_revision(REVISION, **kwargs)
    elapsed = time.monotonic() - start
    summary = result["summary"]
    if summary["status"] != "REFUSED" or result["model_candidate"] is not None:
        raise RuntimeError("Pinned numerical refusal changed; re-review before promotion")
    report = {
        "schema": "camerae2e_p0014_wavefront_dev26_verification_v1",
        "source": source,
        "lens_revision_id": REVISION,
        "reviewed_csv_sha256": CSV_SHA256,
        "source_semantics_review": preflight["source_semantics_review"],
        "opd_preflight_status": preflight["status"],
        "directional_radiometry_preflight": preflight["directional_radiometry_preflight"],
        "paraxial_translation_diagnostic": preflight["paraxial_translation_diagnostic"],
        "wavefront": {
            "elapsed_s": elapsed,
            "status": summary["status"],
            "factory_id": summary["factory_id"],
            "backend": summary["backend"],
            "request_sha256": summary["request_sha256"],
            "numerical_agreement": summary["numerical_agreement"],
            "approximation_domain_checks": summary["approximation_domain_checks"],
            "max_kernel_l1_change_observed": summary["max_kernel_l1_change_observed"],
            "max_kernel_l1_change_allowed": preflight["psf_request"]["max_kernel_l1_change"],
            "minimum_retained_energy_observed": summary["minimum_retained_energy_observed"],
            "minimum_retained_energy_allowed": preflight["psf_request"][
                "minimum_retained_energy"
            ],
            "maximum_neighbor_opd_waves_observed": summary[
                "maximum_neighbor_opd_waves_observed"
            ],
            "maximum_ray_cone_deg_observed": summary["maximum_ray_cone_deg_observed"],
            "issues": summary["issues"],
            "arrays_shape": {
                key: list(value.shape) for key, value in result["arrays"].items()
            },
            "model_candidate_created": False,
        },
        "physical_validation": "not_established",
        "product_ranking_allowed": False,
        "approved_diffraction_psf_available": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
