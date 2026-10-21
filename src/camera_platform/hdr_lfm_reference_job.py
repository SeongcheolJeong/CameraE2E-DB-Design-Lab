"""Source-pinned RAW and radiance-reference comparison for PWM HDR research.

This compares aggregate observations from an independently supplied acquisition
with one completed Lens x Sensor PWM HDR job.  A virtual 8..32 pixel model
window has no established native-die crop, so spatial registration, product
accuracy and HDR/LFM approval are deliberately outside this comparison.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from copy import deepcopy
from typing import Annotated, Any

import numpy as np
from fastapi import File, Form, UploadFile

from .hdr_lfm_reference_packet import SCHEMA as PACKET_SCHEMA
from .hdr_lfm_reference_packet import contract as packet_contract
from .hdr_lfm_reference_packet import validate_packet
from .hdr_lfm_registration_qualification import SCHEMA as QUALIFICATION_SCHEMA
from .hdr_lfm_registration_qualification import contract as qualification_contract
from .hdr_lfm_registration_qualification import qualify
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_hdr_lfm_reference_comparison_v1"
JOB_KIND = "hdr_lfm_reference_comparison"
MAX_NPY_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
LIMITATION = (
    "Supplied source evidence is SHA-256 pinned; new packet logs are checked for "
    "internal consistency and global per-frame/per-phase means are compared. "
    "Source authenticity, timing values, phase registration, "
    "native crop, lens radiometry, pixel angular response, measurement uncertainty "
    "and physical HDR/LFM accuracy are not established. No pixelwise comparison "
    "or product acceptance decision is made."
)


def _sha(value: Any, name: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError(f"{name} must be a nonblank bounded string")
    return value.strip()


def _equal_numbers(actual: Any, expected: Any, name: str) -> None:
    if (type(actual) not in (int, float) or not math.isfinite(actual)
            or not math.isclose(float(actual), float(expected), rel_tol=0, abs_tol=1e-12)):
        raise ValueError(f"{name} differs from the executed simulation condition")


def _source(service, identity: str) -> tuple[dict, dict, dict]:
    from .lens_sensor_lfm_job import TIMED_BOUNDED_SCHEMA
    from .lens_sensor_lfm_job import get as get_lfm
    from .lens_sensor_pwm_hdr_job import get as get_hdr

    hdr = get_hdr(service, identity)
    if hdr["status"] != "succeeded":
        raise ValueError("Reference comparison requires completed PWM HDR RAW")
    lfm = get_lfm(service, hdr["source_lfm_job_id"])
    if lfm["status"] != "succeeded" or lfm.get("plugin_id") != TIMED_BOUNDED_SCHEMA:
        raise ValueError("Reference comparison needs the source-bound timed LFM Job")
    mode = lfm.get("timing_mode") or {}
    if mode.get("provenance", {}).get("source_kind") not in {
        "vendor_document_declared", "measurement_declared"
    }:
        raise ValueError("Authored timing is not a source-backed operating mode")
    if hdr["source_identities"] != lfm["source_identities"]:
        raise IntegrityError("PWM HDR and source LFM identities differ")
    return hdr, lfm, mode


def template(service, identity: str) -> dict:
    """An exact intake binding from a real completed RAW/HDR pair."""
    from pyisetcam.sensor_simulation_db import sensor_simulation_db_get

    hdr, lfm, mode = _source(service, identity)
    report = hdr["result"]["summary"]["report"]
    source = lfm["result"]["summary"]["report"]
    revision = hdr["source_identities"]["sensor"]
    saved = sensor_simulation_db_get(
        revision["report_code"], root=service.workspace / "camerae2e_db",
        run_id=revision["run_id"],
    )
    readout_profile = saved["metadata"]["readout_profile"]
    controls = source["controls"]
    shape = report["original_pwm_raw_shape"]
    return {
        "schema": SCHEMA,
        "reference_comparison_available": True,
        "submission_endpoint": "/api/v1/hdr-lfm-reference-comparisons",
        "metadata": {
            "schema": SCHEMA,
            "source_hdr_job_id": identity,
            "source_hdr_report_sha256": digest(report),
            "sensor_revision": deepcopy(revision),
            "physical_instance": "",
            "instrument": {"id": "", "calibration_revision": ""},
            "mode": {
                "mode_id": mode["mode_id"],
                "timing_mode_sha256": lfm["timing_mode_sha256"],
                "operating_point": readout_profile["operating_point"],
                "shutter": "rolling",
                "line_time_s": controls["line_time_s"],
                "native_crop_origin_row_col": None,
                "phase_alignment": "declared_phase_index_only_not_verified",
            },
            "stimulus": {
                "pwm_period_s": controls["pwm_period_s"],
                "duty_cycle": controls["duty_cycle"],
                "off_to_on_ratio": controls["off_to_on_ratio"],
                "on_luminance_cd_m2": controls["on_luminance_cd_m2"],
                "phase_fractions_of_period": controls["phase_fractions_of_period"],
            },
            "raw_acquisition": {
                "exposure_s": controls["exposure_s"],
                "shape": shape,
                "dtype": "int32", "unit": "DN",
                "bit_depth": lfm["result"]["summary"]["report"]["controls"]
                ["saved_readout"]["bit_depth"],
                "black_level_dn": controls["saved_readout"]["black_level_dn"],
                "source_uri": "",
            },
            "reference_rate": {
                "shape": shape[1:], "dtype": "float64",
                "unit": "electron/pixel/s at PWM on luminance",
                "method": "independent_calibrated_reference_declared",
                "source_uri": "",
            },
            "sources": {
                key: {"uri": "", "sha256": ""}
                for key in ("mode_log", "waveform_log", "reference_log")
            },
            "acknowledge_comparison_limits": False,
        },
        "file_parts": {
            "measured_raw": {"format": "npy", "dtype": "int32", "shape": shape},
            "measured_reference_rate": {
                "format": "npy", "dtype": "float64", "shape": shape[1:],
            },
            "mode_log": {"format": "UTF-8 JSON", "max_bytes": MAX_DOCUMENT_BYTES},
            "waveform_log": {"format": "UTF-8 JSON", "max_bytes": MAX_DOCUMENT_BYTES},
            "reference_log": {"format": "UTF-8 JSON", "max_bytes": MAX_DOCUMENT_BYTES},
        },
        "packet_contract": packet_contract(),
        "registration_qualification_contract": qualification_contract(),
        "limitation": LIMITATION,
    }


def _load_array(content: bytes, expected_shape: list[int], dtype: str, name: str) -> np.ndarray:
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_NPY_BYTES:
        raise ValueError(f"{name} NPY bytes missing or over 8 MiB")
    if not content.startswith(b"\x93NUMPY"):
        raise ValueError(f"{name} must be an uncompressed NPY array")
    try:
        stream = io.BytesIO(content)
        version = np.lib.format.read_magic(stream)
        if version == (1, 0):
            shape, fortran_order, actual_dtype = np.lib.format.read_array_header_1_0(stream)
        elif version == (2, 0):
            shape, fortran_order, actual_dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError("unsupported NPY header version")
        if (list(shape) != expected_shape or actual_dtype != np.dtype(dtype)
                or fortran_order or len(content) - stream.tell()
                != math.prod(expected_shape) * np.dtype(dtype).itemsize):
            raise ValueError("header or payload differs from the bounded NPY contract")
        array = np.load(io.BytesIO(content), allow_pickle=False)
    except (OSError, ValueError, EOFError, SyntaxError) as exc:
        raise ValueError(f"{name} NPY cannot be decoded") from exc
    if list(array.shape) != expected_shape or array.dtype != np.dtype(dtype):
        raise ValueError(f"{name} shape or dtype differs from the bound intake")
    if dtype == "float64" and (not np.isfinite(array).all() or np.any(array < 0)):
        raise ValueError(f"{name} rates must be finite and nonnegative")
    return array


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate reference metadata key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"Nonfinite reference metadata value: {value}")


def _validate(
    service, metadata: dict, files: dict[str, bytes], *,
    require_structured_packet: bool = False,
) -> tuple[dict, dict, dict, dict | None]:
    if not isinstance(metadata, dict) or set(metadata) != {
        "schema", "source_hdr_job_id", "source_hdr_report_sha256", "sensor_revision",
        "physical_instance", "instrument", "mode", "stimulus", "raw_acquisition",
        "reference_rate", "sources", "acknowledge_comparison_limits",
    } or metadata.get("schema") != SCHEMA:
        raise ValueError("Declare exactly the HDR/LFM reference comparison metadata")
    if metadata["acknowledge_comparison_limits"] is not True:
        raise ValueError("Acknowledge non-product comparison limits")
    hdr, lfm, mode = _source(service, metadata["source_hdr_job_id"])
    expected = template(service, hdr["id"])["metadata"]
    for key in (
        "source_hdr_job_id", "source_hdr_report_sha256", "sensor_revision",
        "stimulus",
    ):
        if canonical(metadata[key]) != canonical(expected[key]):
            raise ValueError(f"Reference {key} differs from the source RAW/HDR Job")
    source_uris = set()
    for key in ("raw_acquisition", "reference_rate"):
        observed = metadata[key]
        if not isinstance(observed, dict) or set(observed) != set(expected[key]):
            raise ValueError(f"Reference {key} fields differ from the intake contract")
        if any(canonical(observed[field]) != canonical(expected[key][field])
               for field in expected[key] if field != "source_uri"):
            raise ValueError(f"Reference {key} differs from the source RAW/HDR Job")
        uri = _text(observed["source_uri"], f"{key}.source_uri")
        if uri.startswith("authored://") or uri in source_uris:
            raise ValueError("RAW and reference need distinct non-authored source URIs")
        source_uris.add(uri)
    selected_mode = metadata["mode"]
    if not isinstance(selected_mode, dict) or set(selected_mode) != set(expected["mode"]):
        raise ValueError("Declare the exact bound physical acquisition mode fields")
    for key in ("mode_id", "timing_mode_sha256", "operating_point", "shutter",
                "phase_alignment"):
        if selected_mode[key] != expected["mode"][key]:
            raise ValueError(f"Mode {key} differs from the selected source-bound mode")
    _equal_numbers(selected_mode["line_time_s"], expected["mode"]["line_time_s"],
                   "line_time_s")
    origin = selected_mode["native_crop_origin_row_col"]
    native_size = lfm["native_cfa"]["native_size"]
    raw_shape = expected["raw_acquisition"]["shape"]
    if (not isinstance(origin, list) or len(origin) != 2
            or any(type(value) is not int for value in origin)
            or any(value < 0 for value in origin)
            or any(origin[i] + raw_shape[i + 2] > native_size[i] for i in range(2))):
        raise ValueError("Declare a real native crop inside the selected Sensor array")
    _text(metadata["physical_instance"], "physical_instance")
    instrument = metadata["instrument"]
    if not isinstance(instrument, dict) or set(instrument) != {"id", "calibration_revision"}:
        raise ValueError("Instrument needs ID and calibration revision")
    for key in instrument:
        _text(instrument[key], f"instrument.{key}")
    if set(files) != {"measured_raw", "measured_reference_rate", "mode_log",
                      "waveform_log", "reference_log"}:
        raise ValueError("Provide original RAW, independent reference and three source logs")
    raw = _load_array(files["measured_raw"], raw_shape, "int32", "measured_raw")
    if np.any(raw < 0) or np.any(raw >= 2 ** expected["raw_acquisition"]["bit_depth"]):
        raise ValueError("Measured RAW contains codes outside the bound ADC range")
    with np.load(service.registry.blob_path(
        hdr["result"]["summary"]["evidence"]["npz"]["sha256"]
    ), allow_pickle=False) as source_arrays:
        if np.array_equal(raw, source_arrays["original_pwm_raw_dn"]):
            raise ValueError("Measured RAW exactly copies the source simulation RAW")
    reference_rate = _load_array(
        files["measured_reference_rate"], expected["reference_rate"]["shape"],
        "float64", "measured_reference_rate",
    )
    sources = metadata["sources"]
    if not isinstance(sources, dict) or set(sources) != {
        "mode_log", "waveform_log", "reference_log"
    }:
        raise ValueError("Declare mode, waveform and independent reference source logs")
    seen = set()
    for key in sources:
        declaration = sources[key]
        if not isinstance(declaration, dict) or set(declaration) != {"uri", "sha256"}:
            raise ValueError(f"{key} needs URI and SHA-256")
        uri = _text(declaration["uri"], f"{key}.uri")
        if uri.startswith("authored://") or uri in source_uris:
            raise ValueError(f"{key} needs a distinct non-authored source URI")
        source_uris.add(uri)
        if not isinstance(files[key], bytes) or not 0 < len(files[key]) <= MAX_DOCUMENT_BYTES:
            raise ValueError(f"{key} source bytes missing or over 2 MiB")
        sha = _sha(declaration["sha256"], f"{key}.sha256")
        if hashlib.sha256(files[key]).hexdigest() != sha or sha in seen:
            raise ValueError(f"{key} source bytes/hash must be exact and distinct")
        seen.add(sha)
    # The source itself remains a declaration: a hash does not authenticate its
    # author, verify a line-time value, or make the reference independent.
    packet_receipt = None
    if require_structured_packet:
        packet_receipt = validate_packet(
            metadata, files, raw=raw, reference_rate=reference_rate,
            source_lfm_report=lfm["result"]["summary"]["report"],
            native_cfa=lfm["native_cfa"],
        )
    return hdr, lfm, mode, packet_receipt


def submit(service, metadata: dict, files: dict[str, bytes]) -> dict:
    hdr, lfm, mode, packet_receipt = _validate(
        service, metadata, files, require_structured_packet=True,
    )
    from .imaging import _current

    qualification_receipt = qualify(
        metadata, files, packet_receipt=packet_receipt,
        hdr_report=hdr["result"]["summary"]["report"],
        source_lfm_report=lfm["result"]["summary"]["report"],
    )

    artifacts = {
        key: service.registry.store_blob(
            content, f"hdr-lfm-reference-{key}.{'npy' if key.startswith('measured_') else 'bin'}",
            "application/octet-stream",
        )
        for key, content in files.items()
    }
    frozen = {
        "name": f"HDR/LFM reference comparison: {mode['mode_id']}",
        "job_kind": JOB_KIND, "plugin_id": SCHEMA,
        "specification": deepcopy(metadata),
        "source_hdr_content_hash": hdr["content_hash"],
        "source_hdr_report_sha256": digest(hdr["result"]["summary"]["report"]),
        "reference_artifacts": artifacts,
        "packet_contract_schema": PACKET_SCHEMA,
        "packet_consistency_receipt": packet_receipt,
        "registration_qualification_schema": QUALIFICATION_SCHEMA,
        "registration_qualification_receipt": qualification_receipt,
        "environment": _current(service), "limitation": LIMITATION,
    }
    return service.registry.submit(frozen, digest(frozen))


def _packet_schema(job: dict, summary: dict | None = None) -> str | None:
    schema = job.get("packet_contract_schema")
    if schema not in (None, PACKET_SCHEMA):
        raise IntegrityError("Reference packet contract is unsupported")
    if schema is None and (
        "packet_consistency_receipt" in job
        or (summary is not None and summary.get("packet_consistency", {}).get(
            "internal_consistency"
        ) == "verified_against_uploaded_arrays_and_frozen_job")
    ):
        raise IntegrityError("Structured reference packet marker was removed")
    if schema == PACKET_SCHEMA and not isinstance(
        job.get("packet_consistency_receipt"), dict
    ):
        raise IntegrityError("Structured reference packet has no frozen receipt")
    return schema


def _qualification_schema(job: dict, summary: dict | None = None) -> str | None:
    schema = job.get("registration_qualification_schema")
    if schema is None:
        if ("registration_qualification_receipt" in job
                or (summary is not None and "registration_qualification" in summary)):
            raise IntegrityError("HDR/LFM registration qualification marker was removed")
        return None
    if (schema != QUALIFICATION_SCHEMA or job.get("packet_contract_schema") != PACKET_SCHEMA
            or not isinstance(job.get("registration_qualification_receipt"), dict)):
        raise IntegrityError("HDR/LFM registration qualification contract is invalid")
    return schema


def _phase_rate_means(model_rate: np.ndarray, valid: np.ndarray,
                      measured_rate: np.ndarray) -> list[dict]:
    """Describe two unregistered supports without indexing reference by model mask."""
    if (model_rate.shape != measured_rate.shape or valid.shape != model_rate.shape
            or model_rate.ndim != 3 or model_rate.shape[0] != 4):
        raise ValueError("Phase rate comparison requires matching four-phase shapes")
    phases = []
    for phase in range(4):
        eligible = valid[phase]
        phases.append({
            "phase_index": phase,
            "model_valid_pixel_count": int(np.count_nonzero(eligible)),
            "model_mean_rate_on_model_valid_support": (
                float(np.mean(model_rate[phase][eligible])) if np.any(eligible) else None
            ),
            "reference_mean_rate_on_whole_phase": float(np.mean(measured_rate[phase])),
            "support_relation": "unregistered_different_supports_no_pixelwise_error",
        })
    return phases


def execute(service, job: dict) -> dict:
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("HDR/LFM reference comparison is not running")
    artifacts = job["reference_artifacts"]
    files = {}
    for key, descriptor in artifacts.items():
        content = service.registry.blob_path(descriptor["sha256"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != descriptor["sha256"]:
            raise IntegrityError(f"Reference {key} bytes changed before comparison")
        files[key] = content
    contract_schema = _packet_schema(job)
    qualification_schema = _qualification_schema(job)
    hdr, lfm, _, packet_receipt = _validate(
        service, job["specification"], files,
        require_structured_packet=contract_schema == PACKET_SCHEMA,
    )
    if contract_schema == PACKET_SCHEMA and packet_receipt != job.get(
        "packet_consistency_receipt"
    ):
        raise IntegrityError("Reference packet differs from its frozen consistency receipt")
    qualification = None
    if qualification_schema == QUALIFICATION_SCHEMA:
        qualification = qualify(
            job["specification"], files, packet_receipt=packet_receipt,
            hdr_report=hdr["result"]["summary"]["report"],
            source_lfm_report=lfm["result"]["summary"]["report"],
        )
        if qualification != job["registration_qualification_receipt"]:
            raise IntegrityError("Reference registration qualification changed after submission")
    if (hdr["content_hash"] != job["source_hdr_content_hash"]
            or digest(hdr["result"]["summary"]["report"])
            != job["source_hdr_report_sha256"]):
        raise IntegrityError("Source HDR RAW/report changed before comparison")
    report = hdr["result"]["summary"]["report"]
    with np.load(service.registry.blob_path(
        hdr["result"]["summary"]["evidence"]["npz"]["sha256"]
    ), allow_pickle=False) as archive:
        model_raw = np.asarray(archive["original_pwm_raw_dn"]).copy()
        model_rate = np.asarray(archive["reconstructed_on_luminance_rate"]).copy()
        valid = np.asarray(archive["reconstructed_valid_mask"]).copy()
    measured_raw = _load_array(files["measured_raw"], list(model_raw.shape), "int32", "RAW")
    measured_rate = _load_array(
        files["measured_reference_rate"], list(model_rate.shape), "float64", "reference"
    )
    if list(model_rate.shape) != list(measured_rate.shape) or valid.shape != model_rate.shape:
        raise IntegrityError("Source HDR rate/mask shape differs from the reference contract")
    readout = service.registry.get_run(hdr["source_lfm_job_id"])["result"]["summary"]["report"]
    black = float(readout["controls"]["saved_readout"]["black_level_dn"])
    # Whole-frame and whole-phase means are not spatially registered. Per-pixel
    # error would falsely claim a known native crop/scene correspondence.
    model_mean = np.mean(model_raw.astype(np.float64) - black, axis=(-2, -1))
    measured_mean = np.mean(measured_raw.astype(np.float64) - black, axis=(-2, -1))
    phase_rates = _phase_rate_means(model_rate, valid, measured_rate)
    comparison = {
        "schema": SCHEMA, "source_hdr_job_id": hdr["id"],
        "source_hdr_report_sha256": digest(report),
        "reference_metadata_sha256": digest(job["specification"]),
        "reference_artifacts": artifacts,
        "raw_mean_black_relative_dn": {
            "model": model_mean.tolist(), "reference": measured_mean.tolist(),
            "difference_reference_minus_model": (measured_mean - model_mean).tolist(),
        },
        "phase_rate_means": phase_rates,
        "packet_consistency": packet_receipt if packet_receipt is not None else {
            "internal_consistency": "not_assessed_legacy_opaque_logs",
            "physical_validation": "not_established",
        },
        **({"registration_qualification": qualification} if qualification is not None else {}),
        "comparison_scope": "unregistered_global_means_only",
        "source_authenticity": "declared_not_independently_verified",
        "timing_value_verification": "declared_not_independently_verified",
        "physical_validation": "not_established",
        "product_hdr_lfm_decision": "inconclusive",
        "limitation": LIMITATION,
    }
    report_artifact = service.registry.store_blob(
        canonical(comparison).encode(), "hdr-lfm-reference-comparison.json", "application/json"
    )
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("HDR/LFM reference comparison was cancelled")
    return {"summary": comparison, "artifacts": [*artifacts.values(), report_artifact]}


def get(service, identity: str) -> dict:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected Job is not an HDR/LFM reference comparison")
    if job["status"] == "succeeded":
        result = job["result"]
        summary = result["summary"]
        files = {}
        for key, descriptor in job["reference_artifacts"].items():
            files[key] = service.registry.blob_path(descriptor["sha256"]).read_bytes()
            if hashlib.sha256(files[key]).hexdigest() != descriptor["sha256"]:
                raise IntegrityError("Reference input bytes changed")
        contract_schema = _packet_schema(job, summary)
        qualification_schema = _qualification_schema(job, summary)
        hdr, lfm, _, packet_receipt = _validate(
            service, job["specification"], files,
            require_structured_packet=contract_schema == PACKET_SCHEMA,
        )
        if contract_schema == PACKET_SCHEMA and (
            packet_receipt != job.get("packet_consistency_receipt")
            or summary.get("packet_consistency") != packet_receipt
        ):
            raise IntegrityError("Reference packet result differs from frozen logs")
        if qualification_schema == QUALIFICATION_SCHEMA:
            qualification = qualify(
                job["specification"], files, packet_receipt=packet_receipt,
                hdr_report=hdr["result"]["summary"]["report"],
                source_lfm_report=lfm["result"]["summary"]["report"],
            )
            if (qualification != job["registration_qualification_receipt"]
                    or qualification != summary.get("registration_qualification")):
                raise IntegrityError("Reference registration qualification differs from source")
        if (summary["reference_metadata_sha256"] != digest(job["specification"])
                or summary["reference_artifacts"] != job["reference_artifacts"]
                or summary["source_hdr_report_sha256"] != job["source_hdr_report_sha256"]
                or hdr["content_hash"] != job["source_hdr_content_hash"]):
            raise IntegrityError("Reference comparison differs from its frozen source")
        for descriptor in result["artifacts"]:
            content = service.registry.blob_path(descriptor["sha256"]).read_bytes()
            if hashlib.sha256(content).hexdigest() != descriptor["sha256"]:
                raise IntegrityError("Reference comparison artifact bytes changed")
        by_name = {a["name"]: a for a in result["artifacts"]}
        if service.registry.blob_path(
            by_name["hdr-lfm-reference-comparison.json"]["sha256"]
        ).read_bytes() != canonical(summary).encode():
            raise IntegrityError("Reference comparison summary artifact changed")
    return job


def install_routes(app, service) -> None:
    @app.get("/api/v1/hdr-lfm-reference-comparisons/template/{identity}", tags=["hdr_lfm"])
    def intake_template(identity: str):
        return template(service, identity)

    @app.post("/api/v1/hdr-lfm-reference-comparisons", status_code=202, tags=["hdr_lfm"])
    async def create(
        metadata: Annotated[str, Form()],
        measured_raw: Annotated[UploadFile, File()],
        measured_reference_rate: Annotated[UploadFile, File()],
        mode_log: Annotated[UploadFile, File()],
        waveform_log: Annotated[UploadFile, File()],
        reference_log: Annotated[UploadFile, File()],
    ):
        if len(metadata.encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError("Reference metadata exceeds 2 MiB")
        try:
            document = json.loads(
                metadata, object_pairs_hook=_unique_pairs, parse_constant=_reject_nonfinite
            )
        except json.JSONDecodeError as exc:
            raise ValueError("Reference metadata must be JSON") from exc
        files = {
            "measured_raw": await measured_raw.read(MAX_NPY_BYTES + 1),
            "measured_reference_rate": await measured_reference_rate.read(MAX_NPY_BYTES + 1),
            "mode_log": await mode_log.read(MAX_DOCUMENT_BYTES + 1),
            "waveform_log": await waveform_log.read(MAX_DOCUMENT_BYTES + 1),
            "reference_log": await reference_log.read(MAX_DOCUMENT_BYTES + 1),
        }
        return submit(service, document, files)

    @app.get("/api/v1/hdr-lfm-reference-comparisons/{identity}", tags=["hdr_lfm"])
    def detail(identity: str):
        return get(service, identity)
