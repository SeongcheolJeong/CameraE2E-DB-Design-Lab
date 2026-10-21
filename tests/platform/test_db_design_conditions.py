"""Condition recapture retains fixed optics/ISP while reacquiring the sensor."""

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform import db_design_conditions as engine
from camera_platform.electrical import default_electrical_model
from camera_platform.reference_isp import default_profile
from camera_platform.virtual_lab_job import sha256


def _payload(index=1, rate=2e5):
    readout = {"bit_depth": 12, "black_level_dn": 64, "full_well_e": 10000,
               "read_noise_e": 5, "dark_current_e_per_s": 3, "gain": 1}
    cell = {"readout": readout, "electrical_model": default_electrical_model(readout)}
    calibration = {"color": {"matrix": np.eye(3).tolist()}, "lsc": None, "identity": "fixed"}
    spec = {"luminance_cd_m2": 1000, "exposure_ms": 4, "gain": 1, "frames": 8,
            "seed": 7, "isp": default_profile(), "lens": {"optical_profile": "exit_pupil_diffraction"}}
    geometry = {"native_shape_row_col": [256, 256],
                "cfa_origin": {"pattern": "RGGB", "local_row": 0, "local_col": 0}}
    fields = {}
    for field_id in engine.FIELDS:
        fields[field_id] = {
            "optics": {"id": field_id, "geometry": geometry},
            "capture": {"configuration": spec, "geometry": geometry, "readout": readout,
                        "calibration": calibration["color"],
                        "scene_reference": {"reference_xyz": [[1, 1, 1]] * 24, "white_xyz": [1, 1, 1]},
                        "captures": {k: {"rate_sha256": "source-rate", "planes_sha256": "source-planes"}
                                     for k in engine.CHARTS}},
            "arrays": {k: {"mean-electron-rate.npy": np.full((256, 256), rate),
                           "planes-electron-rate.npy": np.full((256, 256, 4), rate),
                           "source-reference-linear.npy": np.ones((256, 256, 3), np.float32)}
                       for k in engine.CHARTS},
        }
    candidate = {"id": f"candidate-{index:02d}", "label": f"F/{index}", "lens_key": f"lens-{index}",
                 "result": {"configuration": spec, "limitations": []},
                 "source_identities": {"lens": {"source": {"paraxial": {"effective_f_number": index + 2}}}}}
    return {"candidate": candidate, "pixel-cell": cell, "pixel-response": {}, "calibration": calibration,
            "optical-response": {}, "support": {}, "fields": fields}


def _fake_render(folder, capture, spec, optics, response, cell, calibration):
    for chart in engine.CHARTS:
        np.save(folder / chart / "processed.npy", np.ones((256, 256, 3), np.float32) * .5)
    return {"id": optics["id"], "status": "succeeded", "metrics": {"temporal_noise_dn": 2},
            "tests": {"noise": {}, "resolution": {"status": "ok", "sites": {"center": {
                "processed_system": {"status": "ok", "mtf50_cycles_per_pixel": .2},
                "roi": [108, 148, 108, 148]}}}}}


def test_verified_source_rejects_changed_bytes_and_path_escape(tmp_path):
    payload = b'{"meaning":"pre-noise"}'
    (tmp_path / "rate.json").write_bytes(payload)
    manifest = {"schema": "camera_reference_workshop_files_v1", "files": {
        "rate.json": hashlib.sha256(payload).hexdigest()}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    reader = engine.VerifiedSource(tmp_path, sha256(tmp_path / "manifest.json"))
    assert reader.json("rate.json") == {"meaning": "pre-noise"}
    assert set(reader.used) == {"rate.json"}
    with pytest.raises(ValueError, match="relative path"):
        reader.read("../rate.json")
    (tmp_path / "rate.json").write_bytes(payload + b" ")
    with pytest.raises(ValueError, match="hash changed"):
        reader.read("rate.json")
    with pytest.raises(ValueError, match="manifest changed"):
        engine.VerifiedSource(tmp_path, "0" * 64)


def test_light_scales_rate_exposure_reacquires_and_keeps_fixed_isp(tmp_path, monkeypatch):
    monkeypatch.setattr(engine.reference, "_render_field", _fake_render)
    payload = _payload()
    snapshot = copy.deepcopy(payload["candidate"]["result"]["configuration"])
    identity = {"run_id": "source", "manifest_sha256": "a" * 64}
    for index, luminance, exposure in ((0, 1000, 4), (1, 250, 16), (2, 250, 4)):
        result = engine._camera(tmp_path, payload,
            {"id": f"condition-{index:03d}", "luminance_cd_m2": luminance, "exposure_ms": exposure},
            identity, lambda _: None, lambda: None)
        assert result["result"]["configuration"]["isp"] == snapshot["isp"]
        assert result["prefix"] == f"conditions/condition-{index:03d}/candidates/candidate-01/"
    folders = [tmp_path / f"condition-{i:03d}/candidates/candidate-01/fields/center/uniform" for i in range(3)]
    rates = [np.load(p / "mean-electron-rate.npy") for p in folders]
    raws = [np.load(p / "raw-00.npy") for p in folders]
    assert np.array_equal(rates[1], rates[0] * .25)
    assert np.array_equal(rates[2], rates[1])  # exposure never scales photons/second
    assert not np.array_equal(raws[2], np.round((raws[0] - 64) * .25 + 64))
    summaries = [json.loads((p / "capture.json").read_text()) for p in folders]
    assert [s["exposure_s"] for s in summaries] == [.004, .016, .004]
    assert all(s["noise_enabled"] for s in summaries)
    assert payload["candidate"]["result"]["configuration"] == snapshot
    captures = [json.loads((p.parent / "capture.json").read_text()) for p in folders]
    assert captures[1]["scene_reference"]["white_xyz"] == [1, 1, 1]
    assert captures[2]["scene_reference"]["white_xyz"] == [.25, .25, .25]
    for capture in captures:
        assert len(set(r["seed"] for r in capture["captures"]["uniform"]["raw"])) == 8
        assert capture["calibration"] == payload["calibration"]["color"]
    assert len(list(tmp_path.glob("condition-*/candidates/*/fields/*/*/raw-*.npy"))) == 192


def test_cancellation_stops_before_acquisition(tmp_path):
    def stop():
        raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        engine._camera(tmp_path, _payload(),
            {"id": "condition-000", "luminance_cd_m2": 100, "exposure_ms": 1}, {}, lambda _: None, stop)
    assert not list(tmp_path.rglob("raw-*.npy"))


def test_condition_default_and_budget_check_are_per_condition(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    result = {"id": "source", "configuration": {"f_numbers": [2.75, 4.12]}}
    (source / "result.json").write_text(json.dumps(result))
    (source / "manifest.json").write_text(json.dumps({
        "schema": "camera_reference_workshop_files_v1", "files": {"result.json": sha256(source / "result.json")}}))
    monkeypatch.setattr(engine, "_validate_source", lambda reader, summary: [_payload(1), _payload(2, 1e5)])
    monkeypatch.setattr(engine.reference, "_render_field", _fake_render)
    checks = []
    request = {"source_run_id": "source", "luminance_cd_m2": [100, 1000], "exposure_ms": [4],
               "selection_policy": {"min_snr_db": 25, "min_relative_signal": 0, "max_saturation_fraction": .01}}
    output = engine.run(None, tmp_path / "output", request, source, result, lambda _: None,
                        check=lambda: checks.append(True))
    assert len(checks) == 3  # initial + completed conditions, never each frame
    assert output["capture_count"] == 256
    assert output["aperture_conditions"]["default_condition_id"] == "condition-001"
    assert output["candidates"][0]["prefix"].startswith("conditions/condition-001/")
    assert output["aperture_conditions"]["optical_response_recomputed"] is False
    assert output["calibration"] == _payload()["calibration"]
    assert len(output["aperture_conditions"]["conditions"]) == 2
