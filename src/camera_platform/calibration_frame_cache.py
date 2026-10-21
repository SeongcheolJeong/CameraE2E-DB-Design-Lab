"""Hash-checked full native frames, keyed by numerical code and complete inputs.

A reused frame never counts as a completed calibration job. Every new job keeps
its own source guard, parents, independent curves/windows, partition check and
activation CAS. Generic API/worker additions cannot change this pixel operator;
changes to its math, inputs, kernels, scene or numeric libraries invalidate cache.
"""

from __future__ import annotations

import ast
import fcntl
import inspect
import json
import os
import platform
import shutil
import textwrap
import time
from copy import deepcopy
from pathlib import Path

import numpy as np

from . import calibration_direct_optics as optics
from .registry import digest

SCHEMA = "calibration_native_frame_cache_v1"
MATH_FILES = (
    "calibration_spatial_psf.py",
    "calibration_direct_convergence.py",
    "calibration_db_core_engine.py",
    "calibration_lab_engine.py",
    "reference_isp.py",
    "virtual_lab_metrics.py",
)


def definition(text, name, *, normalize_capture=False):
    tree = ast.parse(text)
    node = next(
        v for v in tree.body if isinstance(v, ast.FunctionDef | ast.ClassDef) and v.name == name
    )
    node = deepcopy(node)
    # The old public capture body is moved unchanged under a cache wrapper.
    if normalize_capture or name in {"capture", "_capture_native"}:
        node.name = "native_capture_math"
    return digest(ast.dump(node, include_attributes=False))


def contract(
    profile, application, scene, tile, *, source_text=None, capture_name="_capture_native"
):
    from . import calibration_anchor_density as renderer

    text = source_text if source_text is not None else Path(renderer.__file__).read_text()
    directory = Path(renderer.__file__).parent
    a = deepcopy(application)
    a.pop("renderer_sha256", None)
    a["anchor_order"] = list(a["anchors"])
    a["kernels"] = [{k: v for k, v in row.items() if k != "file"} for row in a["kernels"]]
    fragments = {
        name: definition(text, name)
        for name in (capture_name, "estimate", "weights", "KernelBank", "BoundaryAtlas")
    }
    fragments["native_capture_math"] = fragments.pop(capture_name)
    if source_text is None:
        driver = renderer._capture_native
        fragments["native_capture_math"] = definition(
            textwrap.dedent(inspect.getsource(driver)), driver.__name__, normalize_capture=True
        )
    files = {name: optics.sha(directory / name) for name in MATH_FILES}
    engine = directory.parent / "pyisetcam"
    files.update({"pyisetcam/" + p.name: optics.sha(p) for p in sorted(engine.glob("*.py"))})
    inputs = {
        k: deepcopy(profile[k])
        for k in ("fields", "electronics", "conditions", "isp", "isp_tuning")
    }
    inputs["sensor"] = {
        k: deepcopy(profile["sensor_crops"][k])
        for k in (
            "full_sensor_shape",
            "white_e_rgb",
            "incident_photons_per_pixel",
            "pixel_pitch_um",
            "optical_axis_active_row_col",
        )
    }
    return dict(
        schema=SCHEMA,
        application=a,
        numeric_inputs=inputs,
        scene=scene.specification(),
        tile=tile,
        python=platform.python_version(),
        system=platform.platform(),
        numpy=np.__version__,
        scipy=renderer.spatial.scipy.__version__,
        numerical_fragments=fragments,
        numerical_files=files,
    )


def folder(workspace, key):
    return Path(workspace) / ".cache" / "calibration-native-frames" / key


def verify(base, record, expected):
    if record.get("manifest_sha256") != digest(
        {k: v for k, v in record.items() if k != "manifest_sha256"}
    ):
        raise ValueError("Native frame cache manifest changed")
    if (
        record.get("schema") != SCHEMA
        or record.get("contract") != expected
        or record.get("key") != digest(expected)
    ):
        raise ValueError("Native frame cache contract differs")
    h, w = expected["numeric_inputs"]["sensor"]["full_sensor_shape"]
    for name, shape, dtype in [("raw", (h, w), np.uint16), ("linear", (h, w, 3), np.float64)]:
        path = Path(base) / (name + ".npy")
        if record["capture"].get(name + "_sha256") != record["files"][name]["sha256"]:
            raise ValueError("Cached capture file binding differs")
        if optics.sha(path) != record["files"][name]["sha256"]:
            raise ValueError("Native frame cache bytes changed: " + name)
        a = np.load(path, mmap_mode="r", allow_pickle=False)
        if a.shape != shape or a.dtype != dtype:
            raise ValueError("Native frame cache array contract differs")
        if name == "raw":
            bits = expected["numeric_inputs"]["electronics"]["bit_depth"]
            clipped = int(np.count_nonzero(a >= 2**bits - 1))
            if clipped != record["capture"]["clipped_adc_pixels"]:
                raise ValueError("Cached ADC clipping diagnostics differ")
        for row in range(0, h, 128):
            if not np.isfinite(a[row : row + 128]).all():
                raise ValueError("Nonfinite cached frame")


def _store(workspace, spec, capture, root, *, origin=None):
    key = digest(spec)
    destination = folder(workspace, key)
    if (destination / "manifest.json").exists():
        record = json.loads((destination / "manifest.json").read_text())
        verify(destination, record, spec)
        if any(
            record["files"][name]["sha256"] != capture[name + "_sha256"]
            for name in ("raw", "linear")
        ):
            raise ValueError("Conflicting results for one deterministic numerical contract")
        return record
    destination.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ("raw", "linear"):
        src = Path(root) / capture[name]
        expected = capture[name + "_sha256"]
        if optics.sha(src) != expected:
            raise ValueError("Unverified native frame cannot enter cache")
        tmp = destination / (name + ".npy.tmp")
        if tmp.exists():
            tmp.unlink()
        try:
            os.link(src, tmp)
        except OSError:
            shutil.copyfile(src, tmp)
        tmp.replace(destination / (name + ".npy"))
        files[name] = {"sha256": expected, "bytes": src.stat().st_size}
    record = dict(
        schema=SCHEMA,
        key=key,
        contract=spec,
        capture=deepcopy(capture),
        files=files,
        origin=origin
        or dict(run_id=Path(root).name, status="computed_under_frozen_numerical_contract"),
    )
    record["manifest_sha256"] = digest(record)
    verify(destination, record, spec)
    optics.save(destination / "manifest.json", record)
    return record


def store(workspace, spec, capture, root, *, origin=None):
    destination = folder(workspace, digest(spec))
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / "cache.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        return _store(workspace, spec, capture, root, origin=origin)


def capture(
    driver, profile, root, application, bank, scene, out, tile=128, progress=None, *, workspace=None
):
    from . import calibration_anchor_density as renderer
    from .calibration_anchor_density import BoundaryAtlas

    if driver is not renderer._capture_native:
        raise ValueError("Only the pinned native renderer may populate frame cache")

    if type(scene) is BoundaryAtlas and (
        scene.angle not in (0, 90) or scene.phase not in renderer.direct.PHASES
    ):
        raise ValueError("Select a qualified edge orientation and phase")
    if workspace is None or type(scene) is not BoundaryAtlas:
        return driver(profile, root, application, bank, scene, out, tile, progress)
    spec = contract(profile, application, scene, tile)
    key = digest(spec)
    base = folder(workspace, key)
    if not (base / "manifest.json").exists():
        result = driver(profile, root, application, bank, scene, out, tile, progress)
        store(workspace, spec, result, root)
        result["native_frame_cache"] = {"status": "computed", "key": key}
        return result
    record = json.loads((base / "manifest.json").read_text())
    verify(base, record, spec)
    if progress:
        progress(
            "동일 수치 계약의 전체 센서 캐시 검증 · " + str(len(application["anchors"])) + "점"
        )
    start = time.perf_counter()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    result = deepcopy(record["capture"])
    for name in ("raw", "linear"):
        dest = out / (name + ".npy")
        shutil.copyfile(base / (name + ".npy"), dest)
        if optics.sha(dest) != record["files"][name]["sha256"]:
            raise ValueError("Cached frame transfer changed")
        result[name] = str(dest.relative_to(Path(root)))
    result["native_frame_cache"] = dict(
        status="verified_reuse",
        key=key,
        origin=record["origin"],
        restored_seconds=time.perf_counter() - start,
        original_elapsed_s=result["elapsed_s"],
    )
    return result
