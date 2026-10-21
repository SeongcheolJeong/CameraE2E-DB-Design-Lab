"""Actual local HTTP/Worker ISP Study qualification on authored evaluation RAW.

Preserves durable POST intents, full records and bundles. Synthetic captures and
scalar authored references are implementation evidence, never physical validation.
The caller owns server lifecycle and verifies browser behavior independently.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from verify_camera_isp_tradeoffs import scalar_bilinear

from camera_platform import __version__
from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if address.scheme != "http" or address.hostname != "127.0.0.1" or address.port in (None, 8770):
        parser.error("Require a separate explicit local QA port")
    state, out = args.state_dir.resolve(), args.output_dir.resolve()
    if "qa" not in state.name or state == out or state in out.parents:
        parser.error("Use explicit QA state and separate evidence directory")
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
            "schema": "camera_isp_study_http_verification_v1",
            **binding,
            "operations": {},
            "records": {},
            "checks": [],
            "bundles": {},
            "physical_validation": "not_established",
            "total_uncertainty": {"kind": "unknown"},
        }
    )
    if any(report[k] != v for k, v in binding.items()):
        raise ValueError("Resume requires the same source/environment/state")

    def save():
        report["updated_at"] = datetime.now(UTC).isoformat()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        tmp.replace(path)

    def check(name, value):
        if not value:
            report["status"] = "failed"
            report["failed_check"] = name
            save()
            raise AssertionError(name)
        if name not in report["checks"]:
            report["checks"].append(name)

    def preserve(label, record, route):
        target = out / (label + ".json")
        target.write_text(json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        report["records"][label] = {
            "id": record["id"],
            "route": route,
            "record_sha256": digest(record),
            "path": str(target),
        }
        save()
        return record

    def npy(label, array):
        target = out / (label + ".npy")
        stream = io.BytesIO()
        np.save(stream, array, allow_pickle=False)
        content = stream.getvalue()
        if target.exists() and target.read_bytes() != content:
            raise ValueError("Authored input changed: " + label)
        target.write_bytes(content)
        return target

    with PlatformClient(args.base_url, timeout=120) as client:
        health = client.get("health")
        check(
            "healthy exact state with Worker",
            Path(health["state_dir"]).resolve() == state
            and health["status"] == "ok"
            and health["worker"] == "running",
        )
        check("source version", client.get("session")["version"] == __version__)

        def send(label, endpoint, body, operation=None):
            existing = report["operations"].get(label)
            intent = {"endpoint": endpoint, "payload_sha256": digest(body)}
            if existing:
                if any(existing[k] != v for k, v in intent.items()):
                    raise ValueError("Changed durable intent: " + label)
                if "response" not in existing:
                    raise ValueError("Uncertain POST requires reconciliation: " + label)
                return existing["response"]
            report["operations"][label] = intent
            save()
            response = operation() if operation else client.post(endpoint, body)
            report["operations"][label]["response"] = response
            save()
            return response

        if not args.verify_only:
            capture_template = client.get("templates/capture")
            assembly = client.get("assemblies/" + capture_template["assembly_id"])
            models = client.get("models")
            sensor = next(
                m for m in models if m["id"] in assembly["model_ids"] and m["kind"] == "sensor"
            )
            pitch = sensor["payload"]["pixel_pitch_um"]
            full = [
                round(sensor["properties"][name]["value"] * 1000 / pitch)
                for name in ("active_height", "active_width")
            ]
            geometry = {
                "kind": "native_sensor",
                "full_sensor_shape": full,
                "crop_origin": [full[0] // 4 * 2, full[1] // 4 * 2],
            }
            datasets, truths = {}, {}
            frames, height, width, black, scale = 8, 32, 48, 64, 4031
            yy, xx = np.indices((height, width))
            # Both split definitions and seeds are authored before candidate evaluation.
            for phase_index, phase in enumerate(("tuning", "independent_validation")):
                datasets[phase] = []
                for index, kind in enumerate(("flat", "edge", "texture")):
                    label = phase + "-" + kind
                    edge = 24 + phase_index * 2
                    signal = (
                        np.full((height, width), 700.0 + phase_index * 35)
                        if kind == "flat"
                        else np.where(xx < edge, 400.0, 1000.0)
                        if kind == "edge"
                        else 700.0 + 24 * np.sin(2 * np.pi * (xx + phase_index) / 8)
                    )
                    noiseless = np.rint(signal + black).astype(np.uint16)
                    raw = np.rint(
                        signal
                        + black
                        + np.random.default_rng(6300 + 100 * phase_index + index).normal(
                            0, 12, (frames, height, width)
                        )
                    ).astype(np.uint16)
                    raw_path = npy(label + "-raw", raw)
                    truths[label] = npy(
                        label + "-reference",
                        scalar_bilinear(noiseless.astype(float) - black) / scale,
                    )
                    metadata = deepcopy(capture_template)
                    metadata.update(
                        name="ISP Study authored " + label,
                        source_kind="estimated",
                        source_uri="fixture://isp-study-evaluation/" + label,
                        method_and_version="authored_gaussian_raw_v1",
                        physical_instance=None,
                        dataset_ids=["isp-study-evaluation:" + label],
                    )
                    metadata["observation"].update(
                        geometry=geometry,
                        rois=[{"id": "center", "field_radius": 0.0, "box": [8, 8, 16, 16]}],
                    )
                    imported = send(
                        "capture-" + label,
                        "captures/import",
                        {
                            "metadata": metadata,
                            "raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                        },
                        lambda m=metadata, p=raw_path: client.import_capture(m, p),
                    )
                    capture = preserve("capture-" + label, imported["capture"], "captures")
                    metric = {
                        "kind": kind,
                        "stationary": True,
                        "roi": {"row_start": 8, "row_end": 24, "column_start": 8, "column_end": 40},
                    }
                    if kind == "edge":
                        metric["edge"] = {
                            "direction": "column",
                            "position_px": float(edge),
                            "half_width_px": 6.0,
                        }
                    if kind == "texture":
                        metric["texture"] = {"direction": "column", "period_px": 8.0}
                    datasets[phase].append(
                        {
                            "key": kind,
                            "source_kind": "capture",
                            "source_id": capture["id"],
                            "purpose": phase,
                            "acquisition_group_id": metadata["dataset_ids"][0],
                            "stationarity": {
                                "source_kind": "estimated",
                                "description": "Authored static signal and Gaussian noise; "
                                "no physical acquisition",
                            },
                            "metric_spec": metric,
                        }
                    )
            first = datasets["tuning"][0]
            calibration_doc = client.isp_calibration_template(
                first["source_kind"], first["source_id"]
            )
            calibration_doc.update(
                name="Declared Gaussian variance · no fit", dataset_ids=[], evidence_refs=[]
            )
            calibration_doc["payload"]["noise"].update(
                slope_dn=[0.0] * 4, intercept_dn2=[144.0] * 4
            )
            calibration = preserve(
                "calibration",
                send("calibration", "isp-calibrations", calibration_doc),
                "isp-calibrations",
            )
            profile_doc = client.advanced_isp_template(
                "capture", first["source_id"], calibration_id=calibration["id"]
            )
            profile_doc["name"] = "Fixed baseline · NR0 sharpen0"
            profile = profile_doc["payload"]
            profile["raw_denoise"]["strength"] = 0.0
            profile["linear_sharpen"].update(mode="bounded_detail_v1", amount=0.0)
            model = preserve("baseline", send("baseline", "models", profile_doc), "models")
            common = {
                key: profile[key] for key in ("black_level", "demosaic", "white_balance", "ccm")
            }
            for phase, entries in datasets.items():
                for entry in entries:
                    label = phase + "-" + entry["key"]
                    metadata = {
                        "name": "Independent authored reference " + label,
                        "source_kind": "estimated",
                        "source_uri": "fixture://isp-study-noiseless-scalar/" + label,
                        "license": "MIT; authored fixture",
                        "method_and_version": "independent_scalar_bilinear_v1",
                        "acquisition_group_id": "reference:" + label,
                        "derivation": "authored_independent",
                        "geometry": geometry,
                        "pixel_pitch_um": pitch,
                        "normalization_dn": float(scale),
                        "common_linear_profile": common,
                        "uncertainty": {
                            "kind": "unknown",
                            "reason": "Implementation oracle, physical truth unverified",
                        },
                        "description": "Noiseless authored signal and separate scalar "
                        "interpolation; no candidate-derived pixels",
                    }
                    ref_path = truths[label]
                    reference = send(
                        "reference-" + label,
                        "isp-references/import",
                        {
                            "metadata": metadata,
                            "image_sha256": hashlib.sha256(ref_path.read_bytes()).hexdigest(),
                        },
                        lambda m=metadata, p=ref_path: client.import_isp_reference(m, p),
                    )
                    entry["reference_id"] = preserve(
                        "reference-" + label, reference, "isp-references"
                    )["id"]
            spec = client.isp_study_template(
                "capture", first["source_id"], calibration["id"], model["id"]
            )
            spec.update(name="고정 RAW · 잡음·질감·halo 탐색", datasets=datasets["tuning"])
            spec["axes"] = [
                {"parameter": "raw_denoise.strength", "values": [0.0, 0.4, 0.8]},
                {"parameter": "linear_sharpen.amount", "values": [0.0, 0.6]},
            ]
            spec["objectives"] = [
                {
                    "metric": "temporal_residual_std_dn",
                    "dataset_keys": ["flat"],
                    "reducer": "max",
                    "direction": "minimize",
                },
                {
                    "metric": "edge_overshoot_dn",
                    "dataset_keys": ["edge"],
                    "reducer": "max",
                    "direction": "minimize",
                },
            ]
            spec["constraints"] = [
                {
                    "metric": "mean_abs_bias_dn",
                    "dataset_keys": ["flat", "edge", "texture"],
                    "reducer": "max",
                    "operator": "<=",
                    "value": 10.0,
                    "unit": "DN_equivalent",
                },
                *[
                    {
                        "metric": "texture_amplitude_ratio",
                        "dataset_keys": ["texture"],
                        "reducer": "max",
                        "operator": op,
                        "value": value,
                        "unit": "1",
                    }
                    for op, value in ((">=", 0.8), ("<=", 1.1))
                ],
            ]
            plan = client.plan_isp_study(spec)
            check("18 fixed candidate dataset cells", plan["job_count"] == 18)
            study = preserve(
                "tuning-study",
                send(
                    "tuning-study",
                    "isp-studies",
                    {"specification": plan["specification"], "plan_hash": plan["plan_hash"]},
                ),
                "isp-studies",
            )
            analysis = client.wait_isp_study(study["id"], timeout=300, poll_interval=0.5)
            check(
                "tuning 18 succeeded",
                len(analysis["rows"]) == 18
                and all(r["status"] == "succeeded" for r in analysis["rows"]),
            )
            (out / "tuning-analysis.json").write_text(
                json.dumps(analysis, ensure_ascii=False, indent=2)
            )
            candidate = next(
                c
                for c in study["candidates"]
                if c["profile"]["raw_denoise"]["strength"] == 0.4
                and c["profile"]["linear_sharpen"]["amount"] == 0.0
            )
            selection = preserve(
                "selection",
                send(
                    "selection",
                    f"isp-studies/{study['id']}/selection",
                    {
                        "candidate_id": candidate["candidate_id"],
                        "rationale": "Explicit demonstration selection: moderate denoise, "
                        "no sharpen; compare held-out authored split without claiming an optimum",
                        "analysis_sha256": analysis["analysis_sha256"],
                    },
                ),
                "isp-selections",
            )
            validation_spec = deepcopy(spec)
            validation_spec.update(
                name="선택 ISP · 별도 취득 검증",
                phase="independent_validation",
                axes=[],
                selection_id=selection["id"],
                parent_study_id=study["id"],
                datasets=datasets["independent_validation"],
            )
            validation_plan = client.plan_isp_study(validation_spec)
            check("validation fixes two profiles six cells", validation_plan["job_count"] == 6)
            validation = preserve(
                "validation-study",
                send(
                    "validation-study",
                    "isp-studies",
                    {
                        "specification": validation_plan["specification"],
                        "plan_hash": validation_plan["plan_hash"],
                    },
                ),
                "isp-studies",
            )
            validation_analysis = client.wait_isp_study(
                validation["id"], timeout=300, poll_interval=0.5
            )
            check(
                "validation six succeeded",
                all(r["status"] == "succeeded" for r in validation_analysis["rows"]),
            )
            (out / "validation-analysis.json").write_text(
                json.dumps(validation_analysis, ensure_ascii=False, indent=2)
            )
            requirements = [
                {
                    "key": d["key"],
                    "description": "Fixed retained RAW " + d["key"],
                    "test_id": d["test_id"],
                }
                for d in study["datasets"]
            ]
            case = {
                "request_id": "isp-study-http-case-v1",
                "name": "ISP 잡음과 질감 설계 검토",
                "question": spec["question"],
                "requirements": requirements,
                "assembly_ids": [assembly["id"]],
                "evidence": [
                    {
                        "kind": "isp_studies",
                        "id": item["id"],
                        "requirement_keys": [r["key"] for r in requirements],
                        "dataset_requirements": {r["key"]: [r["key"]] for r in requirements},
                        "selection_id": selection["id"],
                    }
                    for item in (study, validation)
                ],
                "decision": {
                    "status": "candidate",
                    "rationale": "Synthetic held-out evaluation only; physical measurements remain",
                },
                "unresolved": ["Hardware fidelity and total uncertainty are unknown"],
                "next_experiments": [],
            }
            preserve("case", send("case", "case_reviews", case), "case_reviews")
            # Draft intended for independent visible UI verification; no hidden UI mutations.
            ui_spec = deepcopy(spec)
            ui_spec.update(
                name="화면 검증 · 고정 RAW ISP 탐색",
                datasets=[datasets["tuning"][0]],
                objectives=[spec["objectives"][0]],
                constraints=[],
            )
            ui_spec["axes"] = [{"parameter": "raw_denoise.strength", "values": [0.0, 0.4, 0.8]}]
            (out / "ui-specification.json").write_text(
                json.dumps(ui_spec, ensure_ascii=False, indent=2)
            )
            for label, record in (("tuning", study), ("validation", validation)):
                content = client.isp_study_bundle(record["id"])
                target = out / (label + "-bundle.zip")
                target.write_bytes(content)
                report["bundles"][label] = {
                    "study_id": record["id"],
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "path": str(target),
                }
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    if manifest.get("schema") == "camera_evidence_bundle_v2":
                        from camera_platform.evidence_package import read_bundle

                        manifest = read_bundle(io.BytesIO(content))
                    check(
                        label + " export equals bundle manifest",
                        manifest == client.get(f"isp-studies/{record['id']}/export"),
                    )
                    for member in archive.namelist():
                        if member.startswith("artifacts/"):
                            check(
                                label + " artifact " + member,
                                hashlib.sha256(archive.read(member)).hexdigest()
                                == member.split("/")[1],
                            )
            save()

        for label, descriptor in report["records"].items():
            current = client.get(descriptor["route"] + "/" + descriptor["id"])
            check("exact record readback " + label, digest(current) == descriptor["record_sha256"])
        for label, descriptor in report["bundles"].items():
            with zipfile.ZipFile(
                io.BytesIO(client.isp_study_bundle(descriptor["study_id"]))
            ) as archive:
                with zipfile.ZipFile(descriptor["path"]) as original:
                    check(
                        "repeat bundle members " + label,
                        sorted(archive.namelist()) == sorted(original.namelist()),
                    )
                    check(
                        "repeat bundle bytes " + label,
                        all(archive.read(n) == original.read(n) for n in archive.namelist()),
                    )
        report["status"] = "passed"
        report.setdefault("readbacks", []).append(
            {"at": datetime.now(UTC).isoformat(), "verify_only": args.verify_only}
        )
        save()
    print(
        json.dumps(
            {"status": report["status"], "checks": len(report["checks"]), "report": str(path)}
        )
    )


if __name__ == "__main__":
    main()
