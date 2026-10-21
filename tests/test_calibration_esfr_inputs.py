from copy import deepcopy
from types import SimpleNamespace

import pytest

from camera_platform import calibration_esfr_inputs as inputs, esfr_lab
from camera_platform.registry import IntegrityError


def data():
    capture = dict(id="dev", content_hash="bound", metadata=dict(role="development"),
                   native_pixel_sha256="pixels", files={"raw": {"sha256": "raw"}, "isp": {"sha256": "isp"}})
    roi = dict(id="edge", kind="edge", box=[0, 0, 62, 62])
    measurement = dict(role="training", provenance=dict(esfr_run_id="source", roi_id="edge",
        native_roi=roi["box"], constituents=[dict(capture_id="dev", capture_sha256="bound",
        metadata=capture["metadata"], source_files=capture["files"], native_pixel_sha256="pixels")]))
    source = dict(id="source", status="succeeded", development_ids=["dev"], result={},
                  capture_snapshots=[capture, dict(id="heldout", metadata=dict(role="validation"))],
                  specification=dict(rois=[roi], thresholds={"raw_sfr_rmse": .03, "isp_sfr_rmse": .05}))
    return source, measurement


def test_snapshot_excludes_every_holdout_byte_and_freezes_exact_development_metadata(monkeypatch):
    source, m = data()
    monkeypatch.setattr(esfr_lab, "get", lambda *args: source)
    snap = inputs.development_snapshot(SimpleNamespace(), [m])
    assert [c["id"] for c in snap["capture_snapshots"]] == ["dev"]
    assert len(snap["input_artifacts"]) == 2
    source["capture_snapshots"][0]["metadata"]["role"] = "changed"
    assert snap["capture_snapshots"][0]["metadata"]["role"] == "development"


@pytest.mark.parametrize("change", ["missing_repeat", "roi", "pixels", "role"])
def test_changed_or_validation_inputs_fail_before_fitting(monkeypatch, change):
    source, m = data()
    if change == "missing_repeat":
        m["provenance"]["constituents"][0]["capture_id"] = "heldout"
    elif change == "roi":
        m["provenance"]["native_roi"] = [1, 0, 62, 62]
    elif change == "pixels":
        m["provenance"]["constituents"][0]["native_pixel_sha256"] = "changed"
    else:
        m["role"] = "final_validation"
    monkeypatch.setattr(esfr_lab, "get", lambda *args: source)
    with pytest.raises((IntegrityError, ValueError)):
        inputs.development_snapshot(SimpleNamespace(), [m])


def test_validation_capture_cannot_be_bound_to_forward_fitter(monkeypatch):
    monkeypatch.setattr(esfr_lab, "_binding", lambda *args: pytest.fail("must reject before reading bytes"))
    with pytest.raises(IntegrityError, match="validation"):
        inputs.bound_development_captures(SimpleNamespace(), {
            "capture_snapshots": [dict(metadata=dict(role="validation"))]})
