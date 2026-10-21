"""Numerical cache integrity with artificial tiny native-frame producers."""

from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

from camera_platform import calibration_anchor_density as renderer
from camera_platform import calibration_direct_optics as optics
from camera_platform import calibration_frame_cache as cache


def inputs():
    p = {
        "fields": {"center": {}},
        "electronics": {"bit_depth": 12},
        "conditions": {"gain": 1},
        "isp": {"test_scalar": 1},
        "isp_tuning": {},
        "sensor_crops": {
            "full_sensor_shape": [257, 257],
            "white_e_rgb": [1, 2, 3],
            "incident_photons_per_pixel": 8000.0,
            "pixel_pitch_um": 2.9,
            "optical_axis_active_row_col": [128, 128],
        },
    }
    a = {
        "renderer_sha256": "irrelevant-whole-module-tag",
        "anchors": {"a": [0, 0]},
        "kernels": [{"field": "a", "file": "one.npz", "sha256": "a" * 64}],
        "setting": {"samples_per_pixel": 8, "radius_native_px": 64},
    }
    return p, a


@pytest.fixture
def producer(monkeypatch):
    calls = []

    def native(profile, root, application, bank, scene, out, tile=128, progress=None):
        calls.append(1)
        out = Path(out)
        out.mkdir(parents=True, exist_ok=False)
        raw = np.full(
            (257, 257), 100 + profile["isp"]["test_scalar"] + int(scene.phase * 100), np.uint16
        )
        linear = np.repeat(raw[..., None].astype(float), 3, axis=2) / 1000
        np.save(out / "raw.npy", raw)
        np.save(out / "linear.npy", linear)
        return dict(
            raw=str((out / "raw.npy").relative_to(root)),
            linear=str((out / "linear.npy").relative_to(root)),
            raw_sha256=optics.sha(out / "raw.npy"),
            linear_sha256=optics.sha(out / "linear.npy"),
            clipped_adc_pixels=0,
            elapsed_s=1.0,
            shape_row_col=[257, 257],
        )

    monkeypatch.setattr(renderer, "_capture_native", native)
    return native, calls


def test_same_numeric_contract_reuses_and_changed_ccm_kernel_phase_tile_recompute(
    tmp_path, producer
):
    native, calls = producer
    p, a = inputs()
    scene = renderer.BoundaryAtlas(p["sensor_crops"], 0, 0.0)
    work = tmp_path / "workspace"
    one = tmp_path / "run-one"
    two = tmp_path / "run-two"
    one.mkdir()
    two.mkdir()
    first = cache.capture(native, p, one, a, None, scene, one / "case", workspace=work)
    moved = deepcopy(a)
    moved["renderer_sha256"] = "wrapper-change"
    moved["kernels"][0]["file"] = "relocated.npz"
    second = cache.capture(native, p, two, moved, None, scene, two / "case", workspace=work)
    assert len(calls) == 1 and first["raw_sha256"] == second["raw_sha256"]
    assert second["native_frame_cache"]["status"] == "verified_reuse"
    assert second["native_frame_cache"]["origin"]["run_id"] == "run-one"
    q = deepcopy(p)
    q["isp"]["test_scalar"] = 2
    cache.capture(native, q, two, a, None, scene, two / "ccm", workspace=work)
    b = deepcopy(a)
    b["kernels"][0]["sha256"] = "b" * 64
    cache.capture(native, p, two, b, None, scene, two / "kernel", workspace=work)
    other = renderer.BoundaryAtlas(p["sensor_crops"], 0, 0.25)
    cache.capture(native, p, two, a, None, other, two / "phase", workspace=work)
    cache.capture(native, p, two, a, None, scene, two / "tile", tile=96, workspace=work)
    assert len(calls) == 5


def test_tampered_cache_fails_before_creating_new_output(tmp_path, producer):
    native, calls = producer
    p, a = inputs()
    scene = renderer.BoundaryAtlas(p["sensor_crops"], 0, 0.0)
    root = tmp_path / "run"
    root.mkdir()
    work = tmp_path / "work"
    first = cache.capture(native, p, root, a, None, scene, root / "one", workspace=work)
    target = cache.folder(work, first["native_frame_cache"]["key"]) / "raw.npy"
    target.write_bytes(b"corrupt frame")
    with pytest.raises(ValueError, match="bytes changed"):
        cache.capture(native, p, root, a, None, scene, root / "two", workspace=work)
    assert len(calls) == 1 and not (root / "two").exists()


def test_public_applied_capture_has_no_cache_workspace(monkeypatch, tmp_path):
    calls = []

    def calculate(*args, **kwargs):
        calls.append(kwargs)
        return "calculated"

    monkeypatch.setattr(renderer, "_capture_native", calculate)
    # Even when the scene is cacheable, no workspace means actual execution.
    p, a = inputs()
    scene = renderer.BoundaryAtlas(p["sensor_crops"], 0, 0.0)
    assert renderer.capture(p, tmp_path, a, None, scene, tmp_path / "out") == "calculated"
    assert calls == [{}]


def test_validated_active_profile_replay_always_executes_native_math(monkeypatch, tmp_path):
    calls = []

    def calculate(*args, **kwargs):
        calls.append((args, kwargs))
        return "native-executed"

    monkeypatch.setattr(renderer, "_capture_native", calculate)
    p, a = inputs()
    a["grid_size"] = 5
    p["psf_anchor_geometry"] = {}
    p["boundary_spatial_psf"] = {**a, "validation_status": "passed"}
    p["profile_sha256"] = cache.digest(p)
    monkeypatch.setattr(renderer, "application", lambda *args: a)
    scene = renderer.BoundaryAtlas(p["sensor_crops"], 0, 0.0)
    assert renderer.capture_applied(p, tmp_path, scene, tmp_path / "out") == "native-executed"
    assert len(calls) == 1 and calls[0][1] == {}
