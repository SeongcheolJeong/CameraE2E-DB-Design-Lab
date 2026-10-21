#!/usr/bin/env python3
"""Create a constrained radial PSF and inspect its MTF round-trip from CSV.

Run with PYTHONPATH=src. Exit 0 means numerical input consistency passed;
exit 2 means a candidate was saved but did not meet the requested tolerances.
Neither status establishes physical-camera validation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

import pyisetcam.mtf_psf as mtf_psf_module
from pyisetcam.mtf_psf import measured_mtf_to_psf


def _json_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _read_curve(raw: bytes) -> tuple[np.ndarray, np.ndarray]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if reader.fieldnames is None or any(
        reader.fieldnames.count(key) != 1 for key in ("frequency", "mtf")
    ):
        raise ValueError("CSV must contain exactly one 'frequency' and one 'mtf' column.")
    frequency, mtf = [], []
    for row in reader:
        try:
            frequency.append(float(row["frequency"]))
            mtf.append(float(row["mtf"]))
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"CSV line {reader.line_num}: frequency and mtf must be numeric."
            ) from error
    return np.asarray(frequency), np.asarray(mtf)


def _roundtrip_csv(result: dict[str, Any]) -> bytes:
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(
        [
            "sample_index",
            "frequency",
            "frequency_unit",
            "angle_deg",
            "input_mtf",
            "returned_mtf",
            "residual",
            "abs_error",
        ]
    )
    original = result["input"]
    returned = result["roundtrip"]
    for sample, frequency in enumerate(original["frequency"]):
        for direction, angle in enumerate(returned["angles_deg"]):
            residual = returned["residual_by_angle"][direction, sample]
            writer.writerow(
                [
                    sample,
                    frequency,
                    original["frequency_unit"],
                    angle,
                    original["mtf"][sample],
                    returned["mtf_by_angle"][direction, sample],
                    residual,
                    abs(residual),
                ]
            )
    return text.getvalue().encode("utf-8")


def _plot(result: dict[str, Any]) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from matplotlib.colors import LogNorm

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6), layout="constrained")
    psf = result["psf"]
    spatial = result["spatial_axis_um"]
    half_pitch = result["sample_pitch_um"] / 2
    extent = [
        spatial[0] - half_pitch,
        spatial[-1] + half_pitch,
        spatial[-1] + half_pitch,
        spatial[0] - half_pitch,
    ]
    peak = float(psf.max())
    # The display floor is visual only; psf.npy preserves every original value.
    display_floor = peak * 1e-6
    im = axes[0].imshow(
        np.maximum(psf, display_floor),
        extent=extent,
        norm=LogNorm(vmin=display_floor, vmax=peak),
        cmap="magma",
    )
    axes[0].set(title="PSF (log scale; display floor 1e-6 peak)", xlabel="x [um]", ylabel="y [um]")
    fig.colorbar(im, ax=axes[0], label="Discrete impulse weight")
    original = result["input"]
    returned = result["roundtrip"]
    frequency = original["frequency"]
    axes[1].plot(frequency, original["mtf"], "ko", markersize=3, label="Input samples")
    for index, angle in enumerate(returned["angles_deg"]):
        label = f"{angle:g} deg"
        axes[1].plot(frequency, returned["mtf_by_angle"][index], label=label)
        axes[2].plot(frequency, returned["residual_by_angle"][index], label=label)
    tolerance = returned["max_abs_error_tolerance"]
    axes[2].axhspan(-tolerance, tolerance, color="grey", alpha=0.15, label="Max-error limit")
    axes[2].axhline(0, color="black", linewidth=0.5)
    axes[1].set(title="Input and returned MTF", ylabel="Normalized MTF")
    axes[2].set(title="Returned minus input", ylabel="MTF residual")
    for ax in axes[1:]:
        ax.set_xlabel(f"Frequency [{original['frequency_unit']}]")
        ax.grid(alpha=0.2)
        ax.legend(fontsize=8)
    status = "PASS" if returned["passed"] else "FAIL"
    fig.suptitle(
        f"Numerical consistency: {status} | max error {returned['max_abs_error']:.5g} | "
        f"RMSE {returned['rmse']:.5g}\n"
        "Radial / zero-phase approximation; physical validation not established",
        fontsize=11,
    )
    buffer = io.BytesIO()
    try:
        fig.savefig(buffer, format="png", dpi=150)
        return buffer.getvalue()
    finally:
        plt.close(fig)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_csv",
        type=Path,
        help="UTF-8 CSV with frequency,mtf columns; normalized DC must be first",
    )
    parser.add_argument("--frequency-unit", required=True, choices=("cycles/mm", "cycles/pixel"))
    parser.add_argument(
        "--sample-pitch-um",
        required=True,
        type=float,
        help="Discrete PSF sample spacing in micrometers",
    )
    parser.add_argument(
        "--measurement-pixel-pitch-um",
        type=float,
        help="Pixel pitch for cycles/pixel measurements; defaults to the PSF sample pitch",
    )
    parser.add_argument(
        "--kernel-size",
        type=int,
        default=129,
        help="Odd square kernel side, 3..1025 (default: 129)",
    )
    parser.add_argument("--high-frequency", choices=("zero", "hold"), default="zero")
    parser.add_argument("--negative-policy", choices=("project", "raise"), default="project")
    parser.add_argument("--max-abs-error", type=float, default=0.02)
    parser.add_argument("--rmse-tolerance", type=float, default=0.01)
    parser.add_argument(
        "--measurement-domain",
        choices=("optical", "camera_raw", "camera_isp", "unspecified"),
        default="unspecified",
    )
    parser.add_argument(
        "--source-metadata",
        type=Path,
        help="Optional JSON object with acquisition conditions and provenance",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="New output directory; existing paths are never overwritten",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output = args.output.absolute()
        if output.exists() or output.is_symlink():
            raise ValueError(f"Output path already exists; refusing to overwrite: {output}")
        raw = args.input_csv.read_bytes()
        frequency, mtf = _read_curve(raw)
        metadata = {}
        metadata_raw = None
        if args.source_metadata is not None:
            metadata_raw = args.source_metadata.read_bytes()
            metadata = json.loads(metadata_raw)
            if not isinstance(metadata, dict):
                raise ValueError("Source metadata must be a JSON object.")
        source = {
            "kind": "user_supplied_csv",
            "original_path": str(args.input_csv.resolve()),
            "preserved_file": "input.csv",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "measurement_domain": args.measurement_domain,
            "measurement_metadata": metadata,
            "acquisition_independently_verified": False,
        }
        if metadata_raw is not None:
            source["metadata_provenance"] = {
                "original_path": str(args.source_metadata.resolve()),
                "preserved_file": "source_metadata.json",
                "sha256": hashlib.sha256(metadata_raw).hexdigest(),
            }
        result = measured_mtf_to_psf(
            frequency,
            mtf,
            sample_pitch_um=args.sample_pitch_um,
            measurement_pixel_pitch_um=args.measurement_pixel_pitch_um,
            kernel_size=args.kernel_size,
            frequency_unit=args.frequency_unit,
            high_frequency=args.high_frequency,
            negative_policy=args.negative_policy,
            max_abs_error=args.max_abs_error,
            rmse_tolerance=args.rmse_tolerance,
            source=source,
        )
        result["export"] = {
            "created_at_utc": datetime.now(UTC).isoformat(),
            "tool": "tools/convert_measured_mtf.py",
            "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "algorithm": "pyisetcam.mtf_psf",
            "algorithm_sha256": hashlib.sha256(
                Path(mtf_psf_module.__file__).read_bytes()
            ).hexdigest(),
            "validation_scope": "numerical_consistency_with_supplied_mtf_only",
            "camera_platform_pixel_integrated_contract": "not_qualified",
            "measurement_domain_note": (
                "Optical, RAW and ISP MTFs include different effects. This tool does not "
                "separate them or add/remove sensor aperture, CFA, or ISP processing."
            ),
        }
        psf_bytes = io.BytesIO()
        np.save(psf_bytes, result["psf"], allow_pickle=False)
        artifacts = {
            "input.csv": raw,
            "psf.npy": psf_bytes.getvalue(),
            "report.json": (
                json.dumps(_json_value(result), indent=2, allow_nan=False) + "\n"
            ).encode("utf-8"),
            "roundtrip.csv": _roundtrip_csv(result),
            "comparison.png": _plot(result),
        }
        if metadata_raw is not None:
            artifacts["source_metadata.json"] = metadata_raw
        # Compute and validate everything before creating any output. Exclusive
        # creation also rejects a directory created after the initial check.
        output.mkdir(parents=True, exist_ok=False)
        for name, contents in artifacts.items():
            with (output / name).open("xb") as stream:
                stream.write(contents)
    except (OSError, ValueError, TypeError, ImportError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    returned = result["roundtrip"]
    status = "PASS" if returned["passed"] else "FAIL"
    print(
        f"{status}: max_abs_error={returned['max_abs_error']:.8g}, "
        f"rmse={returned['rmse']:.8g}; {output}"
    )
    print("Numerical input consistency only; physical validation is not established.")
    for warning in result["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    return 0 if returned["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
