"""Portable evidence packages containing immutable manifests and original artifact bytes."""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from .registry import canonical


def build_bundle(service, kind, identity):
    if kind in {"runs", "scene_renders", "scene_projections"}:
        manifest = service.export_run(identity)
        runs = [manifest["run"]]
        measurements = []
        captures = []
        factory_jobs = manifest.get("factory_jobs", [])
    elif kind == "replays":
        manifest = service.export_replay(identity)
        runs = [manifest["replay"], manifest["source"]]
        measurements = []
        captures = []
        factory_jobs = manifest["factory_jobs"]
    elif kind == "releases":
        manifest = service.export_release(identity)
        runs = [entry["run"] for entry in manifest["runs"]]
        measurements = manifest["measurements"]
        captures = manifest.get("captures", [])
        factory_jobs = manifest.get("factory_jobs", [])
    else:
        raise ValueError("Only run, replay and release evidence can be bundled")
    entries = [a for run in runs for a in run["artifacts"]]
    entries += manifest.get("isp_calibration", {}).get("artifacts", [])
    entries += manifest.get("isp_calibration", {}).get("evidence_artifacts", [])
    entries += [a for record in manifest.get("isp_calibration_lineage", [])
                for a in [*record.get("artifacts", []), *record.get("evidence_artifacts", [])]]
    entries += [a for record in manifest.get("isp_calibration_sources", [])
                for a in record.get("artifacts", [])]
    entries += [a for collection in ("readout_calibrations", "nonlinear_acquisition_lineage",
                                     "readout_observations", "readout_fit_jobs")
                for record in manifest.get(collection, []) for a in record.get("artifacts", [])]
    # Route aliases must preserve identical original evidence, including jobs
    # without successful outputs yet.
    entries += [
        a for run in runs
        if run.get("job_kind") in {"spectral_scene_render", "spectral_scene_projection"}
        for a in run.get("input_artifacts", [])
    ]
    entries += [m["original_artifact"] for m in measurements]
    entries += [a for capture in captures for a in capture["artifacts"]]
    entries += [a for analysis in manifest.get("capture_analyses", [])
                for a in analysis["artifacts"]]
    entries += [
        a for job in factory_jobs
        for a in [*job.get("input_artifacts", []), *job["artifacts"]]
    ]
    # Equal bytes can have distinct semantic names (input image and solver
    # intermediate). Preserve every manifest path, even when hashes coincide.
    artifacts = {(a["sha256"], a["name"]): a for a in entries}
    if sum(a["bytes"] for a in artifacts.values()) > 256 * 1024 * 1024:
        raise ValueError("Local download packages are limited to 256 MiB; export individual runs")
    paths = {checksum: service.registry.blob_path(checksum) for checksum, _ in artifacts}
    directory = service.registry.directory / "downloads"
    directory.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".zip", delete=False) as temporary:
        target = Path(temporary.name)
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", canonical(manifest))
            archive.writestr(
                "README.txt",
                (
                    "Camera Platform evidence package\n"
                    "manifest.json contains frozen inputs, model/test revisions, "
                    "environment, and claims.\n"
                    "artifacts/<sha256>/<name> contains original bytes. "
                    "Verify each SHA-256 before use.\n"
                    "A package preserves its actual validation/approval status; "
                    "export is not approval.\n"
                    "NumPy arrays are .npy files; load with numpy.load(..., allow_pickle=False).\n"
                ),
            )
            for checksum, name in artifacts:
                archive.write(paths[checksum], f"artifacts/{checksum}/{name}")
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise
