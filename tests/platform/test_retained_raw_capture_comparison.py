"""A recorded fixture can exercise the RAW path without becoming a measurement."""

import io
from copy import deepcopy

import numpy as np
import pytest
from test_retained_isp_workflow import acquisition

from camera_platform import nonlinear_captures, retained_isp_source
from camera_platform import retained_raw_capture_comparison as comparison
from camera_platform.registry import IntegrityError

pytest_plugins = ["test_retained_charge_workflow"]


def _npy(array):
    stream = io.BytesIO()
    np.save(stream, array, allow_pickle=False)
    return stream.getvalue()


def _request(run, capture, roi=None):
    return {
        "schema": comparison.SCHEMA,
        "run_id": run["id"],
        "capture_id": capture["id"],
        "readout_id": "single",
        "roi": roi or {"row_start": 2, "row_end": 8,
                       "column_start": 4, "column_end": 10},
    }


def _import(service, source, raw, *, suffix, settings_status="recorded_applied",
            raw_format=None, geometry=None, exposure_delta_ns=0):
    run = source["source_snapshot"]
    data = nonlinear_captures.template(
        service, run["assembly_id"], run["test_id"], "sequential_frames",
    )
    data.update(
        name="Authored comparison fixture " + suffix,
        source_kind="simulated",
        source_uri="fixture://retained-raw-comparison/" + suffix,
        method_and_version="authored-fixture:1",
        dataset_ids=["authored-comparison-dataset-" + suffix],
        acquisition_group_id="authored-comparison-group-" + suffix,
        raw_formats=[deepcopy(raw_format or source["raw_format"])],
        geometry=deepcopy(geometry or source["geometry"]),
        pixel_pitch_um=source["pixel_pitch_um"],
    )
    original = data["frames"][0]
    data["frames"] = []
    for index, condition in enumerate(source["frame_conditions"]):
        frame = deepcopy(original)
        frame.update(frame_id=f"authored-{suffix}-{index}", raw_index=index)
        frame["applied"].update(
            exposure_ns=condition["exposure_ns"] + exposure_delta_ns,
            gain=condition["gain"],
            temperature_c=condition["temperature_c"],
            readout_settings=[{
                "analog_gain": condition["readout_analog_gain"],
                "digital_gain": condition["readout_digital_gain"],
            }],
        )
        frame["settings_evidence"].update(
            status=settings_status,
            source_uri="fixture://settings/" + suffix,
            method_and_version="authored-fixture:1",
            description="Authored test settings, not physical measurement evidence",
        )
        data["frames"].append(frame)
    return nonlinear_captures.import_capture(service, data, _npy(raw))


def test_cold_original_raw_comparison_is_descriptive_and_refuses_alias(workflow):
    service, sdk, run = acquisition(workflow, "nonlinear")
    source = retained_isp_source.resolve(service, run["id"], {
        "schema": retained_isp_source.SELECTION_SCHEMA, "readout_id": "single",
    })
    simulated = retained_isp_source.load_inputs(service, source)
    white = 2 ** source["raw_format"]["bit_depth"] - 1
    acquired = np.minimum(simulated.astype(np.uint32) + 1, white).astype(np.uint32)
    capture = _import(service, source, acquired, suffix="positive")
    request = _request(run, capture)
    result = comparison.compare(service, request)
    assert result == comparison.compare(service, request)
    assert result == service.compare_retained_raw_capture(request)
    assert result == sdk.compare_retained_raw_capture(
        run["id"], capture["id"], "single", request["roi"],
    )
    assert result["comparison_status"] == "DESCRIPTIVE_ONLY"
    assert result["issues"] == []
    assert len(result["frame_metrics"]) == simulated.shape[0]
    r, c = slice(2, 8), slice(4, 10)
    delta = acquired[:, r, c].astype(float) - simulated[:, r, c].astype(float)
    for index, metric in enumerate(result["frame_metrics"]):
        assert metric["pixelwise_mae_dn"] == pytest.approx(np.mean(np.abs(delta[index])))
        assert metric["pixelwise_rmse_dn"] == pytest.approx(
            np.sqrt(np.mean(delta[index] ** 2))
        )
        assert metric["capture_minus_simulated_mean_dn"] == pytest.approx(
            np.mean(delta[index])
        )
        assert set(metric["cfa_site_mean_difference_dn"]) == {"R", "Gr", "Gb", "B"}
    assert result["provenance"]["capture_source_kind"] == "simulated"
    assert result["physical_validation"] == "not_established"
    assert result["temporal_validation"] == "not_established"
    assert result["hdr_lfm_validation"] == "not_established"
    assert result["optimizer_validation"] == "not_established"
    assert result["model_device_binding"] == "unknown"
    assert result["blinding_enforced"] is False
    assert not service.registry.list("measurements")

    alias = _import(service, source, simulated, suffix="whole-alias")
    with pytest.raises(ValueError, match="aliases"):
        comparison.compare(service, _request(run, alias))

    # A different border must not mask an exact alias in the selected receiver ROI.
    roi_alias = acquired.copy()
    roi_alias[:, r, c] = simulated[:, r, c]
    aliased_roi = _import(service, source, roi_alias, suffix="roi-alias")
    with pytest.raises(ValueError, match="aliases"):
        comparison.compare(service, _request(run, aliased_roi))

    original = service.registry.blob_path(capture["raw_sha256"])
    content = original.read_bytes()
    original.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    with pytest.raises(IntegrityError, match="checksum mismatch|artifact differs"):
        comparison.compare(service, request)


def test_unknown_settings_return_no_numeric_metrics_and_format_or_crop_fail(workflow):
    service, _, run = acquisition(workflow, "nonlinear")
    source = retained_isp_source.resolve(service, run["id"], {
        "schema": retained_isp_source.SELECTION_SCHEMA, "readout_id": "single",
    })
    simulated = retained_isp_source.load_inputs(service, source)
    raw = simulated.astype(np.uint32) + 1
    white = 2 ** source["raw_format"]["bit_depth"] - 1

    def distinct(offset):
        result = raw.copy()
        result[:, 0, 0] = (result[:, 0, 0] + offset) % (white + 1)
        return result

    for offset, status in enumerate(("command_only", "unknown"), start=1):
        unrecorded = _import(
            service, source, distinct(offset), suffix=status, settings_status=status,
        )
        result = comparison.compare(service, _request(run, unrecorded))
        assert result["comparison_status"] == "INCONCLUSIVE"
        assert result["frame_metrics"] is None
        assert {item["reason"] for item in result["issues"]} == {
            "capture_applied_settings_not_recorded",
        }
        assert result["physical_validation"] == "not_established"

    different_exposure = _import(
        service, source, distinct(3), suffix="exposure-mismatch", exposure_delta_ns=1,
    )
    result = comparison.compare(service, _request(run, different_exposure))
    assert result["comparison_status"] == "INCONCLUSIVE"
    assert result["frame_metrics"] is None
    assert {item["field"] for item in result["issues"]} == {"exposure_ns"}

    changed_format = deepcopy(source["raw_format"])
    changed_format["bit_depth"] += 1
    wrong_format = _import(
        service, source, distinct(4), suffix="wrong-bit-depth", raw_format=changed_format,
    )
    with pytest.raises(ValueError, match="format, native crop"):
        comparison.compare(service, _request(run, wrong_format))

    smaller = distinct(5)[:, :8, :8]
    wrong_crop = _import(service, source, smaller, suffix="wrong-crop")
    with pytest.raises(ValueError, match="format, native crop"):
        comparison.compare(service, _request(run, wrong_crop))


@pytest.mark.parametrize("readout", [["single"], "SINGLE", None])
def test_readout_request_rejects_noncanonical_values(readout):
    with pytest.raises(ValueError, match="readout"):
        comparison._request({
            "schema": comparison.SCHEMA,
            "run_id": "run-example", "capture_id": "capture-example",
            "readout_id": readout,
            "roi": {"row_start": 0, "row_end": 2,
                    "column_start": 0, "column_end": 2},
        })


def test_selected_roi_budget_and_cfa_support_are_explicit():
    with pytest.raises(ValueError, match="ROI"):
        comparison._roi({"row_start": 0, "row_end": 1000,
                         "column_start": 0, "column_end": 1000}, (2, 1000, 1000))
    with pytest.raises(ValueError, match="ROI"):
        comparison._roi({"row_start": 0, "row_end": 1,
                         "column_start": 0, "column_end": 2}, (1, 2, 2))
