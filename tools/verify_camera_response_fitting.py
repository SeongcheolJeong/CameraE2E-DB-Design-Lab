#!/usr/bin/env python3
"""Actual HTTP fitting/publication/replay proof on a caller-owned frozen QA server.

Numerical RAW files use independent scalar forward equations and authored charge
means. They are not physical measurements. No lifecycle operations or automatic
POST retries are performed. Every POST is journaled before transmission. Restart
readback is GET-only and writes a distinct report, preserving the first journal.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import time
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import verify_camera_measured_charge as transport

from camera_platform import nonlinear_dcg, nonlinear_readout
from camera_platform.evidence_package import read_bundle
from camera_platform.registry import canonical, digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint

LEVELS = [0.0, 0.5, 3.0, 7.0, 14.0, 22.0]
SIDE = 16
sha, original_json, npy = transport.sha, transport.original_json, transport.npy


def fixture_model(template, paired):
    """Build a small authored model; no capture/decoder kernel generates the RAW."""
    model = deepcopy(template)
    source = {"kind": "simulated", "description": "Authored scalar HTTP fixture, no sensor data"}
    model.update(
        dark_current_e_per_s=0.0,
        reference_temperature_c=25.0,
        dark_current_doubling_c=None,
        source=deepcopy(source),
    )
    model["fixed_pattern"] = {"prnu_sigma": 0.0, "dsnu_e_per_s": 0.0, "seed": 0}
    if paired:
        model["shared_charge_capacity_e"] = 32.0
        paths = model["paths"]
        model["read_noise"]["covariance_v2"] = [
            [0.005**2, 0.35 * 0.005 * 0.008],
            [0.35 * 0.005 * 0.008, 0.008**2],
        ]
    else:
        model["full_well_e"] = 32.0
        model["read_noise"]["variance_v2"] = 0.005**2
        model["read_noise"]["analog_gain_range"] = [1.5, 1.5]
        paths = [model]
    model["read_noise"].update(temperature_range_c=[25.0, 25.0], source=deepcopy(source))
    for index, path in enumerate(paths):
        capacity = 24.0 if paired and index == 0 else 32.0
        gain = 1.0 if paired else 1.5
        path["digital_gain"] = 0.8 if paired and index == 1 else 1.3
        path["adc"].update(bit_depth=10, black_level_dn=128.0, full_scale_v=1.0)
        if paired:
            path.update(input_linear_ceiling_e=capacity, analog_gain=gain)
        voltage = (
            ([0.0, 0.18, 0.38] if index == 0 else [0.0, 0.09, 0.25])
            if paired
            else ([0.0, 0.12, 0.30])
        )
        path["charge_voltage_response"].update(
            charge_e=[0.0, 8.0, capacity],
            signal_voltage_v=voltage,
            valid_domain={"temperature_c": [25.0, 25.0], "analog_gain": [gain, gain]},
            signed_inverse={"minimum_voltage_v": -0.05, "method": "origin_tangent"},
            source=deepcopy(source),
        )
    validator = nonlinear_dcg.validate_model if paired else nonlinear_readout.validate_model
    return validator(model)


def scalar_raw(model, means, seed, side=SIDE):
    """Poisson + scalar PWL + Gaussian + two nearest-even clipped quantizers.

    Each paired site draws one Poisson charge shared by HCG and LCG, with a
    correlated voltage draw. No production forward/decode primitive is called.
    """
    rng = np.random.default_rng(seed)
    paired = "paths" in model
    paths = model["paths"] if paired else [model]
    covariance = (
        model["read_noise"]["covariance_v2"] if paired else [[model["read_noise"]["variance_v2"]]]
    )
    result = np.empty((len(means), len(paths), side, side), dtype=np.uint16)
    for frame, mean in enumerate(means):
        charge = rng.poisson(mean, side * side)
        noise = rng.multivariate_normal(np.zeros(len(paths)), covariance, side * side)
        for row in range(side * side):
            for index, path in enumerate(paths):
                capacity = (
                    min(model["shared_charge_capacity_e"], path["input_linear_ceiling_e"])
                    if paired
                    else model["full_well_e"]
                )
                electrons = min(float(charge[row]), capacity)
                curve = path["charge_voltage_response"]
                knots, values = curve["charge_e"], curve["signal_voltage_v"]
                segment = next(i for i in range(len(knots) - 1) if electrons <= knots[i + 1])
                voltage = values[segment] + (electrons - knots[segment]) * (
                    values[segment + 1] - values[segment]
                ) / (knots[segment + 1] - knots[segment])
                voltage += noise[row, index]
                white, black = 2 ** path["adc"]["bit_depth"] - 1, path["adc"]["black_level_dn"]
                gain = path["analog_gain"] if paired else 1.5
                first = min(
                    white,
                    max(0, round(black + voltage * gain * white / path["adc"]["full_scale_v"])),
                )
                code = min(white, max(0, round(black + path["digital_gain"] * (first - black))))
                result[frame, index, row // side, row % side] = code
    return result if paired else result[:, 0]


def seed_model(truth):
    seed = deepcopy(truth)
    for path in seed.get("paths", [seed]):
        curve = path["charge_voltage_response"]
        increments = np.diff(curve["signal_voltage_v"]) * [0.8, 1.25]
        curve["signal_voltage_v"] = np.r_[0.0, np.cumsum(increments)].tolist()
    if "paths" in seed:
        cross = 0.35 * 0.006 * 0.009
        seed["read_noise"]["covariance_v2"] = [[0.006**2, cross], [cross, 0.009**2]]
    else:
        seed["read_noise"]["variance_v2"] = 0.006**2
    return seed


def evidence(metadata, *, means=None, seed=None):
    anchor = metadata["charge_reference"]
    tables = (
        []
        if means is None
        else [
            {
                "name": "independently authored expected total Poisson charge",
                "columns": [
                    {"name": "frame_index", "unit": "1"},
                    {"name": "mean_total_charge_e", "unit": "electron"},
                ],
                "rows": [[float(index), mean] for index, mean in enumerate(means)],
            }
        ]
    )
    return {
        "schema": "camera_nonlinear_response_evidence_v1",
        "documents": [
            {
                "id": identity,
                "role": "charge_reference",
                "source_uri": anchor["source_uri"],
                "method_and_version": anchor["method_and_version"],
                "description": "Authored scalar Poisson/PWL/Gaussian/two-quantizer fixture; "
                f"RNG seed {seed}; known dark zero; no physical camera or certificate.",
                "tables": tables,
            }
            for identity in anchor["evidence_document_ids"]
        ],
    }


class Proof(transport.Proof):
    def __init__(self, args):
        if not args.verify_only and args.output_dir.exists():
            raise ValueError("Initial proof requires a new, nonexistent output directory")
        super().__init__(args)
        self.report.update(
            schema=(
                "camera_response_fitting_http_readback_v1"
                if args.verify_only
                else "camera_response_fitting_http_verification_v1"
            ),
            tool_sha256=sha(Path(__file__).read_bytes()),
            helper_tool_sha256=sha(Path(transport.__file__).read_bytes()),
            numerical_scope="Independent scalar authored forward fixtures; conditional fitted "
            "response/noise recovery; replay arrays compared with the software "
            "kernel; no physical accuracy or EMVA compliance proof",
        )
        if not args.verify_only:
            for key in ("observation_files", "fit_jobs", "fitted_calibrations"):
                self.report.setdefault(key, {})
        self.save()

    def load_resume(self, source):
        """Explicitly reuse an acknowledged exact prefix, including read-only refusals."""
        initial = (source / "http-verification.json").read_bytes()
        prior = json.loads(initial)
        if prior["status"] != "failed" or any(
            prior[key] != value for key, value in self.binding.items()
        ):
            raise ValueError("Resume requires the same frozen source and failed QA journal")
        if any(
            getattr(self.args, key) != prior["selection"][key] for key in ("assembly_id", "test_id")
        ):
            raise ValueError("Resume cannot change assembly or Test selection")
        pins = [
            entry
            for key in ("fixtures", "records", "artifacts", "bundles")
            for entry in prior[key].values()
        ]
        for label, operation in prior["operations"].items():
            code = operation.get("http_status")
            known_refusal = code == 422 and operation["endpoint"] == "readout-fits/plan"
            if (
                operation["status"] != "response_received"
                or not (code in (200, 201, 202) or known_refusal)
                or "HTTP status " + label not in prior["checks"]
            ):
                raise ValueError("Resume refuses unacknowledged or unexpected POST: " + label)
            pins.extend((operation["request"], operation["response"]))
        for entry in pins:
            path = source / entry["path"]
            if not path.resolve().is_relative_to(source) or path.is_symlink():
                raise ValueError("Resume file escapes original evidence")
            content = path.read_bytes()
            if sha(content) != entry["sha256"] or len(content) != entry["bytes"]:
                raise ValueError("Resume evidence bytes changed: " + entry["path"])
            previous = self.resume_files.setdefault(entry["path"], entry["sha256"])
            if previous != entry["sha256"]:
                raise ValueError("Conflicting resume file identities")
        for label, operation in prior["operations"].items():
            request = json.loads((source / operation["request"]["path"]).read_bytes())
            value = json.loads((source / operation["response"]["path"]).read_bytes())
            if request["endpoint"] != operation["endpoint"] or (
                operation.get("id") != value.get("id")
            ):
                raise ValueError("Resume request/response identity changed: " + label)
            for entry in (request.get("files") or {}).values():
                if self.resume_files.get(entry["path"]) != entry["sha256"]:
                    raise ValueError("Resume upload was not pinned: " + label)
        self.resume_operations = deepcopy(prior["operations"])
        self.report = deepcopy(prior)
        self.saved = self.report
        for key in ("error", "phase", "completed_at", "check_count"):
            self.report.pop(key, None)
        self.report.update(
            status="in_progress",
            resume={
                "source_directory": str(source),
                "source_journal_sha256": sha(initial),
                "source_tool_sha256": prior["tool_sha256"],
                "reused_operations": [],
                "copied_file_sha256": dict(self.resume_files),
                "policy": "Known exact responses, including explicit read-only plan refusals, "
                "are reused without repeating any prior POST",
            },
        )
        for relative in self.resume_files:
            transport.durable(self.out / relative, (source / relative).read_bytes(), exclusive=True)
        transport.durable(self.out / "resume-source-journal.json", initial, exclusive=True)

    def post(self, label, endpoint, *, payload=None, files=None, status=(200, 201, 202)):
        if label not in self.resume_operations or label in self.resumed_operations:
            return super().post(label, endpoint, payload=payload, files=files, status=status)
        item = self.report["operations"][label]
        request = canonical({"endpoint": endpoint, "payload": payload, "files": files}).encode()
        original = (self.out / item["request"]["path"]).read_bytes()
        response = (self.out / item["response"]["path"]).read_bytes()
        if (
            request != original
            or sha(original) != item["request"]["sha256"]
            or sha(response) != item["response"]["sha256"]
            or item["http_status"] not in status
        ):
            raise ValueError("Resume would change an acknowledged request: " + label)
        value = json.loads(response)
        item["reused_without_post"] = True
        self.resumed_operations.add(label)
        self.report["resume"]["reused_operations"].append(label)
        self.check("HTTP status " + label, True)
        self.progress("reuse acknowledged POST " + label)
        return value

    def import_base(self, label, data):
        files = {
            "metadata_file": self.fixture(label + ".json", original_json(data)),
            "dark_rate": self.fixture(label + "-dark.npy", npy(np.zeros(data["dark_rate_shape"]))),
            "evidence": self.fixture(label + "-evidence.json", original_json(evidence(data))),
        }
        record = self.post(label, "readout-calibrations/import", files=files, status=(201,))
        self.report["calibration_files"][record["id"]] = files
        self.save()
        return self.preserve(label, "readout-calibrations/" + record["id"], record)

    def capture(self, label, template, base, truth, *, seed, means=LEVELS):
        data = deepcopy(template)
        data.update(
            name="QA authored " + label,
            source_kind="simulated",
            parents=[],
            source_uri="fixture://response-fitting/" + label,
            method_and_version="independent-scalar-quantized-poisson:1",
            license="Authored numerical test data",
            physical_instance=None,
            dataset_ids=[self.prefix + "-" + label],
            acquisition_group_id=self.prefix + "-" + label,
            raw_formats=deepcopy(base["raw_formats"]),
        )
        data["geometry"]["crop_origin"] = [0, 0]
        paired = "paths" in truth
        settings = [
            {"analog_gain": row["analog_gain"][0], "digital_gain": row["digital_gain"]}
            for row in base["applicability"]["readouts"]
        ]
        data["frames"] = [
            {
                "frame_id": label + "-" + str(index),
                "raw_index": index,
                "frame_start_ns": None,
                "applied": {
                    "exposure_ns": 4_000_000,
                    "gain": 1.0 if paired else 1.5,
                    "temperature_c": 25.0,
                    "applied_at_ns": None,
                    "readout_settings": deepcopy(settings),
                },
                "settings_evidence": {
                    "status": "recorded_applied",
                    "source_uri": "fixture://scalar-forward-settings",
                    "method_and_version": "authored-settings:1",
                    "description": "Recorded authored generator settings, not camera telemetry",
                },
            }
            for index in range(len(means))
        ]
        raw = scalar_raw(truth, means, seed)
        files = {
            "metadata_file": self.fixture(label + ".json", original_json(data)),
            "raw": self.fixture(label + ".npy", npy(raw)),
        }
        record = self.post(label, "captures/nonlinear/import", files=files, status=(201,))
        self.report["capture_files"][record["id"]] = files
        self.report["fixtures"][label + "-generator.json"] = self.file(
            "fixtures/" + label + "-generator.json",
            original_json(
                {
                    "seed": seed,
                    "means_total_e": means,
                    "truth_model": truth,
                    "sample_rows": len(means) * SIDE**2,
                    "raw_code_samples": raw.size,
                    "physical_measurement_count": 0,
                }
            ),
        )
        self.save()
        return self.preserve(label, "captures/" + record["id"], record)

    def observation(self, label, capture, base, role, *, seed, means=LEVELS):
        data = self.client.template("readout-observation", capture_id=capture["id"])
        data.update(
            name="QA targets " + label,
            source_kind="simulated",
            parents=[],
            source_uri="fixture://external-authored-total-charge/" + label,
            method_and_version="independent-total-targets:1",
            dataset_role=role,
            dataset_ids=[self.prefix + "-" + label],
            charge_reference=deepcopy(base["charge_reference"]),
            roi={"row_range": [0, SIDE], "column_range": [0, SIDE]},
        )
        for frame, mean in zip(data["frames"], means, strict=True):
            frame["mean_total_charge_e"] = [mean] * 4
        files = {
            "metadata_file": self.fixture(label + ".json", original_json(data)),
            "evidence": self.fixture(
                label + "-evidence.json", original_json(evidence(data, means=means, seed=seed))
            ),
        }
        record = self.post(label, "readout-observations/import", files=files, status=(201,))
        self.report["observation_files"][record["id"]] = files
        self.save()
        return self.preserve(label, "readout-observations/" + record["id"], record)

    def wait_fit(self, label, queued):
        deadline = time.monotonic() + 180
        route = "readout-fits/" + queued["id"]
        while time.monotonic() < deadline:
            job = self.client.get(route)
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            self.progress("wait response fit " + label + ": " + job["status"])
            time.sleep(1)
        else:
            raise TimeoutError("Fit remains active; reconcile recorded job ID " + queued["id"])
        self.check("fit Worker succeeded " + label, job["status"] == "succeeded")
        self.check(
            "fit executable source binding " + label,
            job["environment"] == self.binding["environment"],
        )
        self.report["fit_jobs"][label] = job["id"]
        return self.preserve(label, route, job)

    def verify_fit(self, label, job, truth):
        result = job["result"]
        candidate = result["candidate"]
        optimizer = result["optimizer_report"]
        self.check(
            "candidate frozen before evaluation " + label,
            result["candidate_frozen_before_evaluation"] is True
            and candidate["candidate_sha256"]
            == digest({k: v for k, v in candidate.items() if k != "candidate_sha256"}),
        )
        self.check(
            "no physical authority " + label,
            result["physical_validation"] == "not_established"
            and result["authority"] == {"physical_validation": False, "release": False}
            and result["uncertainty"] == {"kind": "unknown"},
        )
        self.check("fit converged " + label, optimizer["converged"] is True)
        self.check(
            "train and heldout exposures distinct " + label,
            not set(job["training_tokens"]) & set(job["evaluation_tokens"]),
        )
        self.check(
            "no invented mean or joint-coverage claim " + label,
            result["validation_report"]["predicted_mean_dn"] is None
            and optimizer["joint_coverage"] == "not_claimed"
            and optimizer["correlation_fitted"] is False,
        )
        paths = truth.get("paths", [truth])
        fitted_paths = candidate["electrical_model"].get("paths", [candidate["electrical_model"]])
        recovery = []
        for index, (actual, expected, report) in enumerate(
            zip(fitted_paths, paths, optimizer["paths"], strict=True)
        ):
            slopes = np.diff(actual["charge_voltage_response"]["signal_voltage_v"]) / np.diff(
                expected["charge_voltage_response"]["signal_voltage_v"]
            )
            sigma = math.sqrt(
                truth["read_noise"]["covariance_v2"][index][index]
                if "paths" in truth
                else truth["read_noise"]["variance_v2"]
            )
            self.check(
                "authored slope recovery " + label + str(index),
                bool(np.all(np.abs(slopes - 1) < 0.15)),
            )
            self.check(
                "authored voltage-noise recovery " + label + str(index),
                abs(report["sigma_v"] / sigma - 1) < 0.25,
            )
            recovery.append(
                {
                    "readout_id": report["readout_id"],
                    "fitted_to_truth_segment_slopes": slopes.tolist(),
                    "fitted_to_truth_sigma": report["sigma_v"] / sigma,
                    "interpretation": "deterministic seeded numerical acceptance",
                }
            )
        for artifact in result["artifacts"]:
            self.artifact(artifact)
        heldout = job["validation_observations"][0]
        heldout_raw = self.array(heldout["capture_snapshot"], "raw-capture")
        for index, path in enumerate(result["validation_report"]["paths"]):
            for group in path["groups"]:
                frame = LEVELS.index(group["mean_charge_e"])
                codes = heldout_raw[frame, index] if heldout_raw.ndim == 4 else heldout_raw[frame]
                self.check(
                    "heldout empirical location " + label + str(index) + str(frame),
                    group["sample_count"] == codes.size
                    and group["observed_median_dn"] == float(np.median(codes))
                    and group["observed_mean_dn"] == float(np.mean(codes)),
                )
                for coverage in group["coverage"]:
                    count = int(
                        np.count_nonzero(
                            (codes >= coverage["lower_dn"]) & (codes <= coverage["upper_dn"])
                        )
                    )
                    self.check(
                        "heldout empirical interval count "
                        + label
                        + str(index)
                        + str(frame)
                        + str(coverage["nominal_probability"]),
                        count == coverage["covered_samples"]
                        and coverage["empirical_fraction"] == count / codes.size,
                    )
        self.report.setdefault("recovery", {})[label] = recovery
        self.save()

    def bundle(self, label, route):
        self.progress("bundle " + label)
        manifest = self.client.get(route + "/export")
        self.preserve(label + "-export", route + "/export", manifest)
        for artifact in transport.artifacts(manifest):
            self.artifact(artifact)
        response = self.client.http.get("/api/v1/" + route + "/bundle")
        response.raise_for_status()
        content = response.content
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            self.check("unique ZIP members " + label, len(names) == len(set(names)))
            members = {name: sha(archive.read(name)) for name in names}
            physical = sum(info.file_size for info in archive.infolist())
            header = json.loads(archive.read("manifest.json"))
            if header.get("schema") == "camera_evidence_bundle_v2":
                decoded = read_bundle(
                    io.BytesIO(content), expected_manifest_sha256=digest(manifest)
                )
                self.check("exact graph reconstruction " + label, decoded == manifest)
            else:
                self.check("exact plain manifest " + label, header == manifest)
            graph = header.get("schema") == "camera_evidence_bundle_v2"
            required = {
                "artifacts/" + a["sha256"] + ("" if graph else "/" + a["name"])
                for a in transport.artifacts(manifest)
            }
            self.check(
                "full original and generated artifact closure " + label, required <= set(names)
            )
            for name in required:
                self.check(
                    "ZIP artifact " + label + " " + name, members[name] == name.split("/")[1]
                )
        key = "fit_job" if route.startswith("readout-fits/") else "replay"
        self.check("bundle exact saved job " + label, manifest[key] == self.client.get(route))
        if key == "replay":
            closure = manifest["replay"]["source"]["response_calibration_lineage"]
            self.check(
                "replay retains fitted candidate and observation closure " + label,
                bool(manifest["readout_fit_jobs"])
                and bool(manifest["readout_observations"])
                and {r["id"] for r in closure["evaluation_captures"]}
                <= {r["id"] for r in manifest["nonlinear_acquisition_lineage"]},
            )
        if self.args.verify_only:
            saved = self.saved["bundles"][label]
            self.check(
                "ZIP member readback " + label,
                members == saved["members"] and digest(manifest) == saved["manifest_sha256"],
            )
            self.check(
                "original ZIP retained " + label,
                sha((self.out / saved["path"]).read_bytes()) == saved["sha256"],
            )
        else:
            entry = self.file("bundles/" + label + ".zip", content)
            self.report["bundles"][label] = {
                **entry,
                "route": route,
                "members": members,
                "manifest_sha256": digest(manifest),
                "uncompressed_bytes": physical,
            }
            self.save()

    def verify_originals(self):
        super().verify_originals()
        for identity, files in self.saved["observation_files"].items():
            record = self.client.get("readout-observations/" + identity)
            names = {
                "readout-fit-observation-metadata.json": "metadata_file",
                "readout-fit-observation-evidence.json": "evidence",
            }
            for artifact in record["artifacts"]:
                self.check(
                    "exact uploaded observation " + identity + artifact["name"],
                    self.artifact(artifact)
                    == (self.out / files[names[artifact["name"]]]["path"]).read_bytes(),
                )

    def initial(self):
        self.prefix = self.report.get(
            "fixture_prefix", "qa-response-fit-" + sha(str(self.out).encode())[:12]
        )
        self.report["fixture_prefix"] = self.prefix
        tool_name = "proof-resume-tool.py" if self.args.resume_from else "proof-tool.py"
        self.fixture(tool_name, Path(__file__).read_bytes())
        self.fixture("proof-transport.py", Path(transport.__file__).read_bytes())
        assembly = self.client.get("assemblies/" + self.args.assembly_id)
        test = self.client.get("tests/" + self.args.test_id)
        self.preserve("input-assembly", "assemblies/" + assembly["id"], assembly)
        self.preserve("input-test", "tests/" + test["id"], test)
        models = [self.client.get("models/" + identity) for identity in assembly["model_ids"]]
        sensor = next(model for model in models if model["kind"] == "sensor")
        self.preserve("input-sensor", "models/" + sensor["id"], sensor)
        self.report["selection"] = {
            "assembly_id": assembly["id"],
            "test_id": test["id"],
            "sensor_model_id": sensor["id"],
        }
        self.save()
        for family, paired in (("single", False), ("dcg", True)):
            mode = "shared_charge_paths" if paired else "sequential_frames"
            metadata = self.client.template(
                "readout-calibration", sensor_model_id=sensor["id"], mode=mode
            )
            truth = fixture_model(metadata["electrical_model"], paired)
            metadata.update(
                name="QA small response seed " + family,
                dark_rate_shape=[SIDE, SIDE],
                electrical_model=seed_model(truth),
                dataset_ids=[self.prefix + "-base-" + family],
            )
            metadata["applicability"].update(temperature_c=[25.0, 25.0], exposure_s=[0.004, 0.004])
            metadata["applicability"]["readouts"] = [
                {
                    "readout_id": ["HCG", "LCG"][index] if paired else "single",
                    "analog_gain": [path["analog_gain"]] * 2 if paired else [1.5, 1.5],
                    "digital_gain": path["digital_gain"],
                }
                for index, path in enumerate(truth.get("paths", [truth]))
            ]
            base = self.import_base(family + "-base", metadata)
            template = self.client.template(
                "nonlinear-capture",
                assembly_id=assembly["id"],
                test_id=test["id"],
                readout_mode=mode,
            )
            if min(template["geometry"]["full_sensor_shape"]) < SIDE:
                raise ValueError("Selected sensor requires at least a 16x16 native region")
            seeds = [91831, 81841, 71851] if paired else [91830, 81840, 71850]
            captures = [
                self.capture(family + "-" + label, template, base, truth, seed=seed)
                for label, seed in zip(("train", "heldout", "fresh"), seeds, strict=True)
            ]
            train = self.observation(
                family + "-train-targets", captures[0], base, "development", seed=seeds[0]
            )
            heldout = self.observation(
                family + "-heldout-targets", captures[1], base, "holdout", seed=seeds[1]
            )
            specification = {
                "schema": "camera_readout_fit_request_v1",
                "name": "QA anchored " + family,
                "base_calibration_id": base["id"],
                "training_observation_ids": [train["id"]],
                "validation_observation_ids": [heldout["id"]],
                "slope_scale_bounds": [0.5, 1.6],
                "sigma_v_bounds": [0.001, 0.03],
                "max_iterations": 100,
            }
            wrong = deepcopy(specification)
            wrong.update(
                training_observation_ids=[heldout["id"]], validation_observation_ids=[train["id"]]
            )
            self.readonly_post(
                family + "-role-refusal", "readout-fits/plan", wrong, expected=422, detail="roles"
            )
            alias = self.observation(
                family + "-train-as-holdout", captures[0], base, "holdout", seed=seeds[0]
            )
            wrong = deepcopy(specification)
            wrong["validation_observation_ids"] = [alias["id"]]
            self.readonly_post(
                family + "-same-exposure-refusal",
                "readout-fits/plan",
                wrong,
                expected=422,
                detail="overlap",
            )
            plan = self.readonly_post(family + "-plan", "readout-fits/plan", specification)
            self.report["plans"][family] = {
                "plan_hash": plan["plan_hash"],
                "resources": plan["resources"],
            }
            self.save()
            queued = self.post(
                family + "-fit-submit",
                "readout-fits",
                payload={
                    "specification": plan["specification"],
                    "plan_hash": plan["plan_hash"],
                },
                status=(202,),
            )
            job = self.wait_fit(family + "-fit", queued)
            self.verify_fit(family, job, truth)
            route = "readout-fits/" + job["id"]
            exported = self.client.get(route + "/export")
            publication = self.post(
                family + "-publish",
                route + "/publish",
                payload={
                    "name": "QA fitted " + family,
                    "revision": "1",
                    **exported["publication_binding"],
                },
                status=(201,),
            )
            self.preserve(
                family + "-fitted", "readout-calibrations/" + publication["id"], publication
            )
            self.report["fitted_calibrations"][family] = publication["id"]
            self.check(
                "publication exact candidate " + family,
                publication["electrical_model"] == job["result"]["candidate"]["electrical_model"]
                and publication["request_sha256"] == job["content_hash"]
                and publication["result_sha256"] == digest(job["result"]),
            )
            wrong = deepcopy(specification)
            wrong["base_calibration_id"] = publication["id"]
            self.readonly_post(
                family + "-ancestor-holdout-refusal",
                "readout-fits/plan",
                wrong,
                expected=422,
                detail="overlap",
            )
            self.bundle(family + "-fit", route)
            for path in ["hcg", "lcg"] if paired else [None]:
                label = family + ("-" + path if path else "")
                selector = self.selection(publication, path)
                draft = self.client.advanced_isp_template(
                    "capture", captures[2]["id"], selector=selector
                )
                draft["name"] = "QA fitted-response replay " + label
                gain = 1.0 if paired else 1.5
                draft["valid_domain"]["gain"] = {"min": gain, "max": gain, "unit": "1"}
                profile = self.post(label + "-profile", "models", payload=draft, status=(201,))
                self.preserve(label + "-profile", "models/" + profile["id"], profile)
                queued = self.post(
                    label + "-replay-submit",
                    "replays",
                    payload={
                        "schema": "camera_isp_replay_v3",
                        "name": "QA fresh fitted " + label,
                        "source_kind": "capture",
                        "source_id": captures[2]["id"],
                        "selector": selector,
                        "isp_model_id": profile["id"],
                        "force": False,
                    },
                    status=(202,),
                )
                replay = self.wait_replay(label + "-replay", queued)
                self.verify_replay(replay)
                self.bundle(label + "-replay", "replays/" + replay["id"])
        self.verify_originals()
        for label, entry in list(self.report["records"].items()):
            self.check(
                "final immutable JSON " + label,
                sha(canonical(self.client.get(entry["route"])).encode()) == entry["sha256"],
            )
        self.report["final_db_snapshot"] = transport.db_snapshot(self.state)
        self.save()

    def run(self):
        try:
            with PlatformClient(self.binding["base_url"], timeout=180) as client:
                self.client = client
                health = client.get("health")
                self.check(
                    "caller-owned healthy QA state and Worker",
                    Path(health["state_dir"]).resolve() == self.state
                    and health["worker"] == "running",
                )
                self.report["initial_health"] = health
                self.save()
                self.readback() if self.args.verify_only else self.initial()
                self.verify_resume_source()
                self.check(
                    "local source remains frozen",
                    environment_fingerprint() == self.binding["environment"],
                )
                self.report.update(
                    status="passed",
                    completed_at=datetime.now(UTC).isoformat(),
                    check_count=len(self.report["checks"]),
                )
                self.save()
                print(
                    json.dumps(
                        {
                            "status": "passed",
                            "checks": len(self.report["checks"]),
                            "report": str(self.path),
                        }
                    ),
                    flush=True,
                )
        except Exception as exc:
            self.report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            self.save()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--assembly-id", required=True)
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--readback-output", type=Path)
    parser.add_argument(
        "--resume-from",
        type=Path,
        help="Explicit acknowledged-prefix reuse into a new output directory",
    )
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if (
        address.scheme != "http"
        or address.hostname != "127.0.0.1"
        or address.port in (None, 8770)
        or address.path not in ("", "/")
        or address.query
        or address.fragment
        or address.username
        or address.password
    ):
        parser.error("Use an explicit loopback QA port separate from primary8770")
    state, out = args.state_dir.resolve(), args.output_dir.resolve()
    if (
        "qa" not in state.name.lower()
        or state == out
        or state in out.parents
        or out in state.parents
        or not (state / "registry.sqlite").is_file()
    ):
        parser.error("Require an existing separate QA state and report directory")
    if args.verify_only != bool(args.readback_output):
        parser.error("--verify-only requires --readback-output; initial run must omit it")
    if args.resume_from:
        original = args.resume_from.resolve()
        if (
            args.verify_only
            or original == out
            or original in out.parents
            or out in original.parents
            or original == state
            or state in original.parents
            or not (original / "http-verification.json").is_file()
        ):
            parser.error("Resume requires a separate failed journal and new output directory")
    if args.verify_only and (
        args.readback_output.resolve().is_relative_to(out)
        or args.readback_output.resolve().is_relative_to(state)
    ):
        parser.error("Readback output must be outside the initial evidence and QA state")
    Proof(args).run()


if __name__ == "__main__":
    main()
