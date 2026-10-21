#!/usr/bin/env python3
"""Verify one freshly installed R1 wheel through its API, Worker and browser.

Run with a new project root outside this checkout and a new output directory
under ``reports/camera_platform``. The report is evidence, not product approval
or physical-camera validation. Every incomplete check remains blocked.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = ROOT / "reports/camera_platform"
SCHEMA = "camera_platform_r1_install_evidence_v1"
PACKAGE_SCHEMA = "camera_platform_installed_package_v1"
CHECKS = (
    "isolated_install",
    "installed_imports",
    "required_assets",
    "two_model_scene",
    "numerical_contract",
    "deterministic_reproduction",
    "complete_user_journey",
    "failure_paths",
    "restart_restore",
    "performance",
    "support_display_consistency",
    "release_file_identity",
)
RC2_CHECKS = ("design_isp_replay", "selected_temporal_hdr_dcg")
FAILURE_CASES = (
    "invalid_input",
    "no_candidate",
    "evaluation_failure",
    "cancelled",
    "interrupted",
    "save_failure",
)
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
PACKAGE_PREFIXES = ("camera_platform/", "pyisetcam/")


def sha(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def integer_raw_dtype(value: object) -> bool:
    """Accept the engine's signed integer code container as well as unsigned RAW."""
    return isinstance(value, str) and re.fullmatch(r"u?int(?:8|16|32)", value) is not None


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def safe_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "CAMERAE2E_R1_ASSET_BUNDLE"):
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def installed_command(python: Path, *args: str) -> list[str]:
    return [str(python), "-I", "-B", *args]


def run_command(
    command: list[str], *, cwd: Path, timeout: int = 90, env: dict[str, str] | None = None
) -> dict:
    started = time.monotonic()
    process = subprocess.run(
        command,
        cwd=cwd,
        env=env or safe_env(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return {
        "command": command,
        "returncode": process.returncode,
        "stdout": process.stdout[-16000:],
        "stderr": process.stderr[-16000:],
        "runtime_seconds": time.monotonic() - started,
    }


def validate_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path, Path]:
    if not PROJECT_ID.fullmatch(args.project_id):
        raise ValueError("--project-id must be a lowercase 1..64 character slug")
    if not 1024 <= args.port <= 65535:
        raise ValueError("--port must be 1024..65535")
    project = args.project_root.expanduser().absolute()
    output = args.output_dir.expanduser().absolute()
    # A venv executable is commonly a symlink to the base interpreter; keep
    # its invoked path so sys.prefix, not realpath, identifies the environment.
    python = args.python.expanduser().absolute()
    wheel = args.wheel.expanduser().absolute()
    support = args.support_doc.expanduser().absolute()
    if project.exists() or project.is_symlink():
        raise ValueError("--project-root must be a nonexistent new directory")
    if (
        project == ROOT
        or project.is_relative_to(ROOT)
        or project.parent.resolve().is_relative_to(ROOT)
    ):
        raise ValueError("--project-root must be outside the checkout")
    if output.exists() or output.is_symlink() or not output.is_relative_to(REPORT_ROOT):
        raise ValueError("--output-dir must be new and under reports/camera_platform")
    if not python.is_file() or not wheel.is_file() or wheel.suffix != ".whl":
        raise ValueError("A venv Python and regular wheel file are required")
    if (
        wheel.is_symlink()
        or support.is_symlink()
        or not support.is_file()
        or support.suffix != ".md"
    ):
        raise ValueError("The wheel and support document must be regular files")
    venv = python.parent.parent
    if not (venv / "pyvenv.cfg").is_file():
        raise ValueError("--python must be inside a virtual environment")
    if "include-system-site-packages = false" not in (venv / "pyvenv.cfg").read_text().lower():
        raise ValueError("Virtual environment must disable system site packages")
    if (
        output.parent.resolve() != REPORT_ROOT.resolve()
        and not output.parent.resolve().is_relative_to(REPORT_ROOT.resolve())
    ):
        raise ValueError("--output-dir parent must remain inside reports/camera_platform")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except PermissionError as error:
            raise ValueError("Local loopback bind is denied by this execution sandbox") from error
        except OSError as error:
            raise ValueError(f"Port {args.port} is occupied") from error
    return project, output, python, wheel, support


def wheel_package_files(wheel: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    with zipfile.ZipFile(wheel) as archive:
        seen = set()
        total = 0
        for member in archive.infolist():
            name = member.filename
            parts = PurePosixPath(name)
            if (
                parts.is_absolute()
                or ".." in parts.parts
                or "\\" in name
                or name in seen
                or member.flag_bits & 1
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise ValueError("Wheel contains an unsafe or duplicate member")
            seen.add(name)
            total += member.file_size
            if name.startswith(PACKAGE_PREFIXES) and not member.is_dir():
                expected[name] = hashlib.sha256(archive.read(member)).hexdigest()
        if len(seen) > 20_000 or total > 512 * 1024**2 or archive.testzip() is not None:
            raise ValueError("Wheel exceeds supported bounds or has a corrupt member")
    if (
        not {
            "camera_platform/__init__.py",
            "camera_platform/static/index.html",
            "camera_platform/data/r1/manifest.json",
            "pyisetcam/__init__.py",
        }
        <= expected.keys()
    ):
        raise ValueError("Wheel lacks required package or R1 assets")
    return expected


def wheel_version(wheel: Path) -> str:
    """Read the distribution version from wheel bytes, not its renameable filename."""
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist()
                 if name.endswith(".dist-info/METADATA")]
        if len(names) != 1 or archive.getinfo(names[0]).file_size > 1_048_576:
            raise ValueError("Wheel must contain one bounded distribution METADATA")
        metadata = archive.read(names[0]).decode("utf-8")
    name = re.search(r"^Name:\s*(.+)$", metadata, re.MULTILINE)
    version = re.search(r"^Version:\s*(.+)$", metadata, re.MULTILINE)
    if name is None or name.group(1).strip().lower() != "pyisetcam" or version is None:
        raise ValueError("Wheel distribution metadata must identify pyisetcam and a version")
    return version.group(1).strip()


def required_checks_for_version(version: str) -> tuple[str, ...]:
    # The frozen RC1 proof only covered the registered static design path.
    # Every subsequent candidate must prove the added time and ISP paths.
    return CHECKS if version == "0.16.1rc1" else CHECKS + RC2_CHECKS


PROBE = r"""
import importlib.metadata as md, json, site, sys, sysconfig
from pathlib import Path
import camera_platform, pyisetcam
from camera_platform.r1_design_assets import bundle_root, catalog
root=Path(sysconfig.get_paths()["purelib"]).resolve()
bundle=bundle_root(Path.cwd())
data={"prefix":sys.prefix,"base_prefix":sys.base_prefix,"purelib":str(root),
"camera_platform_file":str(Path(camera_platform.__file__).resolve()),
"pyisetcam_file":str(Path(pyisetcam.__file__).resolve()),"user_site_enabled":site.ENABLE_USER_SITE,
"bundle_root":str(bundle),"catalog":catalog(Path.cwd()),
"version":md.version("pyisetcam")}
print(json.dumps(data,sort_keys=True))
"""


def installed_probe(python: Path, project: Path) -> tuple[dict, dict]:
    result = run_command(installed_command(python, "-c", PROBE), cwd=project)
    if result["returncode"]:
        raise RuntimeError("Installed package probe failed: " + result["stderr"][-2000:])
    return json.loads(result["stdout"].strip()), result


def installed_payload(wheel: Path, probe: dict, output: Path) -> tuple[dict, dict]:
    expected = wheel_package_files(wheel)
    purelib = Path(probe["purelib"]).resolve()
    if not purelib.is_dir():
        raise ValueError("Installed purelib is missing")
    actual = {}
    for package in ("camera_platform", "pyisetcam"):
        base = purelib / package
        if not base.is_dir() or base.is_symlink():
            raise ValueError("Installed package directory is missing or symlinked")
        for item in base.rglob("*"):
            if "__pycache__" in item.parts:
                continue
            if item.is_symlink():
                raise ValueError("Installed package contains a symlink")
            if item.is_file():
                actual[item.relative_to(purelib).as_posix()] = sha(item)
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    changed = sorted(
        name for name in expected.keys() & actual.keys() if expected[name] != actual[name]
    )
    report = {
        "wheel_sha256": sha(wheel),
        "purelib": str(purelib),
        "wheel_package_count": len(expected),
        "installed_package_count": len(actual),
        "missing": missing,
        "extra": extra,
        "changed": changed,
    }
    if missing or extra or changed:
        raise ValueError(
            "Installed package differs from the complete wheel payload: " + json.dumps(report)
        )
    payload = {
        "schema": PACKAGE_SCHEMA,
        "wheel_sha256": sha(wheel),
        "environment": {"python": probe["prefix"], "version": probe["version"]},
        "files": actual,
    }
    write_json(output, payload)
    return payload, report


class API:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        self.token: str | None = None

    def request(
        self, method: str, path: str, data: dict | None = None, *, timeout: int = 30
    ) -> tuple[int, object]:
        payload = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        headers = {"Origin": self.base}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if method != "GET" and self.token:
            headers["X-Platform-Token"] = self.token
        request = urllib.request.Request(
            self.base + path, data=payload, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                return response.status, json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            body = error.read()
            try:
                value = json.loads(body)
            except ValueError:
                value = body.decode(errors="replace")[-1000:]
            return error.code, value

    def get(self, path: str):
        status, data = self.request("GET", path)
        if status != 200:
            raise RuntimeError(f"GET {path}: {status} {data}")
        return data

    def post(self, path: str, data: dict):
        status, result = self.request("POST", path, data)
        if status not in (200, 202):
            raise RuntimeError(f"POST {path}: {status} {result}")
        return result

    def bytes(self, path: str) -> bytes:
        with urllib.request.urlopen(self.base + path, timeout=30) as response:
            return response.read()


def wait_health(api: API, process: subprocess.Popen, state: Path, *, seconds: int = 45) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Installed server exited with status {process.returncode}")
        try:
            value = api.get("/api/v1/health")
            if value.get("state_dir") == str(state) and value.get("worker") == "running":
                return value
        except (OSError, RuntimeError):
            pass
        time.sleep(0.2)
    raise TimeoutError("Installed API and Worker did not become healthy")


def wait_job(api: API, identity: str, *, seconds: int = 180) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job = api.get("/api/v1/design-experiments/" + identity)
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.15)
    raise TimeoutError("Experiment did not reach a terminal state")


def wait_collection_job(api: API, collection: str, identity: str, *, seconds: int = 180) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job = api.get(f"/api/v1/{collection}/{identity}")
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.15)
    raise TimeoutError(f"{collection} job did not reach a terminal state")


def npy_identity(content: bytes) -> dict:
    """Verify a saved uncompressed NPY header against its exact payload length."""
    if len(content) < 12 or not content.startswith(b"\x93NUMPY"):
        raise ValueError("Saved artifact is not NPY")
    major = content[6]
    if major == 1:
        offset, header_length = 10, struct.unpack_from("<H", content, 8)[0]
    elif major in (2, 3):
        offset, header_length = 12, struct.unpack_from("<I", content, 8)[0]
    else:
        raise ValueError("Unsupported NPY format version")
    if not 0 < header_length <= 65_536 or offset + header_length > len(content):
        raise ValueError("NPY header exceeds supported bounds")
    encoding = "utf-8" if major == 3 else "latin-1"
    header = ast.literal_eval(content[offset:offset + header_length].decode(encoding))
    if not isinstance(header, dict) or set(header) != {"descr", "fortran_order", "shape"}:
        raise ValueError("NPY header fields differ")
    if header["fortran_order"] is not False:
        raise ValueError("Saved R1 NPY must use C order")
    descriptor = header["descr"]
    dtype = re.fullmatch(r"[<>=|]([iuf])([1248])", descriptor)
    if dtype is None or not isinstance(header["shape"], tuple):
        raise ValueError("Unsupported NPY dtype or shape")
    shape = header["shape"]
    if not shape or not all(type(value) is int and 0 < value <= 4096 for value in shape):
        raise ValueError("NPY shape exceeds supported bounds")
    count = math.prod(shape)
    size = int(dtype.group(2))
    if count * size != len(content) - offset - header_length:
        raise ValueError("NPY payload length differs from its header")
    kind = {"i": "int", "u": "uint", "f": "float"}[dtype.group(1)]
    return {"shape": list(shape), "dtype": kind + str(size * 8)}


def _http_artifacts(api: API, job: dict, names: set[str]) -> dict[str, dict]:
    by_name = {artifact["name"]: artifact for artifact in job.get("artifacts", [])}
    if not names <= by_name.keys():
        raise ValueError(f"Missing installed artifacts: {sorted(names - by_name.keys())}")
    result = {}
    for name in names:
        descriptor = by_name[name]
        content = api.bytes(descriptor["url"])
        array_identity = npy_identity(content) if name.endswith(".npy") else None
        result[name] = {
            "sha256": descriptor["sha256"],
            "http_sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "shape": descriptor.get("shape"),
            "dtype": descriptor.get("dtype"),
            "npy_identity": array_identity,
            "npy_descriptor_matches": (
                array_identity["shape"] == descriptor.get("shape")
                and array_identity["dtype"] == descriptor.get("dtype")
            ) if array_identity else None,
        }
    return result


def verify_design_isp_replay(api: API, experiment: dict, browser: dict) -> tuple[dict, dict]:
    row = experiment["result"]["summary"]["rows"][0]
    original_raw = api.bytes(row["raw_artifact"]["url"])
    original_selection = experiment["result"]["summary"]["selection"]
    profile = json.loads(json.dumps(experiment["isp_profile"]))
    profile["white_balance"]["gains_rgb"][0] = min(
        16.0, profile["white_balance"]["gains_rgb"][0] * 1.25
    )
    profile["tone"]["curve"] = (
        "linear" if profile["tone"]["curve"] == "srgb" else "srgb"
    )
    submitted = api.post("/api/v1/design-isp-replays", {
        "schema": "camera_design_isp_replay_v1",
        "name": "Installed R1 immutable RAW ISP replay",
        "experiment_id": experiment["id"],
        "case_id": row["case_id"],
        "profile": profile,
    })
    replay = wait_collection_job(api, "design-isp-replays", submitted["id"])
    reopened = api.get("/api/v1/design-isp-replays/" + replay["id"])
    source_after = api.get("/api/v1/design-experiments/" + experiment["id"])
    raw_after = api.bytes(row["raw_artifact"]["url"])
    artifacts = _http_artifacts(api, replay, {
        row["raw_artifact"]["name"], row["rgb_artifact"]["name"],
        "replay-rgb.npy", "replay-rgb.png", "replay-summary.json",
    })
    summary = (replay.get("result") or {}).get("summary") or {}
    comparison = summary.get("comparison") or {}
    passed = (
        replay["status"] == "succeeded"
        and replay == reopened
        and source_after == experiment
        and source_after["result"]["summary"]["selection"] == original_selection
        and original_raw == raw_after
        and hashlib.sha256(raw_after).hexdigest() == row["raw_artifact"]["sha256"]
        and summary.get("new_captures") == 0
        and summary.get("input_unchanged") is True
        and summary.get("source_experiment_id") == experiment["id"]
        and summary.get("source_case_id") == row["case_id"]
        and summary.get("source_raw_sha256") == row["raw_artifact"]["sha256"]
        and summary.get("source_raw_array_sha256") == row["raw_array_sha256"]
        and summary.get("source_original_rgb_sha256") == row["rgb_artifact"]["sha256"]
        and summary.get("replay_rgb_sha256") == artifacts["replay-rgb.npy"]["sha256"]
        and artifacts["replay-rgb.npy"]["sha256"] != row["rgb_artifact"]["sha256"]
        and comparison.get("mean_abs_rgb_delta", 0) > 0
        and all(a["sha256"] == a["http_sha256"] and a["bytes"] > 0
                for a in artifacts.values())
        and artifacts["replay-rgb.npy"]["shape"] == [16, 16, 3]
        and artifacts["replay-rgb.npy"]["dtype"] == "float64"
        and artifacts["replay-rgb.npy"]["npy_descriptor_matches"] is True
        and browser.get("replay_id")
        and browser.get("replay_rgb_sha256")
        and "Saved ISP replay reopens in browser without another capture or POST"
        in browser.get("checks", [])
    )
    return replay, {
        "passed": bool(passed), "job_id": replay["id"],
        "source_experiment_id": experiment["id"], "source_case_id": row["case_id"],
        "original_raw_sha256": row["raw_artifact"]["sha256"],
        "source_selection_unchanged": (
            source_after["result"]["summary"]["selection"] == original_selection
        ),
        "input_unchanged": summary.get("input_unchanged") is True and original_raw == raw_after,
        "no_new_captures": summary.get("new_captures") == 0,
        "rgb_delta_observed": comparison.get("mean_abs_rgb_delta", 0) > 0,
        "saved_reopen": replay == reopened,
        "browser_replay": bool(browser.get("replay_id")),
        "browser_replay_id": browser.get("replay_id"),
        "summary": summary, "artifacts": artifacts,
    }


def verify_r1_modes(api: API, seed: dict) -> tuple[dict[str, dict], dict]:
    modes = (
        ("temporal", "temporal_scene_v1", "changing", None,
         "raw_frames.npy", [4, 16, 16], "rgb_frames.npy", [4, 16, 16, 3], {}),
        ("hdr", "hdr_bracket_v1", "static", "hdr",
         "raw_frames.npy", [4, 16, 16], "rgb_frames.npy", [4, 16, 16, 3],
         {"hdr_merged_rate.npy": [16, 16], "hdr_rgb.npy": [16, 16, 3]}),
        ("dcg", "shared_charge_dcg_v1", "static", "dcg",
         "raw_groups.npy", [4, 2, 16, 16], "rgb_groups.npy", [4, 2, 16, 16, 3],
         {"merged_rate.npy": [4, 16, 16], "merged_rgb.npy": [4, 16, 16, 3]}),
    )
    runs, proof = {}, {}
    for key, plugin, scene, program, raw_name, raw_shape, rgb_name, rgb_shape, merged in modes:
        request = {
            "assembly_id": seed["assembly_id"], "test_id": seed["test_ids"][key],
            "plugin_id": plugin,
            "scene_sequence_id": seed["scene_sequence_ids"][scene],
            "control_sequence_id": seed["control_sequence_ids"][scene],
            "parameters": {"seed": 471},
        }
        if program:
            request["capture_program_id"] = seed["capture_program_ids"][program]
        submitted = api.post("/api/v1/runs", request)
        run = wait_collection_job(api, "runs", submitted["id"])
        reopened = api.get("/api/v1/runs/" + run["id"])
        artifacts = _http_artifacts(
            api, run, {raw_name, rgb_name, "summary.json", *merged.keys()}
        )
        summary = (run.get("result") or {}).get("summary") or {}
        passed = (
            run["status"] == "succeeded" and run == reopened
            and run["assembly_id"] == seed["assembly_id"]
            and run["test_id"] == seed["test_ids"][key]
            and run["plugin_id"] == plugin
            and run["scene_sequence_id"] == seed["scene_sequence_ids"][scene]
            and run["control_sequence_id"] == seed["control_sequence_ids"][scene]
            and run.get("capture_program_id") == (
                seed["capture_program_ids"][program] if program else None
            )
            and summary.get("scene_sequence_id") == seed["scene_sequence_ids"][scene]
            and summary.get("control_sequence_id") == seed["control_sequence_ids"][scene]
            and summary.get("capture_program_id") == (
                seed["capture_program_ids"][program] if program else None
            )
            and summary.get("physical_validation") == "not_established"
            and artifacts[raw_name]["shape"] == raw_shape
            and integer_raw_dtype(artifacts[raw_name]["dtype"])
            and artifacts[rgb_name]["shape"] == rgb_shape
            and artifacts[rgb_name]["dtype"] == "float64"
            and all(a["sha256"] == a["http_sha256"] and a["bytes"] > 0
                    for a in artifacts.values())
            and artifacts[raw_name]["npy_descriptor_matches"] is True
            and artifacts[rgb_name]["npy_descriptor_matches"] is True
            and all(
                artifacts[name]["shape"] == shape
                and artifacts[name]["dtype"] == "float64"
                and artifacts[name]["npy_descriptor_matches"] is True
                for name, shape in merged.items()
            )
        )
        if key == "temporal":
            knots = summary.get("scene_knots") or []
            passed = (passed and summary.get("frame_count") == 4
                      and len({item.get("scene_content_hash") for item in knots}) >= 2)
        elif key == "hdr":
            passed = (passed and summary.get("frame_count") == 4
                      and summary.get("hdr_merge_applied") is True)
        else:
            passed = (passed and summary.get("acquisition_count") == 4
                      and summary.get("merge_applied") is True
                      and summary.get("raw_axes") == ["acquisition", "readout", "row", "column"]
                      and "raw_frames.npy" not in {a["name"] for a in run["artifacts"]})
        runs[key] = run
        proof[key] = {"status": "passed" if passed else "blocked", "job_id": run["id"],
                      "plugin_id": plugin, "summary": summary, "artifacts": artifacts,
                      "reopen_same": run == reopened}
    return runs, proof


def spec_from_template(template: dict, model_id: str, scene_id: str, name: str) -> dict:
    spec = json.loads(json.dumps(template["specification"]))
    spec.update(
        name=name,
        model_ids=[model_id],
        scene_id=scene_id,
        exposures_ms=[1.0, 2.0],
        gains=[1.0, 2.0],
        training_scales=[1.0, 2.0],
        evaluation_scales=[1.5],
        evaluation_noise_repeats=0,
        seed=2701,
        acknowledge_simulation_limits=True,
    )
    spec["limits"].update(
        minimum_green_dn=0.0,
        minimum_green_snr_p10=0.0,
        maximum_well_fraction=1.0,
        maximum_white_fraction=1.0,
        maximum_exposure_ms=4.0,
    )
    return spec


NUMERICAL_PROBE = r"""
import hashlib, json, sys
from pathlib import Path
import numpy as np
from camera_platform.r1_design_assets import load_sources
from camera_platform.response_chain import evaluate_response
from camera_platform import isp, spatial
model_id, scene_id = sys.argv[1:3]
case=load_sources(Path.cwd(), [model_id], scene_id)[model_id]
request=case.rate_request()
image=np.asarray(request["scene"]["spectral_irradiance"])
once=np.asarray(case.scene["spectral_radiance_w_m2_sr_nm"])*case._on_axis_transfer
optical=spatial.photoelectron_rate(request)
response=evaluate_response(request, case.electrical_model, exposure_s=.001,
    analog_gain=1, noise_enabled=False, seed=2701, isp_profile=isp.default_profile())
raw=response["arrays"]["raw_dn"]
before=hashlib.sha256(raw.tobytes()).hexdigest()
profile=isp.default_profile()
fmt={"cfa":"RGGB","bit_depth":case.electrical_model["adc"]["bit_depth"],
"black_level_dn":case.electrical_model["adc"]["black_level_dn"],
"response_type":"linear","packing":"unpacked","layout":"bayer","hdr_streams":1}
isp.process_raw(raw,fmt,profile,geometry={"kind":"native_sensor",
    **optical["summary"]["geometry"]})
after=hashlib.sha256(raw.tobytes()).hexdigest()
result={"on_axis_el_once":bool(np.allclose(image, once, rtol=0, atol=1e-15)
    and request["scene"]["included_effects"]==["on_axis_lens_throughput"]),
"exposure_once":bool(np.allclose(response["arrays"]["mean_photocharge_e"],
    optical["arrays"]["photoelectron_rate"]*.001, rtol=1e-12, atol=1e-12)),
"isp_keeps_raw":before==after,
"scene_reference_plane":request["scene"]["reference_plane"],
"scene_unit":request["scene"]["unit"],
"wavelength_nm":request["scene"]["wavelength_nm"],
"raw_sha256":before}
print(json.dumps(result,sort_keys=True))
"""


R1_BRIDGE_PROBE = r"""
import hashlib, io, json, sys
from pathlib import Path
import numpy as np
from camera_platform import imaging, isp
from camera_platform.r1_design_assets import load_sources
from camera_platform.response_chain import evaluate_response
from camera_platform.service import PlatformService
state=Path(sys.argv[1]);seed=json.loads(sys.argv[2])
service=PlatformService(state,Path.cwd())
source=load_sources(Path.cwd(),["r1-stop-clear-v1"],"r1-flat-v1")["r1-stop-clear-v1"]
original=source.rate_request()
image=service.registry.get("spectral_images",seed["spectral_image_ids"]["r1-flat-v1"])
artifact=next(a for a in image["artifacts"] if a["name"]=="spectral-image.npy")
ideal=np.load(io.BytesIO(service.registry.blob_path(artifact["sha256"]).read_bytes()),allow_pickle=False)
radiance=np.asarray(source.scene["spectral_radiance_w_m2_sr_nm"],dtype=float)
el=np.asarray(original["lens"]["radiometry"]["irradiance_per_radiance_sr"],dtype=float)[:,0]
el_once=bool(np.array_equal(ideal,radiance*el) and
             np.array_equal(ideal,original["scene"]["spectral_irradiance"]) and
             image["included_effects"]==["on_axis_lens_throughput"])
assembly=service.registry.get("assemblies",seed["assembly_id"])
models={service.registry.get("models",mid)["kind"]:service.registry.get("models",mid)
        for mid in assembly["model_ids"]}
lens=models["lens"];sensor=models["sensor"]
source_pinned=("radiometry" not in lens["payload"] and
    lens["payload"]["psf"]==original["psf"] and
    sensor["payload"]["qe"]==original["qe"] and
    sensor["payload"]["sensor_angular_response"]==original["sensor_angular_response"] and
    sensor["payload"]["readout"]==original["readout"] and
    all(m["properties"]["r1_source_identity"]["components"]==seed["source_identity"]["components"]
        for m in models.values()))
test=service.create_test(imaging.spatial_test_template(service,assembly["id"],image["id"]))
queued=service.submit_run({"assembly_id":assembly["id"],"test_id":test["id"],
    "plugin_id":"spatial_scene_v1","scene_id":image["id"],
    "parameters":{"seed":471,"frames":1,"exposure_ms":4}})
claimed=service.registry.claim()
assert claimed["id"]==queued["id"]
run=service.registry.finish(claimed["id"],result=service.execute_run(claimed))
raw_ref=next(a for a in run["artifacts"] if a["name"]=="raw_frames.npy")
raw=np.load(service.registry.blob_path(raw_ref["sha256"]),allow_pickle=False)[0]
reference=evaluate_response(original,source.electrical_model,exposure_s=.004,
    analog_gain=1.,noise_enabled=True,seed=471,isp_profile=isp.default_profile())
same_raw=bool(np.array_equal(raw,reference["arrays"]["raw_dn"]))
result={"on_axis_el_once":el_once,"source_components_pinned":source_pinned,
    "static_integer_raw_identical":same_raw,"raw_sha256":hashlib.sha256(raw.tobytes()).hexdigest(),
    "reference_raw_sha256":hashlib.sha256(reference["arrays"]["raw_dn"].tobytes()).hexdigest(),
    "run_id":run["id"],"physical_validation":run["result"]["summary"]["physical_validation"],
    "registry_integrity":service.registry.audit()["integrity"]}
print(json.dumps(result,sort_keys=True))
"""


def numerical_behavior(rows: list[dict]) -> dict:
    training = {
        (r["exposure_ms"], r["gain"], r["radiance_scale"]): r for r in rows if r["split"] == "train"
    }
    gain_independent = all(
        math.isclose(
            training[(exposure, 1.0, scale)]["green_photocharge_mean_e"],
            training[(exposure, 2.0, scale)]["green_photocharge_mean_e"],
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        for exposure in (1.0, 2.0)
        for scale in (1.0, 2.0)
    )
    exposure_linear = all(
        math.isclose(
            training[(2.0, gain, scale)]["green_photocharge_mean_e"],
            2 * training[(1.0, gain, scale)]["green_photocharge_mean_e"],
            rel_tol=1e-10,
            abs_tol=1e-10,
        )
        for gain in (1.0, 2.0)
        for scale in (1.0, 2.0)
    )
    scale_linear = all(
        math.isclose(
            training[(exposure, gain, 2.0)]["green_photocharge_mean_e"],
            2 * training[(exposure, gain, 1.0)]["green_photocharge_mean_e"],
            rel_tol=1e-10,
            abs_tol=1e-10,
        )
        for gain in (1.0, 2.0)
        for exposure in (1.0, 2.0)
    )
    return {
        "gain_does_not_change_photocharge": gain_independent,
        "photocharge_linear_in_exposure": exposure_linear,
        "photocharge_linear_in_radiance_scale": scale_linear,
    }


def package_proof_record(path: Path | None, wheel_hash: str) -> dict:
    if path is None:
        return {"status": "blocked", "reason": "--package-proof was not supplied"}
    if path.is_symlink() or not path.is_file() or path.suffix != ".json":
        return {
            "status": "blocked",
            "reason": "Package proof is missing or not a regular JSON file",
        }
    try:
        proof = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"status": "blocked", "reason": "Package proof is unreadable"}
    if not isinstance(proof, dict):
        return {
            "status": "blocked",
            "reason": "Package proof must be a JSON object",
            "path": str(path),
            "sha256": sha(path),
        }
    fields = (
        "raw_and_rgb_artifact_http_hashes_verified",
        "r1_registered_design_job_succeeded",
        "r1_integer_raw_and_float64_rgb_verified",
        "r1_fixed_isp_consumed_original_raw",
        "r1_selection_frozen_before_evaluation",
        "r1_saved_result_reopened",
        "r1_workbench_static_http_matches_package",
    )
    accepted = (
        proof.get("schema") == "camera_platform_r1_package_proof_v1"
        and proof.get("status") == "passed"
        and proof.get("wheel_sha256") == wheel_hash
        and proof.get("all_checked_source_static_bytes_match") is True
        and type(proof.get("source_static_file_count")) is int
        and proof["source_static_file_count"] > 0
        and proof.get("temporary_target_removed") is True
        and (proof.get("install") or {}).get("no_deps") is True
        and proof.get("physical_validation") == "not_established"
        and proof.get("registry_integrity") == "verified"
        and isinstance(proof.get("environment"), dict)
        and all(proof.get(key) is True for key in fields)
    )
    return {
        "status": "passed" if accepted else "blocked",
        "path": str(path),
        "sha256": sha(path),
        "wheel_sha256": proof.get("wheel_sha256"),
        "source_static_count": proof.get("source_static_file_count"),
        "required_fields": {key: proof.get(key) for key in fields},
    }


def artifact_proof(api: API, job: dict, output: Path) -> dict:
    if job.get("status") != "succeeded":
        raise ValueError("Experiment did not succeed")
    summary = job["result"]["summary"]
    rows = summary["rows"]
    if summary.get("new_captures") != len(rows) or not rows:
        raise ValueError("Recorded capture count differs from rows")
    report = []
    saved = output / "artifacts" / job["id"]
    saved.mkdir(parents=True, exist_ok=True)
    for row in rows:
        record = {
            "case_id": row["case_id"],
            "model_id": row["model_id"],
            "scene_id": row["scene_id"],
            "artifacts": {},
        }
        for key in ("raw_artifact", "rgb_artifact", "rgb_preview_artifact"):
            descriptor = row[key]
            data = api.bytes(descriptor["url"])
            if hashlib.sha256(data).hexdigest() != descriptor["sha256"]:
                raise ValueError(f"{key} HTTP bytes differ from saved SHA-256")
            if key != "rgb_preview_artifact" and not data.startswith(b"\x93NUMPY"):
                raise ValueError(f"{key} is not an NPY artifact")
            record["artifacts"][key] = {
                "sha256": descriptor["sha256"],
                "bytes": len(data),
                "shape": descriptor.get("shape"),
                "dtype": descriptor.get("dtype"),
            }
            if row is rows[0]:
                if Path(descriptor["name"]).name != descriptor["name"]:
                    raise ValueError("Artifact name is not a basename")
                target = saved / descriptor["name"]
                if target.exists() and sha(target) != descriptor["sha256"]:
                    raise ValueError("Reopened artifact differs from prior downloaded bytes")
                if not target.exists():
                    target.write_bytes(data)
        report.append(record)
    frozen = next(a for a in job["artifacts"] if a["name"] == "selection-before-evaluation.json")
    content = api.bytes(frozen["url"])
    if hashlib.sha256(content).hexdigest() != frozen["sha256"]:
        raise ValueError("Saved frozen selection hash differs")
    decision = json.loads(content)
    if decision.get("evaluation_seen") is not False:
        raise ValueError("Selection was not frozen before evaluation")
    return {
        "job_id": job["id"],
        "captures": report,
        "frozen_selection": decision,
        "saved_first_case_dir": str(saved),
        "selected_candidate_id": summary["selection"]["selected_candidate_id"],
    }


def _descendant_rss(root_pid: int) -> tuple[int, list[int]]:
    process = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True, check=False, timeout=5
    )
    if process.returncode:
        return 0, []
    table = {}
    for line in process.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            table[int(parts[0])] = (int(parts[1]), int(parts[2]))
    descendants = {root_pid}
    for _ in range(4):
        descendants.update(pid for pid, (ppid, _) in table.items() if ppid in descendants)
    return sum(table.get(pid, (0, 0))[1] for pid in descendants) * 1024, sorted(descendants)


class RSSSampler:
    def __init__(self, pid: int):
        self.pid = pid
        self.maximum = 0
        self.samples = 0
        self.pids: list[int] = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._work, daemon=True)

    def _work(self):
        while not self.stop_event.is_set():
            try:
                value, pids = _descendant_rss(self.pid)
                self.maximum = max(self.maximum, value)
                self.pids = pids
                self.samples += 1
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.stop_event.wait(0.25)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=6)


def stop_server(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


class Evidence:
    def __init__(self, output: Path, required_checks: tuple[str, ...] = CHECKS):
        self.output = output
        self.required_checks = required_checks
        self.checks: dict[str, dict] = {}
        self.errors: list[str] = []

    def check(
        self,
        name: str,
        passed: bool,
        details: dict,
        *,
        extra: dict | None = None,
        evidence_path: Path | None = None,
    ) -> None:
        if name not in self.required_checks:
            raise KeyError(name)
        path = evidence_path or self.output / "checks" / (name + ".json")
        if evidence_path is None:
            write_json(
                path, {"check": name, "status": "passed" if passed else "blocked", **details}
            )
        self.checks[name] = {
            "status": "passed" if passed else "blocked",
            "evidence_path": str(path),
            "evidence_sha256": sha(path),
            **(extra or {}),
        }

    def fail(self, message: str):
        self.errors.append(message)

    def fill_blocked(self):
        for name in self.required_checks:
            if name not in self.checks:
                self.check(name, False, {"reason": "Verification did not reach this check"})


def _storage_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def maximum_spec(template: dict, model_ids: list[str], scene_id: str, name: str) -> dict:
    spec = spec_from_template(template, model_ids[0], scene_id, name)
    spec.update(
        model_ids=model_ids[:3],
        exposures_ms=[0.5, 1.0, 2.0, 4.0],
        gains=[1.0, 2.0, 4.0],
        training_scales=[0.5, 1.0, 2.0],
        evaluation_scales=[0.8, 1.5, 3.0],
        evaluation_noise_repeats=4,
    )
    return spec


def maximum_reachable_captures() -> int:
    """Enumerate the public R1 grid bounds; 128 is a cap, not a reachable run."""
    counts = (
        models * exposures * gains * training + evaluations * (1 + repeats)
        for models in range(1, 4)
        for exposures in range(1, 5)
        for gains in range(1, 5)
        if models * exposures * gains <= 36
        for training in range(1, 5)
        for evaluations in range(1, 4)
        for repeats in range(5)
    )
    return max(count for count in counts if count <= 128)


def component_variant_ids(catalog: dict) -> dict[str, str]:
    """Locate pixel, electrical and joint changes with the same registered optics."""
    models = catalog.get("models", [])
    if not models or not isinstance(models[0].get("components"), dict):
        raise ValueError("R1 catalog does not expose registered component identities")
    base = models[0]["components"]
    expected_groups = {"optics_id", "pixel_id", "electrical_id"}
    if set(base) != expected_groups:
        raise ValueError("R1 model lacks exact optical/pixel/electrical component IDs")
    chosen = {"base": models[0]["id"]}
    patterns = {
        "pixel": (True, False),
        "electrical": (False, True),
        "pixel_electrical": (True, True),
    }
    for kind, (pixel_changed, electrical_changed) in patterns.items():
        matches = [
            item
            for item in models
            if item.get("components", {}).get("optics_id") == base["optics_id"]
            and (item["components"].get("pixel_id") != base["pixel_id"]) == pixel_changed
            and (item["components"].get("electrical_id") != base["electrical_id"])
            == electrical_changed
        ]
        if len(matches) != 1:
            raise ValueError(f"R1 catalog lacks one unambiguous {kind} component variant")
        chosen[kind] = matches[0]["id"]
    return chosen


def verify_component_variants(
    api: API,
    template: dict,
    catalog: dict,
    base_job: dict,
    output: Path,
) -> dict:
    chosen = component_variant_ids(catalog)
    base_model = catalog["models"][0]
    scene_id = catalog["scenes"][0]["id"]
    base_rows = base_job["result"]["summary"]["rows"]
    base_hashes = [row["raw_artifact"]["sha256"] for row in base_rows]
    result = {"model_ids": chosen, "jobs": {}, "passed": False}
    for kind in ("pixel", "electrical", "pixel_electrical"):
        model_id = chosen[kind]
        spec = spec_from_template(template, model_id, scene_id, f"r1-component-{kind}")
        job = wait_job(api, api.post("/api/v1/design-experiments", spec)["id"])
        proof = artifact_proof(api, job, output)
        rows = job["result"]["summary"]["rows"]
        identity = job["result"]["summary"]["source_identities"][model_id]
        catalog_model = next(item for item in catalog["models"] if item["id"] == model_id)
        expected = catalog_model["components"]
        observed = {key: item.get("id") for key, item in identity["components"].items()}
        identity_bound = observed == {
            "optics": expected["optics_id"],
            "pixels": expected["pixel_id"],
            "electricals": expected["electrical_id"],
        } and all(
            re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", ""))
            for item in identity["components"].values()
        )
        hashes = [row["raw_artifact"]["sha256"] for row in rows]
        changed = len(hashes) == len(base_hashes) and all(
            a != b for a, b in zip(hashes, base_hashes, strict=True)
        )
        result["jobs"][kind] = {
            "job_id": job["id"],
            "status": job["status"],
            "model_id": model_id,
            "raw_hashes": hashes,
            "base_raw_hashes": base_hashes,
            "component_identities": identity["components"],
            "identity_bound": identity_bound,
            "raw_changed_from_base": changed,
            "artifact_proof": proof,
        }
    result["passed"] = all(
        item["status"] == "succeeded" and item["identity_bound"] and item["raw_changed_from_base"]
        for item in result["jobs"].values()
    )
    result["base_model_id"] = base_model["id"]
    return result


def design_support_consistency(
    *,
    doc: str,
    ui: str,
    template: dict,
    browser: dict,
    browser_job: dict | None,
    wavelength_nm: list[int],
    rc2: bool = False,
) -> dict:
    """Check only the installed design-experiment support declaration."""
    conditions = {
        "doc_names_single_supported_path": (
            "정식 지원 선언 대상" in doc and "설계 실험 경로 하나" in doc
        ),
        "doc_separates_future_pilots": ("기본 공간·시간/HDR/DCG" in doc and "후속" in doc),
        "doc_names_exact_envelope": (
            "16×16·500, 600 nm" in doc and "최대 36후보" in doc and "최대 128 captures" in doc
        ),
        "installed_ui_names_exact_envelope": (
            "R1 설계 실험 범위: 등록된 16×16·500/600 nm" in ui
            and "최대 36개 후보·128회 취득" in ui
            and "고정 ISP" in ui
        ),
        "installed_ui_names_exclusions": (
            "움직임·HDR/LFM 결합" in ui and "실물 예측 정확도 검증은 포함되지 않습니다" in ui
        ),
        "api_template_matches_envelope": (
            template.get("maximum_captures") == 128
            and template.get("specification", {}).get("schema") == "camera_design_experiment_v2"
            and wavelength_nm == [500, 600]
            and "physical prediction accuracy" in template.get("limitation", "")
        ),
        "real_browser_and_worker_confirm_path": (
            browser.get("status") == "passed"
            and browser.get("contract_schema") == "camera_design_experiment_v2"
            and browser.get("new_captures") == 5
            and "R1 browser states its spectral, capture and physical limits"
            in browser.get("checks", [])
            and isinstance(browser_job, dict)
            and (browser_job.get("result") or {}).get("summary", {}).get("physical_validation")
            == "not_established"
        ),
    }
    if rc2:
        conditions.update({
            "doc_names_saved_raw_replay": (
                "저장 RAW ISP 재처리" in doc
                and "RC1 사례" in doc
                and "조회만 가능" in doc
            ),
            "doc_names_bounded_temporal_hdr_dcg": (
                "선택형 시간·HDR·DCG" in doc
                and "4–8 ms" in doc
                and "PWM/LFM" in doc
                and "16×16·500/600 nm" in doc
            ),
            "installed_design_ui_offers_saved_raw_replay": (
                "design-isp-replays" in ui
                and "동일 원본 RAW" in ui
                and "새로운 촬영 0회" in ui
            ),
        })
    return {
        "scope": "registered_r1_design_experiments_only",
        "conditions": conditions,
        "passed": all(conditions.values()),
    }


def _server(
    python: Path, project: Path, state: Path, project_id: str, port: int, log: Path
) -> subprocess.Popen:
    command = installed_command(
        python,
        "-m",
        "camera_platform",
        "serve",
        "--workspace",
        str(project),
        "--state-dir",
        str(state),
        "--r1-project-id",
        project_id,
        "--port",
        str(port),
    )
    with log.open("ab") as stream:
        return subprocess.Popen(
            command,
            cwd=project,
            env=safe_env(),
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
            close_fds=True,
        )


def _main(args: argparse.Namespace) -> int:
    project, output, python, wheel, support = validate_paths(args)
    output.mkdir(parents=True)
    project.mkdir(parents=True)
    state = project / "state"
    wheel_hash, doc_hash = sha(wheel), sha(support)
    version = wheel_version(wheel)
    evidence = Evidence(output, required_checks_for_version(version))
    started = time.monotonic()
    api = API(args.port)
    server: subprocess.Popen | None = None
    marker_hash = None
    payload_path = output / "installed-payload-manifest.json"
    sampler: RSSSampler | None = None
    performance: dict = {}
    seed: dict | None = None
    bridge: dict = {}
    rc2_replay_job: dict | None = None
    rc2_replay_proof: dict = {}
    rc2_mode_jobs: dict[str, dict] = {}
    rc2_mode_proof: dict = {}
    rc2_mode_browser: dict = {}
    try:
        preflight = run_command(installed_command(python, "-m", "pip", "check"), cwd=project)
        write_json(output / "pip-check.json", preflight)
        probe, probe_command = installed_probe(python, project)
        write_json(output / "installed-probe.json", {"probe": probe, "command": probe_command})
        purelib = Path(probe["purelib"]).resolve()
        no_checkout = all(
            Path(probe[key]).is_relative_to(purelib)
            for key in ("camera_platform_file", "pyisetcam_file")
        ) and not purelib.is_relative_to(ROOT)
        venv_age_seconds = time.time() - (python.parent.parent / "pyvenv.cfg").stat().st_mtime
        fresh_environment = 0 <= venv_age_seconds <= 24 * 3600
        isolated = (
            probe["prefix"] != probe["base_prefix"]
            and Path(probe["prefix"]).resolve() == python.parent.parent.resolve()
            and probe["user_site_enabled"] is False
            and fresh_environment
            and no_checkout
            and preflight["returncode"] == 0
            and probe["version"] == version
        )
        evidence.check(
            "isolated_install",
            isolated,
            {
                "venv_python": str(python),
                "pip_check": preflight,
                "prefix": probe["prefix"],
                "purelib": str(purelib),
                "venv_age_seconds": venv_age_seconds,
            },
            extra={
                "fresh_environment": fresh_environment,
                "no_system_site_packages": probe["user_site_enabled"] is False,
                "no_checkout_imports": no_checkout,
            },
        )
        _, payload_proof = installed_payload(wheel, probe, payload_path)
        evidence.check(
            "installed_imports",
            no_checkout,
            {"installed_probe": probe, "package_payload": payload_proof},
        )
        catalog = probe["catalog"]
        bundle = Path(probe["bundle_root"])
        required_assets = (
            bundle.is_relative_to(purelib)
            and len(catalog.get("models", [])) >= 6
            and len(catalog.get("scenes", [])) >= 2
            and catalog.get("schema") == "camerae2e_r1_design_bundle_v1"
            and len(catalog.get("components", {}).get("optics", [])) >= 3
            and len(catalog.get("components", {}).get("pixels", [])) >= 2
            and len(catalog.get("components", {}).get("electricals", [])) >= 2
        )
        evidence.check(
            "release_file_identity",
            True,
            {"payload": payload_proof},
            extra={
                "wheel_sha256": wheel_hash,
                "installed_payload_manifest_sha256": sha(payload_path),
            },
            evidence_path=payload_path,
        )
        init = run_command(
            installed_command(
                python,
                "-m",
                "camera_platform",
                "init-r1",
                "--workspace",
                str(project),
                "--state-dir",
                str(state),
                "--r1-project-id",
                args.project_id,
            ),
            cwd=project,
        )
        write_json(output / "init-r1.json", init)
        if init["returncode"]:
            raise RuntimeError("init-r1 failed: " + init["stderr"][-2000:])
        marker = state / "project-mode.json"
        marker_hash = sha(marker)
        if json.loads(marker.read_text()) != {
            "schema": "camera_platform_project_mode_v1",
            "mode": "r1_engineering",
            "project_id": args.project_id,
        }:
            raise ValueError("R1 marker does not bind the requested project")
        if RC2_CHECKS[0] in evidence.required_checks:
            seed_args = installed_command(
                python, "-m", "camera_platform", "seed-r1-modes",
                "--workspace", str(project), "--state-dir", str(state),
                "--r1-project-id", args.project_id,
            )
            seed_command = run_command(seed_args, cwd=project, timeout=180)
            write_json(output / "seed-r1-modes-command.json", seed_command)
            if seed_command["returncode"]:
                raise RuntimeError(
                    "Installed R1 mode seed failed: " + seed_command["stderr"][-2000:]
                )
            seed = json.loads(seed_command["stdout"])
            write_json(output / "seed-r1-modes.json", seed)
            seed_again = run_command(seed_args, cwd=project, timeout=180)
            write_json(output / "seed-r1-modes-again-command.json", seed_again)
            if seed_again["returncode"] or json.loads(seed_again["stdout"]) != seed:
                raise ValueError("R1 mode seed did not reopen idempotently")
            bridge_command = run_command(
                installed_command(python, "-c", R1_BRIDGE_PROBE, str(state), json.dumps(seed)),
                cwd=project, timeout=180,
            )
            write_json(output / "r1-spatial-bridge-command.json", bridge_command)
            bridge = (
                json.loads(bridge_command["stdout"])
                if bridge_command["returncode"] == 0 else {}
            )
            write_json(output / "r1-spatial-bridge.json", bridge)
        server = _server(
            python, project, state, args.project_id, args.port, output / "server-primary.log"
        )
        sampler = RSSSampler(server.pid)
        sampler.__enter__()
        wait_health(api, server, state)
        api.token = api.get("/api/v1/session")["token"]
        template = api.get("/api/v1/design-experiments/template")
        write_json(output / "template.json", template)
        model_ids = [item["id"] for item in template["catalog"]["models"]]
        scene_ids = [item["id"] for item in template["catalog"]["scenes"]]
        if (
            template["project_mode"]["project_id"] != args.project_id
            or template["project_mode"]["mode"] != "r1_engineering"
            or model_ids[:2] != [item["id"] for item in catalog["models"][:2]]
            or scene_ids[:2] != [item["id"] for item in catalog["scenes"][:2]]
        ):
            raise ValueError("Installed catalog/API project mode mismatch")
        spec = spec_from_template(template, model_ids[0], scene_ids[0], "r1-api-first")
        first = wait_job(api, api.post("/api/v1/design-experiments", spec)["id"])
        write_json(output / "api-first-job.json", first)
        first_proof = artifact_proof(api, first, output)
        write_json(output / "api-first-artifacts.json", first_proof)
        components_proof = verify_component_variants(api, template, catalog, first, output)
        write_json(output / "component-variants.json", components_proof)
        required_assets = required_assets and components_proof["passed"]
        evidence.check(
            "required_assets",
            required_assets,
            {
                "bundle_root": str(bundle),
                "catalog": catalog,
                "no_external_bundle_env": "CAMERAE2E_R1_ASSET_BUNDLE" not in safe_env(),
                "component_variants": components_proof,
            },
            extra={"no_qa_dependency": required_assets, "no_checkout_dependency": required_assets},
        )
        # A distinct run, not registry deduplication, must reproduce the deterministic bytes.
        spec["name"] = "r1-api-reproduction"
        second = wait_job(api, api.post("/api/v1/design-experiments", spec)["id"])
        write_json(output / "api-reproduction-job.json", second)
        second_proof = artifact_proof(api, second, output)
        first_hashes = [
            [a["sha256"] for a in row["artifacts"].values()] for row in first_proof["captures"]
        ]
        second_hashes = [
            [a["sha256"] for a in row["artifacts"].values()] for row in second_proof["captures"]
        ]
        deterministic = first["id"] != second["id"] and first_hashes == second_hashes
        evidence.check(
            "deterministic_reproduction",
            deterministic,
            {
                "first_job": first["id"],
                "reproduction_job": second["id"],
                "first_hashes": first_hashes,
                "reproduction_hashes": second_hashes,
            },
        )
        rows = first["result"]["summary"]["rows"]
        behavior = numerical_behavior(rows)
        core_proof = package_proof_record(args.package_proof, wheel_hash)
        write_json(output / "package-proof-binding.json", core_proof)
        numerical_run = run_command(
            installed_command(python, "-c", NUMERICAL_PROBE, model_ids[0], scene_ids[0]),
            cwd=project,
        )
        write_json(output / "numerical-probe-command.json", numerical_run)
        numerical_probe = (
            json.loads(numerical_run["stdout"]) if numerical_run["returncode"] == 0 else {}
        )
        numerical = (
            len(rows) == 9
            and first["result"]["summary"]["physical_validation"] == "not_established"
            and all(
                r["raw_artifact"]["shape"] == [16, 16]
                and integer_raw_dtype(r["raw_artifact"]["dtype"])
                and r["rgb_artifact"]["shape"] == [16, 16, 3]
                for r in rows
            )
            and all(behavior.values())
            and core_proof["status"] == "passed"
            and numerical_probe.get("on_axis_el_once") is True
            and numerical_probe.get("exposure_once") is True
            and numerical_probe.get("isp_keeps_raw") is True
            and numerical_probe.get("scene_reference_plane") == "sensor_ideal_image_before_psf_ri"
            and numerical_probe.get("scene_unit") == "W/m^2/nm"
            and numerical_probe.get("wavelength_nm") == [500, 600]
        )
        evidence.check(
            "numerical_contract",
            numerical,
            {
                "rows": [
                    {
                        "raw_shape": r["raw_artifact"]["shape"],
                        "raw_dtype": r["raw_artifact"]["dtype"],
                        "rgb_shape": r["rgb_artifact"]["shape"],
                        "raw_array_sha256": r["raw_array_sha256"],
                    }
                    for r in rows
                ],
                "artifact_proof_path": str(output / "api-first-artifacts.json"),
                "behavior": behavior,
                "installed_numerical_probe": numerical_probe,
                "package_proof": core_proof,
            },
        )
        browser_dir = output / "browser"
        browser_env = safe_env() | {
            "CAMERA_UI_BASE_URL": api.base,
            "CAMERA_UI_EXPECTED_STATE": str(state),
        }
        browser_run = run_command(
            ["node", str(ROOT / "tools/verify_camera_design_experiments_ui.cjs"), str(browser_dir)],
            cwd=project,
            env=browser_env,
            timeout=180,
        )
        write_json(output / "browser-command.json", browser_run)
        browser_report_path = browser_dir / "browser-qa.json"
        browser = (
            json.loads(browser_report_path.read_text()) if browser_report_path.is_file() else {}
        )
        browser_ok = browser_run["returncode"] == 0 and browser.get("status") == "passed"
        browser_job = (
            api.get("/api/v1/design-experiments/" + browser["experiment_id"])
            if browser_ok
            else None
        )
        if seed is not None:
            rc2_replay_job, rc2_replay_proof = verify_design_isp_replay(api, first, browser)
            write_json(output / "design-isp-replay-api.json", rc2_replay_proof)
            rc2_mode_jobs, rc2_mode_proof = verify_r1_modes(api, seed)
            write_json(output / "selected-mode-api.json", rc2_mode_proof)
            mode_browser_dir = output / "mode-browser"
            mode_browser_command = run_command(
                ["node", str(ROOT / "tools/verify_camera_r1_modes_ui.cjs"),
                 api.base, str(output / "seed-r1-modes.json"), str(mode_browser_dir)],
                cwd=project, env=browser_env, timeout=240,
            )
            write_json(output / "selected-mode-browser-command.json", mode_browser_command)
            mode_browser_path = mode_browser_dir / "browser-smoke.json"
            rc2_mode_browser = (
                json.loads(mode_browser_path.read_text()) if mode_browser_path.is_file() else {}
            )
        model_scene = (
            browser_ok
            and browser["selected_model_id"] == model_ids[1]
            and browser["selected_scene_id"] == scene_ids[1]
            and browser_job["status"] == "succeeded"
        )
        evidence.check(
            "two_model_scene",
            model_scene,
            {
                "first_job": first["id"],
                "browser_job": browser.get("experiment_id"),
                "browser_report": str(browser_report_path),
            },
            extra={
                "model_ids": [model_ids[0], model_ids[1]],
                "scene_ids": [scene_ids[0], scene_ids[1]],
            },
        )
        if model_scene:
            browser_proof = artifact_proof(api, browser_job, output)
            write_json(output / "browser-artifacts.json", browser_proof)
        reopened = api.get("/api/v1/design-experiments/" + first["id"])
        journey_steps = {
            "model_scene_conditions": "passed" if model_scene else "blocked",
            "raw_rgb": "passed" if model_scene and numerical else "blocked",
            "comparison": "passed" if model_scene and browser.get("selection") else "blocked",
            "selection_save": "passed"
            if model_scene
            and browser_job["result"]["summary"].get("selection_frozen_before_evaluation")
            else "blocked",
            "reopen": "passed" if browser_ok and reopened == first else "blocked",
        }
        evidence.check(
            "complete_user_journey",
            all(x == "passed" for x in journey_steps.values()),
            {
                "steps": journey_steps,
                "browser_report": str(browser_report_path),
                "reopened_api_job": reopened["id"],
            },
            extra={"steps": journey_steps},
        )
        try:
            failure_cases = run_failure_paths(
                api, template, model_ids, scene_ids, output, python, project
            )
        except Exception as error:
            failure_cases = {
                "cases": {name: "blocked" for name in FAILURE_CASES},
                "error": f"{type(error).__name__}: {error}",
            }
        evidence.check(
            "failure_paths",
            all(value == "passed" for value in failure_cases["cases"].values()),
            failure_cases,
            extra={"cases": failure_cases["cases"]},
        )
        max_spec = maximum_spec(template, model_ids, scene_ids[0], "r1-maximum-capture-run")
        before_max_bytes = _storage_bytes(state)
        max_started = time.monotonic()
        max_job = wait_job(api, api.post("/api/v1/design-experiments", max_spec)["id"], seconds=900)
        max_runtime = time.monotonic() - max_started
        max_saved_bytes = _storage_bytes(state) - before_max_bytes
        max_summary = (max_job.get("result") or {}).get("summary") or {}
        write_json(
            output / "maximum-capture-job.json",
            {
                "job_id": max_job["id"],
                "status": max_job["status"],
                "runtime_seconds": max_runtime,
                "new_captures": max_summary.get("new_captures"),
                "selection": max_summary.get("selection"),
                "error": max_job.get("error"),
            },
        )
        before_listing = [
            (item["id"], item["status"]) for item in api.get("/api/v1/design-experiments")
        ]
        before_blobs = {
            item.relative_to(state).as_posix()
            for item in (state / "blobs").rglob("*")
            if item.is_file()
        }
        t0 = time.monotonic()
        listing = api.get("/api/v1/design-experiments")
        list_seconds = time.monotonic() - t0
        after_blobs = {
            item.relative_to(state).as_posix()
            for item in (state / "blobs").rglob("*")
            if item.is_file()
        }
        write_json(output / "listing.json", listing)
        save_bytes = _storage_bytes(state)
        runtime_seconds = time.monotonic() - started
        sampler.__exit__(None, None, None)
        reachable_max = maximum_reachable_captures()
        performance = {
            "published_max_input": (
                "R1 design experiment: registered 16x16x2; 36 candidates and <=128 captures"
            ),
            "observed_input": (
                f"registered 16x16x2; 36 candidates, {max_summary.get('new_captures')} captures"
            ),
            "reachable_maximum_captures": reachable_max,
            "runtime_seconds": max_runtime,
            "full_verifier_runtime_seconds": runtime_seconds,
            "peak_memory_bytes": sampler.maximum,
            "peak_memory_method": "sampled ps RSS API+Worker process tree",
            "sample_count": sampler.samples,
            "saved_bytes": max_saved_bytes,
            "total_state_bytes": save_bytes,
            "list_read_seconds": list_seconds,
            "list_avoids_raw_recalculation": (
                before_listing == [(item["id"], item["status"]) for item in listing]
                and before_blobs == after_blobs
            ),
            "list_job_count": len(listing),
        }
        design_performance_observed = (
            max_job["status"] == "succeeded"
            and reachable_max == 123
            and max_summary.get("new_captures") == reachable_max
            and max_saved_bytes > 0
            and sampler.maximum > 0
            and sampler.samples > 0
            and performance["list_avoids_raw_recalculation"]
        )
        evidence.check(
            "performance",
            design_performance_observed,
            {
                **performance,
                "maximum_job_id": max_job["id"],
                "design_max_observed": design_performance_observed,
                "scope": "registered_r1_design_experiments_only",
                "non_design_pilots": "not_promoted_to_r1_formal_support",
                "sampling_limitation": "RSS is a 250 ms observation, not an OS peak watermark",
            },
            extra=performance,
        )
        ui_js = api.bytes("/design-experiments.js").decode("utf-8")
        support_proof = design_support_consistency(
            doc=support.read_text(),
            ui=ui_js,
            template=template,
            browser=browser,
            browser_job=browser_job,
            wavelength_nm=numerical_probe.get("wavelength_nm", []),
            rc2=seed is not None,
        )
        evidence.check(
            "support_display_consistency",
            support_proof["passed"],
            {
                **support_proof,
                "support_doc_sha256": doc_hash,
                "template_limitation": template["limitation"],
                "browser_status": browser.get("status"),
                "catalog": template["catalog"],
                "installed_ui_js_sha256": hashlib.sha256(ui_js.encode()).hexdigest(),
                "non_design_pilots": "not_promoted_to_r1_formal_support",
            },
        )
        stop_server(server)
        server = None
        restore_proof = restart_restore(
            python, project, state, args.project_id, args.port, output, first["id"], first_proof,
            replay_job=rc2_replay_job, mode_jobs=rc2_mode_jobs,
        )
        evidence.check("restart_restore", restore_proof["passed"], restore_proof)
        if seed is not None:
            replay_reopened = restore_proof.get("replay_reopen_same") is True
            replay_passed = rc2_replay_proof.get("passed") is True and replay_reopened
            evidence.check(
                "design_isp_replay", replay_passed,
                {**rc2_replay_proof, "reopen_after_restart_and_restore": replay_reopened,
                 "browser_report": str(browser_report_path)},
                extra={
                    "input_unchanged": rc2_replay_proof.get("input_unchanged") is True,
                    "no_new_captures": rc2_replay_proof.get("no_new_captures") is True,
                    "rgb_delta_observed": rc2_replay_proof.get("rgb_delta_observed") is True,
                    "browser_replay": rc2_replay_proof.get("browser_replay") is True,
                    "saved_reopen": (
                        rc2_replay_proof.get("saved_reopen") is True and replay_reopened
                    ),
                },
            )
            bridge_ok = (
                bridge.get("on_axis_el_once") is True
                and bridge.get("source_components_pinned") is True
                and bridge.get("static_integer_raw_identical") is True
                and bridge.get("registry_integrity") == "verified"
                and bridge.get("physical_validation") == "not_established"
            )
            mode_reopened = restore_proof.get("mode_reopen_same") or {}
            modes_passed = all(
                rc2_mode_proof.get(key, {}).get("status") == "passed"
                and mode_reopened.get(key) is True
                for key in ("temporal", "hdr", "dcg")
            )
            browser_modes_passed = (
                mode_browser_command["returncode"] == 0
                and rc2_mode_browser.get("status") == "passed"
                and rc2_mode_browser.get("post_paths") == ["/api/v1/runs"]
                and bool(rc2_mode_browser.get("run_id"))
            )
            registered_source = (
                seed.get("schema") == "r1-registry-pilot-v1"
                and seed.get("project_id") == args.project_id
                and seed.get("physical_validation") == "not_established"
                and seed.get("bundle_manifest_sha256") == catalog["manifest_sha256"]
            )
            evidence.check(
                "selected_temporal_hdr_dcg",
                registered_source and bridge_ok and modes_passed and browser_modes_passed,
                {"seed": seed, "seed_idempotent": True, "bridge": bridge,
                 "mode_api": rc2_mode_proof,
                 "mode_browser": rc2_mode_browser,
                 "mode_browser_command": mode_browser_command,
                 "mode_reopen_after_restore": mode_reopened},
                extra={
                    "registered_source": registered_source,
                    "numerical_bridge": bridge_ok,
                    "browser_hdr": browser_modes_passed,
                    "modes": {key: rc2_mode_proof.get(key, {}).get("status")
                              if mode_reopened.get(key) is True else "blocked"
                              for key in ("temporal", "hdr", "dcg")},
                },
            )
    except Exception as error:
        evidence.fail(f"{type(error).__name__}: {error}")
    finally:
        if sampler and sampler.thread.is_alive():
            sampler.__exit__(None, None, None)
        stop_server(server)
        evidence.fill_blocked()
        report = {
            "schema": SCHEMA,
            "status": "passed"
            if not evidence.errors
            and all(item["status"] == "passed" for item in evidence.checks.values())
            else "blocked",
            "project_id": args.project_id,
            "mode": "r1_engineering",
            "state_dir": str(state),
            "project_mode_sha256": marker_hash,
            "wheel_sha256": wheel_hash,
            "support_doc_sha256": doc_hash,
            "installed_payload_manifest_path": str(payload_path),
            "installed_payload_manifest_sha256": sha(payload_path)
            if payload_path.is_file()
            else None,
            "physical_validation": "not_established",
            "approval_status": "unapproved",
            "checks": evidence.checks,
            "errors": evidence.errors,
            "finished_at_utc": datetime.now(UTC).isoformat(),
        }
        write_json(output / "install-evidence.json", report)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "evidence": str(output / "install-evidence.json"),
                    "errors": report["errors"],
                    "blocked_checks": [
                        name
                        for name, check in report["checks"].items()
                        if check["status"] != "passed"
                    ],
                },
                ensure_ascii=False,
            )
        )
    return 0 if report["status"] == "passed" else 2


FAULT_HARNESS = r"""
import json, sys
from pathlib import Path
from camera_platform.api import create_app
from camera_platform.design_experiments import ExperimentFailure, default_spec, submit, get
from camera_platform.project_mode import configure
from camera_platform.r1_design_assets import catalog
root=Path(sys.argv[1]);state=root/"fault-harness-state"
configure(state, project_id=sys.argv[2])
app=create_app(state_dir=state,workspace=root,start_worker=False,seed_demo=False)
service=app.state.service
assets=catalog(root)
spec=default_spec(assets)
spec.update(name="r1-injected-late-save-failure",model_ids=[assets["models"][0]["id"]],
scene_id=assets["scenes"][0]["id"],exposures_ms=[1.,2.],gains=[1.],
training_scales=[1.,2.],evaluation_scales=[1.5],evaluation_noise_repeats=0,
seed=2701,acknowledge_simulation_limits=True)
spec["limits"].update(minimum_green_dn=0.,minimum_green_snr_p10=0.,
maximum_well_fraction=1.,maximum_white_fraction=1.,maximum_exposure_ms=4.)
queued=submit(service,spec);job=service.registry.claim()
original=service.registry.store_blob
def failing_store(content,name,media_type):
    if name in {"metrics.csv","partial-experiment.json"}:
        raise OSError("Injected late result storage failure: "+name)
    return original(content,name,media_type)
service.registry.store_blob=failing_store
try:
    service.execute_run(job)
except ExperimentFailure as error:
    partial=error.evidence_result
    service.registry.finish(job["id"],result=partial,error=str(error))
    stored=get(service,job["id"])
    rows=partial["summary"]["rows"]
    save={"injection":"store_blob(metrics.csv and partial-experiment.json)",
          "failed_job_status":stored["status"],
          "saved_rows":len(rows),"new_captures":partial["summary"]["new_captures"],
          "raw_artifacts":len([a for a in partial["artifacts"] if a["name"].endswith("-raw.npy")]),
          "selection_preserved":bool(partial["summary"]["selection"]["selected_candidate_id"]),
          "partial_store_error":partial["summary"].get("partial_evidence_store_error"),
          "first_raw_reopen_sha256":rows[0]["raw_artifact"]["sha256"],
          "reopened_result_same":stored["result"]["summary"]["rows"]==rows}
else:
    save={"error":"Injected save failure did not raise ExperimentFailure"}
service.registry.store_blob=original
spec["name"]="r1-injected-interrupted-running-job"
queued2=submit(service,spec);running=service.registry.claim()
before=service.registry.get_run(running["id"])
service.registry.recover_interrupted()
after=service.registry.get_run(running["id"])
interrupted={"injection":"recover_interrupted on installed SQLiteRegistry running job",
    "before_status":before["status"],"after_status":after["status"],
    "error":after.get("error"),"frozen_spec_preserved":before["specification"]==after["specification"],
    "job_id":running["id"]}
print(json.dumps({"save_failure":save,"interrupted":interrupted},sort_keys=True))
"""


def run_failure_paths(
    api: API,
    template: dict,
    model_ids: list[str],
    scene_ids: list[str],
    output: Path,
    python: Path,
    project: Path,
) -> dict:
    cases = {name: "blocked" for name in FAILURE_CASES}
    details: dict = {}
    spec = spec_from_template(template, model_ids[0], scene_ids[0], "r1-invalid")
    spec["acknowledge_simulation_limits"] = False
    before = len(api.get("/api/v1/design-experiments"))
    code, response = api.request("POST", "/api/v1/design-experiments", spec)
    after = len(api.get("/api/v1/design-experiments"))
    details["invalid_input"] = {
        "status_code": code,
        "response": response,
        "jobs_before": before,
        "jobs_after": after,
    }
    if code == 422 and before == after:
        cases["invalid_input"] = "passed"
    spec = spec_from_template(template, model_ids[0], scene_ids[0], "r1-no-candidate")
    spec.update(exposures_ms=[1.0])
    spec["limits"]["maximum_exposure_ms"] = 0.5
    job = wait_job(api, api.post("/api/v1/design-experiments", spec)["id"])
    summary = (job.get("result") or {}).get("summary") or {}
    selection = summary.get("selection") or {}
    details["no_candidate"] = {
        "job_id": job["id"],
        "status": job["status"],
        "selection": selection,
        "new_captures": summary.get("new_captures"),
    }
    if (
        job["status"] == "succeeded"
        and selection.get("status") == "no_feasible_candidate"
        and selection.get("selected_candidate_id") is None
        and summary.get("new_captures") == 0
    ):
        cases["no_candidate"] = "passed"
    spec = spec_from_template(template, model_ids[0], scene_ids[0], "r1-evaluation-failed-samples")
    spec.update(
        exposures_ms=[1.0, 2.0], gains=[1.0], training_scales=[0.05], evaluation_scales=[30.0]
    )
    spec["limits"].update(maximum_well_fraction=0.01, maximum_white_fraction=0.01)
    evaluation = wait_job(api, api.post("/api/v1/design-experiments", spec)["id"])
    evaluated = (evaluation.get("result") or {}).get("summary") or {}
    details["evaluation_failure"] = {
        "job_id": evaluation["id"],
        "job_status": evaluation["status"],
        "selection": evaluated.get("selection"),
        "evaluation": evaluated.get("evaluation"),
        "selection_frozen_before_evaluation": evaluated.get("selection_frozen_before_evaluation"),
    }
    if (
        evaluation["status"] == "succeeded"
        and (evaluated.get("selection") or {}).get("selected_candidate_id")
        and (evaluated.get("evaluation") or {}).get("status") == "failed_samples"
        and (evaluated.get("evaluation") or {}).get("reselection") is False
        and evaluated.get("selection_frozen_before_evaluation") is True
    ):
        cases["evaluation_failure"] = "passed"
    spec = spec_from_template(template, model_ids[0], scene_ids[0], "r1-cancel")
    spec.update(
        model_ids=model_ids[:3],
        exposures_ms=[0.5, 1.0, 2.0, 4.0],
        gains=[1.0, 2.0, 4.0],
        training_scales=[0.5, 1.0, 2.0],
        evaluation_scales=[1.5, 3.0, 6.0],
        evaluation_noise_repeats=4,
    )
    spec["limits"]["maximum_exposure_ms"] = 4.0
    try:
        submitted = api.post("/api/v1/design-experiments", spec)
        cancelled = api.post("/api/v1/design-experiments/" + submitted["id"] + "/cancel", {})
        final = wait_job(api, submitted["id"])
        details["cancelled"] = {
            "submitted": submitted["id"],
            "cancel_response_status": cancelled.get("status"),
            "final_status": final.get("status"),
        }
        if final.get("status") == "cancelled":
            cases["cancelled"] = "passed"
    except (RuntimeError, TimeoutError, KeyError, ValueError) as error:
        details["cancelled"] = {"error": str(error)}
    harness = run_command(
        installed_command(python, "-c", FAULT_HARNESS, str(project), "fault-r1-installed"),
        cwd=project,
        timeout=180,
    )
    write_json(output / "installed-fault-harness-command.json", harness)
    observed = json.loads(harness["stdout"]) if harness["returncode"] == 0 else {}
    save = observed.get("save_failure") or {}
    interrupted = observed.get("interrupted") or {}
    details["save_failure"] = save or {"error": harness["stderr"][-2000:]}
    details["interrupted"] = interrupted or {"error": harness["stderr"][-2000:]}
    if (
        save.get("failed_job_status") == "failed"
        and save.get("saved_rows") == 5
        and save.get("new_captures") == 5
        and save.get("raw_artifacts") == 5
        and save.get("selection_preserved") is True
        and save.get("partial_store_error")
        and save.get("reopened_result_same") is True
    ):
        cases["save_failure"] = "passed"
    if (
        interrupted.get("before_status") == "running"
        and interrupted.get("after_status") == "failed"
        and "Worker interrupted" in interrupted.get("error", "")
        and interrupted.get("frozen_spec_preserved") is True
    ):
        cases["interrupted"] = "passed"
    return {"cases": cases, "details": details}


def restart_restore(
    python: Path,
    project: Path,
    state: Path,
    project_id: str,
    port: int,
    output: Path,
    job_id: str,
    original_proof: dict,
    replay_job: dict | None = None,
    mode_jobs: dict[str, dict] | None = None,
) -> dict:
    archive = project / "r1-state-backup.zip"
    restored = project / "restored-state"
    backup = run_command(
        installed_command(
            python,
            "-m",
            "camera_platform",
            "backup",
            "--state-dir",
            str(state),
            "--output",
            str(archive),
            "--expected-project-id",
            project_id,
        ),
        cwd=project,
    )
    restore = run_command(
        installed_command(
            python,
            "-m",
            "camera_platform",
            "restore",
            "--state-dir",
            str(restored),
            "--archive",
            str(archive),
            "--expected-project-id",
            project_id,
        ),
        cwd=project,
    )
    report = {
        "backup": backup,
        "restore": restore,
        "new_state": str(restored),
        "old_state": str(state),
        "passed": False,
    }
    if backup["returncode"] or restore["returncode"] or not restored.is_dir():
        return report
    if sha(restored / "project-mode.json") != sha(state / "project-mode.json"):
        report["reason"] = "Restored project marker hash differs"
        return report
    server = None
    try:
        server = _server(
            python, project, restored, project_id, port, output / "server-restored.log"
        )
        api = API(port)
        health = wait_health(api, server, restored)
        job = api.get("/api/v1/design-experiments/" + job_id)
        proof = artifact_proof(api, job, output)
        report.update(
            restored_pid=server.pid,
            health=health,
            restored_job_id=job["id"],
            restored_artifacts=proof,
            archive_sha256=sha(archive),
        )
        report["passed"] = job["status"] == "succeeded" and [
            [a["sha256"] for a in row["artifacts"].values()] for row in proof["captures"]
        ] == [
            [a["sha256"] for a in row["artifacts"].values()] for row in original_proof["captures"]
        ]
        if replay_job is not None:
            restored_replay = api.get("/api/v1/design-isp-replays/" + replay_job["id"])
            report["replay_reopen_same"] = restored_replay == replay_job
            report["passed"] = report["passed"] and report["replay_reopen_same"]
        if mode_jobs:
            report["mode_reopen_same"] = {
                key: api.get("/api/v1/runs/" + saved["id"]) == saved
                for key, saved in mode_jobs.items()
            }
            report["passed"] = report["passed"] and all(report["mode_reopen_same"].values())
    finally:
        stop_server(server)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--support-doc", required=True, type=Path)
    parser.add_argument(
        "--package-proof",
        type=Path,
        help="Existing installed-wheel core proof for this exact wheel",
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        return _main(args)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    sys.exit(main())
