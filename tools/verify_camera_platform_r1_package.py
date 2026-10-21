#!/usr/bin/env python3
"""Verify the frozen R1 platform wheel in a checkout-free --no-deps target.

This proof covers the registered static design-experiment path only. It uses
already installed dependencies from --python, and does not claim a clean OS,
hardware accuracy, optional optical backends, or RAW replay behavior.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "camera_platform_r1_package_proof_v1"
PACKAGE_PREFIXES = ("camera_platform/", "pyisetcam/")
REQUIRED = (
    "camera_platform/__init__.py",
    "camera_platform/data/r1/manifest.json",
    "camera_platform/static/design-experiments.html",
    "camera_platform/static/design-experiments.js",
    "pyisetcam/__init__.py",
)


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_bytes_match_wheel(wheel: Path) -> dict:
    """Compare every shipped package byte to the source tree being released."""
    if wheel.is_symlink() or not wheel.is_file() or wheel.suffix != ".whl":
        raise ValueError("--wheel requires a regular .whl file")
    checked, missing, changed = [], [], []
    with zipfile.ZipFile(wheel) as archive:
        seen, total = set(), 0
        for member in archive.infolist():
            name = member.filename
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or name in seen
                or member.flag_bits & 1
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise ValueError("Wheel contains an unsafe member")
            seen.add(name)
            total += member.file_size
            if not name.startswith(PACKAGE_PREFIXES) or member.is_dir():
                continue
            source = ROOT / "src" / name
            if not source.is_file() or source.is_symlink():
                missing.append(name)
            elif hashlib.sha256(archive.read(member)).hexdigest() != sha(source):
                changed.append(name)
            checked.append(name)
        if len(seen) > 20_000 or total > 512 * 1024**2 or archive.testzip() is not None:
            raise ValueError("Wheel is corrupt or exceeds package bounds")
    if not set(REQUIRED) <= set(checked):
        raise ValueError("Wheel lacks required R1 package files")
    return {
        "source_static_file_count": len(checked),
        "missing_source": missing,
        "changed_source": changed,
        "all_checked_source_static_bytes_match": not missing and not changed,
    }


def installed_target_matches_wheel(wheel: Path, target: Path) -> dict:
    """Check every package payload file after pip's target installation."""
    with zipfile.ZipFile(wheel) as archive:
        expected = {
            item.filename: hashlib.sha256(archive.read(item)).hexdigest()
            for item in archive.infolist()
            if item.filename.startswith(PACKAGE_PREFIXES) and not item.is_dir()
        }
    actual = {}
    for package in ("camera_platform", "pyisetcam"):
        directory = target / package
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("Installed target package is missing or symlinked")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError("Installed target contains a package symlink")
            if path.is_file() and "__pycache__" not in path.parts:
                actual[path.relative_to(target).as_posix()] = sha(path)
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    changed = sorted(key for key in expected.keys() & actual.keys() if expected[key] != actual[key])
    return {
        "wheel_package_count": len(expected),
        "installed_package_count": len(actual),
        "missing": missing,
        "extra": extra,
        "changed": changed,
        "all_wheel_package_bytes_match_target": not missing and not extra and not changed,
    }


CHILD = r"""
import hashlib, io, json, sys
from pathlib import Path
import numpy as np
target=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(target))
import camera_platform, pyisetcam
from camera_platform.api import create_app
from camera_platform.project_mode import configure
from camera_platform.service import environment_fingerprint
from fastapi.testclient import TestClient
for module in (camera_platform,pyisetcam):
    assert Path(module.__file__).resolve().is_relative_to(target)
workspace=Path.cwd()
state=workspace/'state'
configure(state,project_id='r1-package-proof')
app=create_app(state_dir=state,workspace=workspace,start_worker=False,seed_demo=False)
service=app.state.service
with TestClient(app) as http:
    token=http.get('/api/v1/session').json()['token']
    template_response=http.get('/api/v1/design-experiments/template')
    assert template_response.status_code==200,template_response.text
    template=template_response.json()
    assert template['project_mode']['mode']=='r1_engineering'
    assert template['specification']['schema']=='camera_design_experiment_v2'
    catalog=template['catalog']
    assert len(catalog['models'])>=2 and len(catalog['scenes'])>=2
    spec=template['specification']
    spec.update(name='Installed R1 package proof',model_ids=[catalog['models'][0]['id']],
        scene_id=catalog['scenes'][0]['id'],exposures_ms=[1.,2.],gains=[1.],
        training_scales=[1.,2.],evaluation_scales=[1.5],evaluation_noise_repeats=0,
        seed=2701,acknowledge_simulation_limits=True)
    spec['limits'].update(minimum_green_dn=0.,minimum_green_snr_p10=0.,
        maximum_well_fraction=1.,maximum_white_fraction=1.,maximum_exposure_ms=4.)
    submitted=http.post('/api/v1/design-experiments',json=spec,
        headers={'x-platform-token':token})
    assert submitted.status_code==202,submitted.text
    queued=submitted.json()
    running=service.registry.claim()
    assert running['id']==queued['id'] and running['status']=='running'
    completed=service.registry.finish(running['id'],result=service.execute_run(running))
    detail=http.get('/api/v1/design-experiments/'+running['id'])
    assert detail.status_code==200,detail.text
    reopened=detail.json()
    assert reopened['status']=='succeeded' and reopened['id']==completed['id']
    summary=reopened['result']['summary']
    rows=summary['rows']
    assert len(rows)==5 and summary['new_captures']==5
    assert summary['physical_validation']=='not_established'
    assert summary['selection_frozen_before_evaluation'] is True
    assert summary['evaluation']['reselection'] is False
    assert summary['selection']['selected_candidate_id'] is not None
    assert all(r['model_id']==spec['model_ids'][0] and
               r['scene_id']==spec['scene_id'] for r in rows)
    raw_hashes=[];rgb_hashes=[];profile_hashes=[]
    for row in rows:
        values={}
        for key in ('raw_artifact','rgb_artifact','rgb_preview_artifact'):
            descriptor=row[key]
            response=http.get(descriptor['url'])
            assert response.status_code==200
            assert hashlib.sha256(response.content).hexdigest()==descriptor['sha256']
            values[key]=response.content
        raw=np.load(io.BytesIO(values['raw_artifact']),allow_pickle=False)
        rgb=np.load(io.BytesIO(values['rgb_artifact']),allow_pickle=False)
        assert raw.shape==(16,16) and raw.dtype.kind in 'ui'
        assert rgb.shape==(16,16,3) and rgb.dtype==np.float64 and np.isfinite(rgb).all()
        assert hashlib.sha256(raw.tobytes()).hexdigest()==row['raw_array_sha256']
        assert row['isp_input_raw_sha256']==row['raw_array_sha256']
        assert row['isp_profile_sha256']==reopened['isp_profile_sha256']
        raw_hashes.append(row['raw_artifact']['sha256'])
        rgb_hashes.append(row['rgb_artifact']['sha256'])
        profile_hashes.append(row['isp_profile_sha256'])
    frozen=next(a for a in reopened['artifacts'] if a['name']=='selection-before-evaluation.json')
    frozen_response=http.get(frozen['url'])
    assert frozen_response.status_code==200
    assert hashlib.sha256(frozen_response.content).hexdigest()==frozen['sha256']
    assert json.loads(frozen_response.content)['evaluation_seen'] is False
    listing=http.get('/api/v1/design-experiments')
    assert listing.status_code==200 and any(x['id']==running['id'] for x in listing.json())
    static=Path(camera_platform.__file__).parent/'static'
    for name in ('design-experiments.html','design-experiments.js','design-experiments.css'):
        response=http.get('/'+name)
        assert response.status_code==200 and response.content==(static/name).read_bytes()
    audit=service.registry.audit()
    assert audit['integrity']=='verified'
    print(json.dumps({
      'status':'passed','schema':'camera_platform_r1_installed_child_v1',
      'environment':environment_fingerprint(),
      'installed_module_paths':{'camera_platform':camera_platform.__file__,
                                'pyisetcam':pyisetcam.__file__},
      'registered_model_id':spec['model_ids'][0],
      'registered_scene_id':spec['scene_id'],
      'registered_bundle_manifest_sha256':catalog['manifest_sha256'],
      'job_id':running['id'],'new_captures':len(rows),
      'raw_sha256':raw_hashes,'rgb_sha256':rgb_hashes,
      'fixed_isp_profile_sha256':profile_hashes[0],
      'r1_registered_design_job_succeeded':True,
      'r1_integer_raw_and_float64_rgb_verified':True,
      'r1_fixed_isp_consumed_original_raw':True,
      'r1_selection_frozen_before_evaluation':True,
      'r1_saved_result_reopened':True,
      'r1_workbench_static_http_matches_package':True,
      'raw_and_rgb_artifact_http_hashes_verified':True,
      'registry_integrity':'verified',
      'physical_validation':'not_established',
    },sort_keys=True))
"""


def accepted_child(report: dict, target: Path) -> bool:
    if not isinstance(report, dict) or report.get("status") != "passed":
        return False
    expected = (
        "r1_registered_design_job_succeeded",
        "r1_integer_raw_and_float64_rgb_verified",
        "r1_fixed_isp_consumed_original_raw",
        "r1_selection_frozen_before_evaluation",
        "r1_saved_result_reopened",
        "r1_workbench_static_http_matches_package",
        "raw_and_rgb_artifact_http_hashes_verified",
    )
    if any(report.get(field) is not True for field in expected):
        return False
    paths = report.get("installed_module_paths")
    return (
        report.get("schema") == "camera_platform_r1_installed_child_v1"
        and report.get("registry_integrity") == "verified"
        and report.get("physical_validation") == "not_established"
        and report.get("new_captures") == 5
        and isinstance(report.get("environment"), dict)
        and isinstance(paths, dict)
        and all(
            isinstance(paths.get(name), str)
            and Path(paths[name]).resolve().is_relative_to(target.resolve())
            for name in ("camera_platform", "pyisetcam")
        )
    )


def verify(*, wheel: Path, python: Path, output: Path) -> dict:
    wheel, python, output = (
        wheel.expanduser().absolute(),
        python.expanduser().absolute(),
        output.expanduser().absolute(),
    )
    if output.exists() or output.is_symlink():
        raise ValueError("--output must be a new file")
    if not python.is_file():
        raise ValueError("--python must be an executable Python file")
    report = {
        "schema": SCHEMA,
        "status": "blocked",
        "wheel": str(wheel),
        "wheel_sha256": sha(wheel) if wheel.is_file() else None,
        "physical_validation": "not_established",
        "approval_status": "unapproved",
        "scope": "Registered 16x16, 500/600 nm static R1 design experiment only",
        "no_dependency_resolution_claim": True,
        "no_raw_replay_claim": True,
    }
    try:
        initial_wheel_sha256 = report["wheel_sha256"]
        source = source_bytes_match_wheel(wheel)
        report.update(source)
        if not source["all_checked_source_static_bytes_match"]:
            raise ValueError("Wheel package bytes differ from current source; freeze/rebuild first")
        env = os.environ.copy()
        for name in ("PYTHONPATH", "PYTHONHOME", "CAMERAE2E_R1_ASSET_BUNDLE"):
            env.pop(name, None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory(prefix="r1-package-proof-") as directory:
            outside = Path(directory).resolve()
            if outside.is_relative_to(ROOT):
                raise ValueError("Temporary target must be outside the checkout")
            target = outside / "installed"
            install = subprocess.run(
                [
                    str(python),
                    "-I",
                    "-B",
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--no-compile",
                    "--target",
                    str(target),
                    str(wheel),
                ],
                cwd=outside,
                env=env,
                text=True,
                capture_output=True,
                timeout=180,
                check=False,
            )
            report["install"] = {
                "returncode": install.returncode,
                "stdout": install.stdout[-2000:],
                "stderr": install.stderr[-2000:],
                "no_deps": True,
                "outside_checkout": True,
            }
            if install.returncode:
                raise RuntimeError("Wheel --no-deps installation failed")
            installed_payload = installed_target_matches_wheel(wheel, target)
            report["installed_payload"] = installed_payload
            if not installed_payload["all_wheel_package_bytes_match_target"]:
                raise ValueError("Target package bytes differ from the selected wheel")
            child = subprocess.run(
                [str(python), "-I", "-B", "-c", CHILD, str(target)],
                cwd=outside,
                env=env,
                text=True,
                capture_output=True,
                timeout=180,
                check=False,
            )
            report["installed_execution"] = {
                "returncode": child.returncode,
                "stderr": child.stderr[-4000:],
            }
            if child.returncode:
                raise RuntimeError("Installed R1 API/RAW/RGB execution failed")
            observed = json.loads(child.stdout.strip())
            if not accepted_child(observed, target):
                raise ValueError("Installed R1 proof has missing or unbound checks")
            final_payload = installed_target_matches_wheel(wheel, target)
            if final_payload != installed_payload:
                raise ValueError("Installed package changed during R1 execution")
            report.update(observed)
            report["schema"] = SCHEMA
        report["temporary_target_removed"] = not outside.exists()
        final_source = source_bytes_match_wheel(wheel)
        if (
            not report["temporary_target_removed"]
            or sha(wheel) != initial_wheel_sha256
            or not final_source["all_checked_source_static_bytes_match"]
            or final_source["source_static_file_count"] != source["source_static_file_count"]
        ):
            raise ValueError("Wheel, source, or temporary target changed during verification")
        report["all_checked_source_static_bytes_match"] = True
        report["status"] = "passed"
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.TimeoutExpired,
        zipfile.BadZipFile,
        json.JSONDecodeError,
    ) as error:
        report["status"] = "blocked"
        report["error"] = f"{type(error).__name__}: {error}"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = verify(wheel=args.wheel, python=args.python, output=args.output)
    print(
        json.dumps(
            {
                "status": result["status"],
                "report": str(args.output.absolute()),
                "error": result.get("error"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    sys.exit(main())
