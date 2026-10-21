#!/usr/bin/env python3
"""Qualify the same retained native RAW through a four-candidate ISP Study.

The caller owns the isolated API/Worker lifecycle and source freeze. Mutating
POST intents are durable and uncertain outcomes require explicit reconciliation.
--verify-only performs GET/read-only checks after restart, never new POSTs.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sqlite3
import time
import zipfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np

from camera_platform import __version__
from camera_platform.evidence_package import read_bundle
from camera_platform.evidence_storage import PREFIX
from camera_platform.registry import SQLiteRegistry, canonical, digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def atomic_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def state_size(state):
    """Inspect physical storage without using writable Registry initialization."""
    database = state / "registry.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
        counts = {
            table: conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("records", "jobs", "audit")
        }
        physical = logical = graphs = inline = 0
        for table, column in (("records", "document"), ("jobs", "document"), ("jobs", "result")):
            for (text,) in conn.execute(
                "SELECT " + column + " FROM " + table + " WHERE " + column + " IS NOT NULL"
            ):
                length = len(text.encode())
                physical += length
                if text.startswith(PREFIX):
                    logical += json.loads(text[len(PREFIX) :])["expanded_bytes"]
                    graphs += 1
                else:
                    logical += length
                    inline += 1
    files = [p for p in (state / "blobs").iterdir() if p.is_file()]
    return {
        "counts": counts,
        "registry_json_physical_bytes": physical,
        "registry_json_logical_bytes": logical,
        "graph_documents": graphs,
        "inline_documents": inline,
        "blob_count": len(files),
        "all_blob_bytes": sum(p.stat().st_size for p in files),
        "database_bytes": database.stat().st_size,
        "wal_bytes": Path(str(database) + "-wal").stat().st_size
        if Path(str(database) + "-wal").exists()
        else 0,
    }


def schemas(value):
    if isinstance(value, dict):
        if isinstance(value.get("schema"), str):
            yield value["schema"]
        for child in value.values():
            yield from schemas(child)
    elif isinstance(value, list):
        for child in value:
            yield from schemas(child)


def local_record(state, kind, identity):
    """Fresh read-only recovery path; it does not prove an HTTP case read succeeded."""
    registry = SQLiteRegistry.__new__(SQLiteRegistry)
    registry.directory, registry.path, registry.blobs = (
        state,
        state / "registry.sqlite",
        state / "blobs",
    )
    with sqlite3.connect(registry.path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM records WHERE kind=? AND id=?", (kind, identity)
        ).fetchone()
    if row is None:
        raise ValueError("Recovery record is absent")
    return registry._record(row)


def numeric_metric_evidence(value):
    """Versioned execution context may differ; all numeric/provenance inputs stay exact."""
    result = deepcopy(value)
    context = {key: result["evidence"].pop(key) for key in ("environment", "implementation")}
    return result, context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:51264")
    parser.add_argument(
        "--state-dir", type=Path, default=Path("outputs/camera-platform-evidence-scaling-v016-qa")
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/camera_platform/evidence-scaling-v016/http"),
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("reports/camera_platform/charge-collection-v016/http"),
    )
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--readback-only", action="store_true")
    parser.add_argument("--local-case-readback", action="store_true")
    parser.add_argument("--wait-seconds", type=float, default=900)
    args = parser.parse_args()
    read_only = args.verify_only or args.readback_only
    url = urlsplit(args.base_url)
    if url.scheme != "http" or url.hostname != "127.0.0.1" or url.port not in {51264, 51265}:
        parser.error("Use an explicitly isolated loopback QA port 51264 or 51265")
    state, out, source_dir = (
        args.state_dir.resolve(),
        args.output_dir.resolve(),
        args.source_dir.resolve(),
    )
    if (
        not state.name.startswith("camera-platform-evidence-scaling-v016")
        or not state.name.endswith("qa")
        or state == out
        or state in out.parents
    ):
        parser.error("Use the isolated evidence-scaling QA clone and separate output directory")
    out.mkdir(parents=True, exist_ok=True)
    old = {
        name: json.loads((source_dir / (name + ".json")).read_text())
        for name in ("routing-spatial", "calibration", "isp-v2", "study", "case")
    }
    binding = {
        "base_url": args.base_url,
        "state_dir": str(state),
        "version": __version__,
        "environment": environment_fingerprint(),
        "source_records": {
            name: {"id": value["id"], "sha256": digest(value)} for name, value in old.items()
        },
    }
    report_path = out / "http-verification.json"
    report = (
        json.loads(report_path.read_text())
        if report_path.exists()
        else {
            "schema": "camera_evidence_scaling_http_v1",
            **binding,
            "operations": {},
            "records": {},
            "checks": [],
            "timings": {},
            "status": "in_progress",
            "physical_validation": "not_established",
            "total_uncertainty": "unknown",
        }
    )
    if any(report.get(key) != value for key, value in binding.items()):
        raise ValueError("Resume requires identical source, state and frozen environment")
    if read_only and not {"study", "selection", "case"} <= report["records"].keys():
        raise ValueError("Restart readback requires the completed prior mutation ledger")

    def save():
        atomic_bytes(
            report_path, (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode()
        )

    def check(label, condition):
        if not condition:
            report.update(status="failed", failed_check=label)
            save()
            raise AssertionError(label)
        if label not in report["checks"]:
            report["checks"].append(label)

    def preserve(label, value, route):
        encoded = canonical(value).encode()
        path = out / (label + ".json")
        reference = {
            "id": value["id"],
            "route": route,
            "sha256": digest(value),
            "path": path.name,
            "bytes": len(encoded),
        }
        if label in report["records"]:
            check(label + " immutable record binding", report["records"][label] == reference)
        if path.exists():
            check(label + " preserved bytes", path.read_bytes() == encoded)
        else:
            atomic_bytes(path, encoded)
        report["records"][label] = reference
        save()
        return value

    with PlatformClient(args.base_url, timeout=900) as client:
        health = client.get("health")
        check(
            "isolated healthy API",
            health["status"] == "ok" and Path(health["state_dir"]).resolve() == state,
        )
        report.setdefault("worker_observations", []).append(
            {"read_only": read_only, "worker": health["worker"]}
        )
        if not read_only:
            check("actual Worker running before submission", health["worker"] == "running")
        check("frozen source version", client.get("session")["version"] == __version__)

        def get(route, label):
            started = time.monotonic()
            response = client.http.get("/api/v1/" + route)
            response.raise_for_status()
            elapsed = time.monotonic() - started
            report["timings"].setdefault(label, []).append(
                {"seconds": elapsed, "response_bytes": len(response.content)}
            )
            return response.json()

        def send(label, route, payload):
            if read_only:
                raise ValueError("POST prohibited during restart readback")
            intent = {"route": route, "payload_sha256": digest(payload)}
            existing = report["operations"].get(label)
            if existing:
                if (
                    any(existing.get(key) != value for key, value in intent.items())
                    or "response_ref" not in existing
                ):
                    raise ValueError(
                        "Changed or uncertain POST requires explicit reconciliation: " + label
                    )
                ref = existing["response_ref"]
                content = (out / ref["path"]).read_bytes()
                check(
                    label + " saved response hash",
                    len(content) == ref["bytes"]
                    and hashlib.sha256(content).hexdigest() == ref["sha256"],
                )
                return json.loads(content)
            report["operations"][label] = intent
            save()
            started = time.monotonic()
            try:
                value = client.post(route, payload)
            except BaseException as exc:
                report["operations"][label]["error"] = str(exc)
                report["operations"][label]["status"] = "requires_explicit_reconciliation"
                save()
                raise
            elapsed = time.monotonic() - started
            content = canonical(value).encode()
            target = out / "operation-responses" / (label + ".json")
            atomic_bytes(target, content)
            report["operations"][label].update(
                status="response_preserved",
                seconds=elapsed,
                response_ref={
                    "path": str(target.relative_to(out)),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                    "id": value.get("id"),
                },
            )
            save()
            print(
                json.dumps({"operation": label, "seconds": elapsed, "id": value.get("id")}),
                flush=True,
            )
            return value

        legacy_routes = {
            "routing-spatial": "runs",
            "calibration": "isp-calibrations",
            "isp-v2": "models",
            "study": "isp-studies",
            "case": "case_reviews",
        }
        for name, route in legacy_routes.items():
            current = (
                local_record(state, "case_reviews", old[name]["id"])
                if args.local_case_readback and name == "case"
                else get(route + "/" + old[name]["id"], "legacy-" + name)
            )
            check("legacy " + name + " exact full record", digest(current) == digest(old[name]))
            del current
        with zipfile.ZipFile(source_dir / "study-bundle.zip") as archive:
            old_manifest_bytes = archive.read("manifest.json")
        old_manifest = json.loads(old_manifest_bytes)
        old_manifest_sha = hashlib.sha256(old_manifest_bytes).hexdigest()
        started = time.monotonic()
        response = client.http.get("/api/v1/isp-studies/" + old["study"]["id"] + "/bundle")
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            current_manifest = archive.read("manifest.json")
        check("legacy v1 bundle manifest bytes unchanged", current_manifest == old_manifest_bytes)
        report["legacy_bundle"] = {
            "manifest_sha256": old_manifest_sha,
            "bytes": len(response.content),
            "seconds": time.monotonic() - started,
        }
        del current_manifest, old_manifest_bytes, response
        source = old["routing-spatial"]
        raw_descriptor = next(a for a in source["artifacts"] if a["name"] == "raw_frames.npy")
        check("original full native RAW 4 by 16 by 16", raw_descriptor["shape"] == [4, 16, 16])
        if not read_only:
            if "before" not in report:
                report["before"] = state_size(state)
                save()
            template = client.isp_study_template(
                "run", source["id"], old["calibration"]["id"], old["isp-v2"]["id"]
            )
            check(
                "default template offers only active denoise",
                [a["parameter"] for a in template["axes"]] == ["raw_denoise.strength"],
            )
            check(
                "default preserves baseline and calibration",
                template["baseline_isp_model_id"] == old["isp-v2"]["id"]
                and template["calibration_id"] == old["calibration"]["id"],
            )
            spec = deepcopy(old["study"]["specification"])
            spec["name"] = "동일 전하 수집 RAW · 4후보 ISP 근거 저장 검증"
            spec["axes"] = [{"parameter": "raw_denoise.strength", "values": [0.0, 0.4, 0.8]}]
            before_plan = state_size(state)
            plan = send("four-candidate-plan", "isp-studies/plan", spec)
            after_plan = state_size(state)
            check(
                "planning leaves records jobs audit and blobs unchanged",
                all(
                    before_plan[key] == after_plan[key]
                    for key in (
                        "counts",
                        "registry_json_physical_bytes",
                        "registry_json_logical_bytes",
                        "blob_count",
                        "all_blob_bytes",
                    )
                ),
            )
            report.setdefault("plan_readonly", {"before": before_plan, "after": after_plan})
            check(
                "four candidates are accepted without reduction",
                len(plan["candidates"]) == plan["job_count"] == 4,
            )
            check(
                "four candidate bundle estimate remains below unchanged 256 MiB limit",
                plan["resources"]["estimated_bundle_bytes"] < 256 * 1024**2,
            )
            study = send(
                "four-candidate-study",
                "isp-studies",
                {"specification": plan["specification"], "plan_hash": plan["plan_hash"]},
            )
            preserve("study", study, "isp-studies")
        else:
            study = json.loads((out / report["records"]["study"]["path"]).read_bytes())
        check(
            "persisted study owns four actual Worker cells",
            len(study["cells"]) == 4 and study["job_count"] == 4,
        )
        check(
            "new study uses graph bundle version",
            study.get("evidence_format") == "camera_evidence_bundle_v2",
        )
        deadline = time.monotonic() + args.wait_seconds
        while True:
            analysis = get("isp-studies/" + study["id"] + "/analysis", "analysis")
            if analysis["status"] == "complete":
                break
            save()
            print(
                json.dumps({"study_id": study["id"], "coverage": analysis["coverage"]}), flush=True
            )
            if read_only or time.monotonic() >= deadline:
                raise TimeoutError(
                    "Preserved Study remains active; resume its existing ID without resubmission"
                )
            time.sleep(3)
        check(
            "all four actual Worker cells succeeded",
            analysis["coverage"]["succeeded"] == 4
            and all(row["status"] == "succeeded" for row in analysis["rows"]),
        )
        analysis_path = out / "analysis.json"
        if analysis_path.exists():
            check(
                "analysis stable after restart",
                digest(json.loads(analysis_path.read_bytes())) == digest(analysis),
            )
        else:
            atomic_bytes(analysis_path, canonical(analysis).encode())
        report["analysis_sha256"] = digest(analysis)
        if not read_only:
            baseline = next(c for c in study["candidates"] if c["is_baseline"])
            selection = send(
                "selection",
                "isp-studies/" + study["id"] + "/selection",
                {
                    "candidate_id": baseline["candidate_id"],
                    "rationale": (
                        "동일 RAW의 수치 동등성 검증을 위해 기준 프로파일을 유지합니다. "
                        "물리 우월성 판단이 아닙니다."
                    ),
                    "analysis_sha256": analysis["analysis_sha256"],
                },
            )
            preserve("selection", selection, "isp-selections")
            case_payload = {
                "request_id": "evidence-scaling-four-candidate-v1",
                "name": "전하 수집 모델 · 동일 RAW 4후보 ISP 검토",
                "question": (
                    "원본·Factory·픽셀 수집 근거를 보존하면서 동일 RAW의 4후보를 비교할 수 있는가?"
                ),
                "assembly_ids": [source["assembly_id"]],
                "requirements": deepcopy(old["case"]["requirements"]),
                "evidence": [
                    {"kind": "runs", "id": source["id"], "requirement_keys": ["raw"]},
                    {
                        "kind": "isp_studies",
                        "id": study["id"],
                        "requirement_keys": ["raw"],
                        "dataset_requirements": {"flat": ["raw"]},
                        "selection_id": selection["id"],
                    },
                ],
                "decision": {
                    "status": "investigate",
                    "rationale": (
                        "계산·저장 경로 검증입니다. 독립 실물 검증은 아직 완료되지 않았습니다."
                    ),
                },
                "unresolved": deepcopy(old["case"]["unresolved"]),
            }
            case = send("case", "case_reviews", case_payload)
            preserve("case", case, "case_reviews")
        for label, reference in report["records"].items():
            value = (
                local_record(state, "case_reviews", reference["id"])
                if args.local_case_readback and label == "case"
                else get(reference["route"] + "/" + reference["id"], "readback-" + label)
            )
            check(label + " exact full readback", digest(value) == reference["sha256"])
            del value
        exported = get("isp-studies/" + study["id"] + "/export", "logical-export")
        expected_manifest_sha = digest(exported)
        if "logical_manifest_sha256" in report:
            check(
                "logical export stable after restart",
                report["logical_manifest_sha256"] == expected_manifest_sha,
            )
        report["logical_manifest_sha256"] = expected_manifest_sha
        logical_bytes = len(canonical(exported).encode())
        del exported
        started = time.monotonic()
        response = client.http.get("/api/v1/isp-studies/" + study["id"] + "/bundle")
        response.raise_for_status()
        bundle_bytes = response.content
        report["timings"].setdefault("graph-bundle", []).append(
            {"seconds": time.monotonic() - started, "response_bytes": len(bundle_bytes)}
        )
        manifest = read_bundle(
            io.BytesIO(bundle_bytes), expected_manifest_sha256=expected_manifest_sha
        )
        check(
            "graph fully reconstructs exact exported evidence",
            digest(manifest) == expected_manifest_sha,
        )
        check("graph carries exact frozen study", manifest["study"] == study)
        check(
            "graph preserves both executed Factory parents",
            len(manifest["factory_jobs"]) >= 2
            and all(job["status"] == "succeeded" for job in manifest["factory_jobs"]),
        )
        retained_schemas = set(schemas(manifest))
        check(
            "graph preserves separate generation and charge collection",
            {"camera_native_generation_response_v1", "camera_charge_collection_v1"}
            <= retained_schemas,
        )
        current_rows = {row["candidate_id"]: row for row in manifest["analysis"]["rows"]}
        current_cells = {cell["candidate_id"]: cell for cell in manifest["cells"]}
        old_rows = {row["candidate_id"]: row for row in old_manifest["analysis"]["rows"]}
        old_cells = {cell["candidate_id"]: cell for cell in old_manifest["cells"]}
        compared = []
        with (
            zipfile.ZipFile(io.BytesIO(bundle_bytes)) as new_zip,
            zipfile.ZipFile(source_dir / "study-bundle.zip") as old_zip,
        ):
            envelope_bytes = new_zip.read("manifest.json")
            envelope = json.loads(envelope_bytes)
            for candidate in old["study"]["candidates"]:
                identity = candidate["candidate_id"]
                match = next(
                    c
                    for c in study["candidates"]
                    if c["profile_sha256"] == candidate["profile_sha256"]
                )
                check(
                    "candidate profile remains exact " + identity,
                    match["profile"] == candidate["profile"],
                )
                new_id = match["candidate_id"]
                check(
                    "candidate metrics remain exact " + identity,
                    current_rows[new_id]["metrics"] == old_rows[identity]["metrics"],
                )
                current_metric, current_context = numeric_metric_evidence(
                    current_rows[new_id]["metric_result"]
                )
                previous_metric, previous_context = numeric_metric_evidence(
                    old_rows[identity]["metric_result"]
                )
                check(
                    "candidate contexts bind their exact execution environments " + identity,
                    current_context["environment"] == binding["environment"]
                    and previous_context["environment"] == old["study"]["environment"],
                )
                check(
                    "candidate metric evidence excluding execution context remains exact "
                    + identity,
                    current_metric == previous_metric,
                )
                previous = {
                    a["name"]: a
                    for a in old_cells[identity]["artifacts"]
                    if a["name"].endswith(".npy")
                }
                current = {
                    a["name"]: a
                    for a in current_cells[new_id]["artifacts"]
                    if a["name"].endswith(".npy")
                }
                check(
                    "candidate array inventory remains exact " + identity,
                    current.keys() == previous.keys(),
                )
                for name, descriptor in current.items():
                    prior = previous[name]
                    new_content = new_zip.read("artifacts/" + descriptor["sha256"])
                    old_content = old_zip.read("artifacts/" + prior["sha256"] + "/" + prior["name"])
                    check(identity + " array bytes " + name, new_content == old_content)
                    new_array = np.load(io.BytesIO(new_content), allow_pickle=False)
                    old_array = np.load(io.BytesIO(old_content), allow_pickle=False)
                    check(
                        identity + " numerical array " + name,
                        new_array.dtype == old_array.dtype
                        and new_array.shape == old_array.shape
                        and np.array_equal(new_array, old_array, equal_nan=True),
                    )
                compared.append(
                    {
                        "profile_sha256": candidate["profile_sha256"],
                        "arrays": len(current),
                        "metrics_equal": True,
                        "previous_execution_context_sha256": digest(previous_context),
                        "current_execution_context_sha256": digest(current_context),
                        "excluded_fields": [
                            "metric_result.evidence.environment",
                            "metric_result.evidence.implementation",
                        ],
                    }
                )
            check(
                "all four candidates retain the exact source RAW",
                all(
                    cell["dataset_snapshot"]["raw_sha256"] == raw_descriptor["sha256"]
                    for cell in manifest["cells"]
                ),
            )
            uncompressed_bytes = sum(info.file_size for info in new_zip.infolist())
        check("baseline and denoise zero both compared", len(compared) == 2)
        check("actual full closure archive stays below 256 MiB", uncompressed_bytes < 256 * 1024**2)
        bundle_path = out / "study-bundle.zip"
        if not bundle_path.exists():
            atomic_bytes(bundle_path, bundle_bytes)
        else:
            with zipfile.ZipFile(bundle_path) as saved:
                check(
                    "graph bundle manifest stable after restart",
                    saved.read("manifest.json") == envelope_bytes,
                )
        report["bundle"] = {
            "path": bundle_path.name,
            "sha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
            "logical_manifest_bytes": logical_bytes,
            "zip_bytes": bundle_path.stat().st_size,
            "uncompressed_bytes": uncompressed_bytes,
            "objects": len(envelope["objects"]),
            "unique_artifacts": len(envelope["artifact_blobs"]),
            "manifest_sha256": hashlib.sha256(envelope_bytes).hexdigest(),
        }
        report["numerical_comparisons"] = compared
        report["after"] = state_size(state)
        report["observations"] = {
            "candidate_count": 4,
            "successful_worker_cells": 4,
            "raw_shape": raw_descriptor["shape"],
            "raw_sha256": raw_descriptor["sha256"],
            "calibration_id": old["calibration"]["id"],
            "baseline_isp_model_id": old["isp-v2"]["id"],
            "physical_validation": "not_established",
            "case_readback": "read_only_SQL_and_fresh_graph_decode"
            if args.local_case_readback
            else "HTTP",
        }
        final_health = client.get("health")
        check(
            "API remains bound to the same isolated state",
            final_health["status"] == "ok"
            and Path(final_health["state_dir"]).resolve() == state,
        )
        report["worker_observations"].append(
            {"phase": "after_evidence_readback", "worker": final_health["worker"]}
        )
        report.update(
            status="evidence_verified_worker_lifecycle_failed"
            if any(o["worker"] != "running" for o in report["worker_observations"])
            else "passed",
            restart_verified=args.verify_only,
        )
        report.pop("failed_check", None)
        save()
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "restart_verified": args.verify_only,
                    "checks": len(report["checks"]),
                    "bundle": report["bundle"],
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
