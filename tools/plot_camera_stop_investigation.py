#!/usr/bin/env python3
"""Plot saved stop-investigation evidence; never run a simulator or query a server."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

LABELS = ("clear", "quarter", "radial")
COLORS = {"clear": "#2368A2", "quarter": "#D66B24", "radial": "#7B4DAB"}
NAMES = {"clear": "Clear", "quarter": "Uniform 25%", "radial": "Radial spectral"}


def sha(content):
    return hashlib.sha256(content).hexdigest()


def digest(value):
    return sha(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    )


def require(condition, message):
    if not condition:
        raise ValueError(message)


def one(records, predicate, description):
    matches = [record for record in records if predicate(record)]
    require(len(matches) == 1, "Expected exactly one " + description)
    return matches[0]


def load_json(path, inputs):
    content = path.read_bytes()
    inputs[str(path)] = {"sha256": sha(content), "bytes": len(content)}
    return json.loads(content)


def read_case(directory, label, verification, inputs):
    lens = load_json(directory / f"{label}-lens.json", inputs)
    run = load_json(directory / f"{label}-run.json", inputs)
    declared = verification["records"][label]
    require(
        lens["id"] == declared["lens"] and run["id"] == declared["run"], "HTTP record ID mismatch"
    )
    require(run["status"] == "succeeded", "Only completed saved runs can be plotted")
    require(run["assembly_id"] == declared["camera"], "Camera binding mismatch")
    require(
        one(run["model_snapshots"], lambda m: m["kind"] == "lens", "run Lens") == lens,
        "Run does not contain this exact Lens snapshot",
    )
    require(
        digest({k: v for k, v in lens.items() if k != "content_hash"}) == lens["content_hash"],
        "Lens content checksum mismatch",
    )
    runtime_fields = {
        "id",
        "status",
        "cache_key",
        "content_hash",
        "created_at",
        "updated_at",
        "result",
        "error",
        "artifacts",
    }
    require(
        digest({k: v for k, v in run.items() if k not in runtime_fields}) == run["content_hash"],
        "Frozen run input checksum mismatch",
    )
    archive_path = directory / f"{label}-evidence.zip"
    inputs[str(archive_path)] = {
        "sha256": sha(archive_path.read_bytes()),
        "bytes": archive_path.stat().st_size,
    }
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "Duplicate ZIP member names")
        require(
            sum(info.file_size for info in archive.infolist()) <= 256 * 1024 * 1024,
            "Evidence archive exceeds the documented bounded size",
        )
        manifest = json.loads(archive.read("manifest.json"))
        require(manifest["run"] == run, "ZIP and standalone run JSON differ")
        require(
            manifest["package_hash"]
            == digest({k: manifest[k] for k in ("run", "model_lineage", "factory_jobs")}),
            "ZIP manifest package hash mismatch",
        )
        require(
            one(manifest["model_lineage"], lambda m: m["id"] == lens["id"], "lineage Lens") == lens,
            "ZIP and standalone Lens JSON differ",
        )
        parent = one(
            manifest["model_lineage"], lambda m: m["id"] == lens["parents"][0], "Lens parent"
        )
        ray_job = one(
            manifest["factory_jobs"],
            lambda j: j["id"] == parent["factory_job_id"],
            "radiometry job",
        )
        psf_job = one(
            manifest["factory_jobs"], lambda j: j["id"] == lens["psf_factory_job_id"], "PSF job"
        )
        response = lens["payload"]["radiometry"]
        require(ray_job["request"] == psf_job["request"]["prescription"], "Prescriptions differ")
        require(
            response["prescription_sha256"] == digest(ray_job["request"]),
            "Prescription SHA mismatch",
        )
        require(
            response["stop_transmission"] == ray_job["request"]["stop_transmission"], "Mask differs"
        )
        require(
            response["stop_transmission_sha256"] == digest(response["stop_transmission"]),
            "Mask SHA mismatch",
        )
        # Validate every retained NPY and other artifact, including originals not plotted.
        entries = [*run["artifacts"], *run.get("input_artifacts", [])]
        entries += [
            a
            for j in manifest["factory_jobs"]
            for a in [*j.get("input_artifacts", []), *j["artifacts"]]
        ]
        artifact_index = {}
        for artifact in entries:
            member = "artifacts/" + artifact["sha256"] + "/" + artifact["name"]
            if member in artifact_index:
                require(
                    artifact_index[member]["bytes"] == artifact["bytes"],
                    "Conflicting artifact metadata",
                )
                continue
            content = archive.read(member)
            require(
                sha(content) == artifact["sha256"] and len(content) == artifact["bytes"],
                "Artifact bytes/SHA mismatch: " + member,
            )
            artifact_index[member] = {"sha256": artifact["sha256"], "bytes": len(content)}

        used = {}

        def array(record, name):
            artifact = one(record["artifacts"], lambda a: a["name"] == name, name)
            member = "artifacts/" + artifact["sha256"] + "/" + name
            values = np.load(io.BytesIO(archive.read(member)), allow_pickle=False)
            require(
                not values.dtype.hasobject and np.isfinite(values).all(),
                "Nonfinite/object plot array",
            )
            used[record["id"] + "/" + name] = {
                **artifact_index[member],
                "zip_member": member,
                "shape": list(values.shape),
                "dtype": str(values.dtype),
            }
            return values

        psf = array(run, "psf_intensity.npy")
        coefficients = array(ray_job, "irradiance_per_radiance_sr.npy")
        electrons = array(run, "mean_electrons.npy")
        raw = array(run, "raw_frames.npy")
        require(
            np.array_equal(psf, array(psf_job, "psf_kernel.npy")),
            "Runtime PSF differs from Factory",
        )
        require(np.array_equal(psf, lens["payload"]["psf"]["values"]), "PSF differs from Lens")
        require(
            np.array_equal(coefficients, response["irradiance_per_radiance_sr"]),
            "E/L differs from Lens",
        )
        require(
            np.all(psf >= 0) and np.allclose(psf.sum(axis=(-2, -1)), 1, rtol=0, atol=1e-10),
            "Conditional PSF is not unit-sum",
        )
        require(
            np.all(electrons >= 0) and electrons.ndim == 2, "Unexpected expected-electron array"
        )
        require(
            np.issubdtype(raw.dtype, np.integer) and raw.shape[1:] == electrons.shape,
            "Unexpected actual RAW geometry",
        )
        projection_job = run["scene_snapshot"]["projection_job_snapshot"]
        require(projection_job["id"] == declared["projection"], "Projection ID mismatch")
        require(
            projection_job["source_render_id"] == verification["source_render_id"],
            "Different source radiance",
        )
        projection = projection_job["request"]["projection"]
        require(
            np.array_equal(
                coefficients[:, 0], projection["on_axis_irradiance_per_radiance_sr"]["values"]
            ),
            "Projection E/L differs from retained Factory array",
        )
        require(
            projection["stop_transmission_sha256"] == response["stop_transmission_sha256"],
            "Projection mask mismatch",
        )
        return {
            "label": label,
            "lens": lens,
            "run": run,
            "psf": psf,
            "coefficients": coefficients,
            "electrons": electrons,
            "raw": raw,
            "mask": response["stop_transmission"],
            "ray_request": ray_job["request"],
            "used_arrays": used,
            "verified_artifacts": artifact_index,
            "manifest_package_hash": manifest["package_hash"],
        }


def build(directory):
    directory = directory.resolve()
    inputs = {}
    verification = load_json(directory / "http-verification.json", inputs)
    require(verification["status"] == "passed", "HTTP verification has not passed")
    cases = [read_case(directory, label, verification, inputs) for label in LABELS]
    clear, quarter, radial = cases
    for case in cases[1:]:
        require(
            {k: v for k, v in case["ray_request"].items() if k != "stop_transmission"}
            == {k: v for k, v in clear["ray_request"].items() if k != "stop_transmission"},
            "Optical prescription changes beyond the explicit stop",
        )
        for key in (
            "exposure_s",
            "frames",
            "pixel_pitch_um",
            "qe",
            "readout",
            "sensor_angular_response",
            "temperature_c",
            "wavelength_nm",
        ):
            require(
                case["run"]["request"][key] == clear["run"]["request"][key],
                "Camera comparison condition differs: " + key,
            )
    psf_meta = clear["lens"]["payload"]["psf"]
    waves = np.asarray(psf_meta["wavelength_nm"])
    for case in cases:
        require(
            case["lens"]["payload"]["psf"]["wavelength_nm"] == waves.tolist(),
            "Different wavelength axes",
        )
        require(case["mask"]["wavelength_nm"] == waves.tolist(), "Mask wavelength mismatch")
    field_y = psf_meta["field_y_mm"].index(0.0)
    field_x = psf_meta["field_x_mm"].index(0.0)
    band = 0
    width = clear["psf"].shape[-1]
    center = width // 2
    offsets = (np.arange(width) - center) * psf_meta["sample_pitch_um"]
    means = np.asarray([case["electrons"].mean() for case in cases])
    require(means[0] > 0, "Clear mean electron count is zero")
    ratios = means / means[0]
    maximum_psf_difference = float(np.max(np.abs(quarter["psf"] - clear["psf"])))
    radial_psf_difference = float(np.max(np.abs(radial["psf"] - clear["psf"])))
    require(
        np.allclose(quarter["psf"], clear["psf"], rtol=0, atol=1e-12), "Uniform stop changed PSF"
    )
    require(
        np.allclose(quarter["electrons"], clear["electrons"] * 0.25, rtol=1e-12, atol=1e-12),
        "Stored quarter electron array is not one quarter of clear",
    )
    require(
        np.allclose(quarter["coefficients"], clear["coefficients"] * 0.25, rtol=1e-12),
        "Quarter E/L is not one quarter of clear",
    )
    require(
        np.isclose(ratios[1], verification["ratios"]["quarter_electrons"], rtol=1e-12)
        and np.isclose(ratios[2], verification["ratios"]["radial_electrons"], rtol=1e-12),
        "Calculated summaries differ from the saved HTTP comparison",
    )
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": "#687786",
            "text.color": "#172D41",
            "axes.labelcolor": "#233C50",
            "xtick.color": "#40566B",
            "ytick.color": "#40566B",
            "savefig.facecolor": "white",
        }
    )
    fig, axes = plt.subplots(2, 2, figsize=(14, 9.4))
    fig.subplots_adjust(left=0.075, right=0.965, bottom=0.12, top=0.85, wspace=0.30, hspace=0.46)
    fig.suptitle(
        "Passive stop transmission: saved-evidence comparison",
        x=0.075,
        y=0.975,
        ha="left",
        fontsize=20,
        fontweight="bold",
    )
    fig.text(
        0.075,
        0.925,
        "Same prescribed optics, sensor, light and exposure.  "
        "Only the declared passive stop changes.",
        fontsize=11,
        color="#506478",
    )
    ax = axes[0, 0]
    for case in cases:
        mask = case["mask"]
        power = np.asarray(mask["power_transmittance"])
        same = np.array_equal(power[0], power[1])
        for wi in [0] if same else range(len(waves)):
            label = NAMES[case["label"]] + (" (both bands)" if same else f" ({waves[wi]:g} nm)")
            ax.plot(
                mask["normalized_radius"],
                power[wi],
                color=COLORS[case["label"]],
                linestyle="-" if wi == 0 else "--",
                marker="o",
                ms=4,
                lw=2,
                label=label,
            )
    ax.set(
        title="A   Declared stop power transmission",
        xlabel="Normalized stop radius, r / R",
        ylabel="Power transmittance",
        xlim=(0, 1),
        ylim=(0, 1.08),
    )
    ax.legend(loc="lower left", fontsize=8.4, frameon=False, ncol=2,
              columnspacing=1.2, handlelength=2)
    ax = axes[0, 1]
    for case in cases:
        profile = case["psf"][band, field_y, field_x, center, :]
        ax.semilogy(
            offsets,
            profile,
            color=COLORS[case["label"]],
            lw=2,
            linestyle="--" if case["label"] == "quarter" else "-",
            label=NAMES[case["label"]],
        )
    ax.set(
        title=f"B   Conditional PSF | on axis | {waves[band]:g} nm",
        xlabel="Horizontal image offset (um)",
        ylabel="Pixel-integrated energy, unit-sum 2D kernel",
        xlim=(offsets[0], offsets[-1]),
    )
    ax.legend(loc="upper right", fontsize=8.8, frameon=False)
    ax.text(
        0.03,
        0.06,
        f"Quarter / clear: max |delta PSF| = {maximum_psf_difference:.2g}\n"
        "Original kernel values; no peak renormalization",
        transform=ax.transAxes,
        fontsize=8.5,
        bbox={"facecolor": "white", "alpha": 0.9, "edgecolor": "none"},
    )
    ax = axes[1, 0]
    positions = np.arange(len(waves))
    bar_width = 0.24
    for i, case in enumerate(cases):
        values = case["coefficients"][:, 0] * 1000
        bars = ax.bar(
            positions + (i - 1) * bar_width,
            values,
            bar_width,
            color=COLORS[case["label"]],
            label=NAMES[case["label"]],
        )
        ax.bar_label(bars, labels=[f"{v:.3f}" for v in values], padding=4, fontsize=9)
    ax.set(
        title="C   Absolute on-axis E/L from ray quadrature",
        xlabel="Wavelength (nm)",
        ylabel=r"E/L coefficient ($10^{-3}$ sr)",
        xticks=positions,
        xticklabels=[f"{w:g}" for w in waves],
        ylim=(0, float(clear["coefficients"].max() * 1000) * 1.22),
    )
    ax.legend(loc="upper right", fontsize=8.5, frameon=False)
    ax = axes[1, 1]
    bars = ax.bar(np.arange(3), means, 0.58, color=[COLORS[label] for label in LABELS])
    ax.bar_label(
        bars,
        labels=[
            f"{mean:.3f} e-\n{ratio:.6f} x clear" for mean, ratio in zip(means, ratios, strict=True)
        ],
        padding=6,
        fontsize=10,
    )
    ax.set(
        title="D   Saved expected photoelectrons",
        ylabel="Mean across native CFA pixels (e- / pixel)",
        xticks=np.arange(3),
        xticklabels=[NAMES[label] for label in LABELS],
        ylim=(0, means.max() * 1.30),
    )
    ax.text(
        0.03,
        0.93,
        f"{clear['electrons'].shape[0]} x {clear['electrons'].shape[1]} native crop; "
        f"{clear['run']['request']['exposure_s'] * 1000:g} ms exposure",
        transform=ax.transAxes,
        fontsize=8.7,
    )
    for ax in axes.flat:
        ax.set_axisbelow(True)
        ax.grid(axis="y", color="#DCE3E9", linewidth=0.65, alpha=0.8)
    fig.text(
        0.075,
        0.060,
        "Evidence: retained NPY bytes verified against SHA-256 and their original ZIP manifests. "
        "Means summarize saved arrays; no simulation rerun.",
        fontsize=9,
        color="#465E72",
    )
    fig.text(
        0.075,
        0.033,
        "Authored scalar optical model; physical accuracy and total uncertainty not established. "
        "Expected electrons precede stochastic RAW readout.",
        fontsize=9,
        color="#465E72",
    )
    image_path = directory / "stop-transmission-comparison.png"
    fig.savefig(image_path, dpi=180)
    plt.close(fig)
    evidence = {
        "schema": "camera_stop_comparison_plot_evidence_v1",
        "status": "passed",
        "scope": (
            "Saved evidence visualization only; no simulator/server/product imports or execution"
        ),
        "inputs": inputs,
        "plot_script": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha(Path(__file__).read_bytes()),
        },
        "output": {
            "path": str(image_path),
            "sha256": sha(image_path.read_bytes()),
            "bytes": image_path.stat().st_size,
        },
        "series": {
            case["label"]: {
                "lens_id": case["lens"]["id"],
                "run_id": case["run"]["id"],
                "manifest_package_hash": case["manifest_package_hash"],
                "used_arrays": case["used_arrays"],
                "verified_artifacts": case["verified_artifacts"],
                "mean_photoelectrons_per_pixel": float(means[i]),
                "relative_to_clear": float(ratios[i]),
                "on_axis_E_over_L_sr": case["coefficients"][:, 0].tolist(),
                "raw_verified_sha256": next(
                    v["sha256"]
                    for k, v in case["used_arrays"].items()
                    if k.endswith("/raw_frames.npy")
                ),
            }
            for i, case in enumerate(cases)
        },
        "derived_checks": {
            "quarter_psf_max_absolute_difference": maximum_psf_difference,
            "radial_psf_max_absolute_difference": radial_psf_difference,
            "quarter_electron_pixelwise_one_quarter": True,
            "quarter_E_over_L_one_quarter": True,
            "http_report_ratios_agree": True,
        },
        "plot_semantics": {
            "transmission": (
                "Original mask nodes connected with its declared linear_power interpolation"
            ),
            "psf": {
                "wavelength_nm": float(waves[band]),
                "field_y_mm": 0,
                "field_x_mm": 0,
                "row_index": center,
                "x_coordinates": "(column - array_center) * sample_pitch_um",
                "normalization": "Original unit-sum 2D kernel; no profile renormalization",
            },
            "E_over_L": "Original Factory array column 0, scaled to 10^-3 sr for display",
            "electrons": "Arithmetic mean of saved mean_electrons.npy across all native CFA pixels",
            "no_error_bars": "Total model uncertainty is not established",
        },
        "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
        "visual_qa": (
            "Inspect the generated PNG separately; script does not claim visual observation"
        ),
    }
    report_path = directory / "plot-evidence.json"
    report_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    print(
        json.dumps(
            {
                "output": evidence["output"],
                "evidence": str(report_path),
                "ratios": ratios.tolist(),
                "quarter_psf_max_delta": maximum_psf_difference,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir", type=Path, default=Path("reports/camera_platform/stop-transmission-v016")
    )
    build(parser.parse_args().report_dir)
