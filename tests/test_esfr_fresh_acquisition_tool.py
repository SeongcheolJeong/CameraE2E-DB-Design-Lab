"""Unit fixtures exercise acquisition guards without creating real chart holdouts."""
import importlib.util
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from camera_platform.esfr_lab import chart_template
from camera_platform.registry import digest

script = Path(__file__).resolve().parents[1] / "tools" / "acquire_fresh_esfr_holdout.py"
spec = importlib.util.spec_from_file_location("fresh_esfr_acquisition_tool", script)
acquisition = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acquisition)


def inputs():
    rois = [{"id": "unit-edge", "kind": "edge", "field_id": "unit-edge", "box": [0, 0, 16, 16]}]
    development = []
    for i in range(3):
        development.append({"id": f"dev-{i}", "metadata": {
            "role": "development", "source_kind": "synthetic", "camera_id": "unit-camera",
            "conditions": {"gain": 1}, "raw_format": {"height": 16, "width": 16, "cfa": "RGGB"},
            "geometry": {"full_sensor_shape": [16, 16], "crop_origin": [0, 0]}, "isp": {},
            "synthetic_generator": {"method": "full_chart_linear_rgb_gaussian_rggb_v1", "seed": 100 + i,
                                    "discrete_gaussian_sigma_native_px": 1.4, "pixel_area_supersample": 4}}})
    contract = {"schema": "camera_esfr_frozen_holdout_contract_v1", "id": "unit-contract",
                "created_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                "development_capture_ids": [c["id"] for c in development], "required_validation_capture_count": 3,
                "chart_profile": {"chart_geometry": chart_template()}, "rois": rois,
                "parent_study_id": "unit-frozen-parent", "candidate_sha256": "a" * 64}
    contract["content_hash"] = digest(contract)
    return contract, development


def test_only_fresh_noise_ids_and_bytes_change_with_private_truth_separate(tmp_path):
    contract, development = inputs()
    def render(**kwargs):
        assert kwargs == {"shape": (16, 16), "supersample": 4}
        return np.zeros((16, 16, 3)), contract["rois"], {"scope": "unit-test-only"}
    def capture(scene, seed, sigma):
        assert sigma == 1.4
        raw = np.random.default_rng(seed).integers(0, 4095, (16, 16), dtype=np.uint16)
        return raw, np.repeat(raw[..., None], 3, axis=2)
    folder = tmp_path / "unit-captures"
    manifest = acquisition.generate(contract, development, folder, renderer=render, capture_generator=capture)
    assert len(manifest["captures"]) == 3
    assert len({row["raw_sha256"] for row in manifest["captures"]}) == 3
    assert len({row["acquisition_id"] for row in manifest["captures"]}) == 3
    assert manifest["fit_performed"] is False and manifest["previous_holdout_read"] is False
    assert manifest["generator_truth_uploaded_to_fitter"] is False
    for row in manifest["captures"]:
        metadata = json.loads((folder / row["metadata_path"]).read_text())
        assert metadata["role"] == "validation" and metadata["conditions"] == {"gain": 1}
        assert metadata["validation_contract"] == {"id": contract["id"], "content_hash": contract["content_hash"]}
        assert "discrete_gaussian_sigma_native_px" not in metadata["synthetic_generator"]
        assert row["seed"] >= 1 << 62
    assert json.loads((folder / "private-generator-truth.json").read_text())["discrete_gaussian_sigma_native_px"] == 1.4


def test_tampered_contract_or_old_validation_input_is_refused():
    contract, development = inputs()
    changed = deepcopy(contract); changed["candidate_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="checksum"):
        acquisition.check_contract(changed, development)
    development[0]["metadata"]["role"] = "validation"
    with pytest.raises(ValueError, match="Previous validation"):
        acquisition.check_contract(contract, development)


def test_upload_bootstraps_session_and_never_uploads_private_generator_truth(tmp_path, monkeypatch):
    contract, _ = inputs()
    contract.update(thresholds={"raw_sfr_rmse": 0.03, "isp_sfr_rmse": 0.05}, baseline_sigma_native_px=2.4, source=None)
    row = {}
    for key, content in (("metadata", b'{"role":"validation"}'), ("raw", b"raw-original"), ("isp", b"isp-original")):
        path = tmp_path / (key + ".bin"); path.write_bytes(content)
        row[key + "_path"] = path.name; row[key + "_sha256"] = acquisition._sha(path)
    class Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data
    seen = []
    def get(url, **kwargs):
        assert url.endswith("/api/v1/session")
        return Response({"token": "unit-secret-token"})
    def post(url, **kwargs):
        assert url.endswith("/api/v1/camera-validation/esfr/captures")
        assert kwargs["headers"] == {"X-Platform-Token": "unit-secret-token"}
        assert set(kwargs["files"]) == {"raw", "isp"}
        assert "truth" not in kwargs["data"]["metadata"]
        seen.append(url)
        return Response({"id": "fresh-unit-capture"})
    monkeypatch.setattr(acquisition.requests, "get", get)
    monkeypatch.setattr(acquisition.requests, "post", post)
    request = acquisition.upload("http://localhost:8780", tmp_path, {"captures": [row]}, contract)
    assert request["fit"] is False and request["validation_ids"] == ["fresh-unit-capture"]
    assert len(seen) == 1
