#!/usr/bin/env python3
"""Actual isolated HTTP/Worker proof of authored retained-photodiode RAW.

The caller owns server lifecycle. Every mutation has a durable request/response
journal and is sent at most once. Readback writes a separate report and never
changes original evidence. No measured sensor accuracy or physical pass is claimed.
"""

from __future__ import annotations

import argparse
import io
import json
import time
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import numpy as np
import verify_camera_measured_charge as transport

from camera_platform import __version__
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.evidence_package import read_bundle
from camera_platform.registry import canonical, digest
from camera_platform.sdk import PlatformClient

SIDE = 8
EXPOSURE_NS = 4_000_000
SEED = 517


def fields(record, contract):
    return {k: deepcopy(v) for k, v in record.items() if k in contract.model_fields}


def scalar_voltage(charge, curve):
    """Independent scalar piecewise-linear forward response; no product encoder."""
    knots, volts = curve["charge_e"], curve["signal_voltage_v"]
    if not knots[0] <= charge <= knots[-1]:
        raise ValueError("Independent reference charge exceeds the authored curve")
    for index in range(len(knots) - 1):
        if charge <= knots[index + 1]:
            fraction = (charge - knots[index]) / (knots[index + 1] - knots[index])
            return volts[index] + fraction * (volts[index + 1] - volts[index])
    raise AssertionError("Unreachable reference interval")


def scalar_raw(charge, model, analog_gain):
    adc = model["adc"]
    white, black = 2 ** adc["bit_depth"] - 1, adc["black_level_dn"]
    charge = min(charge, model.get("input_linear_ceiling_e", model.get("full_well_e", charge)))
    voltage = (
        scalar_voltage(charge, model["charge_voltage_response"])
        if "charge_voltage_response" in model
        else charge * model["conversion_gain_v_per_e"]
    )
    code = min(white, max(0, round(black + voltage * analog_gain * white / adc["full_scale_v"])))
    return min(white, max(0, round(black + model["digital_gain"] * (code - black))))


def authored_early_knee(model):
    """Exercise nonlinear response below half-capacity under incomplete transfer."""
    model = deepcopy(model)
    description = (
        "Authored acceptance curve bends at two percent of path capacity; positive origin "
        "slope and original end swing retained. No measured device calibration."
    )
    for path in model.get("paths", [model]):
        curve = path["charge_voltage_response"]
        origin_slope = curve["signal_voltage_v"][1] / curve["charge_e"][1]
        capacity = curve["charge_e"][-1]
        curve["charge_e"][1] = capacity * 0.02
        curve["signal_voltage_v"][1] = capacity * 0.02 * origin_slope
        curve["source"]["description"] = description
    model["source"]["description"] = description
    model["read_noise"]["source"]["description"] = (
        "Authored conversion-output noise scale retained while the response knee is changed; "
        "no measured noise calibration."
    )
    return model


class Proof(transport.Proof):
    def __init__(self, args):
        args.verify_only = args.mode == "readback"
        args.resume_from = None
        args.test_id = None
        super().__init__(args)
        self.report.update(
            schema="camera_retained_charge_http_readback_v1"
            if args.verify_only
            else "camera_retained_charge_http_verification_v1",
            tool_sha256=transport.sha(Path(__file__).read_bytes()),
            helper_tool_sha256=transport.sha(Path(transport.__file__).read_bytes()),
            total_uncertainty={"kind": "unknown"},
            numerical_scope="Authored bright-to-dark retained-photodiode population and "
            "actual RAW checks against an independent scalar charge ledger/encoder; "
            "no physical sensor accuracy, trap identification or scene-radiance claim.",
        )
        if not args.verify_only:
            for key, source in (
                ("tool_source", Path(__file__)),
                ("helper_tool_source", Path(transport.__file__)),
            ):
                content = source.read_bytes()
                self.report[key] = self.fixture(key + "-" + transport.sha(content) + ".py", content)
        self.save()

    def check(self, label, condition):
        """Count each executed assertion, including repeated closure occurrences."""
        if not condition:
            raise AssertionError(label)
        self.report["checks"].append(label)
        self.save()

    def template(self, name, **query):
        value = self.client.get("templates/" + name + "?" + urlencode(query))
        self.check("template GET " + name + " " + digest(query)[:12], isinstance(value, dict))
        return value

    def remember(self, label, route, record):
        self.preserve(label, route + "/" + record["id"], record)
        for artifact in transport.artifacts(record):
            if "url" in artifact:
                self.artifact(artifact)
        return record

    def create(self, label, route, payload, *, read_route=None):
        return self.remember(label, read_route or route, self.post(label, route, payload=payload))

    def wait(self, label, route, queued):
        waiter = self.client.wait_characterization if route == "factory/jobs" else self.client.wait
        value = waiter(queued["id"], timeout=240)
        self.check("Worker succeeded " + label, value["status"] == "succeeded")
        self.check(
            "Worker frozen environment " + label,
            value["environment"] == self.binding["environment"],
        )
        return self.remember(label, route, value)

    def import_image(self, label, metadata, values):
        """Durable multipart Form metadata plus exact original NPY bytes."""
        entry = self.fixture(label + ".npy", transport.npy(values))
        request = {"endpoint": "spectral_images/import", "metadata": metadata, "image": entry}
        if self.args.verify_only or label in self.report["operations"]:
            raise ValueError("No repeated multipart mutations")
        operation = {
            "endpoint": request["endpoint"],
            "status": "intent_saved",
            "request": self.file("requests/" + label + ".json", canonical(request).encode()),
        }
        self.report["operations"][label] = operation
        self.save()
        content = (self.out / entry["path"]).read_bytes()
        if transport.sha(content) != entry["sha256"]:
            raise ValueError("Image fixture changed after its intent was saved")
        started = time.monotonic()
        try:
            response = self.client.http.post(
                "/api/v1/spectral_images/import",
                data={"metadata": canonical(metadata)},
                files={"image": (label + ".npy", content, "application/octet-stream")},
            )
        except Exception:
            operation.update(status="uncertain_response_requires_manual_reconciliation")
            self.save()
            raise
        operation.update(
            status="response_received",
            http_status=response.status_code,
            response=self.file("responses/" + label + ".json", response.content),
            elapsed_s=time.monotonic() - started,
        )
        self.save()
        self.check("HTTP status " + label, response.status_code == 201)
        value = response.json()
        operation["id"] = value.get("id")
        self.save()
        return self.remember(label, "spectral_images", value)

    def bundle(self, label, route):
        manifest = self.client.get(route + "/export")
        self.preserve(label + "-export", route + "/export", manifest)
        response = self.client.http.get("/api/v1/" + route + "/bundle")
        response.raise_for_status()
        content = response.content
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            self.check("ZIP unique names " + label, len(names) == len(set(names)))
            self.check(
                "ZIP bounded paths " + label,
                all(
                    not Path(name).is_absolute() and ".." not in Path(name).parts for name in names
                ),
            )
            members = {name: transport.sha(archive.read(name)) for name in names}
            header = json.loads(archive.read("manifest.json"))
            graph = header.get("schema") == "camera_evidence_bundle_v2"
            restored = (
                read_bundle(io.BytesIO(content), expected_manifest_sha256=digest(manifest))
                if graph
                else header
            )
            self.check("ZIP exact logical manifest " + label, restored == manifest)
            for artifact in transport.artifacts(manifest):
                name = "artifacts/" + artifact["sha256"] + ("" if graph else "/" + artifact["name"])
                self.check(
                    "ZIP complete input/output artifact " + label + " " + name,
                    members.get(name) == artifact["sha256"],
                )
                if "url" in artifact:
                    self.artifact(artifact)
            physical = sum(item.file_size for item in archive.infolist())
        run = self.client.get(route)
        self.check("ZIP exact run " + label, manifest["run"] == run)
        self.check(
            "ZIP frozen acquisition input snapshots " + label,
            all(
                run.get(key)
                for key in (
                    "model_snapshots",
                    "assembly_snapshot",
                    "test_snapshot",
                    "scene_sequence_snapshot",
                    "control_sequence_snapshot",
                    "capture_program_snapshot",
                )
            ),
        )
        self.check(
            "ZIP model ancestry " + label,
            {m["id"] for m in run["model_snapshots"]}
            <= {m["id"] for m in manifest["model_lineage"]},
        )
        if self.args.verify_only:
            saved = self.saved["bundles"][label]
            self.check(
                "ZIP unchanged members " + label,
                members == saved["members"] and digest(manifest) == saved["manifest_sha256"],
            )
            self.check(
                "ZIP original retained " + label,
                transport.sha((self.out / saved["path"]).read_bytes()) == saved["sha256"],
            )
        else:
            self.report["bundles"][label] = {
                **self.file("bundles/" + label + ".zip", content),
                "route": route,
                "members": members,
                "manifest_sha256": digest(manifest),
                "uncompressed_bytes": physical,
            }
            self.save()

    def initial(self):
        assemblies = self.client.get("assemblies")
        original = next((a for a in assemblies if a["id"] == self.args.assembly_id), None)
        if self.args.assembly_id and original is None:
            raise ValueError("Requested input assembly does not exist")
        original = original or sorted(assemblies, key=lambda a: a["id"])[0]
        self.preserve("input-assembly", "assemblies/" + original["id"], original)
        models = {}
        for identity in original["model_ids"]:
            record = self.client.get("models/" + identity)
            models[record["kind"]] = self.remember("input-" + record["kind"], "models", record)
        sensor = fields(models["sensor"], ModelCreate)
        if "qe" not in sensor["payload"]:
            raise ValueError("Use a seeded legacy-QE assembly for this bounded optical fixture")
        sensor.update(
            name="Retained QA · full native 8x8 sensor",
            revision="retained-1",
            parents=[models["sensor"]["id"]],
            source_kind="estimated",
            source_uri="fixture://retained-charge/native-sensor",
            physical_instance=None,
            method_and_version="authored-retained-charge-fixture:1",
            uncertainty={"kind": "unknown"},
            parameter_uncertainty={},
        )
        pitch = sensor["payload"]["pixel_pitch_um"]
        sensor["properties"].update(
            active_height={"value": SIDE * pitch / 1000, "unit": "mm"},
            active_width={"value": SIDE * pitch / 1000, "unit": "mm"},
        )
        sensor["payload"]["readout"].update(
            full_well_e=10000, dark_current_e_per_s=0, gain=1, read_noise_e=2
        )
        sensor = self.create("sensor", "models", sensor)
        lens = models["lens"]
        if not lens["payload"].get("psf"):
            factory = self.template("factory", plugin_id="prysm_psf_v1")
            factory["name"] = "Retained QA · bounded independently declared pupil"
            factory["request"]["wavelength_nm"] = sensor["payload"]["wavelength_nm"]
            factory["request"]["pixel_pitch_um"] = pitch
            job = self.wait(
                "psf-factory",
                "factory/jobs",
                self.post("psf-submit", "factory/jobs", payload=factory),
            )
            self.check("bounded PSF proposed", job["result"]["summary"]["status"] == "PROPOSED")
            lens = self.create(
                "lens",
                "factory/models",
                {
                    "job_id": job["id"],
                    "base_lens_model_id": lens["id"],
                    "name": "Retained QA · bounded pupil PSF",
                    "revision": "retained-1",
                    "conditions": original["conditions"],
                    "condition_scope": "optical_response",
                },
                read_route="models",
            )
        camera = fields(original, AssemblyCreate)
        camera.update(
            name="Retained QA · frozen small native camera",
            revision="retained-1",
            model_ids=[lens["id"], sensor["id"], models["isp"]["id"]],
        )
        camera["conditions"].update(
            exposure_s={"value": EXPOSURE_NS * 1e-9, "unit": "s"}, gain={"value": 1.0, "unit": "1"}
        )
        camera = self.create("camera", "assemblies", camera)
        metadata = self.template("spectral-image", assembly_id=camera["id"])
        self.check(
            "full native fixture geometry",
            metadata["sampling"]["full_sensor_shape"] == [SIDE, SIDE]
            and metadata["sampling"]["crop_origin"] == [0, 0],
        )
        images = {}
        for label, level in (("bright", 0.0001), ("dark", 0.0)):
            meta = deepcopy(metadata)
            meta.update(
                name="Retained QA · " + label,
                revision="retained-1",
                source_kind="estimated",
                source_uri="fixture://retained-charge/" + label,
                license="MIT; authored synthetic ideal-image values",
                method_and_version="authored-bright-dark-step:1",
                physical_instance=None,
            )
            values = np.full((SIDE, SIDE, len(meta["wavelength_nm"])), level, dtype="<f8")
            images[label] = self.import_image(label + "-image", meta, values)
        scene = self.template("temporal-scene", assembly_id=camera["id"])
        scene.update(
            name="Retained QA · bright pulse then exact zero irradiance", revision="retained-1"
        )
        scene["payload"].update(
            interpolation="zero_order_hold",
            knots=[
                {"time_ns": 0, "spectral_image_id": images["bright"]["id"]},
                {"time_ns": EXPOSURE_NS, "spectral_image_id": images["dark"]["id"]},
                {"time_ns": 100_000_000, "spectral_image_id": images["dark"]["id"]},
            ],
        )
        scene = self.create("scene", "scene_sequences", scene)
        controls = {}
        for label, interval in (("short", 8_000_000), ("long", 20_000_000)):
            control = self.template(
                "control-sequence", assembly_id=camera["id"], scene_sequence_id=scene["id"]
            )
            sample = deepcopy(control["payload"]["frames"][0])
            control.update(name="Retained QA · " + label + " event gaps", revision="retained-1")
            control["payload"]["mode"].update(shutter="global", line_time_ns=0, hidden_top_rows=0)
            control["payload"]["frames"] = []
            for index in range(3):
                frame = deepcopy(sample)
                frame.update(frame_id=f"frame-{index:04d}", frame_start_ns=index * interval)
                frame["applied"].update(
                    applied_at_ns=index * interval,
                    exposure_ns=EXPOSURE_NS,
                    gain=1.0,
                    command_id=None,
                )
                control["payload"]["frames"].append(frame)
            controls[label] = self.create(label + "-control", "control_sequences", control)
        variants = [
            ("retain", "linear", 0.5, False, 0.0, "short", False),
            ("clear", "linear", 0.5, True, 0.0, "short", False),
            ("complete", "linear", 1.0, False, 0.0, "short", False),
            ("gap-dark-short", "linear", 0.5, False, 1000.0, "short", False),
            ("gap-dark-long", "linear", 0.5, False, 1000.0, "long", False),
            ("nonlinear", "nonlinear", 0.5, False, 0.0, "short", False),
            ("dcg", "dcg", 0.5, False, 0.0, "short", False),
            ("dcg-noisy", "dcg", 0.5, False, 0.0, "short", True),
        ]
        self.report["cases"] = {}
        self.report["selection"] = {
            "assembly_id": original["id"],
            "camera_id": camera["id"],
            "isp_model_id": models["isp"]["id"],
            "test_id": None,
        }
        for label, family, eta, clear, dark, control_key, noise in variants:
            control = controls[control_key]
            program = self.template(
                "capture-program",
                assembly_id=camera["id"],
                mode="retained_charge",
                control_sequence_id=control["id"],
            )
            program.update(name="Retained QA · " + label, revision="retained-1")
            payload = program["payload"]
            if family != "linear":
                source = self.template(
                    "capture-program",
                    assembly_id=camera["id"],
                    mode="shared_charge_nonlinear"
                    if family == "dcg"
                    else "independent_bracket_nonlinear",
                )
                payload["electrical_model"] = authored_early_knee(
                    source["payload"]["electrical_model"]
                )
            payload["electrical_model"]["dark_current_e_per_s"] = dark
            payload["retained_charge_model"]["transfer_efficiency"] = [eta] * 4
            payload["pd_clear_before_exposure"][1]["clear"] = clear
            program = self.create(label + "-program", "capture_programs", program)
            arguments = {
                "assembly_id": camera["id"],
                "scene_sequence_id": scene["id"],
                "control_sequence_id": control["id"],
                "capture_program_id": program["id"],
            }
            test = self.template("temporal-test", **arguments)
            test["name"] = "Retained QA · " + label + " delivered RAW"
            test = self.create(label + "-test", "tests", test)
            request = {
                **arguments,
                "test_id": test["id"],
                "plugin_id": "retained_charge_v1",
                "parameters": {"seed": SEED, "noise_enabled": noise},
            }
            run = self.wait(
                label + "-run", "runs", self.post(label + "-submit", "runs", payload=request)
            )
            self.report["cases"][label] = {
                "run_id": run["id"],
                "program_id": program["id"],
                "test_id": test["id"],
                "control_id": control["id"],
                "family": family,
                "noise_enabled": noise,
            }
            self.save()
            self.verify_run(label, run)
            self.bundle(label, "runs/" + run["id"])
            if label == "retain":
                repeat_request = {**request, "force": True}
                repeat = self.wait(
                    "repeat-run", "runs", self.post("repeat-submit", "runs", payload=repeat_request)
                )
                self.report["cases"]["repeat"] = {
                    **self.report["cases"][label],
                    "run_id": repeat["id"],
                }
                self.check("forced fresh job identity", repeat["id"] != run["id"])
                self.verify_run("repeat", repeat)
                for name in ("raw_groups", "retained_delivered_e", "retained_residual_e"):
                    self.check(
                        "fresh execution exact " + name,
                        np.array_equal(self.array(run, name), self.array(repeat, name)),
                    )
                self.check(
                    "fresh execution same final state identity",
                    run["result"]["retained_charge"]["final_state_sha256"]
                    == repeat["result"]["retained_charge"]["final_state_sha256"],
                )
                self.bundle("repeat", "runs/" + repeat["id"])
        self.compare_cases()
        self.source_guards()
        self.report["final_db_snapshot"] = transport.db_snapshot(self.state)
        self.report["fixture"] = {
            "full_native_shape": [SIDE, SIDE],
            "frames": 3,
            "bright_irradiance_w_m2_nm": 0.0001,
            "dark_irradiance": 0,
            "exposure_ns": EXPOSURE_NS,
            "seed": SEED,
            "source_plane": "supplied ideal image irradiance includes on-axis throughput",
            "clear_policy": "explicit frame-linked PD clear",
            "physical_measurements": 0,
        }
        self.save()

    def verify_run(self, label, run):
        summary = run["result"]
        program = run["capture_program_snapshot"]["payload"]
        control = run["control_sequence_snapshot"]["payload"]
        self.check(
            "run source binding " + label,
            run["environment"] == self.binding["environment"]
            and summary["capture_program"] == program
            and summary["control_sequence"] == control,
        )
        self.check(
            "history-aware result scope " + label,
            summary["physical_validation"] == "not_established"
            and summary["uncertainty"]["kind"] == "unknown"
            and not summary["current_exposure_charge_estimation"]
            and not summary["hdr_merge_applied"]
            and not summary["preview_contract"]["charge_inversion"],
        )
        schedule = run["request"]["retained_charge_events"]
        self.check(
            "frozen row event schedule " + label,
            schedule == summary["retained_charge"]["events"]
            and digest({k: v for k, v in schedule.items() if k != "schedule_sha256"})
            == schedule["schedule_sha256"],
        )
        photo, prnu, dark_rate = [
            self.array(run, name)
            for name in ("mean_electrons", "retained_prnu", "retained_dark_rate_e_s")
        ]
        names = (
            "initial_residual_e",
            "expected_gap_e",
            "gap_generated_e",
            "gap_overflow_e",
            "clear_loss_e",
            "expected_exposure_e",
            "exposure_generated_e",
            "exposure_overflow_e",
            "pretransfer_e",
            "delivered_e",
            "residual_e",
        )
        values = {name: self.array(run, "retained_" + name) for name in names}
        raw = self.array(run, "raw_groups")
        capacity = program["retained_charge_model"]["capacity_e"]
        eta = program["retained_charge_model"]["transfer_efficiency"]
        noise = summary["noise_enabled"]
        self.check(
            "three full-native acquisitions " + label,
            photo.shape == (3, SIDE, SIDE)
            and raw.shape[0] == 3
            and raw.shape[-2:] == (SIDE, SIDE)
            and summary["geometry"]["crop_origin"] == [0, 0]
            and summary["geometry"]["full_sensor_shape"] == [SIDE, SIDE],
        )
        self.check(
            "actual photo pulse then dark " + label,
            float(photo[0].max()) > 1 and np.array_equal(photo[1:], np.zeros_like(photo[1:])),
        )
        residual = [[0.0 for _ in range(SIDE)] for _ in range(SIDE)]
        expected = {name: np.zeros_like(photo, dtype=float) for name in names}
        expected_raw = np.zeros_like(raw)
        model = program["electrical_model"]
        self.check(
            "authored fixed pattern matches saved maps " + label,
            model["fixed_pattern"]["prnu_sigma"] == 0
            and model["fixed_pattern"]["dsnu_e_per_s"] == 0
            and np.array_equal(prnu, np.ones_like(prnu))
            and np.array_equal(dark_rate, np.full_like(dark_rate, model["dark_current_e_per_s"])),
        )
        rates = self.array(run, "channel_photoelectron_rate_scenes")
        weights = self.array(run, "row_scene_integration_weights_s")
        cfa = self.array(run, "cfa_channel_index")
        optical_integral = np.empty_like(photo)
        for f, row, col in np.ndindex(photo.shape):
            optical_integral[f, row, col] = sum(
                float(weights[f, s, row]) * float(rates[s, row, col, int(cfa[row, col])])
                for s in range(rates.shape[0])
            )
        self.check(
            "independent optical-rate time integral " + label,
            np.allclose(photo, optical_integral, rtol=2e-14, atol=1e-9),
        )
        paths = model["paths"] if "paths" in model else [model]
        for frame_index, (frame, event) in enumerate(
            zip(control["frames"], schedule["frames"], strict=True)
        ):
            exposure = frame["applied"]["exposure_ns"] * 1e-9
            previous_event = schedule["frames"][frame_index - 1] if frame_index else None
            self.check(
                "scalar event time " + label + " " + str(frame_index),
                all(
                    event["row_transfer_ns"][row] - event["row_open_ns"][row]
                    == frame["applied"]["exposure_ns"]
                    and event["row_gap_ns"][row]
                    == (
                        event["row_open_ns"][row] - previous_event["row_transfer_ns"][row]
                        if previous_event
                        else 0
                    )
                    for row in range(SIDE)
                ),
            )
            for row in range(SIDE):
                for col in range(SIDE):
                    i = (frame_index, row, col)
                    old = residual[row][col]
                    gap_mean = float(dark_rate[row, col]) * event["row_gap_ns"][row] * 1e-9
                    exposure_mean = (
                        float(photo[i]) * float(prnu[row, col])
                        + float(dark_rate[row, col]) * exposure
                    )
                    gap_generated = float(values["gap_generated_e"][i]) if noise else gap_mean
                    generated = float(values["exposure_generated_e"][i]) if noise else exposure_mean
                    after_gap = min(capacity, old + gap_generated)
                    gap_overflow = max(0.0, old + gap_generated - capacity)
                    clear = after_gap if event["clear_before_exposure"] else 0.0
                    before_cap = after_gap - clear + generated
                    pretransfer = min(capacity, before_cap)
                    overflow = max(0.0, before_cap - capacity)
                    delivered = (
                        float(values["delivered_e"][i])
                        if noise
                        else pretransfer * eta[(row % 2) * 2 + col % 2]
                    )
                    residual[row][col] = pretransfer - delivered
                    row_values = (
                        old,
                        gap_mean,
                        gap_generated,
                        gap_overflow,
                        clear,
                        exposure_mean,
                        generated,
                        overflow,
                        pretransfer,
                        delivered,
                        residual[row][col],
                    )
                    for name, value in zip(names, row_values, strict=True):
                        expected[name][i] = value
                    if not noise:
                        for path_index, path in enumerate(paths):
                            gain = (
                                path["analog_gain"]
                                if "paths" in model
                                else frame["applied"]["gain"]
                            )
                            expected_raw[frame_index, path_index, row, col] = scalar_raw(
                                delivered, path, gain
                            )
        for name in names:
            self.check(
                "independent scalar ledger " + label + " " + name,
                np.allclose(values[name], expected[name], rtol=2e-14, atol=1e-9),
            )
        incoming = (
            values["initial_residual_e"]
            + values["gap_generated_e"]
            + values["exposure_generated_e"]
        )
        outgoing = sum(
            values[key]
            for key in (
                "gap_overflow_e",
                "clear_loss_e",
                "exposure_overflow_e",
                "delivered_e",
                "residual_e",
            )
        )
        self.check(
            "per-pixel charge conservation " + label,
            np.array_equal(incoming, outgoing)
            if noise
            else np.allclose(incoming, outgoing, rtol=2e-14, atol=1e-9),
        )
        self.check(
            "capacity and split bounds " + label,
            bool(
                np.all(values["delivered_e"] >= 0)
                and np.all(values["residual_e"] >= 0)
                and np.all(values["pretransfer_e"] <= capacity)
                and np.allclose(
                    values["delivered_e"] + values["residual_e"], values["pretransfer_e"]
                )
            ),
        )
        if noise:
            self.check(
                "single sampled integer population " + label,
                all(
                    np.array_equal(values[key], np.floor(values[key]))
                    for key in (
                        "gap_generated_e",
                        "exposure_generated_e",
                        "pretransfer_e",
                        "delivered_e",
                        "residual_e",
                    )
                ),
            )
        else:
            self.check("independent scalar actual RAW " + label, np.array_equal(raw, expected_raw))
        self.check(
            "one ledger population for all readout paths " + label,
            values["delivered_e"].ndim == 3
            and raw.shape[1] == len(paths)
            and len(summary["readout_descriptors"]) == 3,
        )
        ledger = summary["retained_charge"]["frame_ledger"]
        predecessor = None
        previous_hash = None
        for index in range(4):
            state_charge = np.zeros((SIDE, SIDE)) if index == 0 else values["residual_e"][index - 1]
            header = {
                "schema": "camera_retained_photodiode_state_v1",
                "model_sha256": digest(program["retained_charge_model"]),
                "noise_enabled": noise,
                "shape": [SIDE, SIDE],
                "step_index": index,
                "predecessor_state_sha256": predecessor,
            }
            state_hash = transport.sha(
                json.dumps(header, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                + b"\n"
                + np.asarray(state_charge, dtype="<i8" if noise else "<f8").tobytes(order="C")
            )
            expected_hash = (
                ledger[0]["input_state_sha256"]
                if index == 0
                else ledger[index - 1]["output_state_sha256"]
            )
            self.check(
                "canonical actual state identity " + label + " " + str(index),
                state_hash == expected_hash,
            )
            previous_hash = state_hash
            predecessor = previous_hash
        self.check(
            "state predecessor chain " + label,
            all(
                ledger[i]["input_state_sha256"] == ledger[i - 1]["output_state_sha256"]
                for i in range(1, len(ledger))
            )
            and ledger[-1]["output_state_sha256"]
            == summary["retained_charge"]["final_state_sha256"],
        )
        for artifact in transport.artifacts(run):
            if "url" in artifact:
                self.artifact(artifact)

    def case_run(self, label):
        return self.client.get("runs/" + self.saved["cases"][label]["run_id"])

    def compare_cases(self):
        runs = {label: self.case_run(label) for label in self.saved["cases"]}
        delivered = {label: self.array(run, "retained_delivered_e") for label, run in runs.items()}
        raw = {label: self.array(run, "raw_groups") for label, run in runs.items()}
        self.check(
            "bright-to-dark delivered lag decreases",
            bool(
                np.all(delivered["retain"][1] > 0)
                and np.all(delivered["retain"][2] > 0)
                and np.allclose(delivered["retain"][1], delivered["retain"][0] * 0.5)
                and np.allclose(delivered["retain"][2], delivered["retain"][1] * 0.5)
            ),
        )
        self.check(
            "actual dark-frame RAW lag survives encoding",
            bool(np.any(raw["retain"][1:] != raw["complete"][1:])),
        )
        self.check(
            "explicit clear removes dark-frame history",
            bool(
                np.all(delivered["clear"][1:] == 0)
                and np.array_equal(raw["clear"][1:], raw["complete"][1:])
            ),
        )
        self.check(
            "complete transfer leaves no residual",
            bool(np.all(self.array(runs["complete"], "retained_residual_e") == 0)),
        )
        self.check(
            "longer gaps add continuous dark charge without release",
            bool(
                np.all(delivered["gap-dark-long"][1:] > delivered["gap-dark-short"][1:])
                and np.any(raw["gap-dark-long"][1:] != raw["gap-dark-short"][1:])
            ),
        )
        self.check(
            "readout family does not change retained population",
            np.array_equal(delivered["retain"], delivered["nonlinear"])
            and np.array_equal(delivered["retain"], delivered["dcg"]),
        )
        self.check(
            "nonlinear response changes actual RAW at identical delivered population",
            bool(np.any(raw["retain"] != raw["nonlinear"])),
        )
        self.report["comparison"] = {
            label: {
                "noise_enabled": run["result"]["noise_enabled"],
                "delivered_mean_e_by_frame": delivered[label].mean(axis=(1, 2)).tolist(),
                "raw_mean_dn_by_frame_and_path": raw[label].mean(axis=(2, 3)).tolist(),
                "final_state_sha256": run["result"]["retained_charge"]["final_state_sha256"],
            }
            for label, run in runs.items()
        }
        self.save()

    def source_guards(self):
        before = transport.db_snapshot(self.state)
        choices = self.client.get("replay-sources")
        encoded_choices = canonical(choices)
        self.check(
            "history sources excluded from legacy replay discovery",
            all(row["run_id"] not in encoded_choices for row in self.saved["cases"].values()),
        )
        if not self.args.verify_only:
            self.readonly_post(
                "legacy-replay-refused",
                "replays",
                {
                    "name": "Retained QA · current-exposure replay must refuse",
                    "source_kind": "run",
                    "source_id": self.saved["cases"]["retain"]["run_id"],
                    "isp_model_id": self.saved["selection"]["isp_model_id"],
                    "force": False,
                },
                expected=422,
                detail="legacy current-exposure ISP replay is unsupported",
            )
        for label, selector in (
            (
                "nonlinear",
                {"schema": "camera_nonlinear_raw_selection_v1", "mode": "sequential_frames"},
            ),
            (
                "dcg",
                {
                    "schema": "camera_nonlinear_raw_selection_v1",
                    "mode": "shared_charge_path",
                    "readout_id": "hcg",
                },
            ),
        ):
            route = "templates/isp-profile?" + urlencode(
                {
                    "version": 3,
                    "source_kind": "run",
                    "source_id": self.saved["cases"][label]["run_id"],
                    "selector": canonical(selector),
                }
            )
            response = self.client.http.get("/api/v1/" + route)
            self.check(
                "history-aware ISP source refusal " + label,
                response.status_code == 422
                and "current-exposure photocharge decoder is unsupported" in response.text,
            )
            if not self.args.verify_only:
                self.report.setdefault("source_guard_responses", {})[label] = {
                    **self.file("guards/" + label + ".json", response.content),
                    "route": route,
                    "http_status": response.status_code,
                }
                self.save()
            else:
                entry = self.saved["source_guard_responses"][label]
                self.check(
                    "source guard exact readback " + label,
                    response.content == (self.out / entry["path"]).read_bytes(),
                )
        self.check(
            "source guard requests are read-only", transport.db_snapshot(self.state) == before
        )

    def readback(self):
        before = transport.db_snapshot(self.state)
        for collection in ("fixtures", "records", "artifacts", "bundles", "source_guard_responses"):
            for label, entry in self.saved[collection].items():
                content = (self.out / entry["path"]).read_bytes()
                self.check(
                    "retained file " + collection + " " + label,
                    transport.sha(content) == entry["sha256"] and len(content) == entry["bytes"],
                )
        for label, operation in self.saved["operations"].items():
            for key in ("request", "response"):
                entry = operation[key]
                content = (self.out / entry["path"]).read_bytes()
                self.check(
                    "retained POST " + label + " " + key,
                    transport.sha(content) == entry["sha256"] and len(content) == entry["bytes"],
                )
        for label, entry in self.saved["records"].items():
            value = self.preserve(label, entry["route"], self.client.get(entry["route"]))
            if entry["route"].startswith("runs/") and not entry["route"].endswith("/export"):
                self.verify_run(label, value)
        for artifact in self.saved["artifacts"].values():
            self.artifact(artifact)
        for label, entry in self.saved["bundles"].items():
            self.bundle(label, entry["route"])
        self.compare_cases()
        self.source_guards()
        self.check(
            "readback logical rows and blob inventory unchanged",
            transport.db_snapshot(self.state) == before,
        )
        self.check(
            "initial journal remains byte-exact",
            self.initial_path.read_bytes() == self.initial_bytes,
        )

    def run(self):
        try:
            with PlatformClient(self.binding["base_url"], timeout=240) as client:
                self.client = client
                health = client.get("health")
                self.check(
                    "isolated healthy state and Worker",
                    health["status"] == "ok"
                    and health["worker"] == "running"
                    and Path(health["state_dir"]).resolve() == self.state,
                )
                self.check("exact source version", client.get("session")["version"] == __version__)
                if self.args.api_pid:
                    self.report["process_witness"] = transport.process_witness(
                        urlsplit(self.binding["base_url"]).port, self.args.api_pid, self.state
                    )
                    self.save()
                self.readback() if self.args.verify_only else self.initial()
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
        except Exception as error:
            self.report.update(status="failed", error=f"{type(error).__name__}: {error}")
            self.save()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=("verify", "readback"), default="verify")
    parser.add_argument("--readback-output", type=Path)
    parser.add_argument("--assembly-id")
    parser.add_argument("--api-pid", type=int)
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or url.port in (None, 8770)
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or url.username
        or url.password
    ):
        parser.error("Use a separate explicit loopback QA port, never primary8770")
    state, out = args.state_dir.resolve(), args.output_dir.resolve()
    if (
        "qa" not in state.name.lower()
        or not (state / "registry.sqlite").is_file()
        or state == out
        or state in out.parents
        or out in state.parents
    ):
        parser.error("Require an existing isolated QA state and separate evidence directory")
    if (args.mode == "readback") != bool(args.readback_output):
        parser.error("Readback requires --readback-output; verify must omit it")
    if args.readback_output and (
        args.readback_output.resolve().is_relative_to(out)
        or args.readback_output.resolve().is_relative_to(state)
    ):
        parser.error("Readback output must be outside original evidence and QA state")
    Proof(args).run()


if __name__ == "__main__":
    main()
