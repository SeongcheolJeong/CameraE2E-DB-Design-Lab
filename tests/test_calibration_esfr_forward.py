"""Development-only boundary and exact native-edge coverage for forward fitting."""
import hashlib
from copy import deepcopy

import numpy as np
import pytest
import tifffile
from scipy.special import ndtr

from camera_platform import calibration_esfr_forward as forward
from camera_platform.calibration_lab_engine import default_profile, validate_profile
from camera_platform.esfr_analysis import edge_metrics
from camera_platform.esfr_bridge import _field
from camera_platform.esfr_engine import bilinear_isp
from camera_platform.esfr_io import RawFrame
from camera_platform.registry import digest


def fixture(tmp_path, count=2):
    columns = min(count, 14)
    rows = (count + columns - 1) // columns
    shape = [rows * 80 + 16, columns * 80 + 16]
    profile = default_profile()
    profile["native_shape"] = shape
    profile["render_shape"] = [64, 64]
    levels = {c: dict(low_dn=600, high_dn=2800, black_dn=64, white_dn=4095)
              for c in ("R", "Gr", "Gb", "B")}
    rois = [{"id": f"edge-{i:03d}", "kind": "edge", "field_id": f"original-group-{i // 8}",
             "box": [16 + (i // columns) * 80, 16 + (i % columns) * 80, 62, 62]}
            for i in range(count)]
    # Independent analytic CDF scene construction, not production simulate_edge.
    scene = np.full(shape, 1600.0)
    truths = {}
    for i, roi in enumerate(rois):
        row, col, height, width = roi["box"]
        tangent = np.deg2rad((5, 40, 85, 130)[i % 4])
        normal = [-np.sin(tangent), np.cos(tangent)]
        yy, xx = np.indices((height + 12, width + 12), dtype=float)
        distance = normal[0] * (xx - 6 - 30.3) + normal[1] * (yy - 6 - 30.7)
        sigma = 1.22 + 0.03 * (i % 5)
        truths[roi["id"]] = sigma
        scene[row-6:row+height+6, col-6:col+width+6] = 64 + 600 + 2200 * ndtr(distance / sigma)
    captures = []
    for repeat in range(3):
        rng = np.random.default_rng(782 + repeat)
        raw = np.rint(scene + rng.normal(0, 0.3, shape)).clip(0, 4095).astype("<u2")
        raw_path, isp_path = tmp_path / f"dev-{repeat}.raw", tmp_path / f"dev-{repeat}.tiff"
        raw.tofile(raw_path)
        rgb = bilinear_isp(raw, "RGGB", (0, 0), levels)
        tifffile.imwrite(isp_path, np.uint16(np.clip(rgb, 0, 1) * 65535), photometric="rgb")
        metadata = {"camera_id": "forward-independent-cdf-fixture", "source_kind": "synthetic",
                    "role": "development", "conditions": deepcopy(profile["conditions"]),
                    "pixel_pitch_um": profile["pixel_pitch_um"],
                    "geometry": {"full_sensor_shape": shape, "crop_origin": [0, 0]},
                    "raw_format": {"packing": "unpacked16", "width": shape[1], "height": shape[0],
                        "stride_bytes": shape[1] * 2, "header_bytes": 0, "byte_order": "little",
                        "bit_depth": 12, "bit_shift": 0, "cfa": "RGGB", "black_level_dn": 64,
                        "white_level_dn": 4095},
                    "isp": {"transfer": "linear", "coordinate_mapping": "same_native_raster"}}
        files = {k: {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}
                 for k, p in (("raw", raw_path), ("isp", isp_path))}
        record = {"id": f"dev-{repeat}", "metadata": metadata, "files": files}
        record["content_hash"] = digest(record)
        captures.append((record, raw_path, isp_path))
    measurements = []
    for roi in rois:
        xy = _field(captures[0][0]["metadata"], roi["box"])
        profile["blur"]["field_nodes"].append({"field_xy": xy, "sigma_x_px": 1.65,
                                              "sigma_y_px": 1.65, "theta_deg": 0.0})
        metrics = []
        for record, path, _ in captures:
            with RawFrame(path, record["metadata"]) as raw:
                plane, sampling = raw.plane(roi["box"], "Gr")
                metrics.append(edge_metrics(plane, pitch_native=2, black_level=0, white_level=4031))
        assert all(m["status"] == "ok" for m in metrics)
        measurements.append({"id": "measurement-" + roi["id"], "role": "training",
            "provenance": {"roi_id": roi["id"], "native_roi": roi["box"],
                "constituents": [{"capture_id": c[0]["id"], "capture_sha256": c[0]["content_hash"],
                                  "metadata": c[0]["metadata"], "source_files": c[0]["files"]} for c in captures]},
            "observations": {"field_xy": xy, "orientation_deg": metrics[0]["frequency_direction_deg"],
                "frequency_cy_px": metrics[0]["frequency_cy_native_px"],
                "mtf": np.mean([m["sfr"] for m in metrics], axis=0).tolist()}})
    return profile, measurements, captures, rois, truths


def test_native_forward_fit_uses_all_repeats_and_freezes_exact_operator(tmp_path):
    profile, measurements, captures, rois, truths = fixture(tmp_path)
    original = digest(profile)
    result = forward.refine_profile(profile, measurements, captures, rois)
    assert digest(profile) == original
    fitted = result["profile"]
    assert len(fitted["blur"]["field_nodes"]) == 3
    assert fitted["blur"]["field_nodes"][0] == profile["blur"]["field_nodes"][0]
    wrapper = fitted["esfr_forward_model"]
    assert wrapper["profile_sha256"] == digest(wrapper["profile"])
    assert validate_profile(fitted)["esfr_forward_model"] == wrapper
    for row in result["diagnostics"]:
        assert abs(row["sigma_after_px"] - truths[row["roi_id"]]) < 0.04
        assert row["development_repeat_count"] == 3
        assert row["raw_sfr_rmse"]["after"] < 0.01
        assert row["isp_sfr_rmse"]["after"] < 0.01
        assert row["raw_sfr_rmse"]["after"] < row["raw_sfr_rmse"]["before"] / 5
        assert row["covariance_orientation_identifiable"] is False
    metadata = result["forward_observer"]
    assert metadata["fit_data_role"] == "development_only"
    assert metadata["isp_used_in_objective"] is False
    assert metadata["development_metrics"]["raw_sfr_rmse"]["count"] == 6
    assert metadata["input_bindings"]["development_capture_ids"] == [c[0]["id"] for c in captures]
    assert set(wrapper["profile"]["fields"]) == {r["id"] for r in rois}


@pytest.mark.parametrize("corruption", ["validation_capture", "validation_record", "missing_edge",
                                       "wrong_coordinate", "foreign_constituent"])
def test_invalid_boundaries_fail_before_any_image_reads(tmp_path, monkeypatch, corruption):
    profile, measurements, captures, rois, _ = fixture(tmp_path)
    measurements, captures = deepcopy(measurements), deepcopy(captures)
    if corruption == "validation_capture":
        captures[-1][0]["metadata"]["role"] = "validation"
    elif corruption == "validation_record":
        measurements[-1]["role"] = "final_validation"
    elif corruption == "missing_edge":
        measurements.pop()
    elif corruption == "wrong_coordinate":
        measurements[0]["observations"]["field_xy"][0] += 1e-5
    else:
        measurements[0]["provenance"]["constituents"][0]["capture_sha256"] = "foreign"
    monkeypatch.setattr(forward, "_verify_file", lambda *args: pytest.fail("image read crossed invalid boundary"))
    with pytest.raises(ValueError):
        forward.refine_profile(profile, measurements, captures, rois)


def test_112_distinct_native_edges_are_fitted_without_star_group_merging(tmp_path):
    profile, measurements, captures, rois, truths = fixture(tmp_path, 112)
    result = forward.refine_profile(profile, measurements, captures, rois)
    diagnostics = result["diagnostics"]
    frozen = result["profile"]["esfr_forward_model"]["profile"]
    assert len(diagnostics) == len(frozen["fields"]) == len(frozen["geometry"]["roi_nuisance"]) == 112
    assert len(result["profile"]["blur"]["field_nodes"]) == 113
    assert len({tuple(r["field_xy"]) for r in diagnostics}) == 112
    assert {r["roi_id"] for r in diagnostics} == {r["id"] for r in rois}
    assert all(r["development_repeat_count"] == 3 for r in diagnostics)
    assert all(abs(r["sigma_after_px"] - truths[r["roi_id"]]) < 0.05 for r in diagnostics)
    assert result["forward_observer"]["development_metrics"]["raw_sfr_rmse"]["count"] == 336
    assert result["forward_observer"]["development_metrics"]["raw_sfr_rmse"]["worst_after"] < 0.01
    for roi in rois:
        t = frozen["geometry"]["roi_nuisance"][roi["id"]]
        assert t["geometry"]["box"] == roi["box"]
        assert t["geometry"]["field_id"] == roi["id"]


def test_privileged_generator_truth_does_not_change_fitted_operator(tmp_path):
    profile, measurements, captures, rois, _ = fixture(tmp_path)
    original = forward.refine_profile(profile, measurements, captures, rois)
    captures, measurements = deepcopy(captures), deepcopy(measurements)
    for capture, _, _ in captures:
        capture["metadata"]["synthetic_generator"] = {
            "discrete_gaussian_sigma_native_px": 7.9, "pixel_area_supersample": 999,
            "truth_scope": "poisoned metadata must not enter the fit"}
        capture["content_hash"] = digest({k: v for k, v in capture.items() if k != "content_hash"})
    by_id = {c[0]["id"]: c[0] for c in captures}
    for measurement in measurements:
        for source in measurement["provenance"]["constituents"]:
            capture = by_id[source["capture_id"]]
            source.update(metadata=capture["metadata"], capture_sha256=capture["content_hash"])
    poisoned = forward.refine_profile(profile, measurements, captures, rois)
    assert original["profile"] == poisoned["profile"]
    assert original["diagnostics"] == poisoned["diagnostics"]
    assert poisoned["forward_observer"]["privileged_truth_used"] is False
    assert original["forward_observer"]["input_bindings"] != poisoned["forward_observer"]["input_bindings"]
