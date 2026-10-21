"""File-first real-camera E-SFR measurements and scoped response calibration.

The chart revision and analysis method revision are independent. This module
does not assert ISO certification, traceable chart references, or camera identity.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Annotated

import numpy as np
from fastapi import File, Form, UploadFile
from PIL import Image

from .registry import ConflictError, IntegrityError, canonical, digest

ROOT = "/api/v1/camera-validation/esfr"
KIND = "camera_validation_esfr"
CAPTURES = "camera_validation_esfr_captures"
PROFILES = "camera_validation_esfr_profiles"
ACTIVATIONS = "camera_validation_esfr_activations"
MAX_FILE_BYTES = 128 * 1024**2
CHART_ASSETS = Path(__file__).with_name("static") / "esfr-chart"
CHART_FILES = {"generated.svg", "generated-rois.svg", "reference.png", "manifest.json"}


def chart_template():
    """Frozen layout evidence; public graphics are not calibrated print references."""
    manifest = json.loads((CHART_ASSETS / "manifest.json").read_text())
    return {
        **manifest,
        "asset_sha256": {
            name: hashlib.sha256((CHART_ASSETS / name).read_bytes()).hexdigest()
            for name in sorted(CHART_FILES)
        },
        "preview_url": ROOT + "/chart-assets/generated.svg",
        "overlay_url": ROOT + "/chart-assets/generated-rois.svg",
        "reference_url": ROOT + "/chart-assets/reference.png",
    }


def template():
    result = {
        "chart_profile": {
            "id": "owned-esfr-2023-extended",
            "revision": "2023",
            "aspect_ratio": "16:9",
            "layout": "imatest_extended_esfr_2023",
            "physical_size_mm": None,
            "material": "unknown",
            "reference_status": "public_design_targets_not_measured_print",
            "chart_geometry": chart_template()
            if (CHART_ASSETS / "manifest.json").exists()
            else None,
            "rois": [],
        },
        "capture_metadata": {
            "schema": "camera_esfr_capture_v1",
            "source_kind": "measured",
            "camera_id": "enter-camera-serial",
            "pixel_pitch_um": None,
            "session_id": "enter-session",
            "acquisition_id": "enter-unique-acquisition",
            "role": "development",
            "conditions": {
                "exposure_s": 0.01,
                "gain": 1.0,
                "light_id": "enter-light",
                "focus_setting": "enter-fixed-focus",
                "distance_m": None,
                "mode_id": "linear-bayer",
                "temperature_c": None,
                "aperture_f_number": None,
                "focus_distance_m": None,
            },
            "raw_format": {
                "packing": "unpacked16",
                "width": 1920,
                "height": 1080,
                "stride_bytes": 3840,
                "header_bytes": 0,
                "byte_order": "little",
                "bit_depth": 12,
                "bit_shift": 0,
                "cfa": "RGGB",
                "black_level_dn": 64,
                "white_level_dn": 4095,
            },
            "geometry": {"full_sensor_shape": [1080, 1920], "crop_origin": [0, 0]},
            "isp": {"transfer": "srgb", "coordinate_mapping": "same_native_raster"},
        },
        "analysis_template": {
            "name": "E-SFR 실카메라 검증",
            "development_ids": [],
            "validation_ids": [],
            "rois": [],
            "source": None,
            "baseline_sigma_native_px": 1.5,
            "thresholds": {},
            "fit": True,
        },
        "capabilities": {
            "physical_chart_reference": "user_supplied_not_certified",
            "iso_algorithm_conformance": "not_established",
            "color_absolute_calibration": False,
            "qe_dark_ptc_fit": False,
            "max_original_file_bytes": MAX_FILE_BYTES,
        },
    }
    from .esfr_measured import measured_template

    measured = measured_template()
    result["capture_metadata"] = measured["capture_metadata"]
    result["measured_capture_protocol"] = measured
    return result


def _id(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", value):
        raise ValueError(field + " requires a bounded ASCII identity")
    return value


def _metadata(data):
    # canonical also rejects nonfinite numbers; never infer sensor packing.
    if not isinstance(data, dict) or len(canonical(data)) > 128 * 1024:
        raise ValueError("Capture metadata must be a bounded JSON object")
    for key in ("camera_id", "session_id", "acquisition_id"):
        _id(data.get(key), key)
    if data.get("source_kind") not in {"measured", "synthetic"}:
        raise ValueError("source_kind must explicitly be measured or synthetic")
    if data.get("role") not in {"development", "validation"}:
        raise ValueError("role must be development or validation")
    if not isinstance(data.get("conditions"), dict) or not data["conditions"]:
        raise ValueError("Applied capture conditions are required")
    isp = data.get("isp", {})
    if isp.get("coordinate_mapping") not in {"same_native_raster", "unregistered"}:
        raise ValueError("Declare ISP coordinate_mapping same_native_raster or unregistered")
    return json.loads(canonical(data))


def _binding(service, capture, key):
    return service.registry.artifact_binding(
        capture["files"][key]["sha256"], owner_kind=CAPTURES, owner_id=capture["id"]
    )["path"]


def capture(service, identity):
    return service.registry.get(CAPTURES, identity)


def present_capture(record):
    return {**record, "preview_url": f"{ROOT}/captures/{record['id']}/preview.png"}


def import_capture(service, metadata, raw_path, isp_path, *, original_names=None):
    from .esfr_io import RawFrame, read_isp

    metadata = _metadata(metadata)
    if metadata["source_kind"] == "measured":
        from .esfr_measured import validate_measured_metadata

        metadata = validate_measured_metadata(metadata)
        if metadata["role"] == "validation":
            from .esfr_bridge import verify_measured_validation_metadata

            verify_measured_validation_metadata(service, metadata)
    for path in (Path(raw_path), Path(isp_path)):
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_FILE_BYTES:
            raise ValueError("Each original file must be nonempty and at most 128 MiB")
    raw = RawFrame(raw_path, metadata)
    if raw.shape[0] * raw.shape[1] > 40_000_000:
        raise ValueError("This paired Lab accepts at most 40 megapixels per capture")
    isp, isp_info = read_isp(isp_path, metadata["isp"])
    if metadata["source_kind"] == "measured" and (
        isp_info["orientation_tag"] != 1
        or (metadata["isp"]["comparison_mode"] == "matched_bilinear"
            and (isp_info["icc_profile_present"] or isp_info["grayscale_replicated"]))
    ):
        raw.close()
        raise ValueError("Measured native ISP requires identity orientation; matched bilinear additionally requires RGB without an unmodeled ICC profile")
    if metadata["isp"]["coordinate_mapping"] == "same_native_raster" and tuple(
        isp.shape[:2]
    ) != tuple(raw.shape):
        raise ValueError("Same-native-raster ISP must match RAW dimensions; no implicit resize")
    isp_hash = hashlib.sha256()
    for row in range(0, isp.shape[0], 128):
        isp_hash.update(np.asarray(isp[row : row + 128], dtype="<f4").tobytes())
    h = hashlib.sha256()
    h.update(
        canonical(
            {
                "shape": list(raw.shape),
                "geometry": metadata["geometry"],
                "cfa": metadata["raw_format"]["cfa"],
            }
        ).encode()
    )
    minimum, maximum = 65535, 0
    for row in range(0, raw.shape[0], 128):
        part = raw.read([row, 0, min(128, raw.shape[0] - row), raw.shape[1]])
        h.update(np.asarray(part, dtype="<u2").tobytes())
        minimum, maximum = min(minimum, int(part.min())), max(maximum, int(part.max()))
    files = {}
    for name, path in (("raw", raw_path), ("isp", isp_path)):
        original_name = Path((original_names or {}).get(name, Path(path).name)).name
        files[name] = service.registry.store_file(path, original_name, "application/octet-stream")
    # Display only: analysis always returns to the original native samples.
    display = raw.read().astype(np.float32)
    white = float(np.max(metadata["raw_format"]["white_level_dn"]))
    black = float(np.min(metadata["raw_format"]["black_level_dn"]))
    display = np.clip((display - black) / max(1, white - black), 0, 1) ** (1 / 2.2)
    im = Image.fromarray(np.uint8(display * 255), "L").convert("RGB")
    im.thumbnail((1600, 1000))
    with tempfile.TemporaryDirectory() as temp:
        p = Path(temp) / "preview.png"
        im.save(p)
        files["preview"] = service.registry.store_file(p, "preview.png", "image/png")
    doc = {
        "schema": "camera_esfr_capture_v1",
        "metadata": metadata,
        "raw_shape": list(raw.shape),
        "raw_info": raw.info,
        "isp_info": isp_info,
        "native_pixel_sha256": h.hexdigest(),
        "isp_pixel_sha256": isp_hash.hexdigest(),
        "code_range_dn": [minimum, maximum],
        "files": files,
        "artifacts": list(files.values()),
        "physical_identity_verified": False,
    }
    raw.close()
    return present_capture(service.registry.put(CAPTURES, doc))


def detect(service, payload):
    from .esfr_analysis import detect_rois
    from .esfr_io import RawFrame

    c = capture(service, payload["capture_id"])
    raw = RawFrame(_binding(service, c, "raw"), c["metadata"])
    result = detect_rois(raw.read(), payload.get("chart_profile", {}))
    raw.close()
    return {
        **result,
        "capture_id": c["id"],
        "raw_shape": list(raw.shape),
        "preview_url": present_capture(c)["preview_url"],
        "requires_roi_review": True,
    }


def _rois(items, shape):
    if not isinstance(items, list) or not 1 <= len(items) <= 256:
        raise ValueError("Review 1..256 native ROIs before submitting")
    result, ids = [], set()
    for item in items:
        identity = _id(item.get("id"), "ROI id")
        if identity in ids:
            raise ValueError("Duplicate ROI id")
        ids.add(identity)
        box = item.get("box")
        if not isinstance(box, list) or len(box) != 4 or any(type(x) is not int for x in box):
            raise ValueError("ROI box requires [row,col,height,width] integer native coordinates")
        y, x, h, w = box
        if min(y, x) < 0 or min(h, w) < 8 or y + h > shape[0] or x + w > shape[1]:
            raise ValueError("ROI is too small or outside the original RAW raster")
        if item.get("kind") not in {"edge", "gray", "color", "wedge"}:
            raise ValueError("Unsupported ROI kind")
        result.append({**item, "field_id": _id(item.get("field_id", identity), "field_id")})
    return result


def _lineage(c):
    m = c["metadata"]
    return {
        "pixels:" + c["native_pixel_sha256"],
        "raw:" + c["files"]["raw"]["sha256"],
        "isp:" + c["files"]["isp"]["sha256"],
        "isp-pixels:" + c.get("isp_pixel_sha256", c["files"]["isp"]["sha256"]),
        "acquisition:" + m["camera_id"] + ":" + m["acquisition_id"],
        "session:" + m["camera_id"] + ":" + m["session_id"],
        *[
            "acquisition:" + m["camera_id"] + ":" + _id(x, "parent acquisition")
            for x in m.get("parent_acquisition_ids", [])
        ],
    }


def submit(service, payload):
    from .calibration_lab import _source_code, _unique_artifacts

    if service.loaded_environment != _source_code():
        raise ConflictError("Executable source changed; restart before submitting")
    dev = [capture(service, x) for x in payload.get("development_ids", [])]
    val = [capture(service, x) for x in payload.get("validation_ids", [])]
    if not dev or len(dev) + len(val) > 64 or len({c["id"] for c in dev + val}) != len(dev + val):
        raise ValueError("Select unique development captures (64 total maximum)")
    if any(c["metadata"]["role"] != "development" for c in dev) or any(
        c["metadata"]["role"] != "validation" for c in val
    ):
        raise ValueError("Capture roles are immutable; cannot relabel development as validation")
    ref = dev[0]
    for c in dev + val:
        for k in ("camera_id", "conditions", "raw_format", "geometry", "isp", "source_kind"):
            if c["metadata"][k] != ref["metadata"][k]:
                raise ValueError(
                    "Captured conditions/format must match for this paired study: " + k
                )
    dl = set().union(*map(_lineage, dev))
    vl = set().union(*map(_lineage, val)) if val else set()
    if dl & vl:
        raise ValueError("Independent validation overlaps development source/session/lineage")
    rois = _rois(payload.get("rois"), ref["raw_shape"])
    from .esfr_engine import validate_roi_budget

    validate_roi_budget(rois)
    if (
        len(dev + val) * sum(r["kind"] == "edge" for r in rois) > 1024
        or len(dev + val) * len(rois) > 4096
    ):
        raise ValueError(
            "Study exceeds 1024 edge observations or 4096 total ROI observations; "
            "split capture conditions"
        )
    sigma = float(payload.get("baseline_sigma_native_px", 1.5))
    if not np.isfinite(sigma) or not 0.15 <= sigma <= 12:
        raise ValueError("Baseline sigma requires 0.15..12 native pixels")
    thresholds = payload.get("thresholds", {})
    if set(thresholds) - {"raw_sfr_rmse", "isp_sfr_rmse"}:
        raise ValueError("Unsupported threshold")
    if any(
        not isinstance(v, (int, float)) or not np.isfinite(v) or v <= 0 for v in thresholds.values()
    ):
        raise ValueError("Thresholds must be positive finite curve RMSE")
    measured_admission = None
    if ref["metadata"]["source_kind"] == "measured":
        from .esfr_measured import validate_measured_study

        measured_admission = validate_measured_study(
            dev + val, rois, payload.get("chart_profile", {}),
            payload.get("measured_review"), thresholds,
        )
    context = None
    iq_admission_id = payload.get("iq_validation_admission_id")
    if iq_admission_id:
        from .esfr_iq_service import mtf_noise_companions, ADMISSIONS
        from .esfr_bridge import VALIDATION_CONTRACTS
        admission = service.registry.get(ADMISSIONS, iq_admission_id)
        contract = service.registry.get(VALIDATION_CONTRACTS, admission["validation_contract_id"])
        mtf_noise_companions(service, iq_admission_id, contract, [c["id"] for c in val])
    if payload.get("source"):
        from .calibration_db import source_selection

        _, context = source_selection(service, payload["source"])
    doc = {
        "name": str(payload.get("name", "E-SFR Camera Validation"))[:160],
        "job_kind": KIND,
        "plugin_id": "camera_esfr_validation_v1",
        "source_code": _source_code(),
        "specification": {
            "rois": rois,
            "chart_profile": payload.get("chart_profile", template()["chart_profile"]),
            **({"measured_review": payload["measured_review"],
                "measured_admission": measured_admission} if measured_admission else {}),
            "baseline_sigma_native_px": sigma,
            "fit": bool(payload.get("fit", True)),
            "thresholds": thresholds,
            "source": payload.get("source"),
            **({"iq_validation_admission_id": iq_admission_id} if iq_admission_id else {}),
        },
        "source_context": context,
        "capture_snapshots": dev + val,
        "development_ids": [c["id"] for c in dev],
        "validation_ids": [c["id"] for c in val],
        "development_lineage": sorted(dl),
        "validation_lineage": sorted(vl),
        "input_artifacts": _unique_artifacts([a for c in dev + val for a in c["artifacts"]]),
        "default_worker_timeout_seconds": 1800,
    }
    return service.registry.submit(doc, digest(doc), force=True)


def directory(service, identity):
    if not re.fullmatch(r"run-[0-9a-f]{32}", identity):
        raise ValueError("Invalid study id")
    return service.registry.directory / "camera-validation-esfr" / identity


def get(service, identity):
    directory(service, identity)
    row, binding = service.registry.get_run_with_result_binding(identity)
    if row.get("job_kind") != KIND:
        raise ValueError("Not an E-SFR study")
    if row["status"] == "succeeded":
        if binding is None or json.loads(binding["path"].read_bytes()) != row["result"]["summary"]:
            raise IntegrityError("E-SFR result differs from immutable binding")
    else:
        p = directory(service, identity) / "progress.json"
        if p.is_file():
            row["progress"] = json.loads(p.read_text())
    return row


def execute(service, job):
    from .calibration_lab import _source_code, _unique_artifacts
    from .esfr_engine import run
    from .esfr_report import overviews, publish

    if _source_code() != job["source_code"] or service.loaded_environment != job["source_code"]:
        raise IntegrityError("E-SFR executable environment changed")
    out = directory(service, job["id"])
    out.mkdir(parents=True, exist_ok=False)
    chart_profile = job["specification"]["chart_profile"]
    geometry = chart_profile.get("chart_geometry")
    if geometry and geometry.get("asset_sha256"):
        current = chart_template()
        if geometry != current:
            raise IntegrityError("Frozen chart evidence differs from installed chart package")
        for source, target in {
            "generated.svg": "chart.svg",
            "generated-rois.svg": "chart-rois.svg",
            "reference.png": "reference.png",
            "manifest.json": "chart-manifest.json",
        }.items():
            content = (CHART_ASSETS / source).read_bytes()
            if hashlib.sha256(content).hexdigest() != geometry["asset_sha256"][source]:
                raise IntegrityError("Chart asset changed while freezing evidence: " + source)
            (out / target).write_bytes(content)
    captures = []
    for c in job["capture_snapshots"]:
        if capture(service, c["id"]) != c:
            raise IntegrityError("Capture snapshot changed")
        captures.append((c, _binding(service, c, "raw"), _binding(service, c, "isp")))

    def progress(message):
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Study cancelled")
        (out / "progress.json").write_text(canonical({"message": message}))

    t = time.monotonic()
    summary = run(job, captures, out, progress)
    summary["overviews"] = overviews(captures, job["specification"]["rois"], out)
    summary["replay_of_applied_profile"] = job.get("applied_profile_id")
    summary["chart_profile"] = job["specification"]["chart_profile"]
    summary.update(
        run_id=job["id"],
        source_context=job["source_context"],
        source_code=job["source_code"],
        report_url=f"{ROOT}/studies/{job['id']}/files/report.html",
        elapsed_seconds=time.monotonic() - t,
    )
    # Portable evidence includes originals for bounded campaigns; larger inputs
    # remain immutable owned blobs with per-file download URLs and hashes.
    original_size = sum(Path(p).stat().st_size for _, rp, ip in captures for p in (rp, ip))
    archive_originals = original_size <= 256 * 1024**2
    originals = []
    for record, rp, ip in captures:
        for domain, path in (("raw", rp), ("isp", ip)):
            artifact = record["files"][domain]
            name = (
                f"originals/{record['id']}/{domain}{Path(artifact['name']).suffix}"
                if "name" in artifact
                else f"originals/{record['id']}/{domain}.bin"
            )
            if archive_originals:
                target = out / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            originals.append(
                {
                    "capture_id": record["id"],
                    "domain": domain,
                    "sha256": artifact["sha256"],
                    "metadata": record["metadata"],
                    "archive_path": name if archive_originals else None,
                    "download_url": f"{ROOT}/captures/{record['id']}/original/{domain}",
                }
            )
    summary["originals_in_archive"] = archive_originals
    (out / "original-captures.json").write_text(
        canonical(
            {
                "originals_in_archive": archive_originals,
                "total_original_bytes": original_size,
                "files": originals,
            }
        )
    )
    (out / "request.json").write_text(canonical(job))
    (out / "candidate-profile.json").write_text(canonical(summary["candidate_profile"]))
    # Preserve every sampled curve once as a separately hashed evidence file.
    # API/registry summaries must not repeat tens of MB of per-channel arrays.
    details = {key: summary[key] for key in ("observations", "comparisons", "patch_summary")}
    (out / "measurement-details.json").write_text(canonical(details))
    publish(summary, out)
    summary = compact_summary(summary)
    (out / "result.json").write_text(canonical(summary))
    manifest = {
        p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(out.rglob("*"))
        if p.is_file() and p.name != "progress.json"
    }
    if sum((out / name).stat().st_size for name in manifest) > 512 * 1024**2:
        raise ValueError("Study artifact budget exceeds 512 MiB; split the capture conditions")
    (out / "manifest.json").write_text(
        canonical({"schema": "camera_esfr_files_v1", "files": manifest})
    )
    with zipfile.ZipFile(out / "evidence.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in [*manifest, "manifest.json"]:
            z.write(out / name, name)
    files, artifacts = {}, []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "progress.json":
            a = service.registry.store_file(
                p,
                p.relative_to(out).as_posix(),
                mimetypes.guess_type(p.name)[0] or "application/octet-stream",
            )
            files[p.relative_to(out).as_posix()] = a["sha256"]
            artifacts.append(a)
    summary["files"] = files
    binding = service.registry.store_blob(
        canonical(summary).encode(), "esfr-result.json", "application/json"
    )
    if _source_code() != job["source_code"]:
        raise IntegrityError("Source changed during analysis")
    progress("완료 · 차트 측정과 보정 후보·보고서 보존")
    return {"summary": summary, "artifacts": _unique_artifacts([binding, *artifacts])}


def compact_summary(summary):
    """Keep scalar measurements/provenance in API, all curves in owned evidence."""
    curve_keys = {"frequency_cy_native_px", "sfr", "esf", "lsf", "esf_distance_native_px"}

    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in curve_keys}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    result = {
        **summary,
        "observations": strip(summary["observations"]),
        "comparisons": [
            {k: v for k, v in c.items() if k != "curves"} for c in summary["comparisons"]
        ],
        "measurement_details_file": "measurement-details.json",
    }
    # Non-edge observations already exist above; do not duplicate repeats.
    result.pop("patch_summary", None)
    return result


def file(service, identity, relative):
    row = get(service, identity)
    if row["status"] != "succeeded":
        raise ValueError("Study is not complete")
    checksum = row["result"]["summary"]["files"].get(relative)
    if checksum is None:
        raise KeyError(relative)
    if relative.startswith("originals/"):
        # An original is both a job input and an archived output. Resolve the
        # capture owner to avoid ambiguous input/output descriptors on the run.
        for record in row["capture_snapshots"]:
            for domain in ("raw", "isp"):
                if record["files"][domain]["sha256"] == checksum:
                    return _binding(service, record, domain)
        raise IntegrityError("Archived original has no capture owner")
    return service.registry.artifact_binding(checksum, owner_kind="runs", owner_id=identity)["path"]


def active(service, source=None, camera_id=None):
    events = service.registry.list(ACTIVATIONS)
    if source is not None:
        events = [x for x in events if x.get("source") == source]
    if camera_id is not None:
        events = [x for x in events if x["camera_id"] == camera_id]
    if not events:
        return None
    event = max(events, key=lambda x: (x["created_at"], x["id"]))
    profile = service.registry.get(PROFILES, event["profile_id"])
    owner = get(service, profile["run_id"])
    if (
        owner["status"] != "succeeded"
        or digest(owner["result"]["summary"]["candidate_profile"]) != profile["profile_sha256"]
    ):
        raise IntegrityError("Applied E-SFR profile owner/hash mismatch")
    return {**event, "profile": profile}


def apply(service, identity, payload):
    from .calibration_lab import _lock

    row = get(service, identity)
    if row["status"] != "succeeded":
        raise ValueError("Only completed studies can be applied")
    s = row["result"]["summary"]
    p = s["candidate_profile"]
    if not p["fields"] or s["fit"]["status"] != "fitted":
        raise ValueError("No usable fitted response candidate")
    if s["validation"]["status"] != "pass":
        raise ValueError("Independent validation must pass explicit thresholds before activation")
    # Scope is measured chart/camera response, never a replacement optical prescription.
    with _lock(service):
        old = active(service, row["specification"]["source"], p["camera_id"])
        expected = old["profile_id"] if old else None
        if "expected_active_id" not in payload or payload["expected_active_id"] != expected:
            raise ConflictError("Active scoped E-SFR profile changed; refresh before applying")
        doc = service.registry.put(
            PROFILES,
            {
                "schema": "camera_esfr_model_v1",
                "run_id": identity,
                "camera_profile": p,
                "profile_sha256": digest(p),
                "source": row["specification"]["source"],
                "source_context": row["source_context"],
                "scope": "chart_conditioned_effective_response",
                "physical_validation": s["physical_validation"],
                "validation": s["validation"],
            },
        )
        event = service.registry.put(
            ACTIVATIONS,
            {
                "profile_id": doc["id"],
                "run_id": identity,
                "camera_id": p["camera_id"],
                "source": row["specification"]["source"],
                "previous_profile_id": expected,
                "scope": "esfr_only_no_prescription_overwrite",
            },
        )
        return {**doc, "activation": event}


def replay_applied(service, identity, payload):
    from .calibration_lab import _source_code

    original = get(service, identity)
    if original["status"] != "succeeded":
        raise ValueError("Completed study required")
    camera = original["result"]["summary"]["candidate_profile"]["camera_id"]
    selected = active(service, original["specification"]["source"], camera)
    if not selected or selected["profile_id"] != payload.get("profile_id"):
        raise ConflictError("The selected profile is not currently active for this camera/source")
    profile = selected["profile"]["camera_profile"]
    if service.loaded_environment != _source_code():
        raise ConflictError("Restart changed executable source")
    doc = {
        k: original[k]
        for k in (
            "specification",
            "source_context",
            "capture_snapshots",
            "development_ids",
            "validation_ids",
            "development_lineage",
            "validation_lineage",
            "input_artifacts",
        )
    }
    doc.update(
        name="적용 E-SFR 프로파일 재실행",
        job_kind=KIND,
        plugin_id="camera_esfr_validation_v1",
        source_code=_source_code(),
        frozen_profile_snapshot=profile,
        parent_study_id=identity,
        applied_profile_id=selected["profile_id"],
        default_worker_timeout_seconds=1800,
    )
    return service.registry.submit(doc, digest(doc), force=True)


def install_routes(app, service):
    from fastapi.responses import FileResponse

    @app.get(ROOT + "/template")
    def templates():
        return template()

    @app.get(ROOT + "/chart-template")
    def chart_get():
        return chart_template()

    @app.post(ROOT + "/chart-registration")
    def chart_registration(payload: dict):
        from .esfr_chart_registration import register_chart

        record = capture(service, payload["capture_id"])
        result = register_chart(
            chart_template(), payload.get("fiducials_native_xy"), record["raw_shape"]
        )
        result["registration"].update(
            capture_id=record["id"], raw_sha256=record["files"]["raw"]["sha256"]
        )
        return result

    @app.get(ROOT + "/chart-assets/{name}")
    def chart_asset(name: str):
        if name not in CHART_FILES:
            raise ValueError("Unknown frozen chart asset")
        return FileResponse(CHART_ASSETS / name)

    @app.get(ROOT + "/captures")
    def captures():
        return [present_capture(x) for x in service.registry.list(CAPTURES)]

    @app.get(ROOT + "/captures/{identity}")
    def capture_get(identity: str):
        return present_capture(capture(service, identity))

    @app.get(ROOT + "/captures/{identity}/preview.png")
    def preview(identity: str):
        return FileResponse(
            _binding(service, capture(service, identity), "preview"), media_type="image/png"
        )

    @app.get(ROOT + "/captures/{identity}/original/{domain}")
    def original(identity: str, domain: str):
        if domain not in {"raw", "isp"}:
            raise ValueError("Original domain must be raw or isp")
        record = capture(service, identity)
        return FileResponse(
            _binding(service, record, domain),
            media_type="application/octet-stream",
            filename=record["files"][domain].get("name", domain + ".bin"),
        )

    @app.post(ROOT + "/captures", status_code=201)
    async def upload(
        metadata: Annotated[str, Form()],
        raw: Annotated[UploadFile, File()],
        isp: Annotated[UploadFile, File()],
    ):
        from .calibration_lab import _json

        with tempfile.TemporaryDirectory(prefix="camera-esfr-import-") as temp:
            paths = []
            for i, f in enumerate((raw, isp)):
                p = Path(temp) / (str(i) + Path(f.filename or "image.bin").suffix.lower())
                size = 0
                with p.open("wb") as stream:
                    while chunk := await f.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise ValueError("Original file exceeds 128 MiB")
                        stream.write(chunk)
                paths.append(p)
            return import_capture(
                service,
                _json(metadata.encode()),
                *paths,
                original_names={
                    "raw": raw.filename or "capture.raw",
                    "isp": isp.filename or "capture.tiff",
                },
            )

    @app.post(ROOT + "/detect")
    def detection(payload: dict):
        return detect(service, payload)

    @app.post(ROOT + "/studies", status_code=202)
    def study_create(payload: dict):
        return submit(service, payload)

    @app.get(ROOT + "/studies")
    def studies():
        return [
            {
                "id": r["id"],
                "name": r.get("name"),
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in service.registry.list_runs(job_kinds={KIND})
        ]

    @app.get(ROOT + "/studies/{identity}")
    def study_get(identity: str):
        return get(service, identity)

    @app.post(ROOT + "/studies/{identity}/apply")
    def study_apply(identity: str, payload: dict):
        return apply(service, identity, payload)

    @app.post(ROOT + "/studies/{identity}/replay-applied", status_code=202)
    def study_replay(identity: str, payload: dict):
        return replay_applied(service, identity, payload)

    @app.post(ROOT + "/studies/{identity}/export-calibration", status_code=201)
    def study_export(identity: str, payload: dict):
        from .esfr_bridge import export

        record = export(service, identity, aggregate_repeats=payload.get("aggregate_repeats", True))
        return {**record, "lab_url": record["calibration_lab_url"]}

    @app.post(ROOT + "/validation-contracts", status_code=201)
    def validation_contract_create(payload: dict):
        from .esfr_bridge import create_validation_contract

        if set(payload) != {"parent_study_id"}:
            raise ValueError("Validation contract requires parent_study_id only")
        return create_validation_contract(service, payload["parent_study_id"])

    @app.get(ROOT + "/validation-contracts/{identity}")
    def validation_contract_get(identity: str):
        from .esfr_bridge import VALIDATION_CONTRACTS, _verify_validation_contract

        record = service.registry.get(VALIDATION_CONTRACTS, identity)
        return _verify_validation_contract(service, identity, record["parent_study_id"])[0]

    @app.post(ROOT + "/studies/{identity}/export-validation", status_code=201)
    def fresh_validation_export(identity: str, payload: dict):
        from .esfr_bridge import export_validation

        if set(payload) != {"parent_study_id", "validation_contract_id"}:
            raise ValueError("Fresh validation export requires parent_study_id and validation_contract_id")
        return export_validation(service, identity, **payload)

    @app.post(ROOT + "/db-baseline")
    def baseline_import(payload: dict):
        from .esfr_bridge import db_baseline

        return db_baseline(
            service, payload["source"], payload["metadata"], payload.get("field_mapping")
        )

    @app.get(ROOT + "/studies/{identity}/files/{relative:path}")
    def study_file(identity: str, relative: str):
        return FileResponse(
            file(service, identity, relative),
            media_type=mimetypes.guess_type(relative)[0] or "application/octet-stream",
        )

    @app.get(ROOT + "/calibration-completion")
    def full_edge_completion(source_run: str, candidate: str, condition: str | None = None):
        from .calibration_esfr_replay import linked_completion

        source = {"run_id": source_run, "candidate_id": candidate}
        if condition:
            source["condition_id"] = condition
        return linked_completion(service, source)

    @app.get(ROOT + "/active")
    def current(
        source_run: str | None = None,
        candidate: str | None = None,
        condition: str | None = None,
        camera_id: str | None = None,
    ):
        source = {"run_id": source_run, "candidate_id": candidate} if source_run else None
        if source is not None and condition:
            source["condition_id"] = condition
        return active(service, source, camera_id)

    @app.post(ROOT + "/demo", status_code=202)
    def demo(payload: dict):
        from .esfr_demo import create

        return create(service, payload)
