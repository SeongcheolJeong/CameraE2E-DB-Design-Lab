from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pytest

from pyisetcam import (
    camerae2e_dataset_export,
    camerae2e_dataset_validate,
    camerae2e_faca_report,
    camerae2e_run_scenario,
    scene_create,
)


@pytest.fixture
def moving_scene():
    base = scene_create("uniform ee", 8)
    photons = np.asarray(base.data["photons"]).copy()
    cols = photons.shape[1]

    def scene_at(time_s: float):
        scene = base.clone()
        center = 1.5 + 150.0 * time_s
        profile = 0.05 + np.exp(-0.5 * ((np.arange(cols) - center) / 0.65) ** 2)
        scene.data["photons"] = photons * profile[None, :, None]
        return scene

    return scene_at


def _scenario():
    return {
        "name": "moving_temporal_scene",
        "sensor": {"noise_flag": 0, "integration_time": 0.001},
        "hw_isp": {
            "enabled": True,
            "nframes": 3,
            "fps": 200.0,
            "exposure_time_us": 1000.0,
            "line_time_us": 100.0,
            "active_lines": 8,
            "hidden_lines_top": 0,
            "hidden_lines_bottom": 0,
            "temporal_samples_per_exposure": 4,
            "temporal_max_scene_samples": 128,
            "ae_enabled": False,
            "awb_enabled": False,
        },
    }


@pytest.mark.parametrize("embedded", [False, True])
def test_faca_preserves_temporal_sequence_and_safe_scene_description(moving_scene, embedded):
    scenario = _scenario()
    kwargs = {"scene": moving_scene}
    if embedded:
        scenario["scene"] = moving_scene
        kwargs = {}
    result = camerae2e_run_scenario(scenario, seed=8, **kwargs)
    frames = result["hw_isp_sequence"].frames

    assert len(frames) == 3
    assert result["camera"] is frames[-1].camera
    assert not np.array_equal(frames[0].sensor.data["raw_dn"], frames[1].sensor.data["raw_dn"])
    assert result["stages"]["scene_photons"]["reference_time_s"] == 0.0
    report = camerae2e_faca_report(result)
    assert report["scenario"]["scene"]["serialization"] == "external_callable_not_serialized"
    assert report["temporal_capture"]["rolling_shutter"] is True
    encoded = json.dumps(report)
    assert "<function" not in encoded


def test_sequence_dataset_exports_distinct_frames_and_timing(tmp_path: Path, moving_scene):
    manifest = camerae2e_dataset_export(
        tmp_path / "sequence",
        [_scenario()],
        scene=moving_scene,
        seed=13,
        export_sequence=True,
        include_stage_outputs=True,
        include_container=True,
        labels={"objects": []},
        split="train",
    )
    records = manifest["records"]
    assert manifest["case_count"] == 3
    assert manifest["splits"] == {"train": 3}
    assert manifest["split_policy"]["assignment_unit"] == "scenario"
    assert len({record["raw"] for record in records}) == 3
    assert len({record["rgb"] for record in records}) == 3
    raw_frames = []
    for index, record in enumerate(records):
        assert record["sequence_id"] == "case_0000"
        assert record["frame_index"] == index
        assert record["frame_count"] == 3
        assert record["seed"] == 13 + index
        assert record["timing"]["frame_id"] == index
        assert record["controls_applied"]["exposure_time_us"] == 1000.0
        assert record["temporal_capture"]["sample_count"] > 1
        assert record["temporal_capture"]["frame_start_s"] == pytest.approx(
            record["timing"]["timestamps_us"]["exposure_start"] * 1e-6
        )
        assert Path(record["rgb"]).exists()
        with np.load(record["raw"], allow_pickle=False) as payload:
            raw_frames.append(payload["raw"])
            np.testing.assert_array_equal(payload["seed"], [13 + index])
        with np.load(record["stage_outputs"], allow_pickle=False) as payload:
            np.testing.assert_array_equal(payload["sensor_raw"], raw_frames[-1])
        with ZipFile(record["container"]) as archive:
            container = json.loads(archive.read("container_manifest.json"))
            assert container["record"]["frame_index"] == index
    assert not np.array_equal(raw_frames[0], raw_frames[1])
    metadata = [
        json.loads(line)
        for line in (tmp_path / "sequence" / "metadata.jsonl").read_text().splitlines()
    ]
    assert [record["frame_index"] for record in metadata] == [0, 1, 2]
    assert camerae2e_dataset_validate(manifest)["ok"] is True

    legacy = camerae2e_dataset_export(tmp_path / "last", [_scenario()], scene=moving_scene, seed=13)
    assert legacy["case_count"] == 1
    assert "frame_index" not in legacy["records"][0]
    assert Path(legacy["records"][0]["raw"]).name == "case_0000.npz"
    with np.load(legacy["records"][0]["raw"], allow_pickle=False) as payload:
        np.testing.assert_array_equal(payload["raw"], raw_frames[-1])


def test_temporal_faca_requires_hw_and_scene_return_type():
    calls = []

    def scene_at(time_s):
        calls.append(time_s)
        return None

    with pytest.raises(ValueError, match="hw_isp.enabled=True"):
        camerae2e_run_scenario(scene=scene_at)
    assert calls == []
    with pytest.raises(TypeError, match="must return a Scene"):
        camerae2e_run_scenario(_scenario(), scene=scene_at)


def test_sequence_export_requires_enabled_hw(tmp_path: Path):
    with pytest.raises(ValueError, match="export_sequence=True requires"):
        camerae2e_dataset_export(
            tmp_path,
            [{"scene": {"type": "uniform ee", "args": [8]}}],
            export_sequence=True,
        )
