"""Package the manually reviewed representative audit; never parse or mutate PDFs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def build(audit: dict, source_root: Path) -> dict:
    profiles = {}

    def source(report):
        item = report["source"]
        return {
            "relative_path": str(Path(item["path"]).relative_to(source_root)),
            "sha256": item["sha256"],
            "page_count": item["page_count"],
        }

    def fact(item, code):
        return {
            "value": item["value"],
            "unit": item["unit"],
            "status": item["status"],
            "scope": item["scope"],
            "source_id": code,
            "pdf_page_1_based": item["evidence"]["pdf_page_1_based"],
            "notes": item.get("notes", ""),
        }

    for report in audit["representatives"]:
        code = report["report_code"]
        facts = {item["quantity"]: fact(item, code) for item in report["confirmed_facts"]}
        profiles[code] = {
            "device_name": report["device_name"],
            "sources": {code: source(report)},
            "selected_facts": facts,
            "supplemental_observations": {},
            "active_pixel_dti": facts.get("active_pixel_dti_type", {}).get("value", "unconfirmed"),
            "corrected_legacy_classifications": report["catalog_audit"],
            "rejected_legacy_optical_fields": (
                [
                    {
                        "field": "qe.spectral_ranges",
                        "reason": "CFA thickness is not a wavelength band",
                        "value_nm": [
                            1000 * x for x in facts["cfa_thickness_observed_range_um"]["value"]
                        ],
                    }
                ]
                if "cfa_thickness_observed_range_um" in facts
                else []
            ),
            "unmodeled_structure": [
                "AR/coating optical dispersion and coherent thin-film response",
                "full microlens surface and spatial/angular calibration",
                "DTI and surface-texture light redistribution and carrier collection",
            ],
            "missing_calibration": [
                "product RGB CFA n,k or transmission spectra",
                "product spectral EQE and angular response",
                "charge-collection efficiency and spatial crosstalk",
            ],
            "limitations": report["current_proxy_warnings"],
        }
    # Only the explicitly reviewed shared IMX662 sample is transferred across reports.
    detailed = next(r for r in audit["supplemental_reports"] if r["report_code"] == "DEP-2206-801")
    target = profiles["DEF-2206-805"]
    target["sources"][detailed["report_code"]] = source(detailed)
    target["cross_report_links"] = audit["cross_report_links"]
    target["supplemental_observations"] = {
        item["quantity"]: fact(item, detailed["report_code"])
        for item in detailed["confirmed_facts"]
    }
    for key in ("microlens_height_um", "microlens_vertex_radius_um", "green_cfa_thickness_um"):
        target["selected_facts"][key] = target["supplemental_observations"][key]
    target["cfa_range_conflict"] = {
        "DEF_observed_range_um": [0.42, 0.53],
        "DEP_observed_range_um": [0.5, 0.55],
        "pooled_sensitivity_range_um": [0.42, 0.55],
        "resolution": "Local observations, not statistical limits. Apply identified green only; "
        "unresolved red/blue retain DEF midpoint .475 um as a scenario assumption.",
    }
    target["optical_height_budget"] = detailed["optical_height_budget"]
    return {
        "schema": "camerae2e_reviewed_techinsights_v1",
        "source_priority": "reviewed_locally_collected_original_reports",
        "reviewed_on": audit["created_at"],
        "profiles": profiles,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    output = build(json.loads(args.audit.read_text()), args.source_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(f"Packaged {len(output['profiles'])} reviewed representative profiles: {args.output}")


if __name__ == "__main__":
    main()
