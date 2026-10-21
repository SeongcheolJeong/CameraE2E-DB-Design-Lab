"""Explicit electrical calibration on immutable DB optical/pixel photocharge.

The preserved DB response already contains optical blur, receiver integration,
spectral collection, and field throughput. Replaying its expected electron rate
must never apply a camera-effective PSF or a second QE/aperture operation.
"""

from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from pathlib import Path

import numpy as np

from . import calibration_lab_engine as calibration
from . import design_workshop_engine as shared
from . import electrical, reference_isp
from . import reference_workshop_engine as reference
from .db_design_conditions import VerifiedSource
from .registry import digest
from .virtual_lab_job import sha256

SCHEMA = "camera_calibration_db_replay_v1"
FIELDS = ("center", "outer-ne")
CHARTS = ("resolution", "color", "uniform", "texture")
TRANSFERRED = ("black_level_dn", "dn_per_e", "read_noise_e", "dark_current_e_per_s")
SCOPE = (
    "Electrical-only fresh recapture of preserved DB optical/pixel photoelectron rates; "
    "local native 256x256 virtual RGGB probes, not a physical full-sensor image. "
    "Optical response, QE, illumination, exposure, field, CCM and ISP remain source-bound."
)


def _finite_number(value, name, low=0, high=1e12):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} requires a finite number in [{low},{high}]")
    return float(value)


def _source_model(cell):
    model = electrical.validate_electrical_model(cell["electrical_model"])
    if model["digital_gain"] != 1 or model["post_gain_noise_v"] != 0:
        raise ValueError("Electrical bridge requires unit digital gain and zero post-gain noise")
    if any(model["fixed_pattern"][key] != 0 for key in ("prnu_sigma", "dsnu_e_per_s")):
        raise ValueError("Electrical bridge does not transfer or approximate nonzero FPN")
    if cell.get("cfa_pattern") != "RGGB":
        raise ValueError("Electrical bridge requires the saved native RGGB phase")
    _finite_number(cell["pixel_pitch_um"], "pixel pitch", 0.1, 100)
    return model


def _dark_scale(model):
    return electrical.validate_capture_conditions(model, 0.001, 1, 25)[
        "dark_current_temperature_scale"
    ]


def _effective_gain(model, gain):
    adc = model["adc"]
    return (
        model["conversion_gain_v_per_e"]
        * gain
        * (2 ** adc["bit_depth"] - 1)
        / adc["full_scale_v"]
        * model["digital_gain"]
    )


def prepare_source(root, summary, candidate_id, condition_id=None, expected_manifest_sha256=None):
    """Resolve a specific immutable candidate and verify every input actually used.

    ``frozen_files`` contains the checked bytes needed to recreate a self-contained
    source tree. Runtime arrays are read-only and intentionally not JSON payloads.
    """
    if summary is None and (
        not isinstance(expected_manifest_sha256, str)
        or len(expected_manifest_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_manifest_sha256)
    ):
        raise ValueError("A manifest SHA256 pin is required when source summary is omitted")
    reader = VerifiedSource(root, expected_manifest_sha256)
    actual = reader.json("result.json")
    if summary is not None and (
        not isinstance(summary, dict) or any(summary.get(k) != v for k, v in actual.items())
    ):
        raise ValueError("DB source summary differs from its manifest-bound result")
    if (
        actual.get("schema") != "camera_db_design_study_result_v1"
        or actual.get("status", "succeeded") != "succeeded"
    ):
        raise ValueError("Electrical calibration requires a completed DB Design result")
    conditional = actual.get("comparison_mode") == "aperture_conditions"
    if conditional:
        if not isinstance(condition_id, str):
            raise ValueError("Condition-grid sources require an explicit condition_id")
        matches = [
            c
            for c in actual.get("aperture_conditions", {}).get("conditions", [])
            if c.get("id") == condition_id
        ]
        if len(matches) != 1:
            raise ValueError("Unknown or ambiguous DB source condition_id")
        condition = matches[0]
        if reader.json(f"conditions/{condition_id}/result.json") != condition:
            raise ValueError("Selected condition differs from its preserved result")
        candidates = condition["candidates"]
        expected_prefix = f"conditions/{condition_id}/candidates/{candidate_id}/"
    else:
        if condition_id is not None:
            raise ValueError("condition_id is forbidden on a non-condition DB source")
        condition = None
        candidates = actual.get("candidates", [])
        expected_prefix = f"candidates/{candidate_id}/"
    matches = [c for c in candidates if c.get("id") == candidate_id]
    if len(matches) != 1:
        raise ValueError("Unknown or ambiguous DB source candidate_id")
    candidate = deepcopy(matches[0])
    if candidate.get("status") != "succeeded" or candidate.get("prefix") != expected_prefix:
        raise ValueError("Selected candidate must be completed with its exact bound prefix")
    if reader.json(expected_prefix + "result.json") != candidate:
        raise ValueError("Selected candidate differs from its preserved candidate result")
    sources = reader.json(expected_prefix + "sources.json")
    if sources != candidate["source_identities"]:
        raise ValueError("Candidate source identities differ from preserved sources")
    payload = {
        "candidate": candidate,
        "condition": deepcopy(condition),
        "root": str(Path(root).resolve()),
        "summary": deepcopy(summary),
        "fields": {},
        "support": {},
    }
    for name in ("pixel-cell", "pixel-response", "calibration", "optical-response"):
        payload[name] = reader.json(expected_prefix + name + ".json")
    cell, fixed = payload["pixel-cell"], payload["calibration"]
    model = _source_model(cell)
    spec = deepcopy(candidate["result"]["configuration"])
    reference_isp.validate_profile(spec["isp"])
    if (
        spec.get("frames") != 8
        or spec.get("chart_suite") != "classic_v1"
        or spec["isp"].get("lsc_enabled") is not False
        or fixed.get("lsc") is not None
    ):
        raise ValueError("Source requires eight-repeat classic charts and the fixed LSC-off ISP")
    _finite_number(spec["gain"], "source analog gain", 1e-3, 1000)
    exposure = _finite_number(spec["exposure_ms"], "source exposure ms", 0.1, 100) / 1000
    electrical.validate_capture_conditions(model, exposure, spec["gain"], 25)
    if condition is not None and any(
        spec[key] != condition[key] for key in ("luminance_cd_m2", "exposure_ms")
    ):
        raise ValueError("Selected condition does not match its candidate acquisition")
    stored_cell = candidate["result"].get("pixel", {}).get("cell")
    if stored_cell is not None and stored_cell != cell:
        raise ValueError("Candidate pixel-cell differs from its preserved numerical model")
    matrix = np.asarray(fixed["color"]["matrix"], float)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("DB source requires a finite fixed electron-to-RGB calibration")
    optical_fields = {
        f["id"]: f for f in payload["optical-response"]["fields"] if f["id"] in FIELDS
    }
    if set(optical_fields) != set(FIELDS):
        raise ValueError("Both exact source optical fields are required")
    for field_id in FIELDS:
        optical_field = optical_fields[field_id]
        if optical_field.get("status") != "succeeded":
            raise ValueError("Source optical field did not succeed")
        for image_path in optical_field.get("images", {}).values():
            name = "optics/" + image_path
            payload["support"][name] = reader.read(expected_prefix + name)
        prefix = expected_prefix + f"fields/{field_id}/"
        capture = reader.json(prefix + "capture.json")
        if capture["configuration"] != spec or capture["calibration"] != fixed["color"]:
            raise ValueError("Preserved field capture differs from its source acquisition")
        geometry = capture["geometry"]
        phase = geometry.get("cfa_origin", {})
        if (
            geometry["native_shape_row_col"] != [256, 256]
            or geometry != optical_field["geometry"]
            or phase.get("pattern") != "RGGB"
            or phase.get("local_row") != 0
            or phase.get("local_col") != 0
            or geometry.get("pixel_pitch_um", cell["pixel_pitch_um"]) != cell["pixel_pitch_um"]
        ):
            raise ValueError("Source must preserve native 256x256 local RGGB probe geometry")
        expected_readout = {**cell["readout"], "gain": spec["gain"]}
        if capture["readout"] != expected_readout:
            raise ValueError("Preserved readout differs from its source electrical declaration")
        records = capture["captures"]
        if set(records) != set(CHARTS):
            raise ValueError("Electrical DB recapture requires all four preserved charts")
        arrays = {}
        for chart in CHARTS:
            values = {}
            for filename, key in (
                ("mean-electron-rate.npy", "rate_sha256"),
                ("planes-electron-rate.npy", "planes_sha256"),
                ("source-reference-linear.npy", "source_reference_sha256"),
            ):
                name = prefix + chart + "/" + filename
                if reader.files.get(name) != records[chart][key]:
                    raise ValueError("Source capture array hash differs from its manifest")
                values[filename] = reader.array(name)
            rate, planes = values["mean-electron-rate.npy"], values["planes-electron-rate.npy"]
            if (
                rate.shape != (256, 256)
                or planes.shape != (256, 256, 4)
                or values["source-reference-linear.npy"].shape != (256, 256, 3)
                or np.any(rate < 0)
                or np.any(planes < 0)
            ):
                raise ValueError("Source rates/reference must preserve their native finite grids")
            yy, xx = np.indices(rate.shape)
            site = np.array([[0, 1], [2, 3]])[yy % 2, xx % 2]
            if not np.array_equal(
                rate, np.take_along_axis(planes, site[..., None], axis=2)[..., 0]
            ):
                raise ValueError("Source Bayer rate is inconsistent with its four-site planes")
            arrays[chart] = values
        payload["fields"][field_id] = {
            "capture": capture,
            "optics": optical_field,
            "arrays": arrays,
        }
    payload["spec"] = spec
    payload["cell"] = cell
    payload["source_identity"] = {
        "run_id": actual.get("id", (summary or {}).get("id")),
        "candidate_id": candidate_id,
        "condition_id": condition_id,
        "manifest_sha256": reader.manifest_sha256,
        "candidate_sha256": digest(candidate),
    }
    # Re-read through the same hash verifier; snapshot bytes and numerical arrays
    # cannot quietly diverge if an input file changes during source preparation.
    frozen = {name: reader.read(name) for name in list(reader.used)}
    manifest_bytes = (reader.root / "manifest.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != reader.manifest_sha256:
        raise ValueError("Source manifest changed during preparation")
    frozen["manifest.json"] = manifest_bytes
    payload["frozen_files"] = frozen
    payload["source_files"] = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        for name, data in frozen.items()
    }
    payload["manifest_sha256"] = reader.manifest_sha256
    return payload


def baseline_projection(payload):
    """Project only directly mapped electronics, without inventing optical inputs."""
    cell, spec = payload["cell"], payload["spec"]
    model = _source_model(cell)
    adc = model["adc"]
    e = {
        "bit_depth": adc["bit_depth"],
        "black_level_dn": adc["black_level_dn"],
        "dn_per_e": _effective_gain(model, spec["gain"]),
        "read_noise_e": model["read_noise_e"],
        "dark_current_e_per_s": model["dark_current_e_per_s"] * _dark_scale(model),
        "full_well_e": model["full_well_e"],
        "full_well_identified": False,
    }
    source = payload["candidate"]["source_identities"].get("lens", {}).get("source", {})
    applied = source.get("pupil_condition", {}).get("effective_f_number")
    if applied is None:
        applied = source.get("paraxial", {}).get("effective_f_number")
    if applied is not None:
        applied = _finite_number(applied, "applied F-number", 0.01, 1000)
    origins = {
        "bit_depth": ("pixel-cell.electrical_model.adc.bit_depth", "bit"),
        "black_level_dn": ("pixel-cell.electrical_model.adc.black_level_dn", "DN"),
        "dn_per_e": ("conversion_v_per_e * analog_gain * ADC_white/full_scale_v", "DN/e"),
        "read_noise_e": ("pixel-cell.electrical_model.read_noise_e", "e"),
        "dark_current_e_per_s": (
            "source dark law evaluated at fixed capture temperature25C",
            "e/s",
        ),
        "full_well_e": ("pixel-cell.electrical_model.full_well_e", "e"),
    }
    facts = payload["candidate"].get("parameter_sources", [])
    if not facts:
        facts = cell.get("provenance", {}).get("db_selection", {}).get("parameter_sources", [])
    source_kind = (
        cell.get("provenance", {})
        .get("db_selection", {})
        .get("source_kind", "saved_model_snapshot")
    )
    mapping = []
    for key, (source, unit) in origins.items():
        dependencies = (
            {"gain", "full_well_e", "bit_depth", "black_level_dn", "conversion_gain_v_per_e"}
            if key == "dn_per_e"
            else {key}
        )
        evidence = [deepcopy(row) for row in facts if row.get("parameter") in dependencies]
        field_evidence = cell.get("field_classification", {}).get("readout_fields", {}).get(key)
        if field_evidence is not None:
            evidence.append(
                {
                    "source": "pixel-cell.field_classification.readout_fields",
                    "declaration": deepcopy(field_evidence),
                }
            )
        direct = next((row for row in evidence if row.get("parameter") == key), {})
        common = (
            cell.get("field_classification", {}).get("common_assumptions", {}).get("readout", {})
        )
        classification = direct.get("classification", direct.get("origin"))
        if classification is None:
            classification = "assumption" if key in common else "unclassified_saved_model"
        if key == "dn_per_e":
            classification = "derived_from_saved_electrical_model"
        mapping.append(
            {
                "target": f"electronics.{key}",
                "source": source,
                "value": e[key],
                "unit": unit,
                "source_kind": source_kind,
                "classification": classification,
                "origin": direct.get("origin", classification),
                "source_evidence": evidence,
                "physical_validation": "not_established",
            }
        )
    return {
        "electronics": e,
        "pixel_pitch_um": cell["pixel_pitch_um"],
        "cfa": "RGGB",
        "probe_shape": [256, 256],
        "conditions": {"gain": spec["gain"], "temperature_c": 25.0},
        "applied_f_number": applied,
        "parameter_mapping": mapping,
        "unmapped": [
            "physical full-sensor dimensions and focus distance are not identified",
            "optical PSF, pixel aperture, QE and field throughput remain in frozen source rates",
            "DB electron-to-RGB CCM is not a normalized Calibration Lab color matrix",
            "Lab geometry, shading, effective blur, raw_response, ISP and reference_white_e",
        ],
        "scope": "electronics_only",
        "physical_validation": "not_established",
    }


def _models(payload, profile):
    p = calibration.validate_profile(profile)
    projection = baseline_projection(payload)
    for key in ("bit_depth", "full_well_e"):
        if p["electronics"][key] != projection["electronics"][key]:
            raise ValueError(f"Electrical-only transfer cannot change {key}")
    for key in ("gain", "temperature_c"):
        if p["conditions"][key] != projection["conditions"][key]:
            raise ValueError(f"Electrical-only transfer requires source condition {key}")
    for key in ("pixel_pitch_um", "cfa"):
        if p[key] != projection[key]:
            raise ValueError(f"Electrical-only transfer requires source {key}")
    baseline = _source_model(payload["cell"])
    candidate = deepcopy(baseline)
    candidate["adc"]["black_level_dn"] = p["electronics"]["black_level_dn"]
    candidate["read_noise_e"] = p["electronics"]["read_noise_e"]
    candidate["dark_current_e_per_s"] = p["electronics"]["dark_current_e_per_s"] / _dark_scale(
        candidate
    )
    candidate["conversion_gain_v_per_e"] = (
        p["electronics"]["dn_per_e"]
        * candidate["adc"]["full_scale_v"]
        / (2 ** candidate["adc"]["bit_depth"] - 1)
        / payload["spec"]["gain"]
    )
    candidate = electrical.validate_electrical_model(candidate)
    return p, projection, baseline, candidate


def _readout(model, gain):
    return {
        "gain": gain,
        "full_well_e": model["full_well_e"],
        "read_noise_e": model["read_noise_e"],
        "dark_current_e_per_s": model["dark_current_e_per_s"] * _dark_scale(model),
        "bit_depth": model["adc"]["bit_depth"],
        "black_level_dn": model["adc"]["black_level_dn"],
        "dn_per_e": _effective_gain(model, gain),
    }


def render_replay(payload, profile, output_dir, seed, on_progress=None):
    """Recapture paired native RAW and run the unchanged source Reference ISP."""
    if type(seed) is not int or not 0 <= seed <= 2**31 - 65536:
        raise ValueError("Replay seed is outside the admitted deterministic range")
    p, projection, baseline, candidate = _models(payload, profile)
    root = Path(output_dir)
    if root.exists() and any(path.name != "progress.json" for path in root.iterdir()):
        raise ValueError("Electrical replay output must be new or empty")
    root.mkdir(parents=True, exist_ok=True)
    variants, images, comparisons = [], [], []
    source_spec = payload["spec"]
    fixed = payload["calibration"]
    for vi, (variant, model) in enumerate((("baseline", baseline), ("calibrated", candidate))):
        folder = root / variant
        folder.mkdir()
        spec = deepcopy(source_spec)
        spec["seed"] = seed
        spec["name"] = payload["candidate"]["label"] + " · electrical " + variant
        cell = deepcopy(payload["cell"])
        readout = _readout(model, spec["gain"])
        cell["electrical_model"] = model
        cell["readout"] = readout
        cell.setdefault("provenance", {})["electrical_replay"] = {
            "source_cell_sha256": payload["cell"].get("cell_sha256"),
            "applied_electrical_model_sha256": digest(model),
            "scope": "electronics_only",
        }
        cell["cell_sha256"] = digest({k: v for k, v in cell.items() if k != "cell_sha256"})
        for name, value in (
            ("pixel-cell", cell),
            ("pixel-response", payload["pixel-response"]),
            ("calibration", fixed),
            ("optical-response", payload["optical-response"]),
        ):
            shared.write_json(folder / (name + ".json"), value)
        for name, data in payload["support"].items():
            destination = folder / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        fields, captures = [], {}
        for fi, field_id in enumerate(FIELDS):
            source = payload["fields"][field_id]
            field_root = folder / "fields" / field_id
            field_root.mkdir(parents=True)
            capture = deepcopy(source["capture"])
            capture.update(configuration=spec, readout=readout)
            for ci, chart in enumerate(CHARTS):
                if on_progress:
                    on_progress(
                        {
                            "stage": f"{variant} · {field_id}/{chart} · fresh electrical RAW",
                            "fraction": (vi * 8 + fi * 4 + ci) / 16,
                        }
                    )
                chart_root = field_root / chart
                chart_root.mkdir()
                arrays = source["arrays"][chart]
                rate = arrays["mean-electron-rate.npy"]
                for name, values in arrays.items():
                    np.save(chart_root / name, values, allow_pickle=False)
                records = []
                for repeat in range(8):
                    capture_seed = seed + fi * 32 + repeat
                    captured = shared._capture(rate, model, spec, capture_seed)
                    path = chart_root / f"raw-{repeat:02d}.npy"
                    np.save(path, captured["arrays"]["raw_dn"], allow_pickle=False)
                    records.append(
                        {
                            "file": path.name,
                            "sha256": sha256(path),
                            "seed": capture_seed,
                            "saturation_fractions": captured["summary"]["saturation_fractions"],
                        }
                    )
                    if repeat == 0:
                        shared.write_json(chart_root / "capture.json", captured["summary"])
                capture["captures"][chart].update(
                    raw=records,
                    rate_sha256=sha256(chart_root / "mean-electron-rate.npy"),
                    planes_sha256=sha256(chart_root / "planes-electron-rate.npy"),
                    source_reference_sha256=sha256(chart_root / "source-reference-linear.npy"),
                    response_cache_reused=True,
                    electrical_replay_source={
                        **payload["source_identity"],
                        "source_rate_sha256": source["capture"]["captures"][chart]["rate_sha256"],
                        "unit": "photoelectrons_per_second_before_dark_noise_gain_clipping",
                        "spatial_response_application_count": 1,
                        "additional_spatial_operations": 0,
                    },
                )
            shared.write_json(field_root / "capture.json", capture)
            captures[field_id] = capture
            rendered = reference._render_field(
                field_root, capture, spec, source["optics"], payload["pixel-response"], cell, fixed
            )
            rendered["metrics"]["temporal_noise_e"] = (
                rendered["metrics"]["temporal_noise_dn"] / readout["dn_per_e"]
            )
            rendered["tests"]["noise"]["electron_conversion"] = {
                "dn_per_e": readout["dn_per_e"],
                "definition": "input-referred Green temporal RAW noise including quantization",
            }
            fields.append(rendered)
            for chart in CHARTS:
                prefix = f"{variant}/fields/{field_id}/{chart}/"
                images.append(
                    {
                        "variant": variant,
                        "field_id": field_id,
                        "chart": chart,
                        "raw": prefix + "raw-00.npy",
                        "isp": prefix + "display.png",
                        "linear": prefix + "linear.npy",
                        "reference": prefix + "source-reference.png",
                    }
                )
        shared.write_json(
            folder / "capture.json",
            {
                "configuration": spec,
                "fields": captures,
                "calibration_sha256": sha256(folder / "calibration.json"),
                "source": payload["source_identity"],
            },
        )
        result = {
            "id": variant,
            "prefix": variant + "/",
            "fields": fields,
            "readout": readout,
            "applied_electrical_model": model,
            "configuration": spec,
        }
        shared.write_json(folder / "result.json", result)
        variants.append(result)
    for a, b in zip(variants[0]["fields"], variants[1]["fields"], strict=True):
        for metric in sorted(set(a["metrics"]) & set(b["metrics"])):
            before, after = a["metrics"][metric], b["metrics"][metric]
            if type(before) not in (int, float) or type(after) not in (int, float):
                continue
            comparisons.append(
                {
                    "field_id": a["id"],
                    "metric_id": metric,
                    "baseline": before,
                    "candidate": after,
                    "delta": after - before,
                    "reference_kind": "simulation_reference",
                }
            )
    summary = {
        "schema": SCHEMA,
        "scope": "electronics_only",
        "source_identity": payload["source_identity"],
        "source_files": payload["source_files"],
        "projection": projection,
        "variants": variants,
        "comparisons": comparisons,
        "images": images,
        "applied_parameters": [
            {
                "parameter": f"electronics.{key}",
                "before": projection["electronics"][key],
                "after": p["electronics"][key],
                "unit": {
                    "black_level_dn": "DN",
                    "dn_per_e": "DN/e",
                    "read_noise_e": "e",
                    "dark_current_e_per_s": "e/s",
                }[key],
            }
            for key in TRANSFERRED
        ],
        "not_transferred": [
            {
                "parameter": key,
                "reason": "explicit electrical-only transfer; source optics/ISP frozen",
            }
            for key in (
                "native_shape",
                "render_shape",
                "geometry",
                "shading",
                "blur",
                "raw_response",
                "isp",
                "reference_white_e",
            )
        ],
        "seed": seed,
        "capture_count": 128,
        "raw_shape_row_col": [256, 256],
        "physical_validation": "not_established",
        "evidence_kind": "source_bound_model_recapture",
        "optics_recomputed": False,
        "source_raw_modified": False,
        "limitations": [
            SCOPE,
            "Source rates are modeled evidence, not an independent measured chart validation.",
            "Full well is retained; PTC gain changes never imply physical capacity identification.",
            "Calibrated DN/e is applied at the source gain and temperature only.",
            "Non-electrical Calibration Lab parameters are explicitly not transferred.",
        ],
    }
    shared.write_json(root / "comparison.json", summary)
    if on_progress:
        on_progress({"stage": "electrical DB replay complete", "fraction": 1.0})
    return summary
