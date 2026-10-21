"""Aperture selection stays tied to the captured same-field candidates."""

import math
from pathlib import Path

import pytest

from camera_platform import db_design_aperture as evaluation
from camera_platform.db_design_engine import aperture_comparison


def candidate(index, f_number):
    return {
        "id": f"candidate-{index:02d}",
        "lens_key": "reference" if index == 0 else f"lens-{index}",
        "prefix": f"candidates/candidate-{index:02d}/",
        "source_identities": {"lens": {"source": {
            "paraxial": {"effective_f_number": f_number},
        }}},
        "result": {"fields": [{"id": "center"}, {"id": "outer-ne"}]},
    }


def test_same_field_signal_reference_and_calibration_candidate_exclusion(tmp_path, monkeypatch):
    calls = []

    def measure(root, field):
        calls.append(Path(root))
        stopped = "candidate-02" in str(root)
        outer = field["id"] == "outer-ne"
        signal = (100 if outer else 200) * (.5 if stopped else 1)
        return {
            "mean_signal_e": signal,
            "temporal_noise_e": 1,
            "snr_linear": signal,
            "snr_db": 20 * math.log10(signal),
            "processed_sfr50_cpp": .2 if stopped else .1,
            "saturation_fraction": 0,
        }

    monkeypatch.setattr(evaluation, "measure_field", measure)
    result = aperture_comparison(
        tmp_path,
        [candidate(0, 3.61), candidate(1, 2.7511), candidate(2, 4.12)],
        {"f_numbers": [2.75, 4.12], "selection_policy": {
            "min_snr_db": 25, "min_relative_signal": .1, "max_saturation_fraction": .01,
        }},
    )
    assert len(calls) == 4
    assert [r["candidate_id"] for r in result["rows"]] == ["candidate-01", "candidate-02"]
    assert [f["relative_signal"] for f in result["rows"][1]["fields"]] == [.5, .5]
    assert result["selection"]["recommended_candidate_id"] == "candidate-02"
    assert result["rows"][0]["f_number"] == 2.7511
    assert result["rows"][0]["requested_f_number"] == 2.75


def test_incomplete_sweep_cannot_claim_success(tmp_path):
    with pytest.raises(ValueError, match="Every declared aperture"):
        aperture_comparison(tmp_path, [candidate(0, 3.61)], {"f_numbers": [2.75, 4.12]})
