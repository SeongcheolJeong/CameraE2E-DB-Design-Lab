"""Reference Sensor Mode Lab: source-pinned readout experiments, no RAW resizing."""

from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path

import numpy as np

from . import design_workshop_storage as storage
from . import reference_workshop_job as workshop
from .registry import IntegrityError, canonical, digest
from .virtual_lab_job import sha256

SCHEMA = "camera_reference_sensor_mode_study_v1"
ROUTE = workshop.ROUTE + "/sensor-mode-studies"
CHARTS = ("uniform", "resolution", "texture", "color", "demosaic_chirp", "dynamic_range", "dark")
READOUT_RECIPES = {"gain_budget", "noise_structure", "adc_headroom"}
ASSUMPTIONS = [
    "Reference patent optics and IMX462-derived structure; hypothetical readout modes, "
    "not IMX462 product modes.",
    "Fixed 4 ms, gain 1, 25 C, RGGB; eight independent temporal acquisitions, "
    "persistent fixed patterns.",
    "Same-site binning combines four separated sites; actual centroids are "
    "retained for reconstruction.",
    "Binned display is normalized by four contributing pixels; interpolation "
    "adds no acquired detail.",
    "Fixed baseline color matrix, LSC once, bilinear reconstruction, no NR or sharpen; "
    "no frame brightness fit.",
    "Geometric optical PSF and model-derived readout; physical validation is not established.",
]
READOUT_ASSUMPTIONS = [
    "Reference patent optics and IMX462-derived structure; hypothetical readout experiments, "
    "not measured IMX462 product modes.",
    "Same cached mean photoelectron response, 4 ms, 25 C, native RGGB geometry and eight "
    "fresh readouts per mode; source RAW is never gain-scaled or reused as a new acquisition.",
    "Reference source gain is 1; analog, digital and ADC settings are explicit per mode.",
    "Fixed baseline color matrix and LSC once; electron-equivalent bilinear ISP, "
    "no NR, sharpening or frame brightness fit.",
    "Temporal noise comes from per-pixel repeated-frame variance; scene structure and "
    "persistent fixed patterns are not temporal noise.",
    "Dark chart declares zero incident photoelectron rate; model dark current and readout "
    "noise remain enabled. It is not a measured dark frame.",
    "Geometric optical PSF and model-derived readout; physical validation is not established.",
]


def template():
    from .dcg import default_profile
    from .reference_readout_lab import default_settings

    return {
        "schema": SCHEMA,
        "name": "Reference Sensor Mode · Binning",
        "recipe": "binning",
        "source": {
            "run_id": "run-d2105e38b3cd4f83b3550ebff21ca7fd",
            "candidate_id": None,
            "manifest_sha256": "5561e0c26de907f32b23772c864bfbab80c8c1b41f229867c113d25574d7926c",
        },
        "fields": ["center", "outer-ne"],
        "charts": list(CHARTS[:5]),
        "light_scales": [0.01, 0.1, 1.0],
        "frames": 8,
        "seed": 42,
        "exposure_ms": 4.0,
        "gain": 1.0,
        "temperature_c": 25.0,
        "charge_binning": {
            "capacity_multiplier": 4.0,
            "conversion_gain_multiplier": 0.25,
            "read_noise_multiplier": 1.0,
        },
        "isp": {"lsc_enabled": True, "display_white_level": 1.0, "tone_gamma": 2.2},
        "reprocess_of": None,
        "dcg_merge": default_profile(),
        "readout_lab": default_settings(),
    }


def specification(payload):
    expected = set(template())
    if not isinstance(payload, dict) or set(payload) not in (expected, expected - {"readout_lab"}):
        raise ValueError("Sensor mode request requires the explicit template fields.")
    s = copy.deepcopy(payload)
    if s["schema"] != SCHEMA or s["recipe"] not in {"binning", "dual_gain", *READOUT_RECIPES}:
        raise ValueError("Choose a supported Sensor Mode or Readout Lab recipe.")
    if "readout_lab" in s or s["recipe"] in READOUT_RECIPES:
        from .reference_readout_lab import validate_settings

        s["readout_lab"] = validate_settings(s.get("readout_lab", {}))
    if not isinstance(s["name"], str) or not 1 <= len(s["name"]) <= 160:
        raise ValueError("Name requires 1..160 characters.")
    from .dcg import validate_profile

    s["dcg_merge"] = validate_profile(s["dcg_merge"])
    if s["reprocess_of"] is not None and (
        s["recipe"] != "dual_gain" or not re.fullmatch(r"run-[0-9a-f]{32}", str(s["reprocess_of"]))
    ):
        raise ValueError("RAW-only reprocessing requires a dual_gain source study.")
    src = s["source"]
    if not isinstance(src, dict) or set(src) != {"run_id", "candidate_id", "manifest_sha256"}:
        raise ValueError("Pin source run, candidate and manifest hash.")
    if not re.fullmatch(r"run-[0-9a-f]{32}", str(src["run_id"])) or not re.fullmatch(
        r"[0-9a-f]{64}", str(src["manifest_sha256"])
    ):
        raise ValueError("Invalid source run/hash.")
    if src["candidate_id"] is not None and not re.fullmatch(
        r"candidate-[0-9]{2}", str(src["candidate_id"])
    ):
        raise ValueError("Invalid source candidate.")
    for key, choices, maximum in (
        (
            "fields",
            {
                "center",
                "mid-top",
                "mid-right",
                "mid-bottom",
                "mid-left",
                "outer-ne",
                "outer-nw",
                "outer-se",
                "outer-sw",
            },
            9,
        ),
        ("charts", set(CHARTS), len(CHARTS)),
    ):
        if (
            not isinstance(s[key], list)
            or not 1 <= len(s[key]) <= maximum
            or any(not isinstance(v, str) or v not in choices for v in s[key])
            or len(set(s[key])) != len(s[key])
        ):
            raise ValueError("Select supported unique " + key)
    if "dark" in s["charts"] and s["recipe"] not in READOUT_RECIPES:
        raise ValueError("The declared zero-input dark chart requires a Readout Lab recipe.")
    if (
        not isinstance(s["light_scales"], list)
        or not 1 <= len(s["light_scales"]) <= 3
        or any(type(v) not in (int, float) or v not in (0.01, 0.1, 1) for v in s["light_scales"])
        or len(set(s["light_scales"])) != len(s["light_scales"])
    ):
        raise ValueError("Use unique light scales from 0.01, 0.1, 1.")
    for key, expected in (("frames", 8), ("exposure_ms", 4), ("gain", 1), ("temperature_c", 25)):
        if type(s[key]) not in (int, float) or s[key] != expected:
            raise ValueError(f"First Sensor Mode profile fixes {key}={expected}.")
    if type(s["seed"]) is not int or not 0 <= s["seed"] < 2**31:
        raise ValueError("Invalid seed.")
    bounds = {
        "capacity_multiplier": (0.1, 16),
        "conversion_gain_multiplier": (0.01, 4),
        "read_noise_multiplier": (0, 10),
    }
    if not isinstance(s["charge_binning"], dict) or set(s["charge_binning"]) != set(bounds):
        raise ValueError(
            "Explicit charge-binning capacity, conversion gain and read noise required."
        )
    for key, (lo, hi) in bounds.items():
        v = s["charge_binning"][key]
        if type(v) not in (int, float) or not np.isfinite(v) or not lo <= v <= hi:
            raise ValueError("Invalid charge binning assumption: " + key)
    if (
        not isinstance(s["isp"], dict)
        or set(s["isp"]) != {"lsc_enabled", "display_white_level", "tone_gamma"}
        or type(s["isp"]["lsc_enabled"]) is not bool
    ):
        raise ValueError("Explicit fixed ISP required.")
    for key, lo, hi in (("display_white_level", 1, 100), ("tone_gamma", 0.5, 4)):
        v = s["isp"][key]
        if type(v) not in (int, float) or not np.isfinite(v) or not lo <= v <= hi:
            raise ValueError("Invalid fixed display setting.")
    return s


def fingerprint():
    names = [
        "reference_sensor_job.py",
        "reference_sensor_modes.py",
        "reference_sensor_measurements.py",
        "reference_readout_lab.py",
        "reference_isp_lab.py",
        "dcg.py",
        "dcg_electrical.py",
        "electrical.py",
        "reference_workshop_optics.py",
        "reference_workshop_sensor.py",
    ]
    return {name: sha256(Path(__file__).parent / name) for name in names}


def _load_source(service, spec, verify=False):
    src = spec["source"]
    record = workshop.get(service, src["run_id"])
    if (
        record["status"] != "succeeded"
        or record["result"]["summary"]["manifest_sha256"] != src["manifest_sha256"]
    ):
        raise IntegrityError("Source must be complete with the pinned manifest.")
    directory = workshop._directory(service, src["run_id"])
    if sha256(directory / "manifest.json") != src["manifest_sha256"]:
        raise IntegrityError("Source manifest changed.")
    if verify:
        workshop.verify_files(service, record)
    if src["candidate_id"]:
        directory /= src["candidate_id"]
    capture = json.loads((directory / "capture.json").read_text())
    config = capture["configuration"]
    if (
        config["exposure_ms"] != 4
        or config["gain"] != 1
        or config["frames"] != 8
        or config["luminance_cd_m2"] != 1000
    ):
        raise ValueError("Source requires Reference 1000 cd/m², 4ms, gain1, 8 frames.")
    if config["pixel"]["cfa_thickness_scale"] != 1:
        raise ValueError("First readout comparison fixes the baseline CFA structure.")
    for field in spec["fields"]:
        if field not in capture["fields"]:
            raise ValueError("Requested field was not captured: " + field)
        f = capture["fields"][field]
        if (
            f["geometry"]["native_shape_row_col"] != [256, 256]
            or f["geometry"]["cfa_origin"]["pattern"] != "RGGB"
        ):
            raise ValueError("First Sensor Mode adapter requires native 256² RGGB.")
        if any(
            c not in {"dynamic_range", "dark"} and c not in f["captures"] for c in spec["charts"]
        ):
            raise ValueError("Requested chart is absent; make a new source acquisition first.")
    return directory, capture


def submit(service, payload):
    spec = specification(payload)
    _load_source(service, spec)
    if spec["reprocess_of"]:
        _replay_source(service, spec)
    count = len(spec["fields"]) * len(spec["charts"]) * len(spec["light_scales"])
    mode_count = len(spec["readout_lab"]["adc_bits"]) if spec["recipe"] == "adc_headroom" else 3
    case_bytes = (max(128, 96 * mode_count) if spec["recipe"] in READOUT_RECIPES else 128) * 1024**2
    estimate = count * case_bytes + 128 * 1024**2
    state = storage.status(service)
    if (
        state["remaining_bytes"] < estimate
        or state["volume_free_bytes"] - estimate < storage.RESERVE_BYTES
    ):
        raise ValueError("Sensor Mode exceeds SSD budget; resolution was not reduced.")
    doc = {
        "name": spec["name"],
        "job_kind": workshop.JOB_KIND,
        "plugin_id": SCHEMA,
        "sensor_mode_study": spec,
        "source_code": fingerprint(),
        "storage_root": state["path"],
        "estimated_bytes": estimate,
        "default_worker_timeout_seconds": 7200,
    }
    record = service.registry.submit(doc, digest(doc))
    if not any(
        e["run_id"] == record["id"] for e in service.registry.list("reference_sensor_mode_studies")
    ):
        service.registry.put(
            "reference_sensor_mode_studies", {"run_id": record["id"], "name": spec["name"]}
        )
    return record


def _replay_source(service, spec, verify=False):
    record = get(service, spec["reprocess_of"])
    if record["status"] != "succeeded":
        raise ValueError("Reprocessing needs completed preserved DCG RAW.")
    previous = record["sensor_mode_study"]
    ignored = {"name", "dcg_merge", "isp", "reprocess_of", "readout_lab"}
    if {k: v for k, v in spec.items() if k not in ignored} != {
        k: v for k, v in previous.items() if k not in ignored
    }:
        raise ValueError(
            "RAW reprocessing may change only merge/display settings; acquisition is frozen."
        )
    if verify:
        workshop.verify_files(service, record)
    return workshop._directory(service, record["id"])


def _dynamic_rate(service, source, capture, field_id, progress):
    """New radiance chart through the pinned optical solution, never RAW modulation."""
    from . import reference_workshop_engine as engine

    config = capture["configuration"]
    cache = storage.root(service) / "reference-workshop-cache"
    solution, _ = engine._solution(service.workspace, config, cache, lambda v: progress(str(v)))
    pinned = json.loads((source / "optical-response.json").read_text())
    if solution["result"]["revision"] != pinned["revision"]:
        raise IntegrityError("Dynamic chart optical revision differs from the source.")
    field = next(f for f in solution["result"]["fields"] if f["id"] == field_id)
    g = field["geometry"]
    spectral = engine._spectra(service.workspace, "D65", 1000)
    response = json.loads((source / "pixel-response.json").read_text())
    levels = np.geomspace(0.001, 2, 16)
    uniform = np.load(source / "fields" / "center" / "uniform" / "mean-electron-rate.npy")
    readout = capture["fields"][field_id]["readout"]
    scale = readout["full_well_e"] / (float(np.mean(uniform[0::2, 1::2])) * 0.004)
    rows, cols = g["input_shape_row_col"]
    x = np.arange(cols) - g["crop_bounds_row_col"][2]
    steps = levels[np.clip(x // 16, 0, 15)] * scale
    photons = np.broadcast_to(steps[None, :, None] * spectral["white"], (rows, cols, 21)).copy()
    result = engine._apply(solution, field, photons, response)
    return result["mean_electron_rate"], {
        "kind": "new_optical_acquisition",
        "chart": "16_vertical_log_radiance_steps",
        "center_Gr_expected_full_well_fractions": levels.tolist(),
        "radiance_scale": scale,
        "optical_revision": pinned["revision"],
        "optical_pixel_contract": result["metadata"],
    }


def _png(path, values):
    from PIL import Image

    Image.fromarray(np.uint8(np.round(np.clip(np.nan_to_num(values), 0, 1) * 255))).save(path)


def execute(service, job):
    from . import reference_readout_lab as readout_core
    from . import reference_sensor_measurements as metrics
    from . import reference_sensor_modes as core
    from .reference_workshop_engine import write_json
    from .reference_workshop_sensor import lsc_gain_map

    if job["source_code"] != fingerprint() or job["storage_root"] != str(storage.root(service)):
        raise IntegrityError("Sensor Mode code or storage changed; submit a new job.")
    started = time.monotonic()
    spec = specification(job["sensor_mode_study"])
    is_readout_lab = spec["recipe"] in READOUT_RECIPES
    assumptions = READOUT_ASSUMPTIONS if is_readout_lab else ASSUMPTIONS
    directory = workshop._directory(service, job["id"])
    with storage.reserve(service, job["estimated_bytes"]) as (_, check):
        directory.mkdir(parents=True, exist_ok=True)

        def progress(message="Computing"):
            if service.registry.get_run(job["id"])["status"] != "running":
                raise RuntimeError("Sensor Mode study cancelled.")
            write_json(directory / "progress.json", {"stage": message})

        progress("Verifying preserved sources")
        source, capture = _load_source(service, spec, verify=True)
        replay_dir = _replay_source(service, spec, verify=True) if spec["reprocess_of"] else None
        calibration = json.loads((source / "calibration.json").read_text())
        cell = json.loads((source / "pixel-cell.json").read_text())
        write_json(directory / "calibration.json", calibration)
        write_json(directory / "specification.json", spec)
        write_json(directory / "pixel-cell.json", cell)
        cases, curves, raw_reused, new_frames = [], [], 0, 0
        for field_id in spec["fields"]:
            field = capture["fields"][field_id]
            gain = (
                lsc_gain_map(calibration["lsc"], field_id, shape=(256, 256))
                if spec["isp"]["lsc_enabled"]
                else None
            )
            for chart in spec["charts"]:
                progress(field_id + " · " + chart)
                if chart == "dark":
                    rate = np.zeros(tuple(field["geometry"]["native_shape_row_col"]), dtype=float)
                    provenance = {
                        "kind": "declared_zero_incident_photoelectron_rate",
                        "units": "photoelectrons per native pixel per second",
                        "value": 0.0,
                        "optics": "Zero incident signal; no new optical solve or fabricated RAW",
                        "readout": "Declared dark current and readout noise remain active",
                    }
                elif chart == "dynamic_range" and replay_dir is not None:
                    prior_cases = json.loads((replay_dir / "result.json").read_text())["cases"]
                    item = next(
                        c for c in prior_cases if c["field_id"] == field_id and c["chart"] == chart
                    )
                    rate = (
                        np.load(replay_dir / "cases" / item["id"] / "mean-electron-rate.npy")
                        / item["light_scale"]
                    )
                    provenance = {
                        "kind": "preserved_dynamic_optical_response",
                        "run_id": spec["reprocess_of"],
                        "case_id": item["id"],
                    }
                elif chart == "dynamic_range":
                    rate, provenance = _dynamic_rate(service, source, capture, field_id, progress)
                else:
                    rate_file = source / "fields" / field_id / chart / "mean-electron-rate.npy"
                    rate = np.load(rate_file, allow_pickle=False)
                    provenance = {
                        "kind": "preserved_mean_response",
                        "path": str(rate_file),
                        "sha256": sha256(rate_file),
                    }
                for light in spec["light_scales"]:
                    progress(f"{field_id} · {chart} · {light:g}× · {spec['recipe']}")
                    case_id = f"case-{len(cases):03d}"
                    target = directory / "cases" / case_id
                    target.mkdir(parents=True)
                    np.save(target / "mean-electron-rate.npy", rate * light)
                    native = None
                    raw_refs = []
                    if (
                        spec["recipe"] == "binning"
                        and chart != "dynamic_range"
                        and light == 1
                        and spec["seed"] == capture["configuration"]["seed"]
                    ):
                        records = field["captures"][chart]["raw"]
                        raw_refs = [
                            {
                                "path": str(source / "fields" / field_id / chart / f["file"]),
                                "sha256": f["sha256"],
                            }
                            for f in records
                        ]
                        native = np.stack(
                            [np.load(r["path"], allow_pickle=False) for r in raw_refs]
                        )
                        raw_reused += len(records)
                    kwargs = {
                        "exposure_s": 0.004,
                        "light_scale": light,
                        "frames": 8,
                        "seed": spec["seed"]
                        + spec["fields"].index(field_id) * 10000
                        + CHARTS.index(chart) * 100,
                        "electrical_model": cell["electrical_model"],
                    }
                    if spec["recipe"] == "binning":
                        options = {**spec["charge_binning"]}
                        options["node_capacity_multiplier"] = options.pop("capacity_multiplier")
                        calculated = core.binning_modes(
                            rate,
                            field["readout"],
                            native_raw=native,
                            charge_options=options,
                            **kwargs,
                        )
                        if native is not None:
                            for m in ("native", "digital_binning"):
                                calculated["modes"][m]["metadata"].update(
                                    seed=None,
                                    source_seeds=[r["seed"] for r in records],
                                    seed_basis="Preserved original capture seeds, not study seed",
                                )
                        new_frames += 8 if native is not None else 16
                    elif is_readout_lab:
                        calculated = readout_core.readout_modes(
                            rate,
                            field["readout"],
                            recipe=spec["recipe"],
                            settings=spec["readout_lab"],
                            temperature_c=spec["temperature_c"],
                            **kwargs,
                        )
                        new_frames += sum(
                            len(mode["raw_frames"]) for mode in calculated["modes"].values()
                        )
                    elif replay_dir is None:
                        calculated = core.dual_gain_modes(
                            rate, field["readout"], merge_profile=spec["dcg_merge"], **kwargs
                        )
                        new_frames += 16
                    else:
                        prior = replay_dir / "cases" / case_id
                        with np.load(prior / "readout-arrays.npz", allow_pickle=False) as stored:
                            readout = _unpack_arrays(
                                json.loads((prior / "readout.json").read_text()), stored
                            )
                        originals = {}
                        for m in ("hcg", "lcg", "dcg"):
                            with np.load(prior / m / "arrays.npz", allow_pickle=False) as stored:
                                originals[m] = {k: stored[k] for k in stored.files}
                        paired = np.stack(
                            [originals[m]["raw_frames"] for m in ("hcg", "lcg")], axis=1
                        )
                        calculated = core.replay_dual_gain(
                            paired,
                            readout["calibration"],
                            readout["model"],
                            merge_profile=spec["dcg_merge"],
                        )
                        for m in ("hcg", "lcg", "dcg"):
                            for k in ("well_saturated_frames", "adc_saturated_frames"):
                                calculated["modes"][m][k] = originals[m][k]
                            calculated["modes"][m]["metadata"].update(
                                well_mask_basis="Original simulated acquisition mask",
                                adc_mask_basis="Original simulated acquisition mask",
                            )
                        raw_refs = [
                            {
                                "path": str(prior / m / "arrays.npz"),
                                "sha256": sha256(prior / m / "arrays.npz"),
                            }
                            for m in ("hcg", "lcg")
                        ]
                        raw_reused += 16
                    case = {
                        "id": case_id,
                        "field_id": field_id,
                        "chart": chart,
                        "light_scale": light,
                        "modes": [],
                        "assumptions": assumptions,
                        "source_refs": {
                            "source": spec["source"],
                            "response": provenance,
                            "native_raw": raw_refs,
                        },
                        "geometry": field["geometry"],
                    }
                    extra = {k: v for k, v in calculated.items() if k != "modes"}
                    packed = {}
                    write_json(target / "readout.json", _pack_arrays(extra, packed))
                    if packed:
                        np.savez_compressed(target / "readout-arrays.npz", **packed)
                    if is_readout_lab:
                        case["readout_lab"] = {
                            "recipe": spec["recipe"],
                            "settings": spec["readout_lab"],
                            "readout_metadata_file": str(
                                target.relative_to(directory) / "readout.json"
                            ),
                            "new_readout_frames": sum(
                                len(mode["raw_frames"]) for mode in calculated["modes"].values()
                            ),
                            "original_raw_reused": False,
                        }
                    for mode_id, mode in calculated["modes"].items():
                        progress(
                            f"{field_id} · {chart} · {light:g}× · {mode_id} ISP and measurements"
                        )
                        mode_dir = target / mode_id
                        mode_dir.mkdir()
                        count = 4 if mode_id in {"digital_binning", "charge_binning"} else 1
                        mode_gain = (
                            core.binned_lsc(gain) if count == 4 and gain is not None else gain
                        )
                        variance = mode["variance_e2"]
                        processed = [
                            core.process_electrons(
                                e,
                                calibration["color"]["matrix"],
                                contribution_count=count,
                                lsc_gain=mode_gain,
                                display_white=spec["isp"]["display_white_level"],
                                gamma=spec["isp"]["tone_gamma"],
                                demosaic="bilinear",
                                valid=mode["valid_frames"][i],
                                variance_e2=variance if variance.ndim == 2 else variance[i],
                            )
                            for i, e in enumerate(mode["electron_frames"])
                        ]
                        linear = np.stack([p["linear"] for p in processed])
                        valid = np.stack([p["valid"] for p in processed])
                        measured = metrics.measure(
                            mode["electron_frames"],
                            linear,
                            valid,
                            contribution_count=count,
                            native_pitch_factor=2 if count == 4 else 1,
                            chart=chart,
                            well_saturated_frames=mode.get("well_saturated_frames"),
                            adc_saturated_frames=mode.get("adc_saturated_frames"),
                            horizontal_fov_deg=field["geometry"]["horizontal_fov_deg"],
                            electron_valid_frames=mode["valid_frames"],
                        )
                        readout_diagnostics = None
                        if is_readout_lab:
                            readout_diagnostics = readout_core.mode_diagnostics(mode, chart=chart)
                            digital = np.asarray(mode["digital_saturated_frames"], dtype=bool)
                            border = measured["native_border_pixels"]
                            interior = digital[:, border:-border, border:-border]
                            measured["digital_saturation_fraction"] = (
                                float(interior.mean()) if interior.size else None
                            )
                            added = mode["digital_added_saturated_frames"][
                                :, border:-border, border:-border
                            ]
                            measured["digital_added_saturation_fraction"] = (
                                float(added.mean()) if added.size else None
                            )
                            measured["digital_saturation_denominator"] = (
                                "All repeats and native interior pixels, including invalid output"
                            )
                        _png(mode_dir / "display.png", processed[0]["display"])
                        _png(mode_dir / "valid.png", valid[0])
                        _png(
                            mode_dir / ("model-noise.png" if is_readout_lab else "noise.png"),
                            np.sqrt(processed[0]["lsc_mosaic_variance_e2_per_contributor"])
                            / field["readout"]["read_noise_e"]
                            / 10,
                        )
                        has_weights = mode_id == "dcg" and "weights" in calculated["diagnostics"]
                        if has_weights:
                            _png(
                                mode_dir / "weights.png", calculated["diagnostics"]["weights"][0, 0]
                            )
                        _png(
                            mode_dir / "raw-preview.png",
                            mode["electron_frames"][0] / (count * field["readout"]["full_well_e"]),
                        )
                        arrays = {k: v for k, v in mode.items() if isinstance(v, np.ndarray)}
                        arrays.update(
                            linear_frames=linear.astype(np.float32),
                            common_valid_frames=valid,
                            lsc_gain=np.ones_like(mode["electron_frames"][0])
                            if mode_gain is None
                            else mode_gain,
                        )
                        arrays["lsc_mosaic_variance_e2_per_contributor"] = np.stack(
                            [p["lsc_mosaic_variance_e2_per_contributor"] for p in processed]
                        )
                        if is_readout_lab:
                            samples = np.asarray(mode["electron_frames"], dtype=float) / count
                            supported = np.all(mode["valid_frames"] & np.isfinite(samples), axis=0)
                            noise_map = np.sqrt(np.var(samples, axis=0, ddof=1))
                            noise_map = np.where(supported, noise_map, np.nan)
                            arrays["measured_temporal_noise_e_per_contributor"] = noise_map
                            arrays["measured_temporal_noise_valid"] = supported
                            noise_white = 4 * max(1.0, *[
                                float(np.sqrt(sum(
                                    m["metadata"]["noise_budget"][key] for key in (
                                        "input_read_e2", "post_gain_read_e2", "cds_residual_e2",
                                        "adc_quantization_e2", "digital_quantization_e2",
                                    )
                                ))) for m in calculated["modes"].values()
                            ])
                            _png(mode_dir / "measured-noise.png", noise_map / noise_white)
                            _png(mode_dir / "noise.png", noise_map / noise_white)
                            _png(mode_dir / "measured-noise-valid.png", supported)
                            residual = np.where(
                                supported, samples[0] - np.mean(samples, axis=0), 0.0
                            )
                            arrays["first_frame_temporal_residual_e_per_contributor"] = residual
                            _png(
                                mode_dir / "temporal-residual.png",
                                0.5 + residual / (2 * noise_white),
                            )
                        mode_metadata = {
                            k: v for k, v in mode.items() if not isinstance(v, np.ndarray)
                        }
                        metadata = (
                            _pack_arrays(mode_metadata, arrays, path="readout_metadata")
                            if is_readout_lab
                            else _json_value(mode_metadata)
                        )
                        np.savez_compressed(mode_dir / "arrays.npz", **arrays)
                        np.save(mode_dir / "linear.npy", linear[0].astype(np.float32))
                        rel = str(mode_dir.relative_to(directory)) + "/"
                        entry = {
                            "id": mode_id,
                            "label": mode_id.replace("_", " ").upper(),
                            "shape": list(mode["electron_frames"].shape[1:]),
                            "metrics": measured,
                            "sfr": measured.pop("sfr", None),
                            "files": {
                                "display": rel + "display.png",
                                "raw_preview": rel + "raw-preview.png",
                                "arrays": rel + "arrays.npz",
                                "raw": rel + "arrays.npz",
                                "linear": rel + "linear.npy",
                                "valid": rel + "valid.png",
                                "noise": rel + "noise.png",
                            },
                            "metadata": metadata,
                            "contribution_count": count,
                        }
                        if has_weights:
                            entry["files"]["weights"] = rel + "weights.png"
                        entry["preview_scales"] = {
                            "noise_white_e_per_contributor": field["readout"]["read_noise_e"] * 10,
                            "weights": "HCG contribution 0 black, 1 white",
                            "valid": "First-frame reconstructed validity; white valid",
                        }
                        if is_readout_lab:
                            entry["label"] = mode["metadata"].get("label", entry["label"])
                            entry["readout_diagnostics"] = _json_value(readout_diagnostics)
                            entry["files"]["measured_noise"] = rel + "measured-noise.png"
                            entry["files"]["measured_noise_valid"] = (
                                rel + "measured-noise-valid.png"
                            )
                            entry["files"]["model_noise"] = rel + "model-noise.png"
                            entry["files"]["temporal_residual"] = rel + "temporal-residual.png"
                            entry["metadata_arrays_file"] = rel + "arrays.npz"
                            entry["preview_scales"].update(
                                measured_noise_white_e_per_contributor=noise_white,
                                noise_white_e_per_contributor=noise_white,
                                residual_limit_e=noise_white,
                                temporal_residual=(
                                    "First frame minus eight-frame per-pixel temporal mean; "
                                    "signed e/pixel diagnostic, not new RAW; invalid mid-gray"
                                ),
                                measured_noise=(
                                    "Pixelwise temporal sample standard deviation before LSC/ISP; "
                                    "eight frames valid; invalid black with separate validity map"
                                ),
                                raw_preview=(
                                    "Signed electron-equivalent first frame / source full well; "
                                    "no per-mode or per-frame normalization"
                                ),
                            )
                        if chart == "dynamic_range":
                            entry["tone_curve"] = _tone_curve(mode, rate * light * 0.004, count)
                        case["modes"].append(entry)
                        curves.append(
                            {
                                "field_id": field_id,
                                "chart": chart,
                                "light_scale": light,
                                "mode": mode_id,
                                **measured,
                                **(
                                    {"readout_diagnostics": _json_value(readout_diagnostics)}
                                    if is_readout_lab
                                    else {}
                                ),
                            }
                        )
                    write_json(target / "case.json", case)
                    cases.append(case)
                    check()
        progress("Publishing Sensor Mode package")
        _load_source(service, spec, verify=True)
        if replay_dir is not None:
            _replay_source(service, spec, verify=True)
        result = {
            "schema": SCHEMA,
            "name": spec["name"],
            "recipe": spec["recipe"],
            "source": spec["source"],
            "specification": spec,
            "cases": [
                {k: c[k] for k in ("id", "field_id", "chart", "light_scale")}
                | {"modes": [m["id"] for m in c["modes"]]}
                for c in cases
            ],
            "curves": curves,
            "assumptions": assumptions,
            "source_code": job["source_code"],
            "raw_preserved": True,
            "reused_native_raw_frames": raw_reused,
            "new_readout_frames": new_frames,
            "pixel_study_id": "run-ef087598619c4108a03f06716aae2c73",
            "elapsed_seconds": time.monotonic() - started,
            "physical_validation": "not_established",
        }
        if is_readout_lab:
            result["readout_lab"] = {
                "settings": spec["readout_lab"],
                "original_raw_reused": False,
                "mean_response_reused": all(
                    c["chart"] not in {"dynamic_range", "dark"} for c in cases
                ),
                "readout_provenance": (
                    "Fresh model readout from the pinned mean photoelectron response"
                ),
            }
        metrics.write_report(result, cases, directory)
        write_json(directory / "result.json", result)
        files = {
            str(p.relative_to(directory)): sha256(p)
            for p in sorted(directory.rglob("*"))
            if p.is_file()
            and not p.name.startswith("._")
            and p.name not in {"manifest.json", "progress.json"}
        }
        write_json(
            directory / "manifest.json",
            {"schema": "camera_reference_workshop_files_v1", "files": files},
        )
        summary = {
            **result,
            "files_base": f"{ROUTE}/{job['id']}/files/",
            "report_url": f"{ROUTE}/{job['id']}/files/report.html",
            "bundle_url": f"{ROUTE}/{job['id']}/bundle.zip",
            "manifest_sha256": sha256(directory / "manifest.json"),
            "package_bytes": sum((directory / p).stat().st_size for p in files),
        }
        artifact = service.registry.store_blob(
            canonical(summary).encode(), "reference-sensor-mode-study.json", "application/json"
        )
        return {"summary": summary, "artifacts": [artifact]}


def _tone_curve(mode, expected, count):
    """Fixed chart strip interiors; clipping remains in the response denominator."""
    values = mode["electron_frames"] / count
    step = values.shape[-1] // 16
    margin = max(1, step // 4)
    rows = slice(16 if count == 1 else 8, -16 if count == 1 else -8)
    result = []
    for i in range(16):
        region = (slice(None), rows, slice(i * step + margin, (i + 1) * step - margin))
        data = values[region]
        finite = np.isfinite(data).all(axis=0)
        selected = data[:, finite]
        result.append(
            {
                "step": i,
                "input_mean_electrons": float(np.mean(expected[16:-16, i * 16 + 4 : i * 16 + 12])),
                "mean_electrons": float(np.mean(selected)) if selected.size else None,
                "temporal_noise_e": float(np.sqrt(np.mean(np.var(selected, axis=0, ddof=1))))
                if selected.size
                else None,
                "well_saturation_fraction": float(np.mean(mode["well_saturated_frames"][region])),
                "adc_saturation_fraction": float(np.mean(mode["adc_saturated_frames"][region])),
                "valid_fraction": float(np.mean(mode["valid_frames"][region])),
                "scope": "Fixed strip interior; response includes clipped finite values, "
                "invalid DCG stays missing",
            }
        )
        if "digital_saturated_frames" in mode:
            result[-1]["digital_saturation_fraction"] = float(
                np.mean(mode["digital_saturated_frames"][region])
            )
        if "digital_added_saturated_frames" in mode:
            result[-1]["digital_added_saturation_fraction"] = float(
                np.mean(mode["digital_added_saturated_frames"][region])
            )
    return result


def _json_value(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def _pack_arrays(value, arrays, path="root"):
    if isinstance(value, np.ndarray):
        arrays[path] = value
        return {"npz_key": path}
    if isinstance(value, dict):
        return {k: _pack_arrays(v, arrays, path + "." + k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_pack_arrays(v, arrays, path + "." + str(i)) for i, v in enumerate(value)]
    return _json_value(value)


def _unpack_arrays(value, arrays):
    if isinstance(value, dict):
        if set(value) == {"npz_key"}:
            return arrays[value["npz_key"]]
        return {k: _unpack_arrays(v, arrays) for k, v in value.items()}
    if isinstance(value, list):
        return [_unpack_arrays(v, arrays) for v in value]
    return value


def get(service, identity):
    record = workshop.get(service, identity)
    if record.get("plugin_id") != SCHEMA:
        raise ValueError("Selected run is not a Sensor Mode study.")
    return record


def case_detail(service, identity, case_id):
    record = get(service, identity)
    if record["status"] != "succeeded" or case_id not in {
        c["id"] for c in record["result"]["summary"]["cases"]
    }:
        raise ValueError("Completed case bound to this study required.")
    directory = workshop._directory(service, identity)
    manifest = json.loads((directory / "manifest.json").read_text())
    relative = f"cases/{case_id}/case.json"
    if (
        sha256(directory / "manifest.json") != record["result"]["summary"]["manifest_sha256"]
        or sha256(directory / relative) != manifest["files"][relative]
    ):
        raise IntegrityError("Sensor Mode case changed.")
    return json.loads((directory / relative).read_text())


def save_profile(service, identity, payload):
    if not isinstance(payload, dict) or set(payload) != {
        "name",
        "case_id",
        "mode",
        "baseline_mode",
        "interpolation",
    }:
        raise ValueError("Profile requires name, case, compared modes and interpolation.")
    if (
        not isinstance(payload["name"], str)
        or not 1 <= len(payload["name"]) <= 160
        or payload["interpolation"] not in {"nearest", "bilinear"}
    ):
        raise ValueError("Invalid profile name/interpolation.")
    case = case_detail(service, identity, payload["case_id"])
    if any(payload[k] not in {m["id"] for m in case["modes"]} for k in ("mode", "baseline_mode")):
        raise ValueError("Selected modes do not belong to this case.")
    record = get(service, identity)
    saved = service.registry.put(
        "reference_sensor_mode_profiles",
        {
            "study_id": identity,
            "selection": payload,
            "manifest_sha256": record["result"]["summary"]["manifest_sha256"],
            "specification": record["sensor_mode_study"],
            "case_sha256": digest(case),
        },
    )
    return {**saved, "download_url": f"{ROUTE}/{identity}/profiles/{saved['id']}"}


def install_routes(app, service):
    from fastapi.responses import RedirectResponse

    @app.get(ROUTE, tags=[workshop.JOB_KIND])
    def listing():
        studies = []
        for e in service.registry.list("reference_sensor_mode_studies"):
            r = get(service, e["run_id"])
            studies.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "status": r["status"],
                    "recipe": r["sensor_mode_study"]["recipe"],
                }
            )
        return {"studies": studies, "template": template()}

    @app.post(ROUTE, status_code=202, tags=[workshop.JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.get(ROUTE + "/{identity}", tags=[workshop.JOB_KIND])
    def detail(identity: str):
        return get(service, identity)

    @app.get(ROUTE + "/{identity}/cases/{case_id}", tags=[workshop.JOB_KIND])
    def case(identity: str, case_id: str):
        return case_detail(service, identity, case_id)

    @app.get(ROUTE + "/{identity}/files/{relative:path}", tags=[workshop.JOB_KIND])
    def file(identity: str, relative: str):
        get(service, identity)
        from urllib.parse import quote

        return RedirectResponse(f"{workshop.ROUTE}/runs/{identity}/files/{quote(relative)}")

    @app.get(ROUTE + "/{identity}/bundle.zip", tags=[workshop.JOB_KIND])
    def bundle(identity: str):
        get(service, identity)
        return RedirectResponse(f"{workshop.ROUTE}/runs/{identity}/bundle")

    @app.post(ROUTE + "/{identity}/profiles", status_code=201, tags=[workshop.JOB_KIND])
    def save(identity: str, payload: dict):
        return save_profile(service, identity, payload)

    @app.get(ROUTE + "/{identity}/profiles/{profile_id}", tags=[workshop.JOB_KIND])
    def profile(identity: str, profile_id: str):
        r = get(service, identity)
        value = service.registry.get("reference_sensor_mode_profiles", profile_id)
        if (
            value["study_id"] != identity
            or value["manifest_sha256"] != r["result"]["summary"]["manifest_sha256"]
            or digest(case_detail(service, identity, value["selection"]["case_id"]))
            != value["case_sha256"]
        ):
            raise IntegrityError("Profile binding changed.")
        return value
