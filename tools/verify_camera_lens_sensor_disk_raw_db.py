"""Execute and retain one saved Lens DB x Sensor DB disk RAW diagnostic proof.

The selected geometry is an explicitly virtual 32x32 centered crop.  This
command verifies the installed Platform job/Worker/blob path, not product FOV,
MTF, Scene-grid convergence, measured physics, or peak-memory safety.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

import camera_platform
import pyisetcam
from camera_platform.lens_sensor_out_of_core_raw_job import (
    default_spec,
    get,
    submit,
)
from camera_platform.sensor_db_bridge import inspect_sensor_revision
from camera_platform.service import PlatformService
from camera_platform.worker import work

PROJECT = Path(__file__).resolve().parents[1]
LENS_REVISION = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
SENSOR_CODE = "DEP-2209-801"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--installed-target", type=Path, required=True)
    parser.add_argument(
        "--reuse-optical-groups", action="store_true",
        help="Opt in to group-local optical reuse; default retains legacy execution.",
    )
    args = parser.parse_args()
    state = args.state_dir.resolve()
    report_path = args.report.resolve()
    installed_target = args.installed_target.resolve(strict=True)
    platform_module = Path(camera_platform.__file__).resolve(strict=True)
    engine_module = Path(pyisetcam.__file__).resolve(strict=True)
    if (
        not platform_module.is_relative_to(installed_target)
        or not engine_module.is_relative_to(installed_target)
    ):
        raise RuntimeError(
            "Proof requires Camera Platform and pyisetcam imported from installed target"
        )
    if state.exists() or report_path.exists():
        parser.error("state and report targets must not already exist")
    if not state.parent.is_dir() or not report_path.parent.is_dir():
        parser.error("state and report parent directories must exist")
    selected = inspect_sensor_revision(SENSOR_CODE, root=PROJECT / "camerae2e_db")
    if not selected["executable"]:
        raise RuntimeError("Pinned saved sensor revision is not executable")
    spec = default_spec()
    spec["lens_revision_id"] = LENS_REVISION
    spec["sensor_revision"] = {
        "identifier": SENSOR_CODE,
        "run_id": selected["revision"]["run_id"],
    }
    spec["virtual_sensor_geometry"].update(
        mode_id="virtual-32x32", pixel_pitch_um=1.22
    )
    spec["accept_approximate_lens_radiometry"] = True
    spec["acknowledge_virtual_geometry_and_limits"] = True
    spec["reuse_optical_groups"] = args.reuse_optical_groups
    service = PlatformService(state, PROJECT)
    started = time.monotonic()
    queued = submit(service, spec)
    admitted_seconds = time.monotonic() - started
    worker_started = time.monotonic()
    work(service, once=True)
    worker_seconds = time.monotonic() - worker_started
    finished = get(service, queued["id"])
    if finished["status"] != "succeeded":
        raise RuntimeError(f"Disk-backed RAW job failed: {finished.get('error')}")
    summary = finished["result"]["summary"]
    arrays = {}
    for key in ("mean_electrons", "raw_dn", "reference_dn", "volts"):
        descriptor = summary["array_artifacts"][key]
        mapped = np.load(
            service.registry.blob_path(descriptor["sha256"]),
            mmap_mode="r", allow_pickle=False,
        )
        arrays[key] = {
            "dtype": str(mapped.dtype),
            "shape_row_col": list(mapped.shape),
            "minimum": float(np.min(mapped)),
            "maximum": float(np.max(mapped)),
            "sha256": descriptor["sha256"],
            "bytes": descriptor["bytes"],
        }
        del mapped
    if (
        arrays["raw_dn"]["shape_row_col"] != [32, 32]
        or arrays["mean_electrons"]["maximum"] <= 0
        or summary["sampling"]["sampling_convergence"] != "not_assessed"
        or summary["physical_fov_qualified"] is not False
        or summary["product_full_frame_qualified"] is not False
        or summary["specification"]["reuse_optical_groups"] is not args.reuse_optical_groups
        or (
            args.reuse_optical_groups
            and summary["execution"]["optical_reuse_group_count"] < 1
        )
    ):
        raise RuntimeError("Saved result lost virtual RAW or fidelity-limit contract")
    proof = {
        "schema": "camera_lens_sensor_disk_raw_db_proof_v1",
        "execution_mode": "separate_worker_child_process_with_default_600s_timeout",
        "platform_import_path": str(platform_module),
        "engine_import_path": str(engine_module),
        "platform_version": camera_platform.__version__,
        "engine_version": pyisetcam.__version__,
        "installed_target": str(installed_target),
        "state_directory": str(state),
        "job_id": queued["id"],
        "job_status": finished["status"],
        "admission_seconds_observed": round(admitted_seconds, 3),
        "worker_seconds_observed": round(worker_seconds, 3),
        "reuse_optical_groups_requested": args.reuse_optical_groups,
        "optical_reuse_group_count": summary["execution"]["optical_reuse_group_count"],
        "optical_work_counter_aggregation": summary["execution"][
            "optical_work_counter_aggregation"
        ],
        "arrays": arrays,
        "result": summary,
        "physical_fov_qualified": False,
        "product_full_frame_qualified": False,
        "hard_process_rss_limit_enforced": False,
    }
    report_path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "job_id": queued["id"],
        "status": finished["status"],
        "worker_seconds": proof["worker_seconds_observed"],
        "report": str(report_path),
        "state": str(state),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
