"""Recompute the ORIGINAL PWM counterexample without opening a writable registry.

This is a numerical research study, not a Worker/install/physical qualification.
Read the archived run and its content-addressed input blobs, verify baseline RAW
parity, and change only explicitly declared analog gain/phase/noise variables.
Every invocation writes to a NEW output directory; historical evidence is never
rewritten. No server, model registration, package installation or network is used.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import sys
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from camera_platform import __version__  # noqa: E402
from camera_platform.electrical_control_runtime import run  # noqa: E402
from camera_platform.electrical_isp_source import _replay_controller  # noqa: E402
from camera_platform.imaging import _scene_input  # noqa: E402

SOURCE = ROOT / "reports/camera_platform/pwm-period-v0161/http/run-7cfe3f379e64437ca5c4d2040f4489b8.json"
STATE = ROOT / "outputs/camera-platform-pwm-period-v0161-qa"


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False).encode("utf-8")


class FrozenEvidence:
    """Minimal read-only adapter for existing scene materialization."""

    def __init__(self, record: dict, state: Path):
        self.models = {model["id"]: model for model in record["model_snapshots"]}
        self.blobs = state / "blobs"
        self.read_hashes: dict[str, str] = {}

    def get(self, kind: str, identity: str):
        if kind != "models" or identity not in self.models:
            raise ValueError(f"Unavailable frozen source: {kind}/{identity}")
        return deepcopy(self.models[identity])

    def blob_path(self, checksum: str) -> Path:
        if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError("Invalid source blob checksum")
        path = self.blobs / checksum
        content = path.read_bytes()
        if sha(content) != checksum:
            raise ValueError(f"Historical source blob checksum mismatch: {checksum}")
        self.read_hashes[checksum] = str(path)
        return path


def load_original(source: Path, state: Path):
    content = source.read_bytes()
    record = json.loads(content)
    if "request" not in record:
        raise ValueError(f"Expected an archived run record, got keys {list(record)}")
    registry = FrozenEvidence(record, state)
    service = SimpleNamespace(registry=registry)
    request = deepcopy(record["request"])
    scene = record["scene_sequence_snapshot"]
    lens_id = next(m["id"] for m in record["model_snapshots"] if m["kind"] == "lens")
    request["scene_sequence"] = {
        **{key: deepcopy(value) for key, value in scene["payload"].items() if key != "knots"},
        "knots": [
            {"time_ns": knot["time_ns"], "scene": _scene_input(service, image, lens_model_id=lens_id)}
            for knot, image in zip(scene["payload"]["knots"], scene["image_snapshots"], strict=True)
        ],
    }
    artifact = next(a for a in record["artifacts"] if a["name"] == "raw_groups.npy")
    raw_bytes = registry.blob_path(artifact["sha256"]).read_bytes()
    original_raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False)
    waveform = request["capture_program"]["waveform"]
    if (waveform["period_ns"], waveform["on_time_ns"], tuple(original_raw.shape)) != (
        8_000_000, 4_000_000, (4, 1, 8, 8)
    ):
        raise ValueError("Source is not the frozen 8x8, four-frame, 8ms/4ms PWM counterexample")
    if request["capture_program"]["electrical_model"]["schema"] != "camera_electrical_readout_v1":
        raise ValueError("This study requires the original single linear readout")
    return record, request, original_raw, registry, sha(content), artifact


def metrics(output: dict, elapsed: float) -> dict:
    arrays, summary = output["arrays"], output["summary"]
    trace = summary["electrical_control"]
    frames = trace["frames"]
    observations = [float(frame["statistics"]["value_dn"]) for frame in frames]
    target = trace["policy"]["target_black_relative_dn"]
    distances = [max(target["minimum"] - x, 0.0, x - target["maximum"]) for x in observations]
    mean = float(np.mean(observations))
    overflow = {key: float(np.sum(value)) for key, value in arrays.items()
                if key.startswith("retained_") and "overflow" in key}
    return {
        "observed_dn": observations,
        "in_target_frames": sum(distance == 0 for distance in distances),
        "worst_target_distance_dn": max(distances),
        "frame_span_dn": max(observations) - min(observations),
        "relative_frame_span": ((max(observations) - min(observations)) / mean if mean > 0 else None),
        "max_digital_high_rail_fraction": max(float(f["statistics"]["digital_high_rail_fraction"]) for f in frames),
        "max_digital_low_rail_fraction": max(float(f["statistics"]["digital_low_rail_fraction"]) for f in frames),
        "overflow_ledger_sums_e_not_additive": overflow,
        "overflow_observed": any(value > 0 for value in overflow.values()),
        "final_trapped_e": float(arrays["retained_trapped_e"][-1].sum()),
        "applied_exposure_ns": arrays["applied_exposure_ns"].tolist(),
        "raw_array_sha256": sha(arrays["raw_groups"].tobytes()),
        "runtime_seconds": elapsed,
    }


def evaluate(request: dict):
    started = time.perf_counter()
    output = run(request)
    elapsed = time.perf_counter() - started
    actual, receipt = _replay_controller(
        output["arrays"]["raw_groups"][:, 0], output["summary"]["raw_formats"][0],
        request["control_sequence"], request["capture_program"], output["summary"]["geometry"],
    )
    if actual != output["summary"]["control_sequence"] or receipt != output["summary"]["electrical_control"]:
        raise AssertionError("Original-RAW causal replay disagrees with the realized acquisition")
    return output, metrics(output, elapsed)


def write_report(directory: Path, report: dict):
    (directory / "results.json").write_bytes(encode(report))
    rows = report["candidates"]
    with (directory / "candidates.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["case", "gain", "phase_shift_ns", "noise_enabled", "seed", "in_target_frames",
                  "worst_target_distance_dn", "relative_frame_span", "max_digital_high_rail_fraction",
                  "overflow_observed", "runtime_seconds"]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# Original PWM counterexample: actual runtime candidate study", "",
             f"Source version: `{__version__}`. Numerical research; not hardware or installed-app qualification.", "",
             "Original 8x8 / 4-frame / 8 ms period / 4 ms ON scene is retained.",
             "Gain changes are explicit hypothetical analog-readout inputs, NOT an expansion of registered model validity.",
             "Every candidate recomputes camera physics and integer RAW. No source RAW scaling is used.", "",
             f"Baseline original RAW exact match: **{report['baseline_raw_exact']}**.",
             f"Noise-off envelope: **{report['noise_off_claim']}**.", "",
             "## Original phase, original seed, noise off", "",
             "| Gain | Observed DN, frames 0-3 | Target hits | Worst miss DN | Relative span | Overflow |",
             "|---:|---|---:|---:|---:|---|"]
    for row in rows:
        if row["phase_shift_ns"] == 0 and not row["noise_enabled"]:
            lines.append(f"| {row['gain']:g} | " + " / ".join(f"{x:.3f}" for x in row["observed_dn"])
                         + f" | {row['in_target_frames']}/4 | {row['worst_target_distance_dn']:.3f}"
                         + f" | {row['relative_frame_span']:.5f} | {row['overflow_observed']} |")
    lines += ["", "## Phase and noise sample coverage", "",
              "| Gain | Target frames / sampled frames | Worst miss DN | Maximum relative span |",
              "|---:|---:|---:|---:|"]
    for gain in sorted({row["gain"] for row in rows}):
        selected = [row for row in rows if row["gain"] == gain]
        lines.append(f"| {gain:g} | {sum(row['in_target_frames'] for row in selected)}/{4 * len(selected)}"
                     + f" | {max(row['worst_target_distance_dn'] for row in selected):.3f}"
                     + f" | {max(row['relative_frame_span'] for row in selected):.5f} |")
    lines += ["", "## Interpretation limits", "",
              "Brightness target satisfaction is not LFM improvement. Lower readout gain cannot undo upstream well saturation.",
              "Relative temporal variation and charge/trap history are reported, not removed.",
              "The 4-frame window includes the authored zero-state transient; no steady-state or long-duration claim is made.",
              "Noise-on data are finite simulation samples, not measured SNR or a statistical guarantee.",
              "The original sensor is not proven to support gain below one; practical hardware feasibility is separate.",
              "No independent physical measurements, temperature sweep, moving target or task-level perception test is included.",
              "No model registration, Worker process, browser, install, database or primary-app mutation was performed.", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


def summarize_saved_samples(directory: Path) -> None:
    """Analyze already saved RAW; never rerun physics or overwrite evidence."""
    directory = directory.resolve()
    if ROOT not in directory.parents:
        raise ValueError("Study directory must be inside the checkout")
    destinations = [directory / "noise-analysis.json", directory / "noise-analysis.md"]
    if any(path.exists() for path in destinations):
        raise FileExistsError("Noise analysis already exists; preserve it rather than overwrite")
    result_bytes = (directory / "results.json").read_bytes()
    results = json.loads(result_bytes)
    if results["status"] != "completed" or not results["baseline_raw_exact"]:
        raise ValueError("Only a completed, baseline-matched study can be summarized")
    raw_by_case = {}
    for row in results["candidates"]:
        label = row["case"]
        if Path(label).name != label or "/" in label or "\\" in label:
            raise ValueError("Invalid saved case name")
        with np.load(directory / f"{label}.npz", allow_pickle=False) as saved:
            raw = saved["raw_groups"].copy()
        if sha(raw.tobytes()) != row["raw_array_sha256"]:
            raise ValueError("Saved candidate RAW differs from its recorded fingerprint")
        raw_by_case[label] = raw.astype(float)
    gains = sorted({row["gain"] for row in results["candidates"]})
    phases = sorted({row["phase_shift_ns"] for row in results["candidates"]})
    noise = []
    for gain in gains:
        for phase in phases:
            rows = [row for row in results["candidates"] if row["gain"] == gain
                    and row["phase_shift_ns"] == phase and row["noise_enabled"]]
            if len(rows) < 2:
                continue
            stack = np.stack([raw_by_case[row["case"]] for row in rows])
            # Variance is across independent seeds at EACH fixed frame/pixel,
            # not spatial texture or the deterministic four-frame transient.
            rms = float(np.sqrt(np.mean(np.var(stack, axis=0, ddof=1))))
            noise.append({"gain": gain, "phase_shift_ns": phase,
                          "seed_count": len(rows), "seeds": [row["seed"] for row in rows],
                          "full_sensor_repeat_noise_rms_dn": rms,
                          "gain_normalized_repeat_noise_rms_dn": rms / gain})
    controls = {(row["phase_shift_ns"], row["seed"]): row for row in results["candidates"]
                if row["gain"] == 1 and not row["noise_enabled"]}
    source_path = Path(results.get("source_record_path", str(SOURCE)))
    source_bytes = source_path.read_bytes()
    if sha(source_bytes) != results["source_record_sha256"]:
        raise ValueError("Original record differs from the one used in the saved study")
    original = json.loads(source_bytes)
    black = original["result"]["summary"]["raw_formats"][0]["black_level_dn"]
    white = 2 ** original["result"]["summary"]["raw_formats"][0]["bit_depth"] - 1
    digital_gain = original["request"]["capture_program"]["electrical_model"]["digital_gain"]
    quantization = []
    for row in results["candidates"]:
        if row["noise_enabled"]:
            continue
        baseline = controls[(row["phase_shift_ns"], row["seed"])]
        reference = raw_by_case[baseline["case"]]
        candidate = raw_by_case[row["case"]]
        gain = row["gain"]
        if digital_gain != 1 or np.any((reference <= 0) | (reference >= white)
                                      | (candidate <= 0) | (candidate >= white)):
            quantization.append({"case": row["case"], "passed": None,
                                 "reason": "two-rounding formula requires unit digital gain and non-railed codes"})
            continue
        error = float(np.max(np.abs((candidate - black) - gain * (reference - black))))
        bound = (1 + gain) / 2
        if error > bound + 1e-9:
            raise AssertionError("Noise-free analog gain response exceeds the two-ADC rounding bound")
        quantization.append({"case": row["case"], "maximum_error_dn": error,
                             "two_rounding_bound_dn": bound, "passed": True})
    analysis = {"source_results_sha256": sha(result_bytes), "analysis_kind": "postprocess_saved_raw_no_simulation",
                "noise": noise, "independent_gain_scaling_checks": quantization,
                "limitations": ["Only three authored seeds per phase, not measured sensor SNR.",
                    "Gain-normalized noise uses gain-one DN units, not measured electrons.",
                    "All four frames and all pixels included; repeat variance is calculated at matched frame/pixel positions."]}
    destinations[0].write_bytes(encode(analysis))
    lines = ["# Noise and gain-response analysis of saved RAW", "",
             "No camera simulations were rerun for this analysis. Three noise seeds per phase.",
             "RMS repeat noise is computed from sample variance across seeds at fixed frame/pixel positions.",
             "Dividing by gain expresses noise in equivalent gain-one DN; it is not a measured electron calibration.", "",
             "| Gain | Repeat noise RMS DN range over phases | Gain-normalized noise range |",
             "|---:|---:|---:|"]
    for gain in gains:
        selected = [row for row in noise if row["gain"] == gain]
        if not selected:
            continue
        values = [row["full_sensor_repeat_noise_rms_dn"] for row in selected]
        normalized = [row["gain_normalized_repeat_noise_rms_dn"] for row in selected]
        lines.append(f"| {gain:g} | {min(values):.3f}–{max(values):.3f}"
                     + f" | {min(normalized):.3f}–{max(normalized):.3f} |")
    passed_count = sum(item["passed"] is True for item in quantization)
    lines += ["", f"Independent linear gain/ADC-rounding checks: {passed_count} passed.",
              "Lower code noise alone does not demonstrate improved input-referred information or flicker mitigation.", ""]
    destinations[1].write_text("\n".join(lines), encoding="utf-8")
    print(destinations[1], flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--summarize-existing", type=Path, help="Analyze saved RAW without rerunning simulations")
    parser.add_argument("--sweep", action="store_true", help="Add quarter-period phase offsets and three noise seeds")
    parser.add_argument("--gains", type=float, nargs="+", default=[1.0, 0.5, 0.4, 0.42])
    args = parser.parse_args()
    if args.summarize_existing is not None:
        summarize_saved_samples(args.summarize_existing)
        return 0
    if not args.gains or len(args.gains) > 8 or 1.0 not in args.gains or any(
        not math.isfinite(gain) or gain <= 0 for gain in args.gains
    ) or len(set(args.gains)) != len(args.gains):
        parser.error("Use up to eight distinct positive gains including the 1.0 control")
    record, original, raw, evidence, source_hash, raw_artifact = load_original(args.source, args.state)
    print(json.dumps({"source_run_id": record["id"], "source_sha256": source_hash,
                      "raw_shape": list(raw.shape), "source_noise_enabled": original.get("noise_enabled"),
                      "seed": original["seed"], "waveform": original["capture_program"]["waveform"],
                      "scene_source_uris": [x["source_uri"] for x in record["scene_sequence_snapshot"]["image_snapshots"]]},
                     ensure_ascii=False), flush=True)
    if args.inspect:
        return 0
    if args.output is None:
        parser.error("--output is required (must not exist)")
    directory = args.output.resolve()
    if directory == ROOT or ROOT not in directory.parents or directory.exists():
        parser.error("Output must be a new directory inside this checkout")
    directory.mkdir(parents=True, exist_ok=False)
    report = {"source_version": __version__, "source_run_id": record["id"],
              "source_record_path": str(args.source.resolve()),
              "source_record_sha256": source_hash, "original_raw_artifact": raw_artifact,
              "execution_kind": "direct_runtime_research_not_worker_or_registry",
              "physical_validation": "not_established", "baseline_raw_exact": False, "candidates": []}
    phases = [0, 2_000_000, 4_000_000, 6_000_000] if args.sweep else [0]
    source_seed = int(original["seed"])
    noise_seeds = [source_seed, source_seed + 1, source_seed + 2] if args.sweep else []
    manifest = {"source_record_sha256": source_hash, "gains": args.gains,
                "phase_shifts_ns": phases, "noise_seeds": noise_seeds,
                "hypothesis": "A declared sub-unity analog readout gain can satisfy the code target at fixed 8ms exposure.",
                "acceptance_before_results": {"baseline_raw": "exact array and dtype equality",
                    "target_dn": [200, 300], "noise_off_candidate": "all sampled frames in target",
                    "gain_only_charge_state": "exact retained ledger equality for matched phase/seed/noise",
                    "physical_lfm": "not claimed; track normalized variation and saturation separately"}}
    (directory / "study-plan.json").write_bytes(encode(manifest))
    try:
        baseline, baseline_metrics = evaluate(original)
        report["baseline_raw_exact"] = bool(raw.dtype == baseline["arrays"]["raw_groups"].dtype
                                              and np.array_equal(raw, baseline["arrays"]["raw_groups"]))
        if not report["baseline_raw_exact"]:
            raise AssertionError("Baseline does not reproduce historical RAW: stop before comparing candidates")
        np.savez_compressed(directory / "original-baseline.npz", **baseline["arrays"])
        combinations = [(False, source_seed)] + [(True, seed) for seed in noise_seeds]
        for noise, seed in combinations:
            for phase in phases:
                reference_state = None
                for gain in [1.0] + [value for value in args.gains if value != 1.0]:
                    request = deepcopy(original)
                    request.update(noise_enabled=noise, seed=seed)
                    request["capture_program"]["waveform"]["phase_origin_ns"] += phase
                    for frame in request["control_sequence"]["frames"]:
                        frame["applied"]["gain"] = gain
                    output, result = evaluate(request)
                    ledger = {key: value for key, value in output["arrays"].items() if key.startswith("retained_")}
                    if reference_state is None:
                        reference_state = ledger
                    elif any(not np.array_equal(value, reference_state[key]) for key, value in ledger.items()):
                        raise AssertionError("Changing downstream gain altered the matched retained-charge ledger")
                    label = f"g{gain:g}-p{phase}-n{int(noise)}-s{seed}"
                    result.update(case=label, gain=gain, phase_shift_ns=phase, noise_enabled=noise, seed=seed,
                                  retained_ledger_equal_to_gain_one=True)
                    report["candidates"].append(result)
                    np.savez_compressed(directory / f"{label}.npz", **output["arrays"])
                    print(json.dumps(result, ensure_ascii=False), flush=True)
        for checksum, path in evidence.read_hashes.items():
            if sha(Path(path).read_bytes()) != checksum:
                raise AssertionError("Source evidence changed while the study was running")
        passing = [gain for gain in args.gains if all(
            row["in_target_frames"] == 4 for row in report["candidates"]
            if row["gain"] == gain and not row["noise_enabled"])]
        report.update(status="completed", baseline_metrics=baseline_metrics,
                      noise_off_passing_gains=passing,
                      noise_off_claim=("sampled code-target candidates found" if passing else "no sampled candidate met every target"),
                      source_blobs_unchanged=True, source_record_unchanged=sha(args.source.read_bytes()) == source_hash,
                      input_blob_hashes=sorted(evidence.read_hashes))
        write_report(directory, report)
        print(f"RESULTS {directory / 'report.md'}", flush=True)
        return 0
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        (directory / "failed.json").write_bytes(encode(report))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
