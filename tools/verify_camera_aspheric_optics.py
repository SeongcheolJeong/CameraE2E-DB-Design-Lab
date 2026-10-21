#!/usr/bin/env python3
"""Qualify matched even-asphere optics through an owned live API/Worker.

Reuses durable request/response journaling and separate readback from the earlier
imported-charge proof. No process lifecycle or primary-state writes are owned.
The two optics share vertex radii, native sensor, ideal irradiance and exposure;
only conic/polynomial departure changes. This is authored numerical evidence.
"""

from __future__ import annotations

import argparse
import io
import json
import zipfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import numpy as np
import verify_camera_measured_charge as transport

from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.evidence_package import read_bundle
from camera_platform.registry import digest

REFUSED_RUN_LABEL = "spherical-limit-run-submit"
NATIVE_BUDGET_MESSAGE = (
    "Native direction/EQE integration exceeds 128M weighted samples; "
    "use a smaller native crop or an independently qualified response grid"
)


def native_crop_budget(directional):
    """Mirror the public runtime's conservative count; retain its exact ray grid."""
    active = np.asarray(directional["conditional_weights"]).any(axis=0)
    expanded = active.sum(axis=1).astype(int)
    expanded[0] *= len(directional["on_axis_rotation_phi_deg"])
    per_pixel = int(expanded.sum()) * len(directional["wavelength_nm"])
    candidates = {side: per_pixel * side * side for side in (32, 16, 8)}
    side = next((side for side, work in candidates.items() if work <= 128_000_000), None)
    if side is None:
        raise ValueError("Even an 8x8 native proof crop exceeds the unchanged sample budget")
    return side, {
        "per_pixel": per_pixel,
        "candidate_work": candidates,
        "selected_shape": [side, side],
        "maximum_weighted_samples": 128_000_000,
    }


def fields(record, contract):
    return {k: deepcopy(v) for k, v in record.items() if k in contract.model_fields}


def native_response():
    values = np.empty((2, 2, 4, 4))
    for w in range(2):
        for t in range(2):
            for p in range(4):
                values[w, t, p] = np.array([0.2, 0.35, 0.55, 0.7]) * (
                    1 - 0.2 * t + 0.05 * t * np.cos(np.pi * p / 2)
                )
    return {
        "schema": "camera_native_pixel_response_v1",
        "site_names": ["R", "Gr", "Gb", "B"],
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": 3.0,
        "reference_plane": "sensor_surface_before_pixel_optics",
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "polarization": "unpolarized",
        "response_basis": "independent_site_collected_electrons_per_incident_pixel_photon",
        "wavelength_nm": [500, 600],
        "theta_deg": [0, 20],
        "phi_deg": [0, 90, 180, 270],
        "interpolation": "linear_wavelength_theta_periodic_phi",
        "values_axis_order": ["wavelength", "theta", "phi", "site"],
        "values": values.tolist(),
        "source": {"kind": "estimated", "description": "Authored directional four-site fixture"},
    }


class Proof(transport.Proof):
    def __init__(self, args):
        # The shared resume journal binds assembly and optional test selection.
        # This proof authors its own Tests; it has no preselected input Test.
        args.test_id = None
        self.recover_native_budget_refusal = False
        super().__init__(args)
        self.report.update(
            schema="camera_aspheric_optics_http_readback_v1"
            if args.verify_only
            else "camera_aspheric_optics_http_verification_v1",
            tool_sha256=transport.sha(Path(__file__).read_bytes()),
            helper_tool_sha256=transport.sha(Path(transport.__file__).read_bytes()),
            selection={"assembly_id": args.assembly_id, "test_id": None},
            numerical_scope="Actual matched optical Factories and native RAW/ISP path; "
            "fixed supplied on-axis image irradiance already includes on-axis throughput. "
            "No independent physical-camera accuracy or scene-radiance projection claim.",
        )
        if not args.verify_only:
            for key, path in (
                ("tool_source", Path(__file__)),
                ("helper_tool_source", Path(transport.__file__)),
            ):
                content = path.read_bytes()
                self.report[key] = self.fixture(key + "-" + transport.sha(content) + ".py", content)
        self.save()

    def load_resume(self, source):
        """Reuse acknowledged writes and only the exact known no-job budget refusal.

        The failed journal is copied byte-for-byte. Its refused request is never
        resent: an appended native image, Test and separately named run replace
        that proof fixture without changing any accepted record.
        """
        initial = (source / "http-verification.json").read_bytes()
        prior = json.loads(initial)
        if prior["status"] != "failed" or any(
            prior[key] != value for key, value in self.binding.items()
        ):
            raise ValueError("Resume requires the same frozen source and failed QA journal")
        if prior.get("selection") != {"assembly_id": self.args.assembly_id, "test_id": None}:
            raise ValueError("Resume cannot change selected inputs")
        pins = [
            entry
            for key in ("fixtures", "records", "artifacts", "bundles")
            for entry in prior[key].values()
        ]
        known_refusals = []
        for label, operation in prior["operations"].items():
            code = operation.get("http_status")
            known = label == REFUSED_RUN_LABEL and code == 422 and operation["endpoint"] == "runs"
            if operation["status"] != "response_received" or not (code in (200, 201, 202) or known):
                raise ValueError("Resume refuses uncertain or unexpected POST: " + label)
            pins.extend((operation["request"], operation["response"]))
            if known:
                known_refusals.append(label)
        for entry in pins:
            path = source / entry["path"]
            if not path.resolve().is_relative_to(source) or path.is_symlink():
                raise ValueError("Resume evidence path escapes its source directory")
            content = path.read_bytes()
            if transport.sha(content) != entry["sha256"] or len(content) != entry["bytes"]:
                raise ValueError("Resume evidence has changed: " + entry["path"])
            self.resume_files[entry["path"]] = entry["sha256"]
        for label, operation in prior["operations"].items():
            request = json.loads((source / operation["request"]["path"]).read_bytes())
            response = json.loads((source / operation["response"]["path"]).read_bytes())
            if label in known_refusals:
                expected = {
                    "endpoint": "runs",
                    "files": None,
                    "payload": {
                        "assembly_id": prior["records"]["spherical-limit-camera"]["id"],
                        "test_id": prior["records"]["spherical-limit-test"]["id"],
                        "scene_id": prior["records"]["shared-image"]["id"],
                        "plugin_id": "spatial_scene_v1",
                        "parameters": {"frames": 2, "seed": 419},
                    },
                }
                refusal = {
                    "error": "Execution refused: compatibility or runtime contract "
                    "is not satisfied",
                    "preflight": {
                        "status": "FAIL",
                        "issues": [
                            {
                                "code": "INVALID_INPUT",
                                "field": "request",
                                "message": NATIVE_BUDGET_MESSAGE,
                                "severity": "error",
                            }
                        ],
                    },
                }
                if request != expected or response != refusal or operation.get("id") is not None:
                    raise ValueError("Resume only reconciles the exact known no-job budget refusal")
            elif (
                request["endpoint"] != operation["endpoint"]
                or not response.get("id")
                or operation.get("id") not in (None, response["id"])
            ):
                raise ValueError("Resume requires a consistent acknowledged identity: " + label)
            for entry in (request.get("files") or {}).values():
                if self.resume_files.get(entry["path"]) != entry["sha256"]:
                    raise ValueError("Resume upload lacks a verified fixture")
        self.resume_operations = {
            label: deepcopy(operation)
            for label, operation in prior["operations"].items()
            if label not in known_refusals
        }
        self.report = deepcopy(prior)
        self.saved = self.report
        for key in ("error", "phase", "completed_at", "check_count"):
            self.report.pop(key, None)
        self.report.update(
            status="in_progress",
            resume={
                "source_directory": str(source),
                "source_journal_sha256": transport.sha(initial),
                "source_tool_sha256": prior["tool_sha256"],
                "reused_operations": [],
                "reconciled_no_job_refusals": known_refusals,
                "copied_file_sha256": dict(self.resume_files),
                "policy": "Exact acknowledged 2xx responses are reused without POST; only the "
                "exact recorded 128M sample refusal is retained without retry. Corrected native "
                "scene/Test/run use new labels and records; original journal and accepted "
                "records remain unchanged.",
            },
        )
        for relative in self.resume_files:
            transport.durable(self.out / relative, (source / relative).read_bytes(), exclusive=True)
        transport.durable(self.out / "resume-source-journal.json", initial, exclusive=True)
        ancestors = []
        previous = prior.get("resume")
        visited = {str(source)}
        while previous:
            ancestor = Path(previous["source_directory"]).resolve()
            if str(ancestor) in visited or len(ancestors) >= 8:
                raise ValueError("Cyclic or excessive resume ancestry")
            visited.add(str(ancestor))
            content = (ancestor / "http-verification.json").read_bytes()
            if transport.sha(content) != previous["source_journal_sha256"]:
                raise ValueError("An earlier failed journal changed")
            ancestors.append(
                {
                    "source_directory": str(ancestor),
                    "source_journal_sha256": previous["source_journal_sha256"],
                    "copied_file_sha256": previous["copied_file_sha256"],
                }
            )
            self.fixture("resume-journal-" + transport.sha(content) + ".json", content)
            previous = json.loads(content).get("resume")
        self.report["resume"]["ancestors"] = ancestors
        self.recover_native_budget_refusal = bool(known_refusals)

    def verify_resume_source(self):
        super().verify_resume_source()
        resume = self.report.get("resume", self.saved.get("resume", {}))
        for ancestor in resume.get("ancestors", []):
            directory = Path(ancestor["source_directory"])
            self.check(
                "earlier failed journal remains byte-exact " + str(directory),
                transport.sha((directory / "http-verification.json").read_bytes())
                == ancestor["source_journal_sha256"],
            )
            for relative, checksum in ancestor["copied_file_sha256"].items():
                path = directory / relative
                if not path.resolve().is_relative_to(directory) or path.is_symlink():
                    raise ValueError("Ancestral evidence path escapes its source directory")
                self.check(
                    "earlier failed evidence " + str(directory) + " " + relative,
                    transport.sha(path.read_bytes()) == checksum,
                )

    def get_template(self, name, **query):
        return self.client.get("templates/" + name + "?" + urlencode(query))

    def remember(self, label, route, record):
        self.preserve(label, route + "/" + record["id"], record)
        for artifact in transport.artifacts(record):
            self.artifact(artifact)
        return record

    def create(self, label, route, payload):
        return self.remember(label, route, self.post(label, route, payload=payload))

    def wait(self, label, route, queued):
        self.progress("wait " + label)
        wait = (
            self.client.wait_characterization
            if route == "factory/jobs"
            else (self.client.wait_replay if route == "replays" else self.client.wait)
        )
        record = wait(queued["id"], timeout=240)
        self.check("succeeded " + label, record["status"] == "succeeded")
        self.check(
            "frozen environment " + label, record["environment"] == self.binding["environment"]
        )
        return self.remember(label, route, record)

    def initial(self):
        original = self.client.get("assemblies/" + self.args.assembly_id)
        self.preserve("input-assembly", "assemblies/" + original["id"], original)
        models = {
            m["kind"]: m
            for m in [self.client.get("models/" + identity) for identity in original["model_ids"]]
        }
        sensor = fields(models["sensor"], ModelCreate)
        sensor.update(
            name="Asphere QA · fixed four-site sensor",
            revision="asphere-1",
            parents=[models["sensor"]["id"]],
            source_kind="estimated",
            source_uri="fixture://asphere-native-sensor",
            physical_instance=None,
            method_and_version="authored-asphere-sensor:1",
            spectral_range={"min": 500, "max": 600, "unit": "nm"},
            uncertainty={"kind": "unknown"},
            parameter_uncertainty={},
        )
        sensor["properties"].update(
            active_height={"value": 0.384, "unit": "mm"},
            active_width={"value": 0.384, "unit": "mm"},
            accepted_angle_range={"min": 0, "max": 20, "unit": "deg"},
        )
        sensor["payload"] = {
            "wavelength_nm": [500, 600],
            "pixel_pitch_um": 3.0,
            "readout": deepcopy(models["sensor"]["payload"]["readout"]),
            "native_pixel_response": native_response(),
        }
        sensor = self.create("sensor", "models", sensor)
        optical = self.get_template("factory", plugin_id="prescription_psf_v1", request_version=4)
        source_request = optical["request"]
        self.check(
            "nonzero authored asphere template",
            any(
                s.get("asphere", {}).get("coefficients", [0]) != [0, 0, 0]
                for s in source_request["prescription"]["surfaces"]
                if s["profile"] == "even_asphere"
            ),
        )
        cases = {}
        shared_scene = None
        selected_side = None
        for label in ("spherical-limit", "asphere"):
            request = deepcopy(source_request)
            if label == "spherical-limit":
                for surface in request["prescription"]["surfaces"]:
                    if surface["profile"] == "even_asphere":
                        surface["asphere"].update(conic_constant=0.0, coefficients=[0.0] * 3)
            ray = self.get_template("factory", plugin_id="rayoptics_lens_v1", request_version=4)
            ray.update(
                name="Asphere QA · " + label + " radiometry", request=request["prescription"]
            )
            ray_job = self.wait(
                label + "-rays",
                "factory/jobs",
                self.post(label + "-ray-submit", "factory/jobs", payload=ray),
            )
            self.check(
                "radiometry proposed " + label, ray_job["result"]["summary"]["status"] == "PROPOSED"
            )
            parent = self.create(
                label + "-parent",
                "factory/models",
                {
                    "job_id": ray_job["id"],
                    "name": "Asphere QA · " + label + " radiometry",
                    "revision": "1",
                    "conditions": original["conditions"],
                },
            )
            # Published model is read through its public model route.
            self.report["records"][label + "-parent"]["route"] = "models/" + parent["id"]
            self.save()
            psf = deepcopy(optical)
            psf.update(name="Asphere QA · " + label + " OPD/PSF", request=request)
            psf_job = self.wait(
                label + "-psf",
                "factory/jobs",
                self.post(label + "-psf-submit", "factory/jobs", payload=psf),
            )
            self.check(
                "PSF proposed " + label, psf_job["result"]["summary"]["status"] == "PROPOSED"
            )
            conditions = {
                **original["conditions"],
                "f_number": {
                    "value": psf_job["result"]["summary"]["geometric_reference_f_number"],
                    "unit": "1",
                },
            }
            lens = self.create(
                label + "-lens",
                "factory/models",
                {
                    "job_id": psf_job["id"],
                    "base_lens_model_id": parent["id"],
                    "name": "Asphere QA · " + label + " full optics",
                    "revision": "1",
                    "conditions": conditions,
                    "condition_scope": "optical_response",
                },
            )
            self.report["records"][label + "-lens"]["route"] = "models/" + lens["id"]
            self.save()
            directional = lens["payload"]["directional_response"]
            self.check(
                "explicit asphere directional schema " + label,
                directional["schema"] == "camera_lens_directional_response_v2",
            )
            suggested_side, budget = native_crop_budget(directional)
            selected_side = suggested_side if selected_side is None else selected_side
            self.check(
                "shared crop within native budget " + label,
                budget["per_pixel"] * selected_side**2 <= 128_000_000,
            )
            self.report.setdefault("native_crop_budget", {})[label] = {
                **budget,
                "actual_shape": [selected_side, selected_side],
                "actual_weighted_samples": budget["per_pixel"] * selected_side**2,
            }
            self.save()
            self.check(
                "exact matched prescription " + label,
                digest(psf_job["request"]["prescription"]) == digest(ray_job["request"]),
            )
            camera = fields(original, AssemblyCreate)
            camera.update(
                name="Asphere QA · " + label,
                revision="1",
                conditions=conditions,
                spectral_range=sensor["spectral_range"],
                model_ids=[lens["id"], sensor["id"], models["isp"]["id"]],
            )
            camera = self.create(label + "-camera", "assemblies", camera)
            self.check("compatible camera " + label, camera["compatibility"]["status"] == "PASS")
            if shared_scene is None:
                shared_scene = self.create(
                    "shared-image",
                    "spectral_images/fixture",
                    {
                        "assembly_id": camera["id"],
                        "name": "Asphere QA · same ideal spectral chart",
                        "shape": (
                            [64, 64]
                            if self.recover_native_budget_refusal
                            else [selected_side, selected_side]
                        ),
                        "irradiance_scale": 0.002,
                    },
                )
                self.report["records"]["shared-image"]["route"] = (
                    "spectral_images/" + shared_scene["id"]
                )
                self.save()
            test = self.get_template(
                "spatial-test", assembly_id=camera["id"], scene_id=shared_scene["id"]
            )
            test["name"] = "Asphere QA · " + label + " fixed acquisition"
            test = self.create(label + "-test", "tests", test)
            run_label = label + "-run-submit"
            if label == "spherical-limit" and self.recover_native_budget_refusal:
                # Consume the exact acknowledged 64x64 scene and Test prefix,
                # then append a bounded native fixture. Neither is overwritten.
                self.check(
                    "known refused run never resubmitted",
                    REFUSED_RUN_LABEL not in self.resume_operations,
                )
                corrected = "native" + str(selected_side)
                shared_scene = self.create(
                    "shared-image-" + corrected,
                    "spectral_images/fixture",
                    {
                        "assembly_id": camera["id"],
                        "name": "Asphere QA · same ideal spectral chart · " + corrected,
                        "shape": [selected_side, selected_side],
                        "irradiance_scale": 0.002,
                    },
                )
                self.report["records"]["shared-image-" + corrected]["route"] = (
                    "spectral_images/" + shared_scene["id"]
                )
                self.save()
                test = self.get_template(
                    "spatial-test", assembly_id=camera["id"], scene_id=shared_scene["id"]
                )
                test["name"] = "Asphere QA · " + label + " fixed acquisition · " + corrected
                test = self.create(label + "-test-" + corrected, "tests", test)
                run_label = label + "-run-" + corrected + "-submit"
            run = self.wait(
                label + "-run",
                "runs",
                self.post(
                    run_label,
                    "runs",
                    payload={
                        "assembly_id": camera["id"],
                        "test_id": test["id"],
                        "scene_id": shared_scene["id"],
                        "plugin_id": "spatial_scene_v1",
                        "parameters": {"frames": 2, "seed": 419},
                    },
                ),
            )
            self.check(
                "physical validation remains unknown " + label,
                run["result"]["physical_validation"] == "not_established",
            )
            self.check(
                "native integration uses exact sensor and optical directions " + label,
                run["result"]["native_pixel_response"]["sha256"]
                == digest(sensor["payload"]["native_pixel_response"])
                and run["result"]["native_pixel_response"]["directional_response_sha256"]
                == digest(directional),
            )
            self.bundle(label, "runs/" + run["id"])
            cases[label] = {"ray": ray_job, "psf": psf_job, "run": run, "camera": camera}
        a, b = cases["spherical-limit"], cases["asphere"]
        for key in ("conditions", "geometry", "readout", "seed_sequence"):
            self.check(
                "matched acquisition " + key, a["run"]["result"][key] == b["run"]["result"][key]
            )
        self.check(
            "exact same supplied ideal irradiance",
            np.array_equal(
                self.array(a["run"], "ideal_spectral_irradiance"),
                self.array(b["run"], "ideal_spectral_irradiance"),
            ),
        )
        comparisons = {}
        # The reciprocal image-side directions sample the same rear geometry;
        # changing a front surface need not change that chosen quadrature grid.
        # Physical changes are its transmitted weights/admission, traced object
        # exit directions, and the separate forward OPD/sensor intersections.
        image_directions = [self.array(case["run"], "lens_direction_xyz") for case in (a, b)]
        weights = [self.array(case["run"], "lens_direction_power_fraction") for case in (a, b)]
        admitted = [self.array(case["ray"], "accepted_rays") for case in (a, b)]
        common = admitted[0] & admitted[1]
        self.check("nonempty common reciprocal physical paths", bool(common.any()))
        exit_directions = [
            self.array(case["ray"], "object_exit_direction_cosines")[common] for case in (a, b)
        ]
        exit_change = np.abs(exit_directions[1] - exit_directions[0])
        self.check(
            "nonzero common-path object-exit direction change", bool((exit_change > 1e-9).any())
        )
        self.report["reciprocal_quadrature"] = {
            "image_direction_grid_unchanged": bool(np.array_equal(*image_directions)),
            "image_direction_grid_max_absolute_difference": float(
                np.abs(image_directions[1] - image_directions[0]).max()
            ),
            "conditional_weight_max_absolute_difference": float(
                np.abs(weights[1] - weights[0]).max()
            ),
            "admission_changes": int(np.count_nonzero(admitted[0] != admitted[1])),
            "common_admitted_rays": int(common.sum()),
            "common_object_exit_direction_max_absolute_difference": float(exit_change.max()),
            "interpretation": "Image-side directions are a fixed reciprocal quadrature for the "
            "unchanged rear geometry, not an assertion of unchanged lens transport. Changed "
            "weights/admission and common-path object-exit directions retain traced effects.",
        }
        self.save()
        for key, owner, array in (
            ("opd_nm", "psf", "fine_opd_nm"),
            ("psf", "psf", "psf_kernel"),
            ("forward_sensor_intersections_mm", "psf", "fine_ray_sensor_xy_mm"),
            ("incident_direction_power_fraction", "run", "lens_direction_power_fraction"),
            ("expected_electrons", "run", "channel_electrons"),
            ("raw_dn", "run", "raw_frames"),
            ("rgb", "run", "rgb_frames"),
        ):
            first, second = self.array(a[owner], array), self.array(b[owner], array)
            self.check("matched array shape " + key, first.shape == second.shape)
            change = np.abs(first.astype(float) - second.astype(float))
            comparisons[key] = {
                "max_absolute_difference": float(change.max()),
                "changed_values": int(np.count_nonzero(change)),
                "shape": list(first.shape),
            }
            self.check("nonzero physical-path change " + key, bool(np.any(change > 1e-9)))
        self.report["comparison"] = comparisons
        self.report["cases"] = {
            k: {n: v["id"] for n, v in case.items()} for k, case in cases.items()
        }
        self.save()
        isp = self.get_template("isp-profile", assembly_id=b["camera"]["id"])
        isp.update(name="Asphere QA · preserved RAW ISP", revision="1")
        isp["payload"]["white_balance"]["gains_rgb"] = [1.3, 1.0, 0.8]
        isp = self.create("replay-profile", "models", isp)
        replay = self.wait(
            "replay",
            "replays",
            self.post(
                "replay-submit",
                "replays",
                payload={
                    "name": "Asphere QA · same RAW replay",
                    "source_kind": "run",
                    "source_id": b["run"]["id"],
                    "isp_model_id": isp["id"],
                },
            ),
        )
        self.check(
            "replay preserves exact RAW",
            np.array_equal(self.array(replay, "raw_frames"), self.array(b["run"], "raw_frames")),
        )
        self.check(
            "explicit ISP changes RGB",
            not np.array_equal(
                self.array(replay, "rgb_frames"), self.array(b["run"], "rgb_frames")
            ),
        )
        self.bundle("replay", "replays/" + replay["id"])
        self.report["final_db_snapshot"] = transport.db_snapshot(self.state)
        self.save()

    def verify_replay(self, job):
        self.check("saved replay succeeded", job["status"] == "succeeded")

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
            members = {name: transport.sha(archive.read(name)) for name in names}
            header = json.loads(archive.read("manifest.json"))
            graph = header.get("schema") == "camera_evidence_bundle_v2"
            decoded = (
                read_bundle(io.BytesIO(content), expected_manifest_sha256=digest(manifest))
                if graph
                else header
            )
            self.check("exact reconstructed manifest " + label, decoded == manifest)
            required = {
                "artifacts/" + a["sha256"] + ("" if graph else "/" + a["name"])
                for a in transport.artifacts(manifest)
            }
            self.check("complete artifact closure " + label, required <= set(names))
            for name in required:
                self.check(
                    "ZIP artifact " + label + " " + name, members[name] == name.split("/")[1]
                )
        key = "run" if route.startswith("runs/") else "replay"
        self.check("bundle owns exact job " + label, manifest[key] == self.client.get(route))
        if key == "run":
            self.check(
                "both optical Factory parents retained " + label, len(manifest["factory_jobs"]) == 2
            )
        if self.args.verify_only or label in self.report.get("bundles", {}):
            saved = self.saved["bundles"][label]
            self.check("ZIP members unchanged " + label, members == saved["members"])
            self.check(
                "original ZIP retained " + label,
                transport.sha((self.out / saved["path"]).read_bytes()) == saved["sha256"],
            )
        else:
            self.report["bundles"][label] = {
                **self.file("bundles/" + label + ".zip", content),
                "route": route,
                "members": members,
                "manifest_sha256": digest(manifest),
            }
            self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--assembly-id", required=True)
    parser.add_argument("--api-pid", type=int, required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--readback-output", type=Path)
    parser.add_argument("--resume-from", type=Path)
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if address.hostname != "127.0.0.1" or address.port in (None, 8770):
        parser.error("Use an explicit isolated loopback port; primary8770 refused")
    if args.state_dir.resolve().name == "camera-platform":
        parser.error("Primary state refused")
    if bool(args.verify_only) != bool(args.readback_output):
        parser.error("Readback requires --verify-only and a new --readback-output")
    Proof(args).run()


if __name__ == "__main__":
    main()
