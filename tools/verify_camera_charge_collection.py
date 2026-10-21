#!/usr/bin/env python3
"""Qualify authored charge collection through isolated HTTP, Worker and RAW evidence.

Uses actual prescription Factory outputs and separate authored generation and
collection declarations. No measured sensor accuracy or physical pass is claimed.
POST intents are durable; uncertain mutations require explicit reconciliation.
The caller owns server lifecycle. --verify-only checks exact persisted evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import zipfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

from camera_platform import __version__
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.contracts import TestCreate as FixedTestCreate
from camera_platform.registry import canonical, digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def authored_sensor(base, *, routing):
    """New explicit synthetic generation and routing declarations, not inferred QE."""
    model = {k: deepcopy(v) for k, v in base.items() if k in ModelCreate.model_fields}
    sites, wave, pitch = ["R", "Gr", "Gb", "B"], [500, 600], 3.0
    common = {
        "site_names": sites,
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": pitch,
        "wavelength_nm": wave,
    }
    source = {"kind": "estimated", "description": "Authored synthetic acceptance; not measured"}
    response = {
        **common,
        "schema": "camera_native_generation_response_v1",
        "reference_plane": "sensor_surface_before_pixel_optics",
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "polarization": "unpolarized",
        "response_basis": "independent_site_generated_electrons_per_incident_pixel_photon",
        "theta_deg": [0, 20],
        "phi_deg": [0, 90, 180, 270],
        "interpolation": "linear_wavelength_theta_periodic_phi",
        "values_axis_order": ["wavelength", "theta", "phi", "site"],
        "values": np.broadcast_to([0.2, 0.35, 0.6, 0.8], (2, 2, 4, 4)).tolist(),
        "source": source,
    }
    collection = {
        **common,
        "schema": "camera_charge_collection_v1",
        "destination_offsets_rc": [[0, 0], [0, 1]],
        "values_axis_order": ["wavelength", "source_site", "destination_offset"],
        "probabilities": [[[0.75, 0.15] if routing else [1.0, 0.0] for _ in sites] for _ in wave],
        "interpolation": "linear_wavelength",
        "input_plane": "silicon_generated_charge",
        "output_plane": "collection_well_before_readout",
        "temporal_model": "instantaneous_memoryless",
        "boundary": "absorb_outside_full_sensor",
        "angular_assumption": "direction_independent_depth_marginal",
        "statistics": "single_destination_poisson_thinning",
        "regime": "linear_low_injection",
        "source": source,
    }
    label = "routing" if routing else "identity"
    model.update(
        name="전하 생성·수집 · " + label,
        revision="charge-collection-1",
        source_kind="estimated",
        source_uri="fixture://charge-collection/" + label,
        method_and_version="authored-generation-collection:1",
        physical_instance=None,
        parents=[base["id"]],
        parameter_uncertainty={},
        uncertainty={"kind": "unknown"},
        spectral_range={"min": 500, "max": 600, "unit": "nm"},
        included_effects=[
            "sensor_charge_generation",
            "sensor_charge_collection",
            "sensor_angular_response",
            "mean_dark_charge",
            "readout_clipping",
        ],
    )
    model["payload"] = {
        "wavelength_nm": wave,
        "pixel_pitch_um": pitch,
        "readout": deepcopy(base["payload"]["readout"]),
        "native_pixel_response": response,
        "charge_collection": collection,
    }
    model["properties"].update(
        active_height={"value": 0.048, "unit": "mm"},
        active_width={"value": 0.048, "unit": "mm"},
        accepted_angle_range={"min": 0, "max": 20, "unit": "deg"},
    )
    return model


def artifact_descriptors(value):
    if isinstance(value, dict):
        if {"sha256", "name", "bytes", "url"} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from artifact_descriptors(child)
    elif isinstance(value, list):
        for child in value:
            yield from artifact_descriptors(child)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--readback-only", action="store_true")
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.port in {None, 8770}:
        parser.error("Use a separate explicit loopback QA port")
    state, out = args.state_dir.resolve(), args.output_dir.resolve()
    if "qa" not in state.name or state == out or state in out.parents:
        parser.error("Use an explicit QA state and separate evidence directory")
    out.mkdir(parents=True, exist_ok=True)
    path = out / "http-verification.json"
    binding = {
        "base_url": args.base_url,
        "state_dir": str(state),
        "version": __version__,
        "environment": environment_fingerprint(),
    }
    report = (
        json.loads(path.read_text())
        if path.exists()
        else {
            "schema": "camera_charge_collection_http_v1",
            **binding,
            "operations": {},
            "records": {},
            "checks": [],
            "bundles": {},
            "physical_validation": "not_established",
            "total_uncertainty": "unknown",
            "status": "in_progress",
        }
    )
    if any(report[k] != v for k, v in binding.items()):
        raise ValueError("Resume requires identical state/source/environment")
    if (args.verify_only or args.readback_only) and not report["records"]:
        raise ValueError("Verification requires previously saved records")

    def save():
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        temp.replace(path)

    def check(label, okay):
        if not okay:
            report.update(status="failed", failed_check=label)
            save()
            raise AssertionError(label)
        if label not in report["checks"]:
            report["checks"].append(label)

    def preserve(label, record, route):
        target = out / (label + ".json")
        encoded = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if target.exists() and target.read_text() != encoded:
            raise ValueError("Preserved immutable record changed: " + label)
        descriptor = {"id": record["id"], "route": route, "record_sha256": digest(record)}
        if target.exists() and report["records"].get(label) == descriptor:
            return record
        target.write_text(encoded)
        report["records"][label] = descriptor
        save()
        return record

    with PlatformClient(args.base_url, timeout=120) as client:
        health = client.get("health")
        check(
            "isolated healthy state and running Worker",
            health["status"] == "ok"
            and health["worker"] == "running"
            and Path(health["state_dir"]).resolve() == state,
        )
        check("exact source version", client.get("session")["version"] == __version__)

        def send(label, endpoint, payload, operation=None):
            intent = {"endpoint": endpoint, "payload_sha256": digest(payload)}
            old = report["operations"].get(label)
            if old:
                if any(old[k] != v for k, v in intent.items()) or not (
                    "response" in old or "response_ref" in old
                ):
                    raise ValueError("Changed or uncertain POST requires reconciliation: " + label)
                if "response_ref" in old:
                    ref = old["response_ref"]
                    content = (out / ref["path"]).read_bytes()
                    if hashlib.sha256(content).hexdigest() != ref["record_sha256"] or (
                        "bytes" in ref and len(content) != ref["bytes"]
                    ):
                        raise ValueError("Saved POST response differs from ledger: " + label)
                    return json.loads(content)
                return old["response"]
            report["operations"][label] = intent
            save()
            response = operation() if operation else client.post(endpoint, payload)
            directory = out / "operation-responses"
            directory.mkdir(exist_ok=True)
            target = directory / (label + ".json")
            content = canonical(response).encode()
            if target.exists() and target.read_bytes() != content:
                raise ValueError("Saved POST response path already differs: " + label)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
            report["operations"][label]["response_ref"] = {
                "path": str(target.relative_to(out)),
                "record_sha256": digest(response),
                "bytes": len(content),
                "id": response.get("id"),
            }
            save()
            return response

        if not (args.verify_only or args.readback_only):
            boot = client.get("workbench")
            original = client.get("assemblies/" + boot["demo"]["assembly_ids"][0])
            models = [client.get("models/" + i) for i in original["model_ids"]]
            base = next(m for m in models if m["kind"] == "sensor")
            isp = next(m for m in models if m["kind"] == "isp")
            psf_input = client.template(
                "factory", plugin_id="prescription_psf_v1", request_version=3
            )
            ray_input = client.template("factory", plugin_id="rayoptics_lens_v1", request_version=3)
            ray_input["request"] = deepcopy(psf_input["request"]["prescription"])
            factory = {}
            for label, body in (("psf", psf_input), ("ray", ray_input)):
                queued = send(label + "-factory", "factory/jobs", body)
                result = client.wait_characterization(queued["id"])
                check(
                    label + " actual Factory completed",
                    result["status"] == "succeeded"
                    and result["result"]["summary"]["status"] == "PROPOSED",
                )
                factory[label] = preserve(label + "-factory", result, "factory/jobs")
            conditions = deepcopy(original["conditions"])
            conditions["f_number"] = {
                "value": factory["psf"]["result"]["summary"]["geometric_reference_f_number"],
                "unit": "1",
            }
            parent = send(
                "ray-model",
                "factory/models",
                {
                    "job_id": factory["ray"]["id"],
                    "name": "Collection QA joint directions",
                    "revision": "1",
                    "conditions": {
                        k: v for k, v in conditions.items() if k not in {"exposure_s", "gain"}
                    },
                },
            )
            preserve("ray-model", parent, "models")
            lens = preserve(
                "lens",
                send(
                    "lens",
                    "factory/models",
                    {
                        "job_id": factory["psf"]["id"],
                        "base_lens_model_id": parent["id"],
                        "name": "Collection QA matched prescription PSF",
                        "revision": "1",
                        "conditions": conditions,
                        "condition_scope": "optical_response",
                    },
                ),
                "models",
            )
            cameras, spatial_runs, images = {}, {}, {}
            for label in ("identity", "routing"):
                sensor = preserve(
                    label + "-sensor",
                    send(
                        label + "-sensor",
                        "models",
                        authored_sensor(base, routing=label == "routing"),
                    ),
                    "models",
                )
                draft = {
                    k: deepcopy(v) for k, v in original.items() if k in AssemblyCreate.model_fields
                }
                draft.update(
                    name="전하 수집 16×16 · " + label,
                    revision="collection-1",
                    model_ids=[lens["id"], sensor["id"], isp["id"]],
                    conditions=conditions,
                    spectral_range=sensor["spectral_range"],
                )
                assembly = preserve(
                    label + "-assembly",
                    send(label + "-assembly", "assemblies", draft),
                    "assemblies",
                )
                check(label + " assembly compatible", assembly["compatibility"]["status"] == "PASS")
                cameras[label] = assembly
                metadata = client.template("spectral-image", assembly_id=assembly["id"])
                metadata.update(
                    name="Known full sensor irradiance · " + label,
                    source_uri="fixture://generation-collection/full-uniform",
                    method_and_version="authored_uniform_irradiance:1",
                )
                image_path = out / (label + "-input.npy")
                np.save(image_path, np.full((16, 16, 2), 1e-3), allow_pickle=False)
                image_record = send(
                    label + "-image",
                    "spectral_images/import",
                    {
                        "metadata": metadata,
                        "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                    },
                    lambda m=metadata, p=image_path: client.import_spectral_image(m, p),
                )
                images[label] = preserve(label + "-image", image_record, "spectral_images")
                test = preserve(
                    label + "-test",
                    send(
                        label + "-test",
                        "tests",
                        client.template(
                            "spatial-test", assembly_id=assembly["id"], scene_id=image_record["id"]
                        ),
                    ),
                    "tests",
                )
                queued = send(
                    label + "-spatial",
                    "runs",
                    {
                        "assembly_id": assembly["id"],
                        "test_id": test["id"],
                        "scene_id": image_record["id"],
                        "plugin_id": "spatial_scene_v1",
                        "parameters": {"frames": 4, "seed": 747},
                    },
                )
                run = client.wait(queued["id"])
                check(label + " spatial Worker completed", run["status"] == "succeeded")
                spatial_runs[label] = preserve(label + "-spatial", run, "runs")
            assembly = cameras["routing"]
            # The flat response mode uses the same full native collection
            # operator, with an explicit flat stimulus and no optical PSF.
            flat_camera = {
                k: deepcopy(v) for k, v in assembly.items() if k in AssemblyCreate.model_fields
            }
            flat_camera.update(
                name="전하 수집 · full native Response DB",
                model_ids=[parent["id"], sensor["id"], isp["id"]],
            )
            flat_camera = preserve(
                "flat-assembly", send("flat-assembly", "assemblies", flat_camera), "assemblies"
            )
            base_test = client.get("tests/" + boot["demo"]["test_id"])
            flat_test = {
                k: deepcopy(v) for k, v in base_test.items() if k in FixedTestCreate.model_fields
            }
            flat_test.update(
                name="전하 생성·수집 · flat RAW 비율",
                revision="collection-flat-1",
                validation_kind="implementation_verification",
                conditions=conditions,
                included_effects=parent["included_effects"] + sensor["included_effects"],
            )
            flat_test["stimulus"].update(
                wavelength_nm=[500, 600], spectral_irradiance_w_m2_nm=[1e-3, 1e-3]
            )
            flat_test = preserve("flat-test", send("flat-test", "tests", flat_test), "tests")
            queued = send(
                "flat-run",
                "runs",
                {
                    "assembly_id": flat_camera["id"],
                    "test_id": flat_test["id"],
                    "plugin_id": "response_db_v1",
                    "parameters": {"output_size": [16, 16], "frames": 4, "seed": 747},
                },
            )
            flat_run = client.wait(queued["id"])
            check("full native Response DB Worker completed", flat_run["status"] == "succeeded")
            preserve("flat-run", flat_run, "runs")
            scene_draft = client.template("temporal-scene", assembly_id=assembly["id"])
            for knot in scene_draft["payload"]["knots"]:
                knot["spectral_image_id"] = images["routing"]["id"]
            scene = preserve(
                "sequence", send("sequence", "scene_sequences", scene_draft), "scene_sequences"
            )
            controls = client.template(
                "control-sequence", assembly_id=assembly["id"], scene_sequence_id=scene["id"]
            )
            controls["payload"]["mode"].update(shutter="global", line_time_ns=0, hidden_top_rows=0)
            controls["payload"]["frames"] = controls["payload"]["frames"][:2]
            control = preserve(
                "control", send("control", "control_sequences", controls), "control_sequences"
            )
            for label, plugin, mode in (
                ("temporal", "temporal_scene_v1", None),
                ("hdr", "hdr_bracket_v1", "independent_bracket"),
                ("dcg", "shared_charge_dcg_v1", "shared_charge"),
                ("reference", "reference_hdr_v1", "reference_guarded"),
            ):
                test_args = {
                    "assembly_id": assembly["id"],
                    "scene_sequence_id": scene["id"],
                    "control_sequence_id": control["id"],
                }
                if mode:
                    program = preserve(
                        label + "-program",
                        send(
                            label + "-program",
                            "capture_programs",
                            client.template(
                                "capture-program", assembly_id=assembly["id"], mode=mode
                            ),
                        ),
                        "capture_programs",
                    )
                    test_args["capture_program_id"] = program["id"]
                test = preserve(
                    label + "-test",
                    send(label + "-test", "tests", client.template("temporal-test", **test_args)),
                    "tests",
                )
                queued = send(
                    label + "-run",
                    "runs",
                    {
                        **test_args,
                        "test_id": test["id"],
                        "plugin_id": plugin,
                        "parameters": {"seed": 747},
                    },
                )
                run = client.wait(queued["id"])
                check(label + " Worker completed", run["status"] == "succeeded")
                preserve(label + "-run", run, "runs")
            source = spatial_runs["routing"]
            cal = preserve(
                "calibration",
                send(
                    "calibration",
                    "isp-calibrations",
                    client.isp_calibration_template("run", source["id"]),
                ),
                "isp-calibrations",
            )
            model = preserve(
                "isp-v2",
                send(
                    "isp-v2",
                    "models",
                    client.advanced_isp_template("run", source["id"], calibration_id=cal["id"]),
                ),
                "models",
            )
            queued = send(
                "replay",
                "replays",
                {
                    "name": "전하 수집 RAW · 고급 ISP",
                    "source_kind": "run",
                    "source_id": source["id"],
                    "isp_model_id": model["id"],
                    "force": False,
                },
            )
            replay = client.wait_replay(queued["id"])
            check("advanced ISP Worker completed", replay["status"] == "succeeded")
            preserve("replay", replay, "replays")
            spec = client.isp_study_template("run", source["id"], cal["id"], model["id"])
            spec.update(name="전하 수집 RAW · 고정 입력 ISP 탐색", constraints=[])
            # The source-bound template bypasses sharpening. Search the active
            # denoise stage explicitly; do not pretend a bypassed axis changes pixels.
            # Keep the original four-candidate refusal as separate evidence;
            # this qualification covers the baseline plus one explicit variant.
            spec["axes"] = [{"parameter": "raw_denoise.strength", "values": [0.0]}]
            spec["datasets"][0]["metric_spec"]["roi"] = {
                "row_start": 4,
                "row_end": 12,
                "column_start": 4,
                "column_end": 12,
            }
            plan = client.plan_isp_study(spec)
            study = send(
                "study",
                "isp-studies",
                {"specification": plan["specification"], "plan_hash": plan["plan_hash"]},
            )
            analysis = client.wait_isp_study(study["id"])
            check(
                "all fixed RAW ISP cells completed",
                analysis["coverage"]["succeeded"] == len(analysis["rows"]) > 0,
            )
            preserve("study", client.get("isp-studies/" + study["id"]), "isp-studies")
            case = send(
                "case",
                "case_reviews",
                {
                    "request_id": "charge-collection-acceptance-v1",
                    "name": "전하 생성·수집 모델 검토",
                    "question": "생성 전하와 수집 손실·이동이 원본 RAW 및 ISP 근거에 남는가?",
                    "assembly_ids": [assembly["id"]],
                    "requirements": [
                        {
                            "key": "raw",
                            "description": "원본 RAW와 수집 모델 일치",
                            "test_id": source["test_id"],
                        }
                    ],
                    "evidence": [
                        {"kind": "runs", "id": source["id"], "requirement_keys": ["raw"]},
                        {
                            "kind": "isp_studies",
                            "id": study["id"],
                            "requirement_keys": ["raw"],
                            "dataset_requirements": {spec["datasets"][0]["key"]: ["raw"]},
                        },
                    ],
                    "decision": {
                        "status": "investigate",
                        "rationale": "합성 구현 경로 확인. 실측 검증 미완료.",
                    },
                    "unresolved": [
                        "독립 실물 측정과 수집 확률 불확실도",
                        "고전하·전하 분할 공분산 모델",
                    ],
                },
            )
            preserve("case", case, "case_reviews")

        arrays, artifacts, required_artifacts = {}, {}, {}
        report["readback_timings"] = {}
        for label, ref in report["records"].items():
            started = time.monotonic()
            record = client.get(ref["route"] + "/" + ref["id"])
            report["readback_timings"][label] = {"record_seconds": time.monotonic() - started}
            check(label + " exact full record", digest(record) == ref["record_sha256"])
            for artifact in artifact_descriptors(record):
                key = (artifact["sha256"], artifact["name"])
                if key in required_artifacts:
                    check(
                        "consistent descriptor " + "/".join(key),
                        required_artifacts[key]["bytes"] == artifact["bytes"],
                    )
                required_artifacts[key] = artifact
            if ref["route"] not in {"runs", "replays", "isp-studies"}:
                del record
                continue
            started = time.monotonic()
            response = client.http.get("/api/v1/" + ref["route"] + "/" + ref["id"] + "/bundle")
            response.raise_for_status()
            report["readback_timings"][label].update(
                bundle_seconds=time.monotonic() - started, bundle_bytes=len(response.content)
            )
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                manifest = archive.read("manifest.json")
                listed = json.loads(manifest)
                graph_bundle = listed.get("schema") == "camera_evidence_bundle_v2"
                if graph_bundle:
                    from camera_platform.evidence_package import read_bundle

                    listed = read_bundle(io.BytesIO(response.content))
                if ref["route"] == "runs":
                    check(label + " bundle exact original run", listed["run"] == record)
                manifest_artifacts = list(artifact_descriptors(listed))
                for artifact in manifest_artifacts:
                    checksum, name = artifact["sha256"], artifact["name"]
                    member = "artifacts/" + checksum + ("" if graph_bundle else "/" + name)
                    # A manifest may preserve an upstream descriptor without
                    # repeating its bytes in this bundle. All required bytes
                    # must be present across the verified bundle closure below.
                    if member not in archive.namelist():
                        continue
                    content = archive.read(member)
                    check(
                        label + " bundle SHA " + member,
                        hashlib.sha256(content).hexdigest() == checksum
                        and len(content) == artifact["bytes"],
                    )
                    artifacts[(checksum, name)] = content
                collection_descriptors = [
                    a for a in manifest_artifacts if a["name"] == "charge_collection.json"
                ]
                check(
                    label + " bundle retains collection closure",
                    bool(collection_descriptors)
                    and all((a["sha256"], a["name"]) in artifacts for a in collection_descriptors),
                )
                checksum = hashlib.sha256(manifest).hexdigest()
            if label in report["bundles"]:
                check(label + " exact bundle manifest", report["bundles"][label] == checksum)
            report["bundles"][label] = checksum
            target = out / (label + "-bundle.zip")
            if not target.exists():
                target.write_bytes(response.content)
            report.setdefault("bundle_zip_sha256", {})[label] = hashlib.sha256(
                target.read_bytes()
            ).hexdigest()
            if ref["route"] == "runs":
                arrays[label] = {
                    a["name"][:-4]: np.load(
                        io.BytesIO(artifacts[(a["sha256"], a["name"])]), allow_pickle=False
                    )
                    for a in record["artifacts"]
                    if a["name"].endswith(".npy")
                }
                summary = record["result"]["summary"]
                check(
                    label + " explicit generation and collection effects",
                    {"sensor_charge_generation", "sensor_charge_collection"}
                    <= set(summary["included_effects"])
                    and "sensor_effective_qe" not in summary["included_effects"],
                )
                check(
                    label + " raw and response provenance retained",
                    all(
                        name in {a["name"] for a in record["artifacts"]}
                        for name in (
                            (
                                "raw_groups.npy"
                                if record["plugin_id"] == "shared_charge_dcg_v1"
                                else "raw_frames.npy"
                            ),
                            "native_pixel_response.json",
                            "charge_collection.json",
                        )
                    ),
                )
                if record["plugin_id"] == "shared_charge_dcg_v1":
                    groups = arrays[label]["raw_groups"]
                    check(
                        label + " original paired RAW axes preserved",
                        groups.ndim == 4
                        and groups.shape[1] == 2
                        and groups.dtype.kind in "iu"
                        and summary["readout_ids"] == ["HCG", "LCG"]
                        and summary["raw_axes"] == ["acquisition", "readout", "row", "column"],
                    )
            del record
            save()
        for key, artifact in required_artifacts.items():
            check(
                "complete preserved artifact closure " + "/".join(key),
                key in artifacts and len(artifacts[key]) == artifact["bytes"],
            )
        identity, routing = arrays["identity-spatial"], arrays["routing-spatial"]
        generated = routing["collection_generated"]
        expected = generated * 0.75
        expected[:, 1:] += generated[:, :-1] * 0.15
        check(
            "independent scalar transport reaches collected electrons",
            np.allclose(routing["mean_electrons"], expected, rtol=2e-13, atol=1e-10),
        )
        check(
            "generation conserved before alternate collection",
            np.array_equal(identity["collection_generated"], generated),
        )
        check(
            "actual integer RAW changes",
            not np.array_equal(identity["raw_frames"], routing["raw_frames"]),
        )
        check(
            "actual RGB changes", not np.array_equal(identity["rgb_frames"], routing["rgb_frames"])
        )
        check(
            "scalar recombination loss",
            np.allclose(
                routing["collection_intrinsic_loss"], generated * 0.1, rtol=2e-13, atol=1e-10
            ),
        )
        expected_boundary = np.zeros_like(generated)
        expected_boundary[:, -1] = generated[:, -1] * 0.15
        check(
            "scalar full sensor boundary loss",
            np.allclose(
                routing["collection_off_sensor_loss"], expected_boundary, rtol=2e-13, atol=1e-10
            ),
        )
        report["observations"] = {
            "raw_shape": list(routing["raw_frames"].shape),
            "changed_raw_samples": int(np.sum(identity["raw_frames"] != routing["raw_frames"])),
            "generated_electrons": float(generated.sum()),
            "collected_electrons": float(routing["mean_electrons"].sum()),
            "intrinsic_loss_electrons": float(routing["collection_intrinsic_loss"].sum()),
            "off_sensor_loss_electrons": float(routing["collection_off_sensor_loss"].sum()),
            "verified_artifacts": len(artifacts),
        }
        report.update(status="passed", restart_verified=args.verify_only)
        report.pop("failed_check", None)
        save()
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": len(report["checks"]),
                "observations": report["observations"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
