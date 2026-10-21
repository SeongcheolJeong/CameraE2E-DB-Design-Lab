"""Data used to evaluate a decoder is not fresh ISP validation evidence."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_isp_capture_studies import dataset, reference, source

from camera_platform import isp_studies_v2 as studies


def test_decoder_evaluation_aliases_exclude_sibling_acquisitions_without_claiming_fitting():
    capture = {
        "id": "evaluated", "dataset_ids": ["evaluation-data"],
        "exposure_tokens": ["capture-id:evaluated", "raw-frame-sha256:shared-charge"],
        "raw_frame_hashes": ["shared-charge"],
        "artifacts": [{"name": "raw-capture.npy", "sha256": "paired-raw"}],
    }
    sibling = deepcopy(capture)
    sibling.update(id="relabelled", dataset_ids=["relabelled-dataset"])
    sibling["exposure_tokens"] = ["capture-id:relabelled", "raw-frame-sha256:shared-charge"]
    service = SimpleNamespace(registry=SimpleNamespace(
        list=lambda kind: [capture, sibling] if kind == "captures" else []
    ))
    closure = {"calibration_lineage": [], "decoder_fitting_tokens": ["dataset-id:training"],
               "decoder_evaluation_tokens": ["dataset-id:evaluation-data"]}
    original = deepcopy(closure)
    used = studies._capture_fit_tokens(service, closure)
    assert {"capture-id:evaluated", "capture-id:relabelled", "paired-raw"} <= used
    assert closure == original
    assert "dataset-id:evaluation-data" not in closure["decoder_fitting_tokens"]


def test_reference_decoder_evaluation_data_is_refused_with_correct_reason():
    target = source()
    decoded_reference = source(identity="reference", group="reference-group")
    decoded_reference["acquisition_lineage"]["decoder_evaluation_tokens"] = [
        "capture-id:capture-a"
    ]
    value = dataset(target, reference=reference(decoded_reference))
    with pytest.raises(ValueError, match="evaluated on Study acquisition"):
        studies._reference_independence(None, [value], set())


def test_separate_fitting_and_evaluation_history_can_be_shared_by_fixed_decoder():
    target = source(decoder_fit=["capture-id:training"])
    target["acquisition_lineage"]["decoder_evaluation_tokens"] = ["capture-id:evaluation"]
    decoded_reference = source(identity="reference", group="reference-group",
                               decoder_fit=["capture-id:training"])
    decoded_reference["acquisition_lineage"]["decoder_evaluation_tokens"] = [
        "capture-id:evaluation"
    ]
    studies._reference_independence(
        None, [dataset(target, reference=reference(decoded_reference))],
        {"capture-id:training", "capture-id:evaluation"},
    )
