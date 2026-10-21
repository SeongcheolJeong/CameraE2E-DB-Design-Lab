"""Import rejection, native CFA sampling, and provenance isolation evidence."""

import copy
import hashlib
import io
import json

import numpy as np
import pytest

from camera_platform.calibration_lab_contracts import (
    DEFAULT_CONDITIONS,
    campaign_spec,
    import_measurement,
    measurement_spec,
    study_spec,
)


def metadata(chart="dark"):
    return {
        "campaign_id": "campaign-1",
        "chart": chart,
        "source_kind": "synthetic",
        "physical_instance": "camera-1",
        "session_id": "session-1",
        "acquisition_id": "capture-1",
        "role": "training",
        "conditions": copy.deepcopy(DEFAULT_CONDITIONS),
        "observations": {},
        "uncertainty": {"kind": "unknown"},
        "method": {"id": "test", "version": "1"},
        "instrument": None,
        "reference": {
            "id": "ref-1",
            "source_uri": "synthetic://test",
            "method": "synthetic linear target",
        },
    }


def npy(array):
    stream = io.BytesIO()
    np.save(stream, array, allow_pickle=False)
    return stream.getvalue()


def raw_metadata(chart="dark", shape=(16, 16), origin=(0, 0), full=(16, 16)):
    value = metadata(chart)
    value.update(
        raw_format={
            "cfa": "RGGB",
            "bit_depth": 12,
            "black_level_dn": 64,
            "response_type": "linear",
        },
        geometry={"full_sensor_shape": list(full), "crop_origin": list(origin)},
        analysis={"channel": "G1", "exposure_s": [0.01]},
    )
    return value


def test_explicit_csv_units_and_original_hash():
    meta = metadata("mtf")
    content = b"frequency_cy_px[cy/px],mtf[1]\n0,1\n0.25,0.5\n0.5,0.1\n"
    result = import_measurement(meta, content, "../original.csv")
    assert result["observations"]["mtf"] == [1, 0.5, 0.1]
    assert result["provenance"]["source_sha256"] == hashlib.sha256(content).hexdigest()
    assert result["provenance"]["filename"] == "original.csv"
    assert result["import_status"] == "ready"
    assert result["provenance"]["physical_validation"] == "not_established"
    with pytest.raises(ValueError, match="explicit unit"):
        import_measurement(meta, content.replace(b"cy/px", b"lp/mm"), "bad.csv")
    with pytest.raises(ValueError, match="explicit unit"):
        import_measurement(meta, b"frequency_cy_px,mtf\n0,1\n", "bad.csv")


@pytest.mark.parametrize(
    "content",
    [
        b"mean_dn[DN],mean_dn[DN]\n1,2\n",
        b"mean_dn[DN],variance_dn2[DN^2]\n1\n",
        b"mean_dn[DN]\nnan\n",
        b"mean_dn[DN]\ninf\n",
    ],
)
def test_bad_csv_rejected(content):
    with pytest.raises(ValueError):
        import_measurement(metadata("ptc"), content, "bad.csv")


def test_missing_columns_are_partial_without_fabrication():
    result = import_measurement(
        metadata("dark"), b"exposure_s[s],mean_dn[DN]\n0.01,65\n", "partial.csv"
    )
    assert result["import_status"] == "partial"
    assert result["missing_observations"] == ["variance_dn2"]
    assert "variance_dn2" not in result["observations"]


def test_measured_identity_instrument_and_reference_required():
    value = metadata()
    value["source_kind"] = "measured"
    with pytest.raises(ValueError, match="instrument"):
        measurement_spec(value)
    value["instrument"] = {"id": "bench", "calibration_revision": "2026-10"}
    value["physical_instance"] = ""
    with pytest.raises(ValueError, match="physical_instance"):
        measurement_spec(value)
    value["physical_instance"] = "camera-1"
    assert measurement_spec(value)["source_kind"] == "measured"


def test_unknown_nonfinite_and_misaligned_observations_rejected():
    for patch in (
        {"invented_field": 1},
        {"observations": {"mean_dn": [float("nan")]}},
        {"observations": {"mean_dn": [1], "variance_dn2": [1, 2]}},
        {"conditions": {**DEFAULT_CONDITIONS, "gain": float("inf")}},
    ):
        with pytest.raises(ValueError):
            measurement_spec({**metadata(), **patch})


def test_raw_temporal_variance_excludes_fixed_pattern_and_retains_odd_crop_phase():
    # Native G1 moves to local [1,0] under an odd row+column crop origin.
    raw = np.full((4, 16, 16), 700, np.uint16)
    pattern = np.arange(64).reshape(8, 8)
    for frame, offset in enumerate((-3, -1, 1, 3)):
        raw[frame, 1::2, 0::2] = 100 + pattern + offset
    meta = raw_metadata(origin=(1, 1), full=(18, 18))
    result = import_measurement(meta, npy(raw), "dark.npy")
    assert result["observations"]["mean_dn"] == [131.5]
    assert result["observations"]["variance_dn2"] == pytest.approx([20 / 3])
    assert result["quality"]["crop_origin"] == [1, 1]
    assert len(set(result["source_frame_hashes"])) == 4
    # Lossless integer dtype conversion must not defeat same-frame lineage.
    converted = import_measurement(meta, npy(raw.astype(np.uint32)), "same.npy")
    assert converted["source_frame_hashes"] == result["source_frame_hashes"]


def test_single_repeat_is_partial_not_spatial_noise():
    raw = np.arange(256, dtype=np.uint16).reshape(16, 16) + 64
    result = import_measurement(raw_metadata(), npy(raw), "single.npy")
    assert result["import_status"] == "partial"
    assert "variance_dn2" not in result["observations"]


def test_clipping_and_nonlinear_response_are_not_fit():
    meta = raw_metadata("ptc")
    meta["analysis"]["exposure_s"] = [0.001, 0.002, 0.003, 0.004]
    raw = np.stack([np.full((2, 16, 16), x, np.uint16) for x in (164, 264, 300, 4095)])
    result = import_measurement(meta, npy(raw), "ptc.npy")
    assert result["import_status"] == "partial"
    assert result["observations"]["clipped_fraction"][-1] == 1
    assert len(result["quality"]["not_fit_reasons"]) == 2


@pytest.mark.parametrize(
    "array",
    [
        np.zeros((16, 16), dtype=float),
        np.zeros((15, 16), dtype=np.uint16),
        np.full((16, 16), 5000, np.uint16),
    ],
)
def test_unsupported_raw_rejected(array):
    with pytest.raises(ValueError):
        import_measurement(raw_metadata(), npy(array), "bad.npy")


def test_oversize_header_rejected_before_allocation():
    stream = io.BytesIO()
    np.lib.format.write_array_header_1_0(
        stream, {"descr": "<u2", "fortran_order": False, "shape": (1000000, 1000000)}
    )
    with pytest.raises(ValueError, match="exceeds"):
        import_measurement(raw_metadata(), stream.getvalue(), "bad.npy")


def test_pickle_and_appended_payload_rejected():
    stream = io.BytesIO()
    np.save(stream, np.array([[{"x": 1}]], dtype=object), allow_pickle=True)
    with pytest.raises(ValueError, match="integer"):
        import_measurement(raw_metadata(), stream.getvalue(), "bad.npy")
    with pytest.raises(ValueError, match="length"):
        import_measurement(raw_metadata(), npy(np.zeros((16, 16), np.uint16)) + b"extra", "bad.npy")


def test_raw_shading_color_tone_use_declared_rois():
    raw = np.full((16, 16), 1064, np.uint16)
    raw[:, 8:] = 564
    for chart in ("shading", "color", "tone"):
        meta = raw_metadata(chart)
        meta["analysis"] = {
            "rois": [
                {"box": [0, 0, 16, 8], "field_radius": 0},
                {"box": [0, 8, 16, 8], "field_radius": 1},
            ]
        }
        if chart == "color":
            meta["analysis"]["reference_rgb"] = [[0.3, 0.3, 0.3], [0.15, 0.15, 0.15]]
        if chart == "tone":
            meta["analysis"]["input_level"] = [0.3, 0.15]
        result = import_measurement(meta, npy(raw), chart + ".npy")
        assert result["import_status"] == "ready"
        if chart == "shading":
            assert result["observations"]["relative_response"] == [1, 0.5]
        elif chart == "tone":
            assert result["observations"]["output_level"] == pytest.approx(
                [1000 / 4031, 500 / 4031]
            )


def test_multiview_csv_units_are_mm_and_native_pixels():
    meta = metadata("geometry")
    meta["analysis"] = {"geometry_mode": "multiview_planar", "native_shape": [100, 120]}
    content = (
        b"object_x[mm],object_y[mm],image_x[px],image_y[px],view_id[1]\n0,0,20,25,1\n10,0,30,25,1\n"
    )
    result = import_measurement(meta, content, "points.csv")
    assert result["observations"]["mode"] == "multiview_planar"
    assert result["observations"]["view_ids"] == ["1", "1"]
    with pytest.raises(ValueError, match="native_shape"):
        import_measurement(meta, content.replace(b"30,25", b"130,25"), "bad.csv")


def test_json_original_matches_metadata_and_study_bounds():
    meta = metadata("tone")
    meta["observations"] = {"input_level": [0, 1], "output_level": [0, 1]}
    result = import_measurement(meta, json.dumps(meta).encode(), "original.json")
    assert result["import_status"] == "ready"
    changed = {**meta, "role": "development"}
    with pytest.raises(ValueError, match="disagree"):
        import_measurement(changed, json.dumps(meta).encode(), "original.json")
    assert (
        study_spec({"campaign_id": "x", "measurement_ids": ["m"], "base_profile_id": "baseline"})[
            "base_profile_id"
        ]
        == "baseline"
    )
    with pytest.raises(ValueError):
        study_spec({"campaign_id": "x", "measurement_ids": ["m", "m"]})
    with pytest.raises(ValueError):
        study_spec({"campaign_id": "x", "measurement_ids": ["m"], "max_iterations": 501})


def test_campaign_requires_fixed_physical_context():
    payload = {
        "name": "test",
        "camera_id": "camera-1",
        "native_shape": [16, 16],
        "pixel_pitch_um": 3,
        "conditions": DEFAULT_CONDITIONS,
        "baseline_profile": {},
        "thresholds": {},
    }
    assert campaign_spec(payload)["camera_id"] == "camera-1"
    with pytest.raises(ValueError):
        campaign_spec({**payload, "camera_id": ""})
    with pytest.raises(ValueError):
        campaign_spec({**payload, "thresholds": {"mtf": float("inf")}})


def test_measured_mtf_requires_domain_and_raw_rejects_isp():
    meta = metadata("mtf")
    meta["source_kind"] = "measured"
    meta["instrument"] = {"id": "bench", "calibration_revision": "1"}
    with pytest.raises(ValueError, match="measurement_domain"):
        measurement_spec(meta)
    meta["measurement_domain"] = "optical"
    assert measurement_spec(meta)["measurement_domain"] == "optical"
    curve = b"frequency_cy_px[cy/px],mtf[1]\n0,1\n0.25,0.5\n"
    result = import_measurement(meta, curve, "missing-field.csv")
    assert result["import_status"] == "partial"
    assert "field_xy" in result["missing_observations"]
    meta["analysis"] = {"field_xy": [0.5, 0], "orientation_deg": 90}
    result = import_measurement(meta, curve, "located.csv")
    assert result["observations"]["field_xy"] == [0.5, 0]
    assert result["import_status"] == "ready"
    raw = raw_metadata("mtf", full=(32, 32))
    raw["measurement_domain"] = "camera_isp"
    with pytest.raises(ValueError, match="camera_raw"):
        import_measurement(raw, npy(np.full((32, 32), 100, np.uint16)), "edge.npy")


def test_raw_slanted_edge_retains_native_frequency_and_crop_location():
    from scipy.ndimage import gaussian_filter

    yy, xx = np.indices((64, 64))
    gray = gaussian_filter((xx > 30 + 0.12 * yy).astype(float), 1.2)
    raw = np.round(64 + 100 + 1800 * gray).astype(np.uint16)
    meta = raw_metadata("mtf", origin=(32, 64), full=(128, 128))
    result = import_measurement(meta, npy(raw), "edge.npy")
    obs = result["observations"]
    assert result["import_status"] == "ready"
    assert max(obs["frequency_cy_px"]) <= 0.25
    assert obs["field_xy"] == pytest.approx([64 / 127, 0])
    assert obs["field_coordinate_domain"] == "sensor_normalized"
    assert result["quality"]["edge_high_dn"] > result["quality"]["edge_low_dn"]
    meta["analysis"]["field_xy"] = [0, 0]
    with pytest.raises(ValueError, match="ROI center"):
        import_measurement(meta, npy(raw), "wrong-location.npy")


@pytest.mark.parametrize("chart,ordinate", [("mtf", "mtf"), ("texture", "response")])
def test_external_curve_coordinate_domain_survives_csv_and_json(chart, ordinate):
    meta = metadata(chart)
    coordinate = [0.6010407640085654, 0.6010407640085653]
    meta["analysis"] = {
        "field_xy": coordinate,
        "field_coordinate_domain": "reference_lens_object_direction",
        "orientation_deg": 45,
    }
    content = f"frequency_cy_px[cy/px],{ordinate}[1]\n0,1\n0.25,0.5\n".encode()
    imported = import_measurement(meta, content, "reference-model.csv")
    observations = imported["observations"]
    assert observations["field_xy"] == coordinate
    assert observations["field_coordinate_domain"] == "reference_lens_object_direction"
    assert observations["orientation_deg"] == 45
    assert imported["provenance"]["source_sha256"] == hashlib.sha256(content).hexdigest()
    meta["observations"] = observations
    assert measurement_spec(meta)["observations"] == observations
    meta["analysis"]["field_coordinate_domain"] = "sensor_normalized"
    with pytest.raises(ValueError, match="domains disagree"):
        measurement_spec(meta)


@pytest.mark.parametrize("chart,ordinate", [("mtf", "mtf"), ("texture", "response")])
def test_legacy_curve_domain_is_sensor_normalized_and_unknown_domains_fail(chart, ordinate):
    meta = metadata(chart)
    meta["observations"] = {"frequency_cy_px": [0, 0.25], ordinate: [1, 0.5]}
    assert measurement_spec(meta)["observations"]["field_coordinate_domain"] == "sensor_normalized"
    meta["observations"]["field_coordinate_domain"] = "lens_height_unspecified"
    with pytest.raises(ValueError, match="field_coordinate_domain"):
        measurement_spec(meta)
    meta["observations"] = {}
    meta["analysis"] = {"field_coordinate_domain": "lens_height_unspecified"}
    with pytest.raises(ValueError, match="field_coordinate_domain"):
        measurement_spec(meta)


@pytest.mark.parametrize("chart", ["mtf", "texture"])
def test_raw_cannot_relabel_sensor_raster_as_reference_lens_field(chart):
    meta = raw_metadata(chart)
    meta["analysis"]["field_coordinate_domain"] = "reference_lens_object_direction"
    with pytest.raises(ValueError, match="sensor_normalized"):
        import_measurement(meta, npy(np.full((16, 16), 100, np.uint16)), "native.npy")


def test_raw_texture_field_is_native_crop_center_and_cannot_be_reassigned():
    raw = np.full((16, 16), 128, np.uint16)
    raw[3:9, 6:11] = 256
    meta = raw_metadata("texture", origin=(0, 16), full=(32, 32))
    meta["analysis"] = {
        "frequency_cy_px": [0.125, 0.25],
        "reference_amplitude": [0.1, 0.1],
    }
    result = import_measurement(meta, npy(raw), "texture.npy")
    assert result["observations"]["field_coordinate_domain"] == "sensor_normalized"
    assert result["observations"]["field_xy"] == pytest.approx([16 / 31, -16 / 31])
    meta["analysis"]["field_xy"] = [0, 0]
    with pytest.raises(ValueError, match="texture ROI center"):
        import_measurement(meta, npy(raw), "relabeled.npy")


def test_raw_geometry_centroid_and_texture_reference_requirements():
    raw = np.full((16, 16), 64, np.uint16)
    raw[7:10, 5:8] = 1064
    meta = raw_metadata("geometry")
    meta["analysis"] = {"rois": [[2, 2, 12, 12]], "object_xy": [[-0.2, 1 / 15]]}
    result = import_measurement(meta, npy(raw), "dot.npy")
    assert result["observations"]["image_xy"][0] == pytest.approx([-0.2, 1 / 15])
    texture = raw_metadata("texture")
    with pytest.raises(ValueError, match="reference_amplitude"):
        import_measurement(texture, npy(raw), "texture.npy")
    texture["analysis"] = {"frequency_cy_px": [0.125, 0.25], "reference_amplitude": [0.1, 0.1]}
    assert import_measurement(texture, npy(raw), "texture.npy")["import_status"] == "ready"


def test_multiview_pose_vector_shapes_and_fixed_study_budget():
    meta = metadata("geometry")
    meta["observations"] = {
        "mode": "multiview_planar",
        "object_xy": [[0, 0]],
        "image_xy": [[10, 20]],
        "view_ids": ["1"],
        "native_shape": [100, 100],
        "view_extrinsics": {"1": {"rvec": [0, 0, 0], "tvec": [0, 0, 100]}},
    }
    assert measurement_spec(meta)["observations"]["view_extrinsics"]["1"]["tvec"][2] == 100
    meta["observations"]["view_extrinsics"]["1"]["rvec"] = [0, 0]
    with pytest.raises(ValueError, match="3 values"):
        measurement_spec(meta)
    for key, value in (("seed", 2**31), ("max_iterations", 501), ("max_iterations", 0)):
        with pytest.raises(ValueError):
            study_spec({"campaign_id": "x", "measurement_ids": ["m"], key: value})


@pytest.mark.parametrize("chart", ["color", "tone"])
def test_npy_color_tone_has_raw_domain_without_rewriting_metadata(chart):
    meta = raw_metadata(chart)
    meta["analysis"] = {"rois": [[0, 0, 16, 16]]}
    if chart == "color":
        meta["analysis"]["reference_rgb"] = [[0.3, 0.3, 0.3]]
    else:
        meta["analysis"]["input_level"] = [0.3]
    original = copy.deepcopy(meta)
    content = npy(np.full((16, 16), 1064, np.uint16))
    result = import_measurement(meta, content, "capture.npy")
    assert result["measurement_domain"] == "camera_raw"
    assert meta == original
    assert (
        result["provenance"]["metadata_sha256"]
        == hashlib.sha256(
            json.dumps(original, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )
    for domain in ("camera_isp", "optical"):
        with pytest.raises(ValueError, match="conflict"):
            import_measurement({**meta, "measurement_domain": domain}, content, "capture.npy")
