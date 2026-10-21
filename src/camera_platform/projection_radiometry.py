"""Bind absolute scene throughput to an immutable prescription characterization.

The coefficient includes the passive stop. Conditional PSF/RI/angular response
remain in the Lens response and are applied downstream, never as extra throughput.
Historical paraxial projections retain their original expression and contract.
"""

from __future__ import annotations

import copy
import json

import numpy as np

from .registry import digest
from .scene_contracts import PrescriptionProjection, Projection

MODEL = "prescription_radiometry_projection_v1"
RADIOMETRY_SCHEMA = "camera_lens_radiometry_v1"


def requires_prescription_projection(lens):
    """A new absolute-radiometry model cannot silently use the old manual T path."""
    payload = lens.get("payload", {})
    return "radiometry" in payload or "stop_transmission" in payload


def wavelength_samples(projection):
    key = (
        "on_axis_irradiance_per_radiance_sr"
        if projection["projection_model"] == MODEL
        else "on_axis_transmittance"
    )
    return projection[key]["wavelength_nm"]


def _factory_evidence(service, job):
    """Check stored numerical evidence; no old solver execution is asserted."""
    if job.get("status") != "succeeded":
        raise ValueError("Absolute projection requires completed Factory evidence")
    artifacts = job["artifacts"]
    for artifact in [*job["input_artifacts"], *artifacts]:
        path = service.registry.blob_path(artifact["sha256"])
        if path.stat().st_size != artifact["bytes"] or artifact["id"] != artifact["sha256"]:
            raise ValueError("Factory artifact size or identity differs from the original evidence")
    summaries = [a for a in artifacts if a["name"] == "characterization.json"]
    if len(summaries) != 1:
        raise ValueError("Absolute projection requires one original characterization.json")
    original = json.loads(service.registry.blob_path(summaries[0]["sha256"]).read_bytes())
    if original != {
        "summary": job["result"]["summary"],
        "model_candidate": job["result"]["model_candidate"],
    }:
        raise ValueError("Factory result differs from its original characterization bytes")


def model_projection(service, lens):
    """Return an exact derived projection and portable parent/solver evidence."""
    from . import factory

    if lens.get("payload", {}).get("radiometry", {}).get("object_conjugate"):
        raise ValueError(
            "Finite planar Lens requires its conjugate-bound plane input; "
            "pinhole or mixed-depth 3D projection is unsupported"
        )
    if not requires_prescription_projection(lens) or not lens.get("psf_factory_job_id"):
        raise ValueError("Absolute projection requires a matched prescription PSF and radiometry")
    if service.registry.get("models", lens["id"]) != lens:
        raise ValueError("Projection Lens differs from its immutable revision")
    factory.verify_psf_model_link(service, lens)
    psf_job = factory.get_job(service, lens["psf_factory_job_id"])
    if psf_job["plugin_id"] != "prescription_psf_v1" or len(lens["parents"]) != 1:
        raise ValueError("Absolute projection requires the matched prescription PSF Factory")
    parent = service.registry.get("models", lens["parents"][0])
    if not parent.get("factory_job_id"):
        raise ValueError("Absolute projection requires an original radiometry Factory parent")
    factory.verify_model_link(service, parent)
    job = factory.get_job(service, parent["factory_job_id"])
    if (
        job["plugin_id"] != "rayoptics_lens_v1"
        or job["request"] != psf_job["request"]["prescription"]
    ):
        raise ValueError("PSF and radiometry must bind the same complete prescription")
    _factory_evidence(service, job)
    _factory_evidence(service, psf_job)
    radiometry = lens["payload"].get("radiometry")
    keys = {
        "schema",
        "wavelength_nm",
        "image_height_mm",
        "irradiance_per_radiance_sr",
        "prescription_sha256",
        "stop_transmission_sha256",
        "stop_transmission",
    }
    if not isinstance(radiometry, dict) or set(radiometry) != keys:
        raise ValueError("Absolute Lens radiometry requires its complete explicit contract")
    if (
        radiometry["schema"] != RADIOMETRY_SCHEMA
        or parent["payload"].get("radiometry") != radiometry
        or job["result"]["model_candidate"].get("radiometry") != radiometry
        or radiometry["prescription_sha256"] != digest(job["request"])
        or radiometry["stop_transmission"] != job["request"].get("stop_transmission")
        or radiometry["stop_transmission_sha256"] != digest(radiometry["stop_transmission"])
        or radiometry["wavelength_nm"] != job["request"]["wavelength_nm"]
        or radiometry["image_height_mm"] != job["request"]["image_height_mm"]
    ):
        raise ValueError("Absolute radiometry or passive stop differs from its Factory evidence")
    values = np.asarray(radiometry["irradiance_per_radiance_sr"], dtype=float)
    wave, heights = radiometry["wavelength_nm"], radiometry["image_height_mm"]
    if (
        values.shape != (len(wave), len(heights))
        or not heights
        or heights[0] != 0
        or not np.isfinite(values).all()
        or np.any(values <= 0)
        or np.any(values > np.pi)
        or radiometry["irradiance_per_radiance_sr"]
        != job["result"]["summary"]["irradiance_per_radiance_sr"]
    ):
        raise ValueError("Absolute E/L must preserve positive passive Factory integrals in sr")
    projection = PrescriptionProjection.model_validate(
        {
            "projection_model": MODEL,
            "focal_length_mm": lens["properties"]["focal_length"]["value"],
            "f_number": lens["properties"]["f_number"]["value"],
            "on_axis_irradiance_per_radiance_sr": {
                "wavelength_nm": wave,
                "values": values[:, 0].tolist(),
            },
            "lens_model_id": lens["id"],
            "lens_model_sha256": digest(lens),
            "factory_job_id": job["id"],
            "factory_job_sha256": digest(job),
            "psf_factory_job_id": psf_job["id"],
            "psf_factory_job_sha256": digest(psf_job),
            "stop_transmission_sha256": radiometry["stop_transmission_sha256"],
            "geometry_model": "pinhole_pixel_centre",
            "irradiance_model": (
                "L_times_verified_on_axis_irradiance_per_radiance_before_relative_illumination"
            ),
            "approximation_uncertainty": "unknown",
            "source_kind": lens["source_kind"],
            "source_uri": "factory://" + job["id"],
            "license": lens["license"],
            "method_and_version": MODEL,
            "physical_instance": None,
            "parents": [lens["id"]],
        }
    ).model_dump()
    evidence = {
        "schema": "camera_projection_radiometry_evidence_v1",
        "radiometry_parent_snapshot": parent,
        "radiometry_factory_snapshot": job,
        "psf_factory_snapshot": psf_job,
        "artifacts": [*job["artifacts"], *psf_job["artifacts"]],
        "factory_evidence_reused": True,
        "factory_executed_for_projection": False,
        "passive_stop_consumed_at": "absolute_on_axis_E_over_L",
        "downstream": "unit_sum_PSF_then_receiver_RI_and_conditional_angular_QE",
        "physical_validation": "not_established",
    }
    return projection, evidence


def verify_projection(service, projection, lens, wave):
    """Validate before cache lookup, execution and publication, including old sources."""
    if projection["projection_model"] == MODEL:
        expected, evidence = model_projection(service, lens)
        actual = PrescriptionProjection.model_validate(projection).model_dump()
        if actual != expected:
            raise ValueError(
                "Projection must retain the exact Lens/Factory/PSF/stop radiometry binding"
            )
    else:
        Projection.model_validate(projection)
        if requires_prescription_projection(lens):
            raise ValueError(
                "This Lens requires prescription radiometry projection; "
                "manual T would ignore or duplicate its stop"
            )
        evidence = None
    if wavelength_samples(projection) != wave:
        raise ValueError("Projection and sensor must use the exact same wavelength samples")
    return evidence


def verify_job(service, job):
    evidence = verify_projection(
        service, job["request"]["projection"], job["lens_snapshot"], job["request"]["wavelength_nm"]
    )
    if evidence is not None and job.get("projection_evidence") != evidence:
        raise ValueError("Frozen projection evidence differs from the verified Factory records")
    return evidence


def project_radiance(radiance, projection):
    """Pure numerical projection; application entry points verify the model binding."""
    if projection["projection_model"] == MODEL:
        checked = PrescriptionProjection.model_validate(projection)
        output = radiance * np.asarray(checked.on_axis_irradiance_per_radiance_sr.values)
    else:
        Projection.model_validate(projection)
        # Preserve historical multiplication order for byte-identical old evidence.
        output = (
            radiance
            * np.asarray(projection["on_axis_transmittance"]["values"])
            * (np.pi / (4 * projection["f_number"] ** 2))
        )
    if not np.isfinite(output).all() or np.any(output < 0):
        raise ValueError("Projection must produce finite nonnegative spectral irradiance")
    return output


def summary(projection):
    if projection["projection_model"] != MODEL:
        return {}
    return {
        "projection_radiometry": {
            "projection_model": MODEL,
            "on_axis_irradiance_per_radiance_sr": copy.deepcopy(
                projection["on_axis_irradiance_per_radiance_sr"]
            ),
            "lens_model_id": projection["lens_model_id"],
            "factory_job_id": projection["factory_job_id"],
            "psf_factory_job_id": projection["psf_factory_job_id"],
            "stop_transmission_sha256": projection["stop_transmission_sha256"],
            "additional_transmittance_multiplier": False,
            "additional_paraxial_aperture_multiplier": False,
            "relative_illumination_applied_here": False,
            "psf_applied_here": False,
            "physical_validation": "not_established",
        }
    }
