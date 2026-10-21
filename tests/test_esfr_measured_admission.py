"""Measured declarations and review gates; no physical-camera evidence is fabricated."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from camera_platform import esfr_measured as measured
from camera_platform.esfr_chart import chart_rois
from camera_platform.esfr_engine import run
from camera_platform.esfr_lab import import_capture
from test_esfr_engine import make_fixture


def metadata(index=0, role="development"):
    """Explicit test declarations only; these are never stored as real captures."""
    result = measured.measured_template()["capture_metadata"]
    result.update(camera_id="declared-camera-serial", session_id=role + "-session",
                  acquisition_id=f"acq-{index}", role=role,
                  captured_at=f"2026-10-18T10:00:{index:02d}+00:00", pixel_pitch_um=2.9)
    result["frame_pair"] = dict(pair_id=f"pair-{index}", raw_frame_id=f"frame-{index}",
                                isp_frame_id=f"frame-{index}", same_exposure=True)
    result["settings_provenance"] = dict(mode="operator_recorded", reference="test operator log")
    result["conditions"] = dict(mode_id="linear-rggb", gain=1.0, exposure_s=0.01,
        temperature_c=25, light_id="fixed-lamp-serial", aperture_f_number=4.12,
        focus_distance_m=2.0, focus_setting="manual-fixed", distance_m=2.0)
    result["raw_format"] = dict(packing="unpacked16", width=2560, height=1440,
        stride_bytes=5120, header_bytes=0, byte_order="little", bit_depth=12,
        bit_shift=0, cfa="RGGB", black_level_dn=64, white_level_dn=4095)
    result["raw_processing"] = deepcopy(measured.RAW_PROCESSING)
    result["geometry"] = dict(full_sensor_shape=[1440, 2560], crop_origin=[0, 0])
    result["sampling"] = dict(binning=[1, 1], subsampling=[1, 1], resized=False, rotation_deg=0, mirrored=False)
    result["isp"].update(transfer="srgb", coordinate_mapping="same_native_raster")
    if role == "validation":
        result["validation_contract"] = dict(id="contract-id", content_hash="a" * 64)
        result["acquisition_attestation"] = dict(operator_id="test-operator", captured_after_contract=True,
            original_raw_isp_pair=True, camera_and_chart_unchanged=True)
    return result


def review_fixture(validation=False):
    rois = chart_rois()
    captures = [{"id": f"capture-{i}", "metadata": metadata(i, "development" if i < 3 else "validation"),
                 "files": {"raw": {"sha256": str(i) * 64}}}
                for i in range(6 if validation else 3)]
    checksum = measured.roi_review_digest(rois)
    chart = {"physical_chart_id": "chart-owned-1"}
    review = dict(operator_id="test-operator", physical_chart_id="chart-owned-1", chart_model=measured.CHART_MODEL,
                  roi_sha256=checksum, capture_reviews=[dict(capture_id=c["id"], raw_sha256=c["files"]["raw"]["sha256"],
                    roi_sha256=checksum, all_112_edges_reviewed=True, registration_suitable=True,
                    fixed_camera_chart=True, no_clipping_or_occlusion=True) for c in captures])
    return captures, rois, chart, review


def test_template_has_no_plausible_measurement_defaults_and_cannot_be_imported():
    template = measured.measured_template()
    data = template["capture_metadata"]
    assert all(v is None for v in data["conditions"].values())
    assert all(v is None for v in data["raw_format"].values())
    assert data["pixel_pitch_um"] is data["captured_at"] is None
    with pytest.raises(ValueError):
        measured.validate_measured_metadata(data)
    assert template["protocol"]["development_capture_count"] == 3
    assert template["protocol"]["fresh_validation_capture_count"] == 3


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(source_kind="synthetic"),
    lambda m: m.update(captured_at="2026-10-18T10:00:00"),
    lambda m: m["conditions"].update(gain=None),
    lambda m: m["frame_pair"].update(isp_frame_id="another-frame"),
    lambda m: m["sampling"].update(resized=True),
    lambda m: m["sampling"].update(binning=[True, True]),
    lambda m: m["sampling"].update(subsampling=[2, 2]),
    lambda m: m["isp"].update(transfer="unknown"),
    lambda m: m["isp"].update(coordinate_mapping="unregistered"),
    lambda m: m["isp"].update(comparison_mode="matched_bilinear"),
    lambda m: m["raw_format"].update(packing="DNG"),
    lambda m: m["raw_processing"].update(companded=True),
    lambda m: m.update(synthetic_generator={"method": "synthetic-fixture"}),
])
def test_unsupported_or_missing_measured_declarations_are_rejected(mutation):
    data = metadata()
    mutation(data)
    with pytest.raises(ValueError):
        measured.validate_measured_metadata(data)


def test_unknown_physical_isp_remains_diagnostic_and_matched_mode_is_explicit():
    data = metadata()
    assert measured.validate_measured_metadata(data) == data
    assert measured.isp_is_diagnostic(data)
    data["isp"].update(comparison_mode="matched_bilinear", processing=deepcopy(measured.MATCHED_PROCESSING))
    assert measured.validate_measured_metadata(data) == data
    assert not measured.isp_is_diagnostic(data)
    data["isp"]["processing"]["sharpening"] = "enabled"
    assert measured.isp_is_diagnostic(data)
    with pytest.raises(ValueError):
        measured.validate_measured_metadata(data)


def test_all_112_edges_all_captures_reviewed_and_frozen_chart_stays_unchanged():
    captures, rois, chart, review = review_fixture(validation=True)
    before = deepcopy(chart)
    admission = measured.validate_measured_study(captures, rois, chart, review,
                                                {"raw_sfr_rmse": 0.03, "isp_sfr_rmse": 0.05})
    assert admission["edge_count"] == 112
    assert admission["validation_capture_count"] == 3
    assert admission["acceptance_metrics"] == ["raw_sfr_rmse"]
    assert chart == before
    assert measured.roi_review_digest(list(reversed(rois))) == measured.roi_review_digest(rois)


@pytest.mark.parametrize("corruption", ["missing_edge", "duplicate_box", "missing_capture", "stale_hash", "no_review", "wrong_chart", "same_session"])
def test_incomplete_or_stale_roi_reviews_are_not_accepted(corruption):
    captures, rois, chart, review = review_fixture(validation=True)
    if corruption == "missing_edge":
        rois.pop(0)
    elif corruption == "duplicate_box":
        rois[1]["box"] = rois[0]["box"]
    elif corruption == "missing_capture":
        review["capture_reviews"].pop()
    elif corruption == "stale_hash":
        review["capture_reviews"][0]["raw_sha256"] = "f" * 64
    elif corruption == "no_review":
        review["capture_reviews"][1]["all_112_edges_reviewed"] = False
    elif corruption == "wrong_chart":
        chart["physical_chart_id"] = "other-print"
    else:
        captures[-1]["metadata"]["session_id"] = "development-session"
    with pytest.raises(ValueError):
        measured.validate_measured_study(captures, rois, chart, review)


def test_invalid_capture_is_rejected_before_file_read_or_storage(tmp_path):
    data = metadata()
    data["sampling"]["resized"] = True
    with pytest.raises(ValueError, match="native sensor"):
        import_capture(SimpleNamespace(), data, tmp_path / "never-read.raw", tmp_path / "never-read.png")


def test_diagnostic_isp_never_counts_as_acceptance_and_thresholds_remain(monkeypatch, tmp_path):
    # Exercise the engine policy with a synthetic fixture that stays explicitly
    # synthetic. Only the policy decision is injected; no physical evidence is made.
    job, captures = make_fixture(tmp_path)
    job["specification"]["thresholds"] = {"raw_sfr_rmse": 0.03, "isp_sfr_rmse": 1e-12}
    monkeypatch.setattr(measured, "isp_is_diagnostic", lambda metadata: True)
    result = run(job, captures, tmp_path / "report")
    assert result["source_kind"] == "synthetic"
    validation = result["validation"]
    assert validation["status"] == "pass"
    assert validation["scope"] == "raw_native_sfr_only"
    assert validation["thresholds"] == job["specification"]["thresholds"]
    raw = [r for r in validation["checks"] if r["metric"] == "raw_sfr_rmse"]
    isp = [r for r in validation["checks"] if r["metric"] == "isp_sfr_rmse"]
    assert len(raw) == len(isp) == 4
    assert all(r["status"] == "pass" for r in raw)
    assert all(r["status"] == "diagnostic" and r["acceptance_eligible"] is False for r in isp)
    assert all(r["value"] > r["threshold"] for r in isp)
