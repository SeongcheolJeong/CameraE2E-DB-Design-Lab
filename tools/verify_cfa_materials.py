#!/usr/bin/env python3
"""Export canonical proxy materials and inexpensive planar optical references.

No FDTD jobs run here. Planar conservation and Beer–Lambert consistency are
numerical checks, not measured product validation. Existing differing files
are preserved: select a fresh output directory when regenerating a report.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

from pyisetcam.cfa_materials import (
    absorption_only_transmission,
    image_sensor_cfa_materials,
    planar_cfa_reference,
)


def _write_preserving(path: Path, text: str) -> None:
    if path.exists():
        if path.read_text(encoding="utf-8") == text:
            return
        raise FileExistsError(f"Preserving existing report: {path}; choose a fresh --output-dir.")
    path.write_text(text, encoding="utf-8")


def build_report(sensor_ids: list[str], root: Path | None = None) -> tuple[dict, list[dict]]:
    """Return JSON-safe canonical bundles and a planar reference report."""
    bundles = [image_sensor_cfa_materials(sensor_id, root=root) for sensor_id in sensor_ids]
    samples = []
    worst_residual = 0.0
    minimum_power = 1.0
    for bundle in bundles:
        for channel, material in bundle["channels"].items():
            low, high = material["thickness_range_um"]
            for wavelength in bundle["wavelength_nm"]:
                transmission = float(
                    absorption_only_transmission(material, wavelength, material["thickness_um"])
                )
                lower = float(absorption_only_transmission(material, wavelength, high))
                upper = float(absorption_only_transmission(material, wavelength, low))
                for angle in (0, 20, 40):
                    for polarization in ("s", "p"):
                        sample = planar_cfa_reference(
                            bundle,
                            wavelength,
                            channel=channel,
                            angle_deg=angle,
                            polarization=polarization,
                        )
                        sample.update(
                            cfa_absorption_only_normal_transmission=transmission,
                            cfa_absorption_only_normal_transmission_range=[lower, upper],
                            cfa_thickness_range_um=[low, high],
                        )
                        samples.append(sample)
                        worst_residual = max(worst_residual, abs(sample["energy_residual"]))
                        minimum_power = min(
                            minimum_power, sample["R"], sample["T"], *sample["layer_absorption"]
                        )
    report = {
        "schema": "camerae2e_cfa_planar_reference_v1",
        "method": "coherent_planar_fresnel_recursion",
        "samples": samples,
        "sensor_ids": [bundle["sensor_id"] for bundle in bundles],
        "sample_count": len(samples),
        "angle_deg": [0, 20, 40],
        "polarization": ["s", "p"],
        "planar_stack": ["air", "CFA", "lumped_passivation", "finite_silicon", "air"],
        "curved_microlens_included": False,
        "normalized_to": "incident normal Poynting power in transparent air",
        "validation": {
            "numerical_conservation_passed": worst_residual < 1e-9,
            "passivity_passed_with_roundoff_tolerance": minimum_power >= -1e-9,
            "maximum_absolute_energy_residual": worst_residual,
            "minimum_power_fraction_before_roundoff": minimum_power,
            "actual_fdtd_comparison_performed": False,
            "measured_product_accuracy_verified": False,
        },
        "provenance": {
            "report_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "material_bundle_code_sha256": bundles[0]["bundle_code_sha256"],
            "sources": [
                {
                    key: bundle[key]
                    for key in (
                        "sensor_id",
                        "report_code",
                        "material_bundle_sha256",
                        "geometry_sha256",
                    )
                }
                for bundle in bundles
            ],
        },
        "limitations": [
            "CFA n,k and layer split are proxies; agreement is not measured product validation.",
            "The planar reference excludes curved microlenses, "
            "pixel boundaries and charge collection.",
            "CFA absorption-only sensitivity uses a normal path "
            "and excludes interfaces/interference.",
            "Thickness ranges are sensitivity bounds, not statistical confidence intervals.",
            "Coherent planar fringes may average differently "
            "under broad wavelength/angle illumination.",
        ],
    }
    return report, bundles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("reports/fdtd_sensor_e2e/materials")
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument(
        "--sensor-ids", nargs="+", default=["DEF-1807-801", "DEF-2011-804", "DEF-2206-805"]
    )
    args = parser.parse_args()
    report, bundles = build_report(args.sensor_ids, args.root)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for bundle in bundles:
        _write_preserving(
            args.output_dir / f"{bundle['sensor_id']}.json",
            json.dumps(bundle, indent=2, allow_nan=False) + "\n",
        )
    _write_preserving(
        args.output_dir / "planar_reference.json",
        json.dumps(report, indent=2, allow_nan=False) + "\n",
    )
    table = io.StringIO(newline="")
    writer = csv.writer(table)
    writer.writerow(
        [
            "sensor_id",
            "channel",
            "wavelength_nm",
            "angle_deg",
            "polarization",
            "R",
            "T",
            "A_cfa",
            "A_passivation",
            "A_silicon",
            "A_total",
            "energy_residual",
            "cfa_absorption_only_normal_T",
            "cfa_absorption_only_normal_T_min",
            "cfa_absorption_only_normal_T_max",
        ]
    )
    for sample in report["samples"]:
        writer.writerow(
            [
                sample[key]
                for key in (
                    "sensor_id",
                    "channel",
                    "wavelength_nm",
                    "angle_deg",
                    "polarization",
                    "R",
                    "T",
                )
            ]
            + sample["layer_absorption"]
            + [
                sample["A"],
                sample["energy_residual"],
                sample["cfa_absorption_only_normal_transmission"],
            ]
            + sample["cfa_absorption_only_normal_transmission_range"]
        )
    _write_preserving(args.output_dir / "planar_reference.csv", table.getvalue())
    validation = report["validation"]
    _write_preserving(
        args.output_dir / "README.md",
        "# Canonical CFA materials and planar reference\n\n"
        f"{report['sample_count']} numerical samples: each sensor's native 400–700 nm CFA grid, "
        "RGB, s/p, and 0/20/40 degrees. All powers use incident normal Poynting flux.\n\n"
        "The stack is air / CFA / lumped passivation / finite silicon / air. "
        "Curved microlenses are excluded from this planar numerical benchmark. "
        "Individual bundle JSONs retain the curved-lens geometry for the separate FDTD runner.\n\n"
        f"Maximum conservation residual: {validation['maximum_absolute_energy_residual']:.3g}. "
        "Negative values within floating-point roundoff are retained in the reference. "
        "This report does not run FDTD and does not establish measured product accuracy.\n\n"
        "Canonical CFA n,k come from per-report optical_qe_db models; "
        "legacy FDTD CFA CSVs are excluded. "
        "Those per-report models share common proxy n,k, with product-specific thickness. "
        "Silicon uses packaged Green 2008 bulk data; "
        "lens and passivation use packaged proxy tables. "
        "Source, material, geometry and code hashes are retained.\n\n"
        "Reported optical stack height is preserved when applying canonical CFA thickness: "
        "passivation = total optical height − lens height − CFA thickness. "
        "This remainder is explicitly inferred and excludes silicon. "
        "For IMX307 it changes 0.576 to 0.934 um while CFA changes 1.008 to 0.65 um.\n\n"
        "Absorption-only thickness ranges are sensitivity bounds excluding interface reflection, "
        "interference, focusing and charge collection; they are not confidence intervals. "
        "A coherent planar reference can show fringes that average under broad illumination.\n",
    )
    print(json.dumps({"output_dir": str(args.output_dir.resolve()), **validation}, indent=2))


if __name__ == "__main__":
    main()
