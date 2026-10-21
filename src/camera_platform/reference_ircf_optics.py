"""Opt-in VIS/NIR patent optics for the Reference IRCF Design Lab.

The legacy Reference optical contract remains 450–650nm. This adapter explicitly
retraces 400–1000nm using the same declared patent dispersion hypotheses; it does
not turn those hypotheses into measured NIR glass transmission or a product lens.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

from . import reference_workshop_optics as optics

SCHEMA = optics.IRCF_SCHEMA


def specification(step_nm=10):
    if type(step_nm) not in (int, float) or step_nm not in (5, 10):
        raise ValueError("IRCF optical sampling requires an explicit5 or10nm step")
    spec = optics.template()
    spec.update(
        schema=SCHEMA,
        wavelength_nm=np.arange(400.0, 1001.0, step_nm).tolist(),
        # The actual400nm traced bundle exceeds the old129 receiver support.
        # Explicitly widen both PSF support and scene halo; native pitch/FOV and
        # the0.999 retained-energy gate remain unchanged.
        kernel_size=257,
    )
    return optics.normalize(spec)


def _reuse_smaller_support(workspace, cache, spec, target):
    """Reuse verified ray/reciprocal work, never a cropped PSF or old result.

    Only kernel support differs. _request uses a fixed17-sample preflight kernel,
    and neither saved geometric rays nor reciprocal radiometry depend on the
    receiver histogram support. The source key includes the exact same current
    backend, prescription, wavelength grid and controls.
    """
    previous = {**spec, "kernel_size": 129}
    source = Path(cache) / "ircf-optics" / optics.cache_key(workspace, previous)
    if source == target or not source.is_dir():
        return
    if (source / "result.json").exists():
        old = json.loads((source / "result.json").read_text())
        if old["specification"] != previous:
            raise ValueError("IRCF intermediate-cache source configuration changed")
    copied = []
    manifests = [*source.glob("geometric-*.json"), *source.glob("batches/*/summary.json")]
    for manifest in manifests:
        record = json.loads(manifest.read_text())
        ray = manifest.name.startswith("geometric-")
        array = manifest.with_suffix(".npz") if ray else manifest.parent / "arrays.npz"
        expected = record["sha256" if ray else "arrays_sha256"]
        if not array.is_file() or optics.shared._file_sha(array) != expected:
            raise ValueError("IRCF intermediate-cache array hash changed: " + str(array))
        for path in (array, manifest):
            relative = path.relative_to(source)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if optics.shared._file_sha(destination) != optics.shared._file_sha(path):
                    raise ValueError("IRCF intermediate cache conflicts with existing artifact")
            else:
                temporary = destination.with_name(destination.name + ".copying")
                shutil.copyfile(path, temporary)
                temporary.replace(destination)
            copied.append(str(relative))
    if copied:
        optics.shared._write(
            target / "support-expansion.json",
            {
                "source_directory": str(source),
                "source_kernel_size": 129,
                "target_kernel_size": spec["kernel_size"],
                "reused_files": copied,
                "psf_or_result_reused": False,
                "reuse_basis": (
                    "same exact ray/radiometry requests and backend; histogram support only"
                ),
            },
        )


def solve(workspace, cache, step_nm=10, on_progress=None):
    """Return an immutable solution and its separate, content-addressed folder."""
    spec = specification(step_nm)
    folder = Path(cache) / "ircf-optics" / optics.cache_key(workspace, spec)
    if not (folder / "result.json").exists():
        _reuse_smaller_support(workspace, cache, spec, folder)
    optics.solve(workspace, spec, folder, on_progress=on_progress)
    return optics.load_solution(folder), folder


def effective_response(solution, field_id, angular_response):
    """Absolute T×QE pupil mean [wavelength,site], matching image transport."""
    if solution["result"].get("schema") != SCHEMA:
        raise ValueError("IRCF calibration needs the explicit VIS/NIR optical solution")
    return optics.effective_pupil_response(solution, field_id, angular_response)


def pupil_samples(solution, field_id, wavelength_index):
    """Return actual filter AOI, azimuth and original conditional photon weights."""
    if solution["result"].get("schema") != SCHEMA:
        raise ValueError("IRCF pupil samples need the explicit VIS/NIR optical solution")
    return optics.pupil_samples(solution, field_id, wavelength_index)
