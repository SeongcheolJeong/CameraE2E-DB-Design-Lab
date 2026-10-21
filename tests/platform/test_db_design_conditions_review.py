"""Independent review of frozen response reuse and fresh readout semantics."""

import copy
import hashlib
import json

import numpy as np
import pytest

from camera_platform import db_design_conditions as conditions
from camera_platform.electrical import default_electrical_model
from camera_platform.reference_isp import default_profile


def test_manifest_bound_reader_rejects_changed_or_unlisted_bytes(tmp_path):
    original = b'{"source": "frozen"}'
    (tmp_path / "source.json").write_bytes(original)
    manifest = {
        "schema": "camera_reference_workshop_files_v1",
        "files": {"source.json": hashlib.sha256(original).hexdigest()},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    expected = hashlib.sha256((tmp_path / "manifest.json").read_bytes()).hexdigest()
    reader = conditions.VerifiedSource(tmp_path, expected)
    assert reader.json("source.json") == {"source": "frozen"}
    (tmp_path / "source.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash changed"):
        reader.read("source.json")
    with pytest.raises(ValueError, match="not present"):
        reader.read("unlisted.json")
    for name in ("../source.json", "/source.json", "nested/../source.json"):
        with pytest.raises(ValueError, match="normalized relative"):
            reader.read(name)
    with pytest.raises(ValueError, match="manifest changed"):
        conditions.VerifiedSource(tmp_path, "0" * 64)


def test_manifest_bound_reader_rejects_nonfinite_array(tmp_path):
    path = tmp_path / "rate.npy"
    np.save(path, np.array([[float("nan")]]))
    (tmp_path / "manifest.json").write_text(json.dumps({
        "schema": "camera_reference_workshop_files_v1",
        "files": {"rate.npy": hashlib.sha256(path.read_bytes()).hexdigest()},
    }))
    with pytest.raises(ValueError, match="finite and numeric"):
        conditions.VerifiedSource(tmp_path).array("rate.npy")


def _payload():
    readout = {
        "gain": 1, "full_well_e": 10000., "read_noise_e": 10.,
        "dark_current_e_per_s": 10., "bit_depth": 12, "black_level_dn": 64.,
    }
    calibration = {"color": {"matrix": np.eye(3).tolist()}, "lsc": None, "identity": "frozen"}
    spec = {
        "frames": 8, "seed": 7, "gain": 1, "luminance_cd_m2": 1000.,
        "exposure_ms": 4., "isp": default_profile(),
    }
    candidate = {
        "id": "candidate-01", "prefix": "candidates/candidate-01/", "label": "test aperture",
        "result": {"configuration": spec, "limitations": []},
        "source_identities": {"lens": {"revision": "fixed"}},
    }
    fields = {}
    for field in conditions.FIELDS:
        capture = {
            "configuration": copy.deepcopy(spec), "readout": copy.deepcopy(readout),
            "calibration": copy.deepcopy(calibration["color"]),
            "scene_reference": {"reference_xyz": np.ones((24, 3)).tolist(), "white_xyz": [1., 1., 1.]},
            "captures": {chart: {"rate_sha256": "rate-source", "planes_sha256": "planes-source"}
                         for chart in conditions.CHARTS},
        }
        fields[field] = {"capture": capture, "optics": {"id": field}, "arrays": {
            chart: {"mean-electron-rate.npy": np.full((16, 16), 250000.),
                    "planes-electron-rate.npy": np.full((16, 16, 4), 250000.),
                    "source-reference-linear.npy": np.full((16, 16, 3), .5)}
            for chart in conditions.CHARTS}}
    return {
        "candidate": candidate, "fields": fields, "support": {},
        "calibration": calibration, "pixel-response": {}, "optical-response": {},
        "pixel-cell": {"readout": readout, "electrical_model": default_electrical_model(readout)},
    }


def test_fresh_readout_scales_rates_by_light_only_and_freezes_isp(tmp_path, monkeypatch):
    payload = _payload()
    original = copy.deepcopy(payload)
    rendered = []

    def render(folder, capture, spec, optical, response, cell, calibration):
        # Capture units, white, CCM and processing are common; no exposure-derived gain.
        assert spec["isp"] == original["candidate"]["result"]["configuration"]["isp"]
        assert calibration == original["calibration"]
        rendered.append((folder, copy.deepcopy(capture)))
        for chart in conditions.CHARTS:
            np.save(folder / chart / "processed.npy", np.zeros((16, 16, 3)))
        return {"id": optical["id"], "metrics": {"temporal_noise_dn": 1.}, "tests": {"noise": {}}}

    monkeypatch.setattr(conditions.reference, "_render_field", render)
    runid = {"run_id": "source", "manifest_sha256": "frozen-manifest"}
    for identity, light, exposure in (("short", 1000., 1.), ("long", 1000., 4.), ("bright", 2000., 1.)):
        conditions._camera(tmp_path, payload, {
            "id": identity, "luminance_cd_m2": light, "exposure_ms": exposure,
        }, runid, lambda _: None, lambda: None)
    roots = {identity: tmp_path / identity / "candidates/candidate-01/fields/center/uniform"
             for identity in ("short", "long", "bright")}
    rates = {key: np.load(folder / "mean-electron-rate.npy") for key, folder in roots.items()}
    assert np.array_equal(rates["short"], rates["long"])
    assert np.array_equal(rates["bright"], 2 * rates["short"])
    raws = {key: np.load(folder / "raw-00.npy") for key, folder in roots.items()}
    assert not np.array_equal(raws["long"] - 64, 4 * (raws["short"] - 64))
    assert not np.array_equal(raws["bright"] - 64, 2 * (raws["short"] - 64))
    assert all(raw.dtype.kind in "iu" and raw.min() >= 0 and raw.max() <= 4095 for raw in raws.values())
    for _, capture in rendered:
        records = capture["captures"]["uniform"]["raw"]
        assert len(records) == 8 and len({record["seed"] for record in records}) == 8
    for field in conditions.FIELDS:
        assert payload["fields"][field]["capture"] == original["fields"][field]["capture"]
        for chart in conditions.CHARTS:
            for name, array in payload["fields"][field]["arrays"][chart].items():
                assert np.array_equal(array, original["fields"][field]["arrays"][chart][name])


def test_recapture_checks_cancellation_before_writing_any_raw(tmp_path, monkeypatch):
    def stop():
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        conditions._camera(tmp_path, _payload(), {
            "id": "cancelled", "luminance_cd_m2": 1000., "exposure_ms": 4.,
        }, {"run_id": "source"}, lambda _: None, stop)
    assert not list(tmp_path.rglob("raw-*.npy"))
