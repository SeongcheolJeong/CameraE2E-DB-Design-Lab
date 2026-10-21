"""Road photograph/sequence acquisition, immutable replay and HDR evaluation."""

from __future__ import annotations

# Embedded first-photo HTML retains natural template line boundaries.
# ruff: noqa: E501
import html
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .road_design_inputs import write_json
from .road_hdr_fusion import calibrated_rate, merge, render_common
from .road_hdr_inputs import TITLE, bound_specification, resource_plan
from .road_rgb_source import file_sha256, json_sha256


def timing_record(frame, exposure, timing, *, hdr=False, long_record=None):
    start = int(frame.get("source_timestamp_us", round(frame["timestamp_s"] * 1e6))) * 1000
    end = start + int(round(exposure * 1e9))
    ready = end + int(round(timing["readout_s"] * 1e9))
    if long_record is not None and long_record["readout_complete_ns"] > start:
        raise ValueError("HDR candidate must be completed before the short exposure begins.")
    available = ready + int(
        round((timing["common_processing_s"] + (timing["hdr_processing_s"] if hdr else 0)) * 1e9)
    )
    return {
        "start_ns": start,
        "end_ns": end,
        "readout_complete_ns": ready,
        "result_available_ns": available,
        "latency_source": "declared_assumption_not_simulator_wall_clock",
    }


def schedule(frames, long_exposure, timing):
    if any(b["timestamp_s"] <= a["timestamp_s"] for a, b in zip(frames, frames[1:], strict=False)):
        raise ValueError("Source timestamps must increase.")
    outputs = []
    pending = None
    for i, frame in enumerate(frames):
        if i % 2 == 0:
            pending = {
                "frame": frame,
                "exposure_s": long_exposure,
                **timing_record(frame, long_exposure, timing),
            }
        else:
            eligible = (
                pending
                if pending
                and pending["readout_complete_ns"] <= int(round(frame["timestamp_s"] * 1e9))
                else None
            )
            outputs.append(
                {
                    "frame": frame,
                    "long": eligible,
                    **timing_record(frame, 0.001, timing, hdr=True, long_record=eligible),
                }
            )
    return outputs


def verify_manifest(directory, name):
    directory = Path(directory)
    path = directory / name
    document = json.loads(path.read_text())
    for relative, sha in document["files"].items():
        target = (directory / relative).resolve()
        if (
            not target.is_relative_to(directory.resolve())
            or not target.is_file()
            or file_sha256(target) != sha
        ):
            raise ValueError("Immutable HDR artifact missing or changed: " + relative)
    return document


def manifest(directory, name, exclude=()):
    directory = Path(directory)
    doc = {
        "files": {
            str(p.relative_to(directory)): file_sha256(p)
            for p in directory.rglob("*")
            if p.is_file() and p.name != name and p.name not in exclude and ".tmp" not in p.name
        }
    }
    write_json(directory / name, doc)
    return doc


def acquire(workspace, package, *, stage="first-photo", on_progress=lambda x: None):
    from pyisetcam.sensor_capture import resample_sensor_capture

    from .road_design_compute import (
        compute_mean,
        fixed_profile,
        load_candidate,
        native_coordinates,
        roi_capture_sensor,
    )
    from .saved_sensor_hdr import _calibration, _native_fpn_calibration

    package = Path(package)
    workspace = Path(workspace)
    started = time.monotonic()
    spec, binding = bound_specification(package)
    root = package / "captures"
    root.mkdir(exist_ok=True)
    if (root / "capture-manifest.json").exists():
        prior = verify_manifest(root, "capture-manifest.json")
        if prior.get("specification_sha256") != binding["sha256"] or prior.get("stage") != stage:
            raise ValueError("Acquisition exists for another frozen stage/specification.")
        return json.loads((root / "index.json").read_text())
    plan = resource_plan(workspace, package, spec, stage)
    write_json(package / "resource-plan.json", plan)
    if plan["status"] != "ready":
        raise ValueError("resource_blocked: " + ",".join(plan["reasons"]))
    refinement = 1
    if stage != "first-photo":
        from .road_hdr_sampling import qualify

        qualification = qualify(workspace, package, on_progress)
        if not qualification["qualified"]:
            raise ValueError(
                "sampling_inconclusive: representative source refinement did not stabilize within x4 budget"
            )
        refinement = qualification["selected_source_refinement"]
    frames = (
        [spec["first_photo"]]
        if stage == "first-photo"
        else [f for e in spec["events"] for f in e["frames"]]
    )
    candidate = spec["candidates"][0]
    records = []
    seeds = spec["seeds"][:1] if stage == "first-photo" else spec["seeds"]
    profile = None
    for frame in frames:
        baseline = None
        for condition in spec["illumination"]:
            directory = root / frame["token"] / condition["id"]
            directory.mkdir(parents=True, exist_ok=True)
            optical = directory / "optical"
            optical_spec = {**spec, "_road_hdr_illumination": condition}
            # Reuse linear response at uniform scalar light levels. Never scale captured RAW.
            if condition["bright_multiplier"] == 1 and baseline is not None:
                mean = (
                    baseline["mean"] * condition["multiplier"] / baseline["condition"]["multiplier"]
                )
                report = baseline["report"]
                optics = baseline["optics"]
                sensor = baseline["sensor"]
                coords = baseline["coords"]
                optical.mkdir(exist_ok=True)
                np.save(optical / "mean_electrons.npy", mean)
                write_json(
                    optical / "optical-execution.json",
                    {**report, "linear_mean_reuse": True, "illumination": condition},
                )
            elif (optical / "completed.json").exists():
                saved = verify_manifest(optical, "completed.json")
                if saved["input_sha256"] != json_sha256(
                    [binding["sha256"], frame["token"], condition]
                ):
                    raise ValueError("Optical checkpoint belongs to a different input.")
                mean = np.load(optical / "mean_electrons.npy", mmap_mode="r")
                report = json.loads((optical / "optical-execution.json").read_text())
                optics, sensor = load_candidate(workspace, spec, candidate)
                coords = native_coordinates(optics, sensor, spec["observation"])
            else:
                on_progress("native optical calculation: " + frame["token"] + "/" + condition["id"])
                start = time.monotonic()
                mean, report, optics, sensor, coords = compute_mean(
                    workspace,
                    optical_spec,
                    frame,
                    spec["observation"],
                    candidate,
                    optical,
                    refinement=refinement,
                )
                checkpoint = manifest(optical, "completed.json")
                checkpoint["input_sha256"] = json_sha256(
                    [binding["sha256"], frame["token"], condition]
                )
                checkpoint["elapsed_s"] = time.monotonic() - start
                write_json(optical / "completed.json", checkpoint)
            if condition["bright_multiplier"] == 1:
                baseline = dict(
                    mean=mean,
                    condition=condition,
                    report=report,
                    optics=optics,
                    sensor=sensor,
                    coords=coords,
                )
            bounds = tuple(report["native_roi_bounds"])
            if profile is None:
                isp_profile, color = fixed_profile(
                    sensor,
                    np.asarray(report["spectral_proxy"]["primary_photons"]),
                    spec["spectral_proxy"]["wavelength_nm"],
                )
                from .hdr import default_profile

                profile = default_profile()
                profile["ccm"] = isp_profile["ccm"]["matrix_rgb"]
                # Fixed physically meaningful reference to full-well at 4ms, never per-frame maxima.
                profile["reference_exposure_s"] = 0.004
                profile["tone"]["exposure_scale"] = 1 / float(
                    sensor.fields["sensor_readout"]["full_well_e"]
                )
                write_json(root / "display-profile.json", {"profile": profile, "color": color})
            for seed in seeds:
                for exposure in spec["single_exposures_s"]:
                    location = (
                        directory / f"seed-{seed}" / f"exposure-{int(round(exposure * 1e6))}us"
                    )
                    location.mkdir(parents=True, exist_ok=True)
                    if (location / "completed.json").exists():
                        verify_manifest(location, "completed.json")
                        record = json.loads((location / "capture.json").read_text())
                        records.append(record)
                        continue
                    charge = mean * exposure / spec["capture"]["exposure_s"]
                    capture_sensor = roi_capture_sensor(sensor, bounds, charge)
                    capture_sensor.fields["integration_time"] = exposure
                    rng_seed = int(
                        json_sha256([frame["token"], condition["id"], seed, exposure])[:15], 16
                    )
                    captured = resample_sensor_capture(capture_sensor, seed=rng_seed)
                    raw = captured.data["raw_dn"]
                    info = captured.metadata["sensor_capture"]
                    scalar = _calibration(info, raw.shape)
                    cal, maps = _native_fpn_calibration(captured, scalar)
                    native = {
                        "kind": "native_sensor",
                        "full_sensor_shape": list(sensor.fields["size"]),
                        "crop_origin": [bounds[0], bounds[2]],
                    }
                    rate = calibrated_rate(raw, exposure, cal, native)
                    if np.min(raw) < 0 or np.max(raw) > 65535:
                        raise ValueError("Lossless uint16 RAW storage unavailable.")
                    np.save(location / "raw.npy", raw.astype(np.uint16))
                    np.savez(location / "calibration.npz", **cal)
                    np.savez(location / "rate.npz", **rate)
                    record = {
                        "frame_token": frame["token"],
                        "timestamp_s": frame["timestamp_s"],
                        "condition": condition["id"],
                        "seed": seed,
                        "actual_seed": rng_seed,
                        "exposure_s": exposure,
                        "geometry": native,
                        "bounds": list(bounds),
                        "source_refinement": refinement,
                        "raw_shape": list(raw.shape),
                        "raw_sha256": file_sha256(location / "raw.npy"),
                        "path": str(location.relative_to(root)),
                        "capture": info,
                        **timing_record(frame, exposure, spec["timing"]),
                        "FPN_policy": "same model maps calibrated identically in all arms; oracle model coefficients, not measured",
                        "specification_sha256": binding["sha256"],
                    }
                    write_json(location / "capture.json", record)
                    manifest(location, "completed.json")
                    records.append(record)
                    on_progress("RAW preserved: " + record["path"])
                    del captured, capture_sensor, rate, charge, cal, maps
            del mean
        del baseline
    result = {
        "schema": "camera_road_hdr_capture_package_v1",
        "stage": stage,
        "specification_sha256": binding["sha256"],
        "records": records,
        "original_RAW_retained": True,
        "acquisition_wall_clock_is_vehicle_latency": False,
        "elapsed_acquisition_s": time.monotonic() - started,
        "peak_rss_bytes": __import__("resource")
        .getrusage(__import__("resource").RUSAGE_SELF)
        .ru_maxrss,
        "peak_rss_scope": "process peak; macOS reports bytes, not a configured hardware limit",
        "sampling_manifest_sha256": file_sha256(package / "sampling/manifest.json")
        if stage != "first-photo"
        else None,
    }
    write_json(root / "index.json", result)
    doc = manifest(root, "capture-manifest.json")
    doc.update(specification_sha256=binding["sha256"], stage=stage)
    write_json(root / "capture-manifest.json", doc)
    return result


def reprocess(workspace, package, *, profiles=("global", "local"), on_progress=lambda x: None):
    from .road_design_compute import load_candidate, native_coordinates

    package = Path(package)
    spec, binding = bound_specification(package)
    root = package / "captures"
    verify_manifest(root, "capture-manifest.json")
    index = json.loads((root / "index.json").read_text())
    if (
        index.get("sampling_manifest_sha256") is not None
        and file_sha256(package / "sampling/manifest.json") != index["sampling_manifest_sha256"]
    ):
        raise ValueError("Acquisition sampling qualification changed.")
    if index["specification_sha256"] != binding["sha256"]:
        raise ValueError("Capture belongs to another specification.")
    implementation = {
        name: file_sha256(Path(__file__).with_name(name))
        for name in ("road_hdr_fusion.py", "road_hdr_study.py")
    }
    output = (
        package
        / "reprocessing"
        / json_sha256(
            [
                binding["sha256"],
                file_sha256(root / "capture-manifest.json"),
                list(profiles),
                implementation,
            ]
        )[:16]
    )
    if (output / "manifest.json").exists():
        verify_manifest(output, "manifest.json")
        return json.loads((output / "result.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    optics, sensor = load_candidate(Path(workspace), spec, spec["candidates"][0])
    coords = native_coordinates(optics, sensor, spec["observation"])
    profile = json.loads((root / "display-profile.json").read_text())["profile"]

    def read(record):
        with np.load(root / record["path"] / "rate.npz") as a:
            return {k: a[k] for k in a.files}

    records = index["records"]
    rendered = []
    grouped = {}
    for rec in records:
        grouped.setdefault((rec["frame_token"], rec["condition"], rec["seed"]), {})[
            rec["exposure_s"]
        ] = rec
    all_frames = (
        [spec["first_photo"]]
        if index["stage"] == "first-photo"
        else [f for e in spec["events"] for f in e["frames"]]
    )
    frame_by_token = {f["token"]: f for f in all_frames}

    def save(arm, rec, arrays, timing, input_raws, summary=None):
        directory = output / rec["frame_token"] / rec["condition"] / f"seed-{rec['seed']}" / arm
        directory.mkdir(parents=True, exist_ok=True)
        linear, valid, rgb = render_common(arrays, sensor, coords, rec["bounds"], profile)
        np.save(directory / "linear-rgb.npy", linear)
        np.save(directory / "valid.npy", valid)
        Image.fromarray(rgb).save(directory / "rgb.png")
        Image.fromarray((valid * 255).astype(np.uint8)).save(directory / "valid.png")
        if summary is not None:
            np.savez_compressed(
                directory / "native-fusion.npz",
                **{k: v for k, v in arrays.items() if isinstance(v, np.ndarray)},
            )
        result = {
            "arm": arm,
            "event_id": next(
                (
                    e["id"]
                    for e in spec["events"]
                    if any(f["token"] == rec["frame_token"] for f in e["frames"])
                ),
                "first-photo",
            ),
            "frame_token": rec["frame_token"],
            "timestamp_s": rec["timestamp_s"],
            "condition": rec["condition"],
            "seed": rec["seed"],
            "native_shape": rec["raw_shape"],
            "common_shape": [360, 640],
            "path": str(directory.relative_to(output)),
            "source_raws": input_raws,
            "timing": timing,
            "summary": summary,
            "display_valid_fraction": float(valid.mean()),
            "display_profile_sha256": json_sha256(profile),
        }
        write_json(directory / "output.json", result)
        rendered.append(result)

    for key, exposures in grouped.items():
        for exposure, rec in exposures.items():
            save(
                f"single-{int(exposure * 1e6)}us",
                rec,
                read(rec),
                {
                    k: rec[k]
                    for k in ("start_ns", "end_ns", "readout_complete_ns", "result_available_ns")
                },
                [rec["raw_sha256"]],
            )
        short = exposures.get(0.001)
        if short is None:
            continue
        if index["stage"] == "first-photo":
            pairs = [(long, exposures[long]) for long in spec["long_exposures_s"]]
        else:
            event = next(
                e
                for e in spec["events"]
                if any(f["token"] == short["frame_token"] for f in e["frames"])
            )
            pairs = []
            for long in spec["long_exposures_s"]:
                scheduled = next(
                    (
                        s
                        for s in schedule(event["frames"], long, spec["timing"])
                        if s["frame"]["token"] == short["frame_token"]
                    ),
                    None,
                )
                if scheduled is not None:
                    lr = (
                        None
                        if scheduled["long"] is None
                        else grouped[(scheduled["long"]["frame"]["token"], key[1], key[2])][long]
                    )
                    pairs.append((long, lr))
        for long, long_rec in pairs:
            ref = read(short)
            for mode in profiles:
                if mode == "static" and index["stage"] != "first-photo":
                    raise ValueError("Static profile cannot process road sequences.")
                if long_rec is None:
                    merged = {**ref, "summary": {"status": "no_completed_past_long_reference_only"}}
                else:
                    merged = merge(
                        ref,
                        read(long_rec),
                        profile=mode,
                        static_input=index["stage"] == "first-photo",
                    )
                timing = timing_record(
                    frame_by_token[short["frame_token"]],
                    0.001,
                    spec["timing"],
                    hdr=index["stage"] != "first-photo",
                    long_record=long_rec if index["stage"] != "first-photo" else None,
                )
                save(
                    f"hdr-{int(long * 1e6)}us-{mode}",
                    short,
                    merged,
                    timing,
                    [short["raw_sha256"]] + ([long_rec["raw_sha256"]] if long_rec else []),
                    merged["summary"],
                )
                on_progress("reprocessed: " + short["frame_token"] + "/" + mode)
                del merged
            del ref
    result = {
        "status": "actual_first_photo_HDR_connection"
        if index["stage"] == "first-photo"
        else "causal_HDR_sequence_rendered",
        "title": TITLE,
        "stage": index["stage"],
        "outputs": rendered,
        "profiles": list(profiles),
        "capture_manifest_sha256": file_sha256(root / "capture-manifest.json"),
        "specification_sha256": binding["sha256"],
        "implementation_sha256": implementation,
        "result_directory": str(output),
        "braking_evaluated": False,
        "recommendation": "not_established",
        "static_photo_timing_is_not_causal_sequence": index["stage"] == "first-photo",
    }
    if index["stage"] == "first-photo":
        from .road_hdr_perception import diagnose_first_photo

        result["single_frame_perception"] = diagnose_first_photo(output, spec, rendered)
    write_json(output / "result.json", result)
    report(output, spec, result)
    manifest(output, "manifest.json")
    # Confirm reprocessing never mutated original acquisition files.
    verify_manifest(root, "capture-manifest.json")
    return result


def report(output, spec, result):
    if result["stage"] != "first-photo":
        from .road_hdr_report import sequence_report

        sequence_report(output, spec, result)
        return
    cards = []
    for rec in result["outputs"]:
        path = html.escape(rec["path"], quote=True)
        cards.append(
            f"<figure><img src='{path}/rgb.png'><figcaption>{html.escape(rec['arm'])} · {html.escape(rec['condition'])}<br>valid {rec['display_valid_fraction']:.3f}<br>native {rec['native_shape']}<br>available {rec['timing']['result_available_ns']} ns<br><a href='{path}/output.json'>수치·원본 연결</a> · <a href='{path}/valid.png'>유효 영역</a> · <a href='{path}/detections.png'>단일 프레임 검출</a></figcaption></figure>"
        )
    source = (
        spec["first_photo"] if result["stage"] == "first-photo" else spec["events"][0]["frames"][0]
    )
    original = Image.open(source["source_path"]).convert("RGB")
    from .road_rgb_source import tangent_to_pixels

    box = spec["observation"]["tangent_bounds_xyxy"]
    u, v = tangent_to_pixels(
        np.array([box[0], box[2]]),
        np.array([box[1], box[3]]),
        source["camera"]["camera_intrinsic"],
        source["camera"]["camera_distortion"],
    )
    draw = ImageDraw.Draw(original)
    draw.rectangle([float(u[0]), float(v[0]), float(u[1]), float(v[1])], outline="yellow", width=4)
    original.save(output / "original.jpg")
    body = f"""<!doctype html><meta charset='utf-8'><title>{TITLE}</title>
<style>body{{background:#101827;color:#e8eef7;font:16px system-ui;margin:24px}}main{{display:flex;flex-wrap:wrap;gap:12px}}figure{{margin:0;width:640px;background:#1d2a3e;padding:12px}}img{{width:100%}}a{{color:#8bc8ff}}figcaption{{line-height:1.5}}</style>
<h1>{TITLE}</h1><p>RGB-derived spectral proxy · geometric PSF · 모델 기반 FPN 보정 · 실제 native RAW → 640×360.</p>
<p>검은 영역은 잡음·포화 guard 및 재표본화의 무효 지원 영역입니다. 카메라의 물리적인 암부 자체와 구분하세요. 단일 프레임 검출은 안정 인지·제동 개선 평가가 아닙니다.</p><p>취득/처리 지연은 선언 가정입니다. 현재 상태: {html.escape(result["status"])}. 인지·제동 개선 권장은 아직 확립되지 않았습니다.</p>
<details><summary>원본 도로 사진 — 전체 사진을 재촬영한 결과가 아닙니다</summary><img style='max-width:900px' src='original.jpg'></details>
<p><a href='result.json'>전체 결과·시간·원본 RAW 해시</a></p><main>{"".join(cards)}</main>"""
    (output / "report.html").write_text(body)


def run(workspace, package, action="first-photo", on_progress=lambda x: None):
    if action in ("first-photo", "acquire"):
        return acquire(
            workspace,
            package,
            stage="first-photo" if action == "first-photo" else "full",
            on_progress=on_progress,
        )
    if action == "reprocess":
        first = (
            json.loads((Path(package) / "captures/index.json").read_text())["stage"]
            == "first-photo"
        )
        return reprocess(
            workspace,
            package,
            profiles=("static", "global", "local") if first else ("global", "local"),
            on_progress=on_progress,
        )
    if action == "evaluate":
        from .road_hdr_evaluation import evaluate_study

        return evaluate_study(workspace, package, on_progress=on_progress)
    raise ValueError("Unsupported road HDR action.")
