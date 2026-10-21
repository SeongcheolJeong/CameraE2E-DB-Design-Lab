"""Verify reviewed structural inputs and compare with the canonical DB scenario."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from pyisetcam.physical_estimate import image_sensor_physical_model, physical_optical_response
from pyisetcam.sensor_techinsights import sensor_techinsights_evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/techinsights_sensor_e2e"))
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    wave, angles = np.arange(400.0, 701.0, 10.0), np.arange(0.0, 61.0, 1.0)
    fig, axes = plt.subplots(3, 2, figsize=(11, 10), constrained_layout=True)
    report = {"estimate_only": True, "measured_accuracy_verified": False, "sensors": {}}
    codes = ["DEF-1807-801", "DEF-2011-804", "DEF-2206-805"]
    for row, (code, name) in enumerate(zip(codes, ("IMX307", "IMX462", "IMX662"), strict=True)):
        old = image_sensor_physical_model(
            code, optical_assumptions={"geometry_source": "canonical_db"}
        )
        new = image_sensor_physical_model(code)
        previous, current = np.asarray(old["eqe"]), np.asarray(new["eqe"])
        before = physical_optical_response(old, 550.0, theta_deg=angles)
        after = physical_optical_response(new, 550.0, theta_deg=angles)
        full = physical_optical_response(new, wave[:, None], theta_deg=angles[None, :])
        residual = float(np.max(np.abs(full["energy_residual"])))
        assert residual < 1e-12
        assert np.all(np.isfinite(full["eqe"]))
        assert np.all((full["eqe"] >= 0) & (full["eqe"] <= 0.75))
        np.testing.assert_array_equal(current[:, 1], current[:, 2])
        # The grid opening must not silently become a total QE multiplier.
        np.testing.assert_allclose(full["eqe"], 0.75 * full["layer_absorption"]["silicon"])
        evidence = sensor_techinsights_evidence(code, source_root=args.source_root)
        item = {
            "name": name,
            "evidence": evidence,
            "canonical_geometry_um": old["geometry_um"],
            "reviewed_geometry_um": new["geometry_um"],
            "applied_facts": new["provenance"]["applied_structural_facts"],
            "peak_eqe_RGB_before": previous[:, [0, 1, 3]].max(axis=0).tolist(),
            "peak_eqe_RGB_after": current[:, [0, 1, 3]].max(axis=0).tolist(),
            "green_550nm_relative_eqe_change": float(current[15, 1] / previous[15, 1] - 1),
            "max_absolute_eqe_change": float(np.max(np.abs(current - previous))),
            "own_site_acceptance_green550_before_at_0_20_40deg": before["own_site_acceptance"][
                [0, 20, 40], 1
            ].tolist(),
            "own_site_acceptance_green550_after_at_0_20_40deg": after["own_site_acceptance"][
                [0, 20, 40], 1
            ].tolist(),
            "maximum_energy_residual": residual,
            "source_checks_passed": (
                all(s["status"] == "matched" for s in evidence["source_checks"].values())
                if args.source_root is not None
                else None
            ),
        }
        report["sensors"][code] = item
        for site, color, label in ((0, "#c64e40", "R"), (1, "#24865e", "G"), (3, "#327ac2", "B")):
            axes[row, 0].plot(wave, previous[:, site], "--", color=color, alpha=0.5)
            axes[row, 0].plot(wave, current[:, site], color=color, label=label)
        axes[row, 0].set(
            title=f"{name}: estimated normal EQE",
            ylabel="Electrons / incident photon",
            xlabel="Wavelength (nm)",
            ylim=(0, 0.6),
        )
        axes[row, 0].legend(loc="upper right", ncol=3)
        axes[row, 1].plot(
            angles, before["own_site_acceptance"][:, 1], "--", color="#8a8a8a", label="Canonical DB"
        )
        axes[row, 1].plot(
            angles, after["own_site_acceptance"][:, 1], color="#24865e", label="Reviewed structure"
        )
        axes[row, 1].set(
            title=f"{name}: own-site routing diagnostic, green 550 nm",
            xlabel="Incident angle (degrees)",
            ylabel="Estimated fraction",
            ylim=(0, 1),
        )
        axes[row, 1].legend()
        for ax in axes[row]:
            ax.grid(alpha=0.15)
    fig.suptitle(
        "Local TechInsights evidence: measured dimensions, uncalibrated optical response\n"
        "Solid = reviewed structure; dashed = canonical DB. Routing is NOT nominal QE loss.",
        fontsize=12,
    )
    fig.savefig(args.output / "optical_changes.png", dpi=160)
    plt.close(fig)
    report["status"] = "passed"
    if args.source_root is not None and not all(
        item["source_checks_passed"] for item in report["sensors"].values()
    ):
        report["status"] = "source_verification_failed"
    report["code_sha256"] = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [
            Path(__file__),
            Path("src/pyisetcam/sensor_techinsights.py"),
            Path("src/pyisetcam/physical_estimate.py"),
        ]
    }
    (args.output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    if report["status"] != "passed":
        raise RuntimeError("Source PDF integrity check failed; see verification.json.")
    print(
        json.dumps(
            {
                "status": report["status"],
                "sensors": {
                    code: {
                        k: v
                        for k, v in value.items()
                        if k
                        in {
                            "name",
                            "green_550nm_relative_eqe_change",
                            "maximum_energy_residual",
                            "source_checks_passed",
                        }
                    }
                    for code, value in report["sensors"].items()
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
