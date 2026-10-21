"""Read-only pixel diagnostics derived from preserved Reference Bench artifacts.

No scene, optical solver, electron capture or ISP is executed here. Pupil weights
already describe delivered sensor-plane photons; no cosine/area factor is added.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np

from .registry import digest

SCHEMA = "camera_reference_pixel_diagnostics_v1"
SITES = ["R", "Gr", "Gb", "B"]
LAYERS = ("lens", "cfa", "passivation", "silicon")


def _model_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class _Inputs:
    def __init__(self, cancel):
        self.dependencies = {}
        self.cancel = cancel

    def check(self):
        if self.cancel:
            self.cancel()

    def file(self, path, expected=None):
        self.check()
        path = Path(path).resolve()
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected is not None and actual != expected:
            raise ValueError(f"Preserved pixel diagnostic input changed: {path.name}")
        self.dependencies[str(path)] = {"path": str(path), "sha256": actual}
        return path

    def document(self, path, expected=None):
        return json.loads(self.file(path, expected).read_text())


def _array(value, shape, name):
    result = np.asarray(value, dtype=float)
    if result.shape != shape or not np.isfinite(result).all():
        raise ValueError(f"Invalid preserved {name} shape or finite values")
    return result


def _budgets(response):
    theta, wave = np.asarray(response["theta_deg"]), np.asarray(response["wavelength_nm"])
    if (
        theta.ndim != 1
        or wave.ndim != 1
        or len(theta) < 2
        or len(wave) < 2
        or not np.isfinite(theta).all()
        or not np.isfinite(wave).all()
        or theta[0] != 0
        or np.any(np.diff(theta) <= 0)
        or np.any(np.diff(wave) <= 0)
        or response.get("sites") != SITES
        or response.get("array_axes") != ["theta_deg", "wavelength_nm", "site"]
    ):
        raise ValueError("Pixel diagnostics require ordered theta/wavelength/native-site axes")
    shape = (len(theta), len(wave), 4)
    budget = {
        "reflection": _array(response["reflection"], shape, "reflection"),
        "deep_transmission": _array(
            response["uncollected_deep_transmission"], shape, "transmission"
        ),
        "collected_electrons": _array(response["eqe"], shape, "absolute QE"),
        "collection_loss": _array(response["collection_loss"], shape, "collection loss"),
    }
    for name in LAYERS:
        budget[name] = _array(response["layer_absorption"][name], shape, name + " absorption")
    if any(np.min(v) < -1e-9 or np.max(v) > 1 + 1e-9 for v in budget.values()):
        raise ValueError("Preserved power fractions must remain within [0,1]")
    optical = budget["reflection"] + budget["deep_transmission"]
    optical = optical + sum(budget[name] for name in LAYERS)
    collection = budget["collected_electrons"] + budget["collection_loss"] - budget["silicon"]
    optical_error = float(np.max(np.abs(optical - 1)))
    collection_error = float(np.max(np.abs(collection)))
    if optical_error > 1e-8 or collection_error > 1e-8:
        raise ValueError("Preserved optical or silicon collection budget does not close")
    normal = _array(response["normal_qe"], (len(wave), 4), "normal QE")
    if not np.allclose(normal, budget["collected_electrons"][0], rtol=1e-12, atol=1e-12):
        raise ValueError("Preserved normal QE differs from the absolute response table")
    return theta.astype(float), wave.astype(float), budget, optical_error, collection_error


def _pack_budget(values):
    return {
        "reflection": values["reflection"].tolist(),
        "layer_absorption": {name: values[name].tolist() for name in LAYERS},
        "deep_transmission": values["deep_transmission"].tolist(),
        "silicon_absorption": values["silicon"].tolist(),
        "collected_electrons": values["collected_electrons"].tolist(),
        "collection_loss": values["collection_loss"].tolist(),
    }


def _interpolate(table, grid, angles):
    """Linear theta interpolation at the exact preserved wavelengths, without extrapolation."""
    angles = np.asarray(angles, float)
    if (
        angles.ndim != 2
        or angles.shape[0] != table.shape[1]
        or not np.isfinite(angles).all()
        or np.min(angles) < grid[0]
        or np.max(angles) > grid[-1]
    ):
        raise ValueError("Pupil/chief directions exceed the preserved pixel angular domain")
    upper = np.clip(np.searchsorted(grid, angles, side="right"), 1, len(grid) - 1)
    lower = upper - 1
    fraction = (angles - grid[lower]) / (grid[upper] - grid[lower])
    wi = np.arange(table.shape[1])[:, None]
    return table[lower, wi] * (1 - fraction[..., None]) + table[upper, wi] * fraction[..., None]


def _acquisition(specification):
    return {k: v for k, v in specification.items() if k not in {"name", "isp"}}


def _capture_identity(capture):
    """Allow equivalent run IDs only with all retained acquisition content matched."""
    return {
        "configuration": _acquisition(capture["configuration"]),
        "fields": {
            key: {
                "geometry": value["geometry"],
                "readout": value["readout"],
                "captures": {
                    chart: {
                        name: item
                        for name, item in record.items()
                        if name != "response_cache_reused"
                    }
                    for chart, record in value["captures"].items()
                },
                "scene_reference": value["scene_reference"],
            }
            for key, value in capture["fields"].items()
        },
    }


def _color_metadata(calibration, bundle_id):
    matrix = _array(calibration["matrix"], (3, 3), "color calibration matrix")
    provenance_keys = (
        "file",
        "sha256",
        "resampled_content_sha256",
        "indices_one_based",
        "wavelength_nm",
        "role",
        "used_in_fit",
    )
    return {
        "id": calibration.get("id", bundle_id),
        "id_kind": "fitted_color_calibration"
        if calibration.get("id")
        else "baseline_calibration_bundle",
        "matrix_sha256": digest(matrix.tolist()),
        "column_l2_norm": np.linalg.norm(matrix, axis=0).tolist(),
        "column_norm_unit": "linear_srgb/electron",
        "method": calibration.get("method") or calibration.get("fit", {}).get("method"),
        "training": {
            k: calibration["training"][k] for k in provenance_keys if k in calibration["training"]
        }
        if "training" in calibration
        else None,
        "holdout": {
            k: calibration["holdout"][k] for k in provenance_keys if k in calibration["holdout"]
        }
        if "holdout" in calibration
        else None,
        "assets": copy.deepcopy(calibration.get("assets")),
        "meaning": "Coefficient norms; actual temporal color noise is measured separately",
    }


def _difference(new, old, *, percent=False):
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not np.isfinite(v)
        for v in (new, old)
    ):
        return None
    if percent:
        return float(100 * (new / old - 1)) if old > 0 else None
    return float(new - old)


def _color_rows(inputs, source, model_binding, color_directory, color_summary):
    if color_directory is None or color_summary is None:
        return {}, "color_study_missing"
    root = Path(color_directory)
    retained = inputs.document(root / "result.json")

    def identity(summary):
        return {
            "schema": summary.get("schema"),
            "study": summary.get("study"),
            "candidates": [
                {"id": c["id"], "configuration": c["configuration"]}
                for c in summary.get("candidates", [])
            ],
        }

    if digest(identity(retained)) != digest(identity(color_summary)):
        raise ValueError("Color summary differs from the preserved result")
    study = retained.get("study", {})
    if study.get("recipe") != "color_recalibration":
        raise ValueError("Pixel color diagnostics require a preserved color-recalibration study")
    rows = {}
    for candidate in retained.get("candidates", []):
        profile = candidate["configuration"]["isp"]
        role = "munsell" if profile.get("color_calibration_id") else "frozen"
        if role in rows:
            raise ValueError("Duplicate color arm in preserved study")
        candidate_id = candidate["id"]
        if not candidate_id.startswith("candidate-") or not candidate_id[10:].isdigit():
            raise ValueError("Invalid preserved color candidate identity")
        folder = root / candidate_id
        capture = inputs.document(folder / "capture.json")
        if _capture_identity(capture) != _capture_identity(source):
            raise ValueError("Color study does not preserve the same CFA/RAW/mean-response content")
        if candidate.get("status") != "succeeded":
            continue
        calibration = candidate["calibration"]["color"]
        if role == "munsell":
            from .reference_isp_color import verify_calibration

            calibration = inputs.document(
                folder / "color-calibrations" / (profile["color_calibration_id"] + ".json")
            )
            verify_calibration(calibration)
            binding = calibration["source_binding"]
            if (
                any(
                    binding.get(key) != value
                    for key, value in model_binding.items()
                    if key != "optics_sha256"
                )
                or binding["files_sha256"]["optics/optics.npz"] != model_binding["optics_sha256"]
            ):
                raise ValueError(
                    "Color calibration belongs to a different preserved pixel/optical model"
                )
            if calibration["matrix"] != candidate["calibration"]["color"]["matrix"]:
                raise ValueError("Color result matrix differs from its preserved calibration")
        metadata = _color_metadata(calibration, candidate["calibration"]["id"])
        rows[role] = {field["id"]: (candidate_id, field, metadata) for field in candidate["fields"]}
    return rows, None if set(rows) == {"frozen", "munsell"} else "color_arms_incomplete"


def _field_color(field_id, rows, reason):
    result = {"status": "unavailable" if reason else "available", "reason": reason}
    for role in ("frozen", "munsell"):
        item = rows.get(role, {}).get(field_id)
        if item is None or item[1].get("status") != "succeeded":
            result.update(status="unavailable", reason="color_field_unavailable")
            continue
        identity, field, metadata = item
        tests = field.get("tests", {})
        lab, color = tests.get("isp_lab", {}), tests.get("color", {})
        result[role] = {
            "candidate_id": identity,
            "delta_e00_mean": color.get("delta_e00_mean"),
            "delta_e00_max": color.get("delta_e00_max"),
            "patches": [
                {key: patch.get(key) for key in ("patch", "delta_e00", "roi")}
                for patch in color.get("patches", [])
            ],
            "calibration": copy.deepcopy(metadata),
            "color_noise_rgb": lab.get("color_noise_rgb"),
            "color_noise_luma": lab.get("color_noise_luma"),
            "uniform_noise_rgb": lab.get("temporal_noise_rgb"),
            "uniform_noise_luma": lab.get("temporal_noise_luma"),
            "metric_support": copy.deepcopy(lab.get("metric_support", {})),
            "images": copy.deepcopy(field.get("images", {})),
            "raw_identity": "matching_capture_content",
            "noise_unit": "signed_linear_srgb RMS",
        }
    if "frozen" in result and "munsell" in result:
        old, new = result["frozen"], result["munsell"]
        old_patches = {p["patch"]: p["delta_e00"] for p in old["patches"]}
        result["delta"] = {
            "definition": "munsell_minus_frozen_within_same_CFA_and_RAW",
            "delta_e00_mean": _difference(new["delta_e00_mean"], old["delta_e00_mean"]),
            "delta_e00_max": _difference(new["delta_e00_max"], old["delta_e00_max"]),
            "patches": [
                {
                    "patch": p["patch"],
                    "delta_e00": _difference(p["delta_e00"], old_patches.get(p["patch"])),
                }
                for p in new["patches"]
            ],
            "color_noise_luma_percent": _difference(
                new["color_noise_luma"], old["color_noise_luma"], percent=True
            ),
            "uniform_noise_luma_percent": _difference(
                new["uniform_noise_luma"], old["uniform_noise_luma"], percent=True
            ),
            "color_noise_rgb_percent": {
                channel: _difference(
                    (new["color_noise_rgb"] or {}).get(channel),
                    (old["color_noise_rgb"] or {}).get(channel),
                    percent=True,
                )
                for channel in ("r", "g", "b")
            },
            "ccm_column_norm_percent": [
                _difference(a, b, percent=True)
                for a, b in zip(
                    new["calibration"]["column_l2_norm"],
                    old["calibration"]["column_l2_norm"],
                    strict=True,
                )
            ],
        }
    return result


def build_candidate(
    source_directory, source_summary, *, color_directory=None, color_summary=None, cancel=None
):
    """Build JSON-safe diagnostics from one retained CFA candidate and optional color study.

    ``cancel`` may raise the worker's cancellation exception. Artifact bytes used
    here are hashed; the caller additionally verifies the enclosing RAW manifests.
    Missing color data is explicit. Invalid physical/source contracts raise.
    """
    root, inputs = Path(source_directory).resolve(), _Inputs(cancel)
    capture = inputs.document(root / "capture.json")
    cell = inputs.document(root / "pixel-cell.json")
    response = inputs.document(root / "pixel-response.json")
    optical = inputs.document(root / "optical-response.json")
    for record, key in ((cell, "cell_sha256"), (response, "table_sha256")):
        if record.get(key) != _model_digest({k: v for k, v in record.items() if k != key}):
            raise ValueError("Preserved pixel model content hash changed")
    if response["cell_sha256"] != cell["cell_sha256"]:
        raise ValueError("Pixel response is bound to a different CFA cell")
    if (
        digest(_acquisition(source_summary["configuration"]))
        != digest(_acquisition(capture["configuration"]))
        or source_summary["optical_revision"] != optical["revision"]
        or source_summary["pixel"]["cell"]["cell_sha256"] != cell["cell_sha256"]
    ):
        raise ValueError("Source summary does not identify this preserved acquisition")
    provenance = response["provenance"]
    if (
        provenance.get("angular_model") != "planar unpolarized; no azimuth variation"
        or provenance.get("angular_weights") != "conditional sensor-plane incident-photon fractions"
    ):
        raise ValueError("Pixel Lab currently requires the preserved azimuth-independent contract")
    theta, wave, budgets, optical_error, collection_error = _budgets(response)
    arrays_path = inputs.file(root / "optics/optics.npz", optical["arrays_sha256"])
    with np.load(arrays_path, allow_pickle=False) as stored:
        arrays = {
            key: stored[key]
            for key in (
                "wavelength_nm",
                "chief_ray_angle_deg",
                "conditional_weights",
                "directions_xyz",
            )
        }
    if not np.array_equal(arrays["wavelength_nm"], wave):
        raise ValueError("Optical and pixel wavelength axes differ")
    model_binding = {
        "cell_sha256": cell["cell_sha256"],
        "pixel_response_sha256": response["table_sha256"],
        "optical_revision": optical["revision"],
        "readout_sha256": digest(cell.get("readout", {})),
        "optics_sha256": optical["arrays_sha256"],
    }
    color_rows, color_reason = _color_rows(
        inputs, capture, model_binding, color_directory, color_summary
    )
    source_fields = {item["id"]: item for item in source_summary["fields"]}
    fields = []
    for fi, field in enumerate(optical["fields"]):
        inputs.check()
        identity = field["id"]
        record = {"id": identity, "status": "unavailable", "reason": None}
        original = source_fields.get(identity, {})
        tests = original.get("tests", {})
        signal = tests.get("signal", {})
        record["source_uniform_metrics"] = {
            "mean_electrons": signal.get("mean_electrons"),
            "mean_electrons_by_site": signal.get("mean_electrons_by_site"),
            "saturation": signal.get("saturation"),
            "raw_green_noise": copy.deepcopy(
                tests.get("noise", {}).get("uniform", {}).get("raw_green")
            ),
            "raw_green_noise_unit": tests.get("noise", {})
            .get("uniform", {})
            .get("raw_green", {})
            .get("unit"),
            "mean_electrons_unit": "electron/native_pixel",
        }
        record["source_images"] = copy.deepcopy(original.get("images", {}))
        record["color_comparison"] = _field_color(identity, color_rows, color_reason)
        if field.get("status") != "succeeded":
            record["reason"] = "preserved_optical_field_unavailable"
            fields.append(record)
            continue
        try:
            ri = [0.0, 0.5, 0.85].index(field["normalized_radius"])
            weights = np.asarray(arrays["conditional_weights"][:, ri], float)
            directions = np.asarray(arrays["directions_xyz"][ri], float)
            if (
                weights.shape != (len(wave), len(directions))
                or directions.shape[1:] != (3,)
                or not np.isfinite(weights).all()
                or (weights < 0).any()
                or not np.allclose(weights.sum(axis=1), 1, rtol=1e-9, atol=1e-10)
            ):
                raise ValueError(
                    "Saved pupil weights must be normalized conditional photon fractions"
                )
            admitted = np.any(weights > 0, axis=0)
            directions, weights = directions[admitted], weights[:, admitted]
            if not np.isfinite(directions).all() or not np.allclose(
                np.linalg.norm(directions, axis=1), 1, rtol=1e-7, atol=1e-9
            ):
                raise ValueError("Saved admitted pupil directions must be unit vectors")
            angles = np.degrees(np.arccos(np.clip(directions[:, 2], -1, 1)))
            chief = _array(arrays["chief_ray_angle_deg"][:, fi], (len(wave),), "chief angle")
            normal_budget = {key: value[0] for key, value in budgets.items()}
            chief_budget = {
                key: _interpolate(value, theta, chief[:, None])[:, 0]
                for key, value in budgets.items()
            }
            pupil_budget = {
                key: np.sum(
                    _interpolate(value, theta, np.broadcast_to(angles, weights.shape))
                    * weights[..., None],
                    axis=1,
                )
                for key, value in budgets.items()
            }
            record.update(
                status="available",
                normal_qe=normal_budget["collected_electrons"].tolist(),
                chief_qe=chief_budget["collected_electrons"].tolist(),
                pupil_qe=pupil_budget["collected_electrons"].tolist(),
                chief_theta_deg=chief.tolist(),
                photon_budget={
                    "normal": _pack_budget(normal_budget),
                    "chief": _pack_budget(chief_budget),
                    "pupil": _pack_budget(pupil_budget),
                },
                pupil_histogram={
                    "bin_edges_deg": theta.tolist(),
                    "weights": [
                        np.histogram(angles, bins=theta, weights=row)[0].tolist() for row in weights
                    ],
                },
                pupil_direction_count=int(len(angles)),
                weighting="stored_conditional_photons_no_extra_cosine_area_or_throughput",
            )
        except (ValueError, IndexError) as error:
            record["reason"] = str(error)
        fields.append(record)
    result = {
        "schema": SCHEMA,
        "id": source_summary["id"],
        "cfa_thickness_scale": cell["cfa_thickness_scale"],
        "source_binding": {
            "cell_sha256": cell["cell_sha256"],
            "response_sha256": response["table_sha256"],
            "optical_revision": optical["revision"],
            "acquisition_content_sha256": digest(_capture_identity(capture)),
        },
        "structure": {
            key: copy.deepcopy(cell[key])
            for key in ("cross_section", "geometry_um", "field_classification")
        },
        "wavelength_nm": wave.tolist(),
        "theta_deg": theta.tolist(),
        "sites": SITES,
        "photon_budget": {
            "axes": ["theta", "wavelength", "site"],
            **_pack_budget(budgets),
            "optical_closure_max_abs": optical_error,
            "collection_closure_max_abs": collection_error,
            "meaning": "R + layer absorption + T = 1; silicon = collected e + collection loss",
        },
        "qe": {
            "normal": budgets["collected_electrons"][0].tolist(),
            "table": budgets["collected_electrons"].tolist(),
            "unit": "collected_electron_per_incident_sensor_plane_photon",
        },
        "fields": fields,
        "dependencies": list(inputs.dependencies.values()),
        "limitations": [
            "Preserved azimuth-independent planar model; no new physics or capture",
            "Normal/chief/pupil comparisons change angular response only; "
            "lens throughput remains fixed",
            "CFA materials, collection and readout retain their declared assumptions",
            "Nine local native probes, not a full-frame sensor simulation",
            "Parent verifies RAW artifact bytes; "
            "color identity matches all retained capture content",
        ],
    }
    result["limitations"] = list(
        dict.fromkeys(
            [
                *result["limitations"],
                *optical.get("limitations", []),
            ]
        )
    )
    json.dumps(result, allow_nan=False)
    return result


def add_delta(candidate, baseline):
    """Add absolute QE differences in place and return the candidate."""
    if (
        candidate["wavelength_nm"] != baseline["wavelength_nm"]
        or candidate["theta_deg"] != baseline["theta_deg"]
        or candidate["sites"] != baseline["sites"]
        or candidate["source_binding"]["optical_revision"]
        != baseline["source_binding"]["optical_revision"]
    ):
        raise ValueError("QE deltas require identical spectral/angular and optical contracts")
    result = candidate
    result["baseline_candidate_id"] = baseline["id"]
    for name in ("normal", "table"):
        result["qe"]["delta_" + name] = (
            np.asarray(candidate["qe"][name]) - np.asarray(baseline["qe"][name])
        ).tolist()
    reference = {field["id"]: field for field in baseline["fields"]}
    for field in result["fields"]:
        base = reference.get(field["id"])
        if field["status"] == "available" and base and base["status"] == "available":
            for name in ("normal_qe", "chief_qe", "pupil_qe"):
                field["delta_" + name] = (np.asarray(field[name]) - np.asarray(base[name])).tolist()
    return result
