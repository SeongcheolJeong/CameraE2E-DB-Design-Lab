"""Immutable, explicit study/validation/profile completion report publication."""

from __future__ import annotations

import csv
import io
import tempfile
import zipfile
from pathlib import Path

from . import calibration_lab as lab
from .registry import IntegrityError, canonical, digest

COLLECTION = "calibration_completion_reports"
SCHEMA = "camera_calibration_completion_record_v1"
FORMAT_VERSION = 2


def _bindings(service, checksums, kind, identity):
    """One fresh verified owner transaction; no cross-request evidence cache."""
    checksums = list(dict.fromkeys(checksums))
    batch = getattr(service.registry, "artifact_bindings", None)
    if batch is not None:
        return batch(checksums, owner_kind=kind, owner_id=identity)
    return {checksum: service.registry.artifact_binding(checksum, owner_kind=kind, owner_id=identity)
            for checksum in checksums}


def _image_resolver(service, report):
    # The report builder has already pinned both complete runs. Each owner and
    # every requested image byte are verified once more in a fresh batch read.
    verified = {}
    outputs = {v["study_id"]: v for v in report["hashes"]["outputs"].values()}

    def resolve(descriptor):
        if descriptor.get("kind") == "original_raw_preview":
            return _images(service, descriptor)
        identity = descriptor.get("study_id") or descriptor.get("run_id")
        relative = descriptor.get("path") or descriptor.get("isp")
        if not identity or not relative:
            return None
        if identity not in outputs or relative not in outputs[identity]["files"]:
            raise IntegrityError("Completion image is outside its frozen output manifest")
        if Path(relative).suffix.lower() != ".png":
            raise ValueError("Completion report previews require preserved PNG images")
        if identity not in verified:
            names = {name: sha for name, sha in outputs[identity]["files"].items() if name.endswith(".png")}
            bound = _bindings(service, names.values(), "runs", identity)
            if bound and digest(next(iter(bound.values()))["owner"]["result"]) != outputs[identity]["result_sha256"]:
                raise IntegrityError("Completion image owner result changed")
            verified[identity] = bound
        checksum = outputs[identity]["files"][relative]
        return verified[identity][checksum]["path"].read_bytes(), "image/png"
    return resolve


def _images(service, descriptor):
    if descriptor.get("kind") == "original_raw_preview":
        import numpy as np
        from PIL import Image

        from .calibration_lab_raw import _load

        measurement = lab._measurement(service, descriptor["measurement_id"])
        checksum = descriptor["artifact_sha256"]
        if not any(
            a["sha256"] == checksum and a["name"].lower().endswith(".npy")
            for a in measurement["artifacts"]
        ):
            raise IntegrityError("RAW preview is not bound to the original NPY")
        binding = service.registry.artifact_binding(
            checksum, owner_kind=lab.MEASUREMENTS, owner_id=measurement["id"]
        )
        raw = _load(binding["path"].read_bytes())
        mean = raw[-1].astype(float).mean(axis=0)
        fmt = measurement["raw_format"]
        white, black = 2 ** fmt["bit_depth"] - 1, fmt["black_level_dn"]
        # Fixed ADC display scale; this is an identified derivative of native RAW,
        # never substituted for its preserved measurement bytes.
        preview = np.rint(np.clip((mean - black) / (white - black), 0, 1) ** (1 / 2.2) * 255)
        stream = io.BytesIO()
        Image.fromarray(preview.astype(np.uint8)).save(stream, format="PNG")
        return stream.getvalue(), "image/png"
    identity = descriptor.get("study_id") or descriptor.get("run_id")
    relative = descriptor.get("path") or descriptor.get("isp")
    if identity and relative:
        path = lab._file(service, identity, relative)
        if Path(relative).suffix.lower() != ".png":
            raise ValueError("Completion report previews require preserved PNG images")
        return path.read_bytes(), "image/png"
    return None


def create(service, payload):
    from .calibration_completion_report import build_completion_report, render_completion_report

    required = {"study_id", "validation_id", "profile_id"}
    if set(payload) != required or any(not isinstance(payload[k], str) for k in required):
        raise ValueError("Completion report requires explicit study_id, validation_id, profile_id")
    identity = "calibration-report-" + digest({**payload, "format_version": FORMAT_VERSION})[:32]
    with lab._lock(service):
        try:
            return get(service, identity)
        except KeyError:
            pass
        report = build_completion_report(service, **payload)
        profile = service.registry.get(lab.PROFILES, payload["profile_id"])
        campaign = service.registry.get(lab.CAMPAIGNS, profile["campaign_id"])
        csv_file = io.StringIO(newline="")
        writer = csv.writer(csv_file)
        writer.writerow(["parameter", "before", "after", "unit"])
        study = lab.get(service, payload["study_id"])
        for row in study["result"]["summary"].get("parameters", []):
            writer.writerow(
                [
                    row.get("parameter"),
                    canonical(row.get("before")),
                    canonical(row.get("after")),
                    row.get("unit", ""),
                ]
            )
        contents = {
            "report.html": (
                render_completion_report(report, image_resolver=_image_resolver(service, report)),
                "text/html",
            ),
            "report.json": (canonical(report).encode(), "application/json"),
            "final-profile.json": (canonical(profile["profile"]).encode(), "application/json"),
            "parameters.csv": (csv_file.getvalue().encode("utf-8-sig"), "text/csv"),
        }
        artifacts = [
            service.registry.store_blob(data, name, media)
            for name, (data, media) in contents.items()
        ]
        validation = lab.get(service, payload["validation_id"])
        verdict = validation["result"]["summary"]["validation"]
        document = {
            "schema": SCHEMA,
            "format_version": FORMAT_VERSION,
            **payload,
            "campaign_id": campaign["id"],
            "candidate_sha256": profile["profile_sha256"],
            "study_result_sha256": digest(study["result"]),
            "validation_result_sha256": digest(validation["result"]),
            "numerical_status": verdict["status"],
            "physical_validation": verdict["physical_validation"],
            "files": {a["name"]: a["sha256"] for a in artifacts},
            "artifacts": artifacts,
            "report_url": f"{lab.ROUTE}/completion-reports/{identity}/files/report.html",
            "bundle_url": f"{lab.ROUTE}/completion-reports/{identity}/bundle",
        }
        return service.registry.put_verified_record(
            COLLECTION,
            document,
            [(lab.CAMPAIGNS, campaign), (lab.PROFILES, profile)],
            identity=identity,
        )


def get(service, identity):
    record = service.registry.get(COLLECTION, identity)
    if record.get("schema") != SCHEMA:
        raise IntegrityError("Invalid completion report record")
    return record


def report_file(service, identity, relative):
    record = get(service, identity)
    if relative not in record["files"]:
        raise KeyError("Unknown completion report file")
    binding = service.registry.artifact_binding(
        record["files"][relative], owner_kind=COLLECTION, owner_id=identity
    )
    return binding["path"]


def bundle(service, identity):
    record = get(service, identity)
    handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    path = Path(handle.name)
    handle.close()
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("completion-record.json", canonical(record))
            for name in record["files"]:
                archive.write(report_file(service, identity, name), name)
            included = set()
            original_paths = {}
            original_references = []
            for phase, key in (("fit", "study_id"), ("validation", "validation_id")):
                run = lab.get(service, record[key])
                hash_key = (
                    "validation_result_sha256" if phase == "validation" else "study_result_sha256"
                )
                if digest(run["result"]) != record[hash_key]:
                    raise IntegrityError("Completion report source result changed")
                archive.writestr(phase + "/study.json", canonical(run))
                result_bindings = _bindings(service, run["result"]["summary"]["files"].values(), "runs", run["id"])
                if result_bindings and digest(next(iter(result_bindings.values()))["owner"]["result"]) != record[hash_key]:
                    raise IntegrityError("Completion ZIP result owner changed")
                for relative in run["result"]["summary"]["files"]:
                    bound = result_bindings[run["result"]["summary"]["files"][relative]]
                    archive.write(
                        bound["path"], phase + "/results/" + relative
                    )
                for measurement in run["measurement_snapshots"]:
                    if measurement["id"] in included:
                        continue
                    if service.registry.get(lab.MEASUREMENTS, measurement["id"]) != measurement:
                        raise IntegrityError("Completion ZIP measurement differs from frozen owner")
                    included.add(measurement["id"])
                    archive.writestr(
                        "measurements/" + measurement["id"] + ".json", canonical(measurement)
                    )
                    measurement_bindings = _bindings(service, [a["sha256"] for a in measurement["artifacts"]], lab.MEASUREMENTS, measurement["id"])
                    for index, artifact in enumerate(measurement["artifacts"]):
                        checksum = artifact["sha256"]
                        binding = measurement_bindings[checksum]
                        if binding["path"].stat().st_size != artifact["bytes"]:
                            raise IntegrityError("Completion ZIP original byte size differs")
                        # 112 ROIs share the same original full-frame captures.
                        # Keep one byte-exact blob and every owner/name reference,
                        # instead of inflating a chart bundle by 112 times.
                        if checksum not in original_paths:
                            relative = f"originals/by-sha256/{checksum}"
                            archive.write(binding["path"], relative)
                            original_paths[checksum] = relative
                        original_references.append({
                            "measurement_id": measurement["id"], "artifact_index": index,
                            "original_name": artifact["name"], "sha256": checksum,
                            "bytes": artifact["bytes"], "archive_path": original_paths[checksum],
                        })
            archive.writestr("originals-manifest.json", canonical({
                "schema": "camera_calibration_bundle_originals_v2",
                "deduplication": "sha256_exact_bytes_all_measurement_references_retained",
                "unique_blob_count": len(original_paths), "references": original_references,
            }))
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def install_routes(app, service):
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    @app.post(lab.ROUTE + "/completion-reports", status_code=201, tags=["calibration_lab"])
    def publish(payload: dict):
        return create(service, payload)

    @app.get(lab.ROUTE + "/completion-reports/{identity}", tags=["calibration_lab"])
    def read(identity: str):
        return get(service, identity)

    @app.get(
        lab.ROUTE + "/completion-reports/{identity}/files/{relative:path}", tags=["calibration_lab"]
    )
    def download(identity: str, relative: str):
        media = {
            "report.html": "text/html",
            "report.json": "application/json",
            "final-profile.json": "application/json",
            "parameters.csv": "text/csv",
        }
        return FileResponse(
            report_file(service, identity, relative), media_type=media.get(relative)
        )

    @app.get(lab.ROUTE + "/completion-reports/{identity}/bundle", tags=["calibration_lab"])
    def package(identity: str):
        path = bundle(service, identity)
        return FileResponse(
            path,
            media_type="application/zip",
            filename=identity + ".zip",
            background=BackgroundTask(path.unlink),
        )
