"""Explicit admission contracts for a fixed-camera, native-raster measured study.

Declarations and signed-off ROI reviews are retained as operator attestations.
They do not authenticate camera identity, timestamps, chart print calibration or
ISO conformance. Synthetic inputs must keep their synthetic source classification.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math

from .registry import canonical, digest

SCHEMA = "camera_esfr_measured_admission_v1"
CHART_MODEL = "imatest_extended_esfr_2023_16_9"
MATCHED_PROCESSING = {
    "operator_id": "esfr_normalized_bilinear_v1",
    "white_balance": [1.0, 1.0, 1.0],
    "color_matrix": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    "denoise": "none", "sharpening": "none", "tone_mapping": "none",
    "lens_shading": "none", "geometric_transform": "none",
}
RAW_PROCESSING = {"linear_response": True, "demosaiced": False, "denoised": False,
                  "sharpened": False, "companded": False, "geometric_transform": False}


def isp_is_diagnostic(metadata):
    """Unknown physical processing cannot become a reference-ISP acceptance test."""
    isp = metadata.get("isp", {})
    if isp.get("comparison_mode") == "diagnostic":
        return True
    return metadata.get("source_kind") == "measured" and not (
        isp.get("comparison_mode") == "matched_bilinear" and isp.get("processing") == MATCHED_PROCESSING
    )


def measured_template():
    """An intentionally incomplete acquisition form, never a sample measurement."""
    return {
        "schema": SCHEMA,
        "capture_metadata": {
            "schema": "camera_esfr_capture_v1", "source_kind": "measured",
            "camera_id": None, "session_id": None, "acquisition_id": None,
            "role": "development", "captured_at": None, "pixel_pitch_um": None,
            "frame_pair": {"pair_id": None, "raw_frame_id": None, "isp_frame_id": None,
                           "same_exposure": None},
            "settings_provenance": {"mode": None, "reference": None},
            "conditions": {"mode_id": None, "gain": None, "exposure_s": None,
                "temperature_c": None, "light_id": None, "aperture_f_number": None,
                "focus_distance_m": None, "focus_setting": None, "distance_m": None},
            "capture_observations": {"temperature_c": None, "illuminance_lux": None},
            "raw_format": {"packing": None, "width": None, "height": None,
                "stride_bytes": None, "header_bytes": None, "byte_order": None,
                "bit_depth": None, "bit_shift": None, "cfa": None,
                "black_level_dn": None, "white_level_dn": None},
            "raw_processing": {key: None for key in RAW_PROCESSING},
            "geometry": {"full_sensor_shape": [None, None], "crop_origin": [None, None]},
            "sampling": {"binning": [None, None], "subsampling": [None, None],
                         "resized": None, "rotation_deg": None, "mirrored": None},
            "isp": {"transfer": None, "coordinate_mapping": None,
                    "comparison_mode": "diagnostic", "processing": {"operator_id": "unknown"}},
        },
        "validation_additions": {
            "role": "validation", "validation_contract": {"id": None, "content_hash": None},
            "acquisition_attestation": {"operator_id": None, "captured_after_contract": None,
                "original_raw_isp_pair": None, "camera_and_chart_unchanged": None},
        },
        "chart_profile": {"id": None, "revision": "2023", "aspect_ratio": "16:9",
            "physical_chart_id": None, "physical_size_mm": [None, None],
            "reference_status": "user_attested_not_traceable"},
        "measured_review_template": {"operator_id": None, "physical_chart_id": None,
                "chart_model": CHART_MODEL, "roi_sha256": None, "capture_reviews": []},
        "capture_review_template": {"capture_id": None, "raw_sha256": None, "roi_sha256": None,
            "all_112_edges_reviewed": None, "registration_suitable": None,
            "fixed_camera_chart": None, "no_clipping_or_occlusion": None},
        "protocol": {
            "development_capture_count": 3, "fresh_validation_capture_count": 3,
            "validation_acquisition": "Freeze the candidate and acquire a new three-frame session after the issued contract.",
            "native_sampling": "Unbinned 1:1 Bayer pixels; no resize, rotation, mirroring or geometric warp.",
            "pairing": "RAW and ISP must refer to the same exposure and recorded sensor frame identity.",
            "registration": "Four fiducials propose positions only. Review all 112 edges on every original capture and correct distorted-layout ROIs before freezing.",
            "isp": "Unknown camera processing is diagnostic only. Linear or inverse-sRGB encoding alone does not establish a matching ISP pipeline.",
            "physical_validation": "not_established",
            "attestation_scope": "operator-declared; identity and capture time are not independently authenticated",
        },
        "supported_inputs": {"raw_packing": ["unpacked8", "unpacked16", "mipi_raw10", "mipi_raw12", "mipi_raw14"],
            "cfa_analysis": ["RGGB", "BGGR", "GRBG", "GBRG"], "cfa_calibration_bridge": ["RGGB"],
            "isp_files": ["JPEG", "PNG unsigned8/16", "single-page contiguous RGB/grayscale TIFF unsigned8/16"],
            "not_decoded": ["DNG", "CR2", "CR3", "NEF", "ARW", "YUV", "vendor RAW containers"],
            "limits": {"capture_count": 6, "edge_count": 112, "paired_capture_pixels": 40_000_000,
                       "each_original_file_bytes": 128 * 1024**2}},
        "matched_bilinear_processing_declaration": deepcopy(MATCHED_PROCESSING),
    }


def _text(value, name, maximum=160):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or value.strip().lower() in {"unknown", "none", "null", "todo"}
            or value.strip().lower().startswith("enter-")):
        raise ValueError(f"Measured {name} requires an explicit non-placeholder value")
    return value


def _number(value, name, minimum, maximum):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise ValueError(f"Measured {name} requires a finite value in [{minimum},{maximum}]")


def validate_measured_metadata(metadata):
    """Validate explicit declarations before decoding or persisting physical files."""
    if not isinstance(metadata, dict) or metadata.get("source_kind") != "measured":
        raise ValueError("Measured admission never relabels synthetic or unspecified inputs")
    data = deepcopy(metadata)
    if len(canonical(data).encode()) > 128 * 1024:
        raise ValueError("Measured metadata exceeds the bounded JSON budget")
    if data.get("schema") != "camera_esfr_capture_v1":
        raise ValueError("Measured capture requires camera_esfr_capture_v1 metadata")
    if "synthetic_generator" in data or data.get("synthetic") is True:
        raise ValueError("Synthetic provenance cannot be relabeled as a physical measurement")
    for name in ("camera_id", "session_id", "acquisition_id"):
        _text(data.get(name), name)
    if data.get("role") not in {"development", "validation"}:
        raise ValueError("Measured role must be development or validation")
    for name in ("frame_pair", "settings_provenance", "conditions", "sampling", "raw_format", "geometry", "isp"):
        if not isinstance(data.get(name), dict):
            raise ValueError("Measured " + name + " must be an explicit object")
    if not isinstance(data.get("capture_observations", {}), dict):
        raise ValueError("Measured capture_observations must be an object")
    try:
        captured = datetime.fromisoformat(data.get("captured_at", "").replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Measured captured_at requires a timezone-aware ISO8601 timestamp") from exc
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise ValueError("Measured captured_at requires an explicit timezone")
    _number(data.get("pixel_pitch_um"), "pixel_pitch_um", 0.1, 100)
    pair = data.get("frame_pair", {})
    for name in ("pair_id", "raw_frame_id", "isp_frame_id"):
        _text(pair.get(name), "frame_pair." + name)
    if pair.get("same_exposure") is not True or pair["raw_frame_id"] != pair["isp_frame_id"]:
        raise ValueError("Measured RAW/ISP must attest the same exposure and sensor frame identity")
    settings = data.get("settings_provenance", {})
    if settings.get("mode") not in {"device_metadata", "operator_recorded"}:
        raise ValueError("Measured settings require device_metadata or operator_recorded provenance")
    _text(settings.get("reference"), "settings_provenance.reference", 1024)
    conditions = data.get("conditions", {})
    for name in ("mode_id", "light_id", "focus_setting"):
        _text(conditions.get(name), "conditions." + name)
    for name, low, high in (("gain", 1e-3, 1000), ("exposure_s", 1e-8, 3600),
                            ("temperature_c", -100, 150), ("aperture_f_number", 0.1, 128),
                            ("focus_distance_m", 1e-4, 1e6), ("distance_m", 1e-4, 1e6)):
        _number(conditions.get(name), "conditions." + name, low, high)
    sampling = data.get("sampling", {})
    if (sampling.get("binning") != [1, 1] or sampling.get("subsampling") != [1, 1]
            or any(type(x) is not int for k in ("binning", "subsampling") for x in sampling.get(k, []))
            or sampling.get("resized") is not False or sampling.get("mirrored") is not False
            or type(sampling.get("rotation_deg")) not in {int, float} or sampling["rotation_deg"] != 0):
        raise ValueError("Measured calibration requires explicitly unbinned, unresized, unrotated native sensor coordinates")
    fmt = data.get("raw_format", {})
    if not isinstance(fmt, dict) or any(fmt.get(k) is None for k in (
            "packing", "width", "height", "stride_bytes", "header_bytes", "byte_order", "bit_depth", "bit_shift", "cfa", "black_level_dn", "white_level_dn")):
        raise ValueError("Every measured RAW packing and level declaration must be supplied")
    if fmt["packing"] not in {"unpacked8", "unpacked16", "mipi_raw10", "mipi_raw12", "mipi_raw14"}:
        raise ValueError("Measured RAW container is unsupported; supply explicitly decoded native Bayer bytes with provenance")
    processing = data.get("raw_processing")
    if not isinstance(processing, dict) or set(processing) != set(RAW_PROCESSING) or any(
            processing[k] is not value for k, value in RAW_PROCESSING.items()):
        raise ValueError("Measured RAW must explicitly declare linear native Bayer without demosaic, denoise, sharpening, companding or geometric transform")
    isp = data.get("isp", {})
    if isp.get("transfer") not in {"linear", "srgb"}:
        raise ValueError("Measured paired calibration requires declared linear or sRGB encoding; unknown transfer is unsupported")
    if isp.get("coordinate_mapping") != "same_native_raster":
        raise ValueError("Measured paired calibration does not support transformed, resized or unregistered ISP")
    if isp.get("comparison_mode") not in {"diagnostic", "matched_bilinear"}:
        raise ValueError("Declare ISP comparison_mode diagnostic or matched_bilinear")
    if isp["comparison_mode"] == "matched_bilinear" and isp.get("processing") != MATCHED_PROCESSING:
        raise ValueError("ISP acceptance requires the exact declared normalized bilinear operator with all other processing disabled")
    for name, low, high in (("temperature_c", -100, 150), ("illuminance_lux", 0, 1e9)):
        observed = data.get("capture_observations", {}).get(name)
        if observed is not None:
            _number(observed, "capture_observations." + name, low, high)
    if data["role"] == "validation":
        binding, attestation = data.get("validation_contract", {}), data.get("acquisition_attestation", {})
        if not isinstance(binding, dict) or not isinstance(attestation, dict):
            raise ValueError("Measured validation contract and attestations must be objects")
        _text(binding.get("id"), "validation_contract.id")
        sha = binding.get("content_hash")
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("Measured validation requires the issued contract content hash")
        _text(attestation.get("operator_id"), "acquisition_attestation.operator_id")
        if any(attestation.get(key) is not True for key in (
                "captured_after_contract", "original_raw_isp_pair", "camera_and_chart_unchanged")):
            raise ValueError("Measured validation requires explicit acquisition attestations")
    return data


def roi_review_digest(rois):
    """Bind only semantically used native ROI fields, independent of UI ordering."""
    return digest(sorted(({k: r[k] for k in ("id", "kind", "field_id", "box")} for r in rois),
                         key=lambda r: r["id"]))


def validate_measured_study(captures, rois, chart_profile, measured_review=None, thresholds=None):
    """Require complete fixed-geometry reviews without inferring physical truth."""
    from .esfr_chart import chart_rois

    if not captures or any(c["metadata"].get("source_kind") != "measured" for c in captures):
        raise ValueError("Measured studies cannot mix physical declarations and synthetic inputs")
    for capture in captures:
        validate_measured_metadata(capture["metadata"])
    dev = [c for c in captures if c["metadata"]["role"] == "development"]
    val = [c for c in captures if c["metadata"]["role"] == "validation"]
    if len(dev) != 3 or len(val) not in {0, 3} or len({c["id"] for c in captures}) != len(captures):
        raise ValueError("Measured workflow requires three development captures and zero or three fresh validation captures")
    if {c["metadata"]["session_id"] for c in dev} & {c["metadata"]["session_id"] for c in val}:
        raise ValueError("Measured validation needs a new session independent of development")
    pairs = [c["metadata"]["frame_pair"]["pair_id"] for c in captures]
    frame_ids = [c["metadata"]["frame_pair"]["raw_frame_id"] for c in captures]
    acquisitions = [c["metadata"]["acquisition_id"] for c in captures]
    if any(len(set(values)) != len(values) for values in (pairs, frame_ids, acquisitions)):
        raise ValueError("Every measured repeat requires distinct exposure/frame/acquisition identities")
    first = dev[0]["metadata"]
    for capture in captures:
        if any(capture["metadata"].get(k) != first.get(k) for k in (
                "camera_id", "conditions", "raw_format", "raw_processing", "geometry", "isp", "sampling", "pixel_pitch_um")):
            raise ValueError("Measured fixed-camera nominal settings, native sampling and ISP declarations differ")
    expected = {r["id"] for r in chart_rois() if r["kind"] == "edge"}
    edges = [r for r in rois if r["kind"] == "edge"]
    if len(edges) != 112 or {r["id"] for r in edges} != expected:
        raise ValueError("Measured Extended E-SFR requires all 112 exact chart edge identities")
    if len({tuple(r["box"]) for r in edges}) != len(edges):
        raise ValueError("Measured chart edge identities must refer to distinct native ROIs")
    if any(min(r["box"][2:]) < 32 for r in edges):
        raise ValueError("Each measured edge requires at least 32 native pixels per dimension for Bayer SFR")
    review = measured_review or {}
    if not isinstance(review, dict):
        raise ValueError("Measured ROI review must be an object")
    for name in ("operator_id", "physical_chart_id"):
        _text(review.get(name), "measured_review." + name)
    if chart_profile.get("physical_chart_id") != review["physical_chart_id"]:
        raise ValueError("Measured ROI review must match the frozen physical chart identity")
    checksum = roi_review_digest(rois)
    if review.get("chart_model") != CHART_MODEL or review.get("roi_sha256") != checksum:
        raise ValueError("Measured ROI review must bind the declared 2023 chart and exact native ROI digest")
    reviewed = review.get("capture_reviews", [])
    if not isinstance(reviewed, list) or any(not isinstance(row, dict) for row in reviewed):
        raise ValueError("Measured per-capture reviews must be a list of objects")
    by_id = {r.get("capture_id"): r for r in reviewed}
    if len(by_id) != len(reviewed) or set(by_id) != {c["id"] for c in captures}:
        raise ValueError("Every measured capture needs its own complete native ROI review")
    for capture in captures:
        row = by_id[capture["id"]]
        if row.get("raw_sha256") != capture["files"]["raw"]["sha256"] or row.get("roi_sha256") != checksum:
            raise ValueError("Measured ROI review is stale or belongs to another RAW capture")
        if any(row.get(k) is not True for k in (
                "all_112_edges_reviewed", "registration_suitable", "fixed_camera_chart", "no_clipping_or_occlusion")):
            raise ValueError("All 112 native edges require explicit per-capture review of registration, visibility and fixed setup")
    diagnostic = first["isp"]["comparison_mode"] == "diagnostic"
    return {"schema": SCHEMA, "source_kind": "measured", "edge_count": 112,
            "development_capture_count": len(dev), "validation_capture_count": len(val),
            "roi_sha256": checksum, "review_sha256": digest(review),
            "isp_comparison_mode": first["isp"]["comparison_mode"],
            "acceptance_metrics": ["raw_sfr_rmse"] if diagnostic else ["raw_sfr_rmse", "isp_sfr_rmse"],
            "declaration_status": "operator_attested_not_independently_authenticated",
            "physical_validation": "not_established"}
