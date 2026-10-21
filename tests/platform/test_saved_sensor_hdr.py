"""Saved DB HDR really captures two RAW frames and reuses the RAW merger."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform.saved_sensor_hdr import evaluate_saved_sensor_hdr_bracket
from camera_platform.sensor_db_bridge import sensor_revision_catalog

WORKSPACE = Path(__file__).resolve().parents[2]
CODE = "DEF-1801-802"


def _selected() -> dict:
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Saved sensor DB is not installed")
    rows = sensor_revision_catalog(root=WORKSPACE / "camerae2e_db", query=CODE)
    selected = next((row for row in rows if row["report_code"] == CODE), None)
    if selected is None:
        pytest.skip("Example saved physical sensor run is not installed")
    return selected


def test_two_exposure_saved_sensor_hdr_preserves_raw_and_fpn_override(tmp_path):
    selected = _selected()
    result = evaluate_saved_sensor_hdr_bracket(
        CODE,
        run_id=selected["run_id"],
        root=WORKSPACE / "camerae2e_db",
        workspace=WORKSPACE,
        output_dir=tmp_path / "evidence",
        exposure_s=[0.0005, 0.008],
        seed=11,
    )
    assert result["schema"] == "camerae2e_saved_sensor_hdr_bracket_v1"
    assert result["revision"]["run_id"] == selected["run_id"]
    assert result["controls"]["fixed_pattern_mode"] == "zero_fpn_override"
    assert result["controls"]["saved_fixed_pattern"]["prnu_fraction"] > 0
    assert set(result["controls"]["executed_fixed_pattern"].values()) == {0.0}
    assert result["fusion"]["frames"] == 2
    assert result["fusion"]["input_array_bytes_sha256"]
    assert result["verification"]["lfm_evaluated"] is False
    assert result["verification"]["physical_validation"] == "not_established"
    assert [capture["exposure_s"] for capture in result["captures"]] == [0.0005, 0.008]
    assert result["captures"][0]["noise_seed"] != result["captures"][1]["noise_seed"]
    assert len(result["levels"]) == 6
    assert result["levels"][0]["mean_merged_rate_e_per_pixel_s"] > result["levels"][-1][
        "mean_merged_rate_e_per_pixel_s"
    ]
    json_path = Path(result["artifacts"]["json"])
    npz_path = Path(result["artifacts"]["npz"])
    assert hashlib.sha256(json_path.read_bytes()).hexdigest() == result["artifact_sha256"][
        "json"
    ]
    assert hashlib.sha256(npz_path.read_bytes()).hexdigest() == result["artifact_sha256"][
        "npz"
    ]
    on_disk = json.loads(json_path.read_text())
    assert on_disk["artifacts"]["npz_sha256"] == result["artifact_sha256"]["npz"]
    with np.load(npz_path, allow_pickle=False) as arrays:
        raw = arrays["original_raw_dn"]
        assert raw.dtype == np.int32
        assert raw.shape == (2, 36, 36)
        assert hashlib.sha256(raw.tobytes()).hexdigest() == result["fusion"][
            "input_array_bytes_sha256"
        ]
        for index, capture in enumerate(result["captures"]):
            assert hashlib.sha256(raw[index].tobytes()).hexdigest() == capture[
                "original_raw_sha256"
            ]
        assert arrays["hdr_merged_rate"].shape == raw.shape[1:]
        assert arrays["hdr_valid_mask"].any()


@pytest.mark.parametrize(
    "change",
    [
        {"exposure_s": [0.01]},
        {"exposure_s": [0.01, 0.01]},
        {"exposure_s": [0.01, 0.001]},
        {"fixed_pattern_mode": "native_saved_fpn"},
        {"output_size": (64, 64)},
        {"luminance_cd_m2": 10**1000},
        {"seed": -1},
    ],
)
def test_hdr_rejects_unsupported_contract_before_evidence(tmp_path, change):
    selected = _selected()
    output = tmp_path / "untouched"
    options = {
        "run_id": selected["run_id"],
        "root": WORKSPACE / "camerae2e_db",
        "workspace": WORKSPACE,
        "output_dir": output,
    }
    options.update(change)
    with pytest.raises(ValueError):
        evaluate_saved_sensor_hdr_bracket(CODE, **options)
    assert not output.exists()
