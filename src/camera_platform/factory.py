"""Detailed characterization on the durable queue and explicit draft-model publication."""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import io
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from .contracts import FactoryJobCreate, FactoryModelCreate
from .registry import ConflictError, canonical, digest

FACTORY_KIND = "characterization"
PLUGINS = {
    "rayoptics_lens_v1": "lens",
    "meep_planar_stack_v1": "sensor",
    "prysm_psf_v1": "lens",
    "prescription_psf_v1": "lens",
}
PSF_PLUGINS = {"prysm_psf_v1", "prescription_psf_v1"}


class FactoryExecutionError(RuntimeError):
    """Failed execution with preserved input and solver diagnostics."""

    def __init__(self, message, artifacts):
        super().__init__(message)
        self.evidence_result = {
            "artifacts": artifacts,
            "model_candidate": None,
            "physical_validation": "not_established",
        }


def adapter(plugin_id):
    if plugin_id == "prescription_psf_v1":
        from . import prescription_psf

        return prescription_psf
    if plugin_id == "prysm_psf_v1":
        from . import psf_factory

        return psf_factory
    if plugin_id == "rayoptics_lens_v1":
        from . import lens_factory

        return lens_factory
    if plugin_id == "meep_planar_stack_v1":
        from . import meep_factory

        return meep_factory
    raise ValueError("Unknown characterization plugin")


def backend_identity(plugin_id):
    if plugin_id == "prescription_psf_v1":
        ray = backend_identity("rayoptics_lens_v1")
        pupil = backend_identity("prysm_psf_v1")
        return {**ray, "versions": {**ray["versions"], **pupil["versions"]}}
    if plugin_id == "prysm_psf_v1":
        version = importlib.metadata.version("prysm")
        if version != "0.21.1":
            raise ValueError("This Prysm adapter requires checked backend version 0.21.1")
        return {
            "kind": "in_process",
            "executable": str(Path(sys.executable).resolve()),
            "versions": {
                name: importlib.metadata.version(name)
                for name in ("prysm", "numpy", "scipy", "setuptools")
            },
        }
    if plugin_id == "rayoptics_lens_v1":
        versions = {
            name: importlib.metadata.version(name)
            for name in ("rayoptics", "opticalglass", "numpy", "scipy", "transforms3d")
        }
        if versions["rayoptics"] != "0.9.8":
            raise ValueError("This RayOptics adapter requires checked backend version 0.9.8")
        return {
            "kind": "in_process",
            "executable": str(Path(sys.executable).resolve()),
            "versions": versions,
        }
    if plugin_id != "meep_planar_stack_v1":
        raise ValueError("Unknown characterization plugin")
    configured = os.environ.get("CAMERA_PLATFORM_MEEP_PYTHON")
    if not configured:
        raise ValueError("Configure CAMERA_PLATFORM_MEEP_PYTHON in the trusted server environment")
    executable = Path(configured).expanduser().resolve(strict=True)
    script = Path(__file__).with_name("meep_factory.py")
    probe = subprocess.run(
        [str(executable), "-I", str(script), "--probe"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    lines = [line for line in probe.stdout.splitlines() if line.strip().startswith("{")]
    if not lines:
        raise ValueError("Meep probe did not provide a structured backend identity")
    return {
        "kind": "isolated_interpreter",
        "executable": str(executable),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "probe": json.loads(lines[-1]),
    }


def list_plugins():
    result = []
    for identity, role in PLUGINS.items():
        try:
            backend = backend_identity(identity)
            available, reason = True, None
        except (
            ValueError,
            OSError,
            subprocess.SubprocessError,
            importlib.metadata.PackageNotFoundError,
        ) as exc:
            backend, available, reason = None, False, str(exc)
        result.append(
            {
                "plugin_id": identity,
                "name": "RayOptics lens radiometry"
                if role == "lens"
                else "Meep planar optical absorption",
                "role": "characterization",
                "request_versions": [1, 2, 3, 4, 5]
                if identity in {"rayoptics_lens_v1", "prescription_psf_v1"}
                else [1],
                "available": available,
                "reason": reason,
                "backend": backend,
                "supports": ["spectral_lens_response", "bounded_sampling_convergence"]
                if role == "lens"
                else ["normal_incidence_optical_absorption", "independent_numerical_checks"],
                "limits": {
                    "worker_timeout_s": 120,
                    "physical_validation": "not_established",
                    "model_publication": "unapproved_lens_candidate"
                    if role == "lens"
                    else "optical_only_no_collected_electron_QE",
                },
            }
        )
        if identity == "rayoptics_lens_v1":
            result[-1]["supports"].extend(
                [
                    "v2_passive_radial_stop_power_transmission",
                    "v2_absolute_irradiance_per_radiance",
                    "v3_joint_incident_directions_and_power",
                    "v4_centered_even_asphere_with_aperture_geometry_checks",
                    "v5_fixed_finite_planar_object_conjugate",
                ]
            )
        if identity == "prysm_psf_v1":
            result[-1].update(
                name="Prysm declared-pupil PSF",
                supports=[
                    "pixel_integrated_intensity_psf",
                    "independent_grid_checks",
                    "complex_discrete_kernel_otf",
                ],
            )
            result[-1]["limits"]["model_publication"] = (
                "unapproved_composite_lens_with_independent_pupil_assumption"
            )
        if identity == "prescription_psf_v1":
            result[-1].update(
                name="RayOptics prescription OPD · field PSF",
                supports=[
                    "prescription_wavefront",
                    "pixel_integrated_field_psf",
                    "shared_spectral_image_reference",
                    "independent_grid_checks",
                    "v2_matched_stop_power_transmission",
                    "v3_matched_directional_radiometry_prescription",
                    "v4_matched_centered_even_asphere_prescription",
                    "v5_finite_object_launch_and_wavefront",
                ],
            )
            result[-1]["limits"].update(
                model_publication="unapproved_lens_with_exact_prescription_radiometry_parent",
                object_conjugate="v1-v4_infinity_plane_wave; v5_fixed_finite_planar_object",
                pupil_transport="paraxial_exit_pupil_approximation",
                pupil_amplitude_by_request_version={
                    "1": "uniform_amplitude",
                    "2": "sqrt_stop_power_transmission_at_actual_ray_intersection",
                    "3": "sqrt_stop_power_transmission_at_actual_ray_intersection",
                    "4": "sqrt_stop_power_transmission_at_actual_ray_intersection",
                    "5": "sqrt_stop_power_transmission_at_actual_ray_intersection",
                },
            )
    return result


def template(service, plugin_id, *, request_version=1):
    role = PLUGINS.get(plugin_id)
    if role is None:
        raise ValueError("Unknown characterization plugin")
    if type(request_version) is not int or request_version not in (1, 2, 3, 4, 5):
        raise ValueError("Factory request_version must be 1, 2, 3, 4 or 5")
    if request_version >= 2 and plugin_id not in {"rayoptics_lens_v1", "prescription_psf_v1"}:
        raise ValueError("Stop transmission is supported only by the prescription lens adapters")
    module = adapter(plugin_id)
    request = (
        module.request_template(version=request_version)
        if request_version >= 2 else module.request_template()
    )
    assets = [a for a in service.registry.list("assets") if a["kind"] == role]
    return {
        "name": "명시적 가정 설계 · 해석 입력을 확인하세요",
        "asset_id": assets[0]["id"] if assets else "",
        "plugin_id": plugin_id,
        "source_kind": "estimated",
        "source_uri": "fixture://camera-platform/" + plugin_id,
        "license": "MIT; authored synthetic design fixture",
        "method_and_version": "declared-design:1",
        "physical_instance": None,
        "parents": [],
        "force": False,
        "request": request,
    }


def submit(service, payload):
    from .service import environment_fingerprint

    if service.loaded_environment != environment_fingerprint():
        raise ConflictError(
            "Executable source changed after startup; restart the API before submitting"
        )
    data = FactoryJobCreate.model_validate(payload).model_dump()
    force = data.pop("force")
    asset = service.registry.get("assets", data["asset_id"])
    if asset["kind"] != PLUGINS[data["plugin_id"]]:
        raise ValueError("Design asset kind does not match the characterization plugin")
    if data["parents"]:
        raise ValueError("Use the pinned design asset for characterization source lineage")
    normalized = adapter(data["plugin_id"]).validate_request(data["request"])
    try:
        backend = backend_identity(data["plugin_id"])
    except (OSError, subprocess.SubprocessError, importlib.metadata.PackageNotFoundError) as exc:
        raise ValueError(f"Characterization backend is unavailable: {exc}") from exc
    original = service.registry.store_blob(
        canonical(data).encode(), "factory-input.json", "application/json"
    )
    frozen = {
        **data,
        "request": normalized,
        "job_kind": FACTORY_KIND,
        "asset_snapshot": asset,
        "environment": environment_fingerprint(),
        "backend": backend,
        "input_artifacts": [original],
    }
    return service.registry.submit(frozen, digest(frozen), force=force)


def get_job(service, identity):
    job = service.registry.get_run(identity)
    if job.get("job_kind") != FACTORY_KIND:
        raise ValueError("This identifier belongs to a camera simulation, not characterization")
    return job


def execute(service, job):
    from .service import environment_fingerprint

    if job["environment"] != environment_fingerprint():
        raise ValueError("Worker source/environment differs from the frozen characterization")
    if job["backend"] != backend_identity(job["plugin_id"]):
        raise ValueError("Characterization backend differs from its frozen identity")
    for artifact in job["input_artifacts"]:
        service.registry.blob_path(artifact["sha256"])
    module = adapter(job["plugin_id"])
    if job["plugin_id"] in {"rayoptics_lens_v1", *PSF_PLUGINS}:
        output = module.characterize(job["request"])
        log_bytes = None
    else:
        with tempfile.TemporaryDirectory(
            prefix="characterization-", dir=service.registry.directory
        ) as temporary:
            root = Path(temporary)
            request_path, output_path = root / "input.json", root / "output.json"
            request_path.write_text(canonical(job["request"]))
            try:
                with (root / "solver.log").open("wb") as log:
                    subprocess.run(
                        [
                            job["backend"]["executable"],
                            "-I",
                            str(Path(module.__file__).resolve()),
                            "--request",
                            str(request_path),
                            "--output",
                            str(output_path),
                        ],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                        timeout=110,
                    )
            except subprocess.SubprocessError as exc:
                artifact = service.registry.store_blob(
                    (root / "solver.log").read_bytes()[:1_048_576], "solver.log", "text/plain"
                )
                raise FactoryExecutionError(
                    f"Meep execution failed: {exc}; inspect preserved solver.log",
                    [*job["input_artifacts"], artifact],
                ) from exc
            if output_path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("Characterization output exceeds 32 MiB")
            output = json.loads(output_path.read_text())
            log_bytes = (root / "solver.log").read_bytes()[:1_048_576]
    summary = output["summary"]
    artifacts = list(job["input_artifacts"])
    for name, values in output["arrays"].items():
        if job["plugin_id"] == "meep_planar_stack_v1" and name in {"configurations", "results"}:
            artifacts.append(
                service.registry.store_blob(
                    canonical(values).encode(), name + ".json", "application/json"
                )
            )
            continue
        array = np.asarray(values)
        if array.dtype.hasobject or array.nbytes > 32 * 1024 * 1024:
            raise ValueError("Factory arrays must be bounded non-object numerical data")
        stream = io.BytesIO()
        np.save(stream, array, allow_pickle=False)
        artifacts.append(
            service.registry.store_blob(
                stream.getvalue(), name + ".npy", "application/octet-stream"
            )
        )
    candidate = output.get("model_candidate")
    artifacts.append(
        service.registry.store_blob(
            canonical({"summary": summary, "model_candidate": candidate}).encode(),
            "characterization.json",
            "application/json",
        )
    )
    if log_bytes is not None:
        artifacts.append(service.registry.store_blob(log_bytes, "solver.log", "text/plain"))
    return {
        "summary": summary,
        "model_candidate": candidate,
        "artifacts": artifacts,
        "physical_validation": "not_established",
        "approval_status": "unapproved",
    }


def candidate_fields(job):
    candidate = (job.get("result") or {}).get("model_candidate")
    if job["status"] != "succeeded" or job["plugin_id"] != "rayoptics_lens_v1" or not candidate:
        raise ConflictError(
            "This characterization has no reusable lens candidate; "
            "inspect convergence and supported physics"
        )
    passive_stop = job["request"].get("schema") in {
        "lens_prescription_v2", "lens_prescription_v3", "lens_prescription_v4",
        "lens_prescription_v5"
    }
    return {
        "asset_id": job["asset_id"],
        "kind": "lens",
        "source_kind": "simulated",
        "source_uri": "factory://" + job["id"],
        "license": job["license"],
        "method_and_version": (
            "rayoptics_lens_v1:reverse_radiometric_finite_planar_object_v5"
            if job["request"].get("schema") == "lens_prescription_v5" else
            "rayoptics_lens_v1:reverse_radiometric_centered_even_asphere_v4"
            if job["request"].get("schema") == "lens_prescription_v4" else
            "rayoptics_lens_v1:reverse_radiometric_quadrature_with_joint_directions_v3"
            if job["request"].get("schema") == "lens_prescription_v3" else
            "rayoptics_lens_v1:reverse_radiometric_quadrature_with_stop_transmission_v2"
            if passive_stop else "rayoptics_lens_v1:reverse_radiometric_quadrature"
        ),
        "physical_instance": None,
        "parents": [],
        "factory_job_id": job["id"],
        "reference_plane": "sensor_surface",
        "included_effects": ["lens_relative_illumination", "lens_incident_angle_distribution"],
        "spectral_range": {
            "min": candidate["wavelength_nm"][0],
            "max": candidate["wavelength_nm"][-1],
            "unit": "nm",
        },
        "properties": {
            **({"object_conjugate": copy.deepcopy(job["request"]["object_conjugate"])}
               if job["request"].get("schema") == "lens_prescription_v5" else {}),
            "image_circle_diameter": {
                "value": 2 * job["request"]["image_height_mm"][-1],
                "unit": "mm",
            },
            "image_circle_basis": "sampled radial irradiance support; image quality unverified",
            "incident_angle_range": {
                "min": candidate["theta_deg"][0],
                "max": candidate["theta_deg"][-1],
                "unit": "deg",
            },
            "transmission_model": (
                "explicit_stop_transmission" if passive_stop else "ideal_lossless"
            ),
        },
        "uncertainty": {"kind": "unknown"},
        "calibration_dataset_ids": [],
        "payload": candidate,
    }


def verify_model_link(service, data):
    job = get_job(service, data["factory_job_id"])
    expected = candidate_fields(job)
    if any(data.get(k) != v for k, v in expected.items()):
        raise ValueError("Published model differs from the preserved characterization candidate")
    verify_conjugate_conditions(job["request"], data["factory_application_conditions"])
    if data["valid_domain"] != application_domain(data["factory_application_conditions"]):
        raise ValueError(
            "Factory model domain must retain the declared fixed application conditions"
        )
    for artifact in job["artifacts"]:
        service.registry.blob_path(artifact["sha256"])
    if "radiometry" in data["payload"]:
        verify_radiometry_evidence(service, job)


def verify_radiometry_evidence(service, job):
    """Tie actionable absolute throughput to the actual retained solver output."""
    from .lens_factory import validate_request

    request = validate_request(job["request"])
    candidate = job["result"]["model_candidate"]
    response = candidate.get("radiometry", {})
    if (
        request.get("schema") not in {
            "lens_prescription_v2", "lens_prescription_v3", "lens_prescription_v4",
            "lens_prescription_v5"
        }
        or request != job["request"]
        or response.get("prescription_sha256") != digest(request)
        or response.get("stop_transmission") != request["stop_transmission"]
        or response.get("stop_transmission_sha256") != digest(request["stop_transmission"])
        or response.get("irradiance_per_radiance_sr")
        != job["result"]["summary"]["irradiance_per_radiance_sr"]
    ):
        raise ValueError(
            "Absolute radiometry must preserve the exact prescription and stop transmission"
        )
    for name in ("characterization.json", "irradiance_per_radiance_sr.npy"):
        artifacts = [a for a in job["artifacts"] if a["name"] == name]
        if len(artifacts) != 1:
            raise ValueError("Absolute radiometry requires unique preserved numerical artifacts")
        content = service.registry.blob_path(artifacts[0]["sha256"]).read_bytes()
        if name.endswith(".json"):
            if json.loads(content) != {
                "summary": job["result"]["summary"], "model_candidate": candidate,
            }:
                raise ValueError("Absolute radiometry differs from its original characterization")
        elif not np.array_equal(
            np.load(io.BytesIO(content), allow_pickle=False),
            np.asarray(response["irradiance_per_radiance_sr"]),
        ):
            raise ValueError("Absolute radiometry differs from its retained numerical array")
    if request["schema"] in {
        "lens_prescription_v3", "lens_prescription_v4", "lens_prescription_v5"
    }:
        from .lens_direction_response import from_ray_samples

        arrays = {}
        names = ["quadrature_weights_sr", "accepted_rays", "stop_power_transmittance",
                 "image_direction_cosines", "irradiance_per_radiance_sr"]
        if request["schema"] == "lens_prescription_v5":
            names.append("object_plane_valid")
        for name in names:
            artifacts = [a for a in job["artifacts"] if a["name"] == name + ".npy"]
            if len(artifacts) != 1:
                raise ValueError("Joint directional response requires original ray power arrays")
            arrays[name] = np.load(io.BytesIO(service.registry.blob_path(
                artifacts[0]["sha256"]).read_bytes()), allow_pickle=False)
        if from_ray_samples(request, arrays) != candidate.get("directional_response"):
            raise ValueError("Joint directional response differs from actual retained rays/power")


def _finite_real(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def application_domain(conditions):
    if not isinstance(conditions, dict) or not conditions:
        raise ValueError("Declare explicit fixed application conditions")
    domain = {}
    for key, condition in conditions.items():
        value = condition.get("value") if isinstance(condition, dict) else None
        if not (
            isinstance(condition, dict)
            and set(condition) == {"value", "unit"}
            and isinstance(condition["unit"], str)
            and condition["unit"]
            and (_finite_real(value) or (isinstance(value, str) and value))
        ):
            raise ValueError(f"Invalid fixed application condition: {key}")
        if type(value) in (int, float):
            domain[key] = {"min": value, "max": value, "unit": condition["unit"]}
    if not domain:
        raise ValueError("Declare the intended fixed numerical application conditions")
    return domain


def verify_conjugate_conditions(prescription, conditions):
    if prescription.get("schema") == "lens_prescription_v5":
        from .finite_conjugate import application_conditions

        required = application_conditions(prescription["object_conjugate"])
        if any(conditions.get(key) != value for key, value in required.items()):
            raise ValueError(
                "Finite Lens conditions must retain exact object distance and magnification"
            )


def publish(service, payload):
    data = FactoryModelCreate.model_validate(payload).model_dump()
    job = get_job(service, data["job_id"])
    if job["plugin_id"] in PSF_PLUGINS:
        if not data["base_lens_model_id"]:
            raise ValueError("PSF publication requires an explicit base Lens response to compose")
        base = service.registry.get("models", data["base_lens_model_id"])
        fields = psf_candidate_fields(
            job, base, data["conditions"], data["condition_scope"], service=service
        )
        return service.create_model({**fields, "name": data["name"], "revision": data["revision"]})
    if data["condition_scope"] != "all_declared":
        raise ValueError("Optical response applicability is supported only for PSF composition")
    if data["base_lens_model_id"] is not None:
        raise ValueError("A base Lens is used only when attaching an independently declared PSF")
    fields = copy.deepcopy(candidate_fields(job))
    verify_conjugate_conditions(job["request"], data["conditions"])
    domain = application_domain(data["conditions"])
    return service.create_model(
        {
            **fields,
            "name": data["name"],
            "revision": data["revision"],
            "valid_domain": domain,
            "factory_application_conditions": data["conditions"],
        }
    )


def psf_candidate_fields(job, base, conditions, condition_scope="all_declared", *, service=None):
    from .contracts import ModelCreate
    from .prescription_identity import sampling_independent_binding
    from .validation import check_domain

    candidate = (job.get("result") or {}).get("model_candidate")
    if job["status"] != "succeeded" or job["plugin_id"] not in PSF_PLUGINS or not candidate:
        raise ConflictError("This characterization has no accepted PSF candidate")
    if base["kind"] != "lens" or base["asset_id"] != job["asset_id"]:
        raise ValueError("PSF and base Lens must reference the same declared lens asset")
    prescription = job["plugin_id"] == "prescription_psf_v1"
    passive_stop = "radiometry" in base["payload"]
    prescription_binding = None
    if passive_stop and not prescription:
        raise ValueError("Stop transmission requires PSF and radiometry from the same prescription")
    if prescription:
        verify_conjugate_conditions(job["request"]["prescription"], conditions)
        if service is None or not base.get("factory_job_id"):
            raise ValueError("Prescription PSF requires its verified RayOptics radiometry parent")
        verify_model_link(service, base)
        parent_job = get_job(service, base["factory_job_id"])
        if parent_job["plugin_id"] != "rayoptics_lens_v1" or parent_job["status"] != "succeeded":
            raise ValueError(
                "PSF and radiometry parent must preserve exactly the same prescription"
            )
        prescription_binding = sampling_independent_binding(
            parent_job["request"], job["request"]["prescription"]
        )
        if prescription_binding is not None:
            summary = job["result"]["summary"]
            if (
                summary.get("normalized_request") != job["request"]
                or summary.get("request_sha256") != digest(job["request"])
                or summary.get("prescription_sha256")
                != prescription_binding["psf_prescription_request_sha256"]
            ):
                raise ValueError(
                    "Independently sampled PSF must retain its exact original request hashes"
                )
        pupil_f_number = job["result"]["summary"]["geometric_reference_f_number"]
        if conditions.get("f_number") != {"value": pupil_f_number, "unit": "1"}:
            raise ValueError("Declare f_number matching the preserved geometric reference f-number")
        focal = base.get("properties", {}).get("focal_length")
        if focal is not None and focal != {
            "value": job["request"]["reference_focal_length_mm"],
            "unit": "mm",
        }:
            raise ValueError("Base Lens focal geometry differs from the PSF shared image reference")
    else:
        pupil_f_number = job["request"]["f_number"]
    domain = application_domain(conditions)
    if condition_scope not in {"all_declared", "optical_response"}:
        raise ValueError("Unknown factory condition scope")
    if condition_scope == "optical_response":
        # Only these known downstream readout controls are independent of the
        # implemented linear PSF/RI/angular Lens response. Parent bounds survive.
        optical_conditions = {
            k: v for k, v in conditions.items() if k not in {"exposure_s", "gain"}
        }
        domain = {**copy.deepcopy(base["valid_domain"]), **application_domain(optical_conditions)}
    issues = check_domain(conditions, base.get("valid_domain"), path="parent.valid_domain")
    if issues:
        raise ValueError(
            "PSF composition must remain inside every declared parent Lens domain: "
            + "; ".join(issue["message"] for issue in issues)
        )
    from .model_conditions import fixed_factory_conditions

    if any(conditions.get(k) != v for k, v in (fixed_factory_conditions(base) or {}).items()):
        raise ValueError("Preserve the base Lens fixed application conditions")
    spectral = base.get("spectral_range")
    wave = candidate.get("wavelength_nm")
    if not (
        isinstance(spectral, dict)
        and spectral.get("unit") == "nm"
        and _finite_real(spectral.get("min"))
        and _finite_real(spectral.get("max"))
        and spectral["min"] < spectral["max"]
        and isinstance(wave, list)
        and len(wave) >= 2
        and all(_finite_real(value) for value in wave)
        and all(b > a for a, b in zip(wave[:-1], wave[1:], strict=True))
        and wave[0] == spectral["min"]
        and wave[-1] == spectral["max"]
    ):
        raise ValueError(
            "PSF wavelength grid must retain the parent Lens's complete declared nm coverage; "
            "no implicit narrowing or extrapolation"
        )
    parent_wave = base["payload"].get("wavelength_nm")
    if parent_wave is not None and wave != parent_wave:
        raise ValueError(
            "PSF and spectral parent Lens wavelength coordinates must match exactly; "
            "no resampling or invented RI/angular response"
        )
    for origin, declaration in (
        ("conditions", conditions.get("f_number")),
        ("parent properties", base.get("properties", {}).get("f_number")),
    ):
        if declaration is not None and not (
            isinstance(declaration, dict)
            and set(declaration) == {"value", "unit"}
            and declaration["unit"] == "1"
            and _finite_real(declaration["value"])
            and declaration["value"] == pupil_f_number
        ):
            raise ValueError(
                f"Explicit f_number in {origin} must match the declared PSF pupil with unit 1"
            )
    result = {k: copy.deepcopy(v) for k, v in base.items() if k in ModelCreate.model_fields}
    result.update(
        source_kind="simulated" if prescription else "estimated",
        source_uri="factory://" + job["id"],
        method_and_version=(
            "prescription_psf_v1:matched_finite_planar_object_composition_v5"
            if prescription and job["request"].get("schema") == "prescription_field_psf_v5" else
            "prescription_psf_v1:matched_centered_even_asphere_composition_v4"
            if prescription and job["request"].get("schema") == "prescription_field_psf_v4" else
            "prescription_psf_v1:matched_joint_directional_radiometry_composition_v3"
            if prescription and job["request"].get("schema") == "prescription_field_psf_v3" else
            "prescription_psf_v1:matched_stop_transmission_paraxial_pupil_composition_v2"
            if prescription and passive_stop else
            "prescription_psf_v1:matched_prescription_paraxial_pupil_composition"
            if prescription
            else "prysm_psf_v1:independently_declared_pupil_composition"
        ),
        license="Mixed parent Lens and PSF source rights; see immutable parent and factory job",
        physical_instance=None,
        parents=[base["id"]],
        factory_job_id=None,
        psf_factory_job_id=job["id"],
        factory_application_conditions=copy.deepcopy(conditions),
        factory_condition_scope=condition_scope,
        valid_domain=domain,
        uncertainty={"kind": "unknown"},
    )
    result["payload"]["psf"] = copy.deepcopy(candidate)
    if prescription:
        # Radiometry's normalized field axis belongs to the traced image heights,
        # not the selected sensor's corner. Never stretch it when reusing lenses.
        result["payload"]["field_reference_radius_mm"] = job["request"]["prescription"][
            "image_height_mm"
        ][-1]
    result["included_effects"] = sorted(
        set(base["included_effects"]) | {"lens_psf", "pixel_aperture"}
    )
    result["properties"]["psf_application_basis"] = (
        "prescription_opd_stop_transmission_paraxial_exit_pupil"
        if prescription and passive_stop else
        "prescription_opd_uniform_paraxial_exit_pupil"
        if prescription
        else job["request"]["application_basis"]
    )
    result["properties"]["psf_pupil_f_number"] = {"value": pupil_f_number, "unit": "1"}
    if prescription:
        result["properties"].update(
            focal_length={"value": job["request"]["reference_focal_length_mm"], "unit": "mm"},
            focal_length_basis="declared_rectilinear_image_reference; see calculated spectral EFL",
            f_number={"value": pupil_f_number, "unit": "1"},
            f_number_basis=(
                "reference_focal_length / entrance_pupil_diameter_at_reference_wavelength"
            ),
            psf_object_conjugate=job["request"]["object_conjugate"],
            psf_field_interpolation="bilinear_source_position; intermediate fields unvalidated",
        )
        if prescription_binding is not None:
            result["properties"]["physical_prescription_binding"] = prescription_binding
        if job["request"].get("schema") == "prescription_field_psf_v5":
            result["properties"]["focal_length_basis"] = (
                "declared_reference_f_number_only; finite image mapping uses signed magnification"
            )
    return result


def verify_psf_model_link(service, data):
    job = get_job(service, data["psf_factory_job_id"])
    if len(data["parents"]) != 1:
        raise ValueError("Composite PSF model requires exactly one preserved parent Lens response")
    base = service.registry.get("models", data["parents"][0])
    expected = psf_candidate_fields(
        job,
        base,
        data["factory_application_conditions"],
        data["factory_condition_scope"],
        service=service,
    )
    if any(data.get(k) != value for k, value in expected.items() if k not in {"name", "revision"}):
        raise ValueError("Composite Lens differs from its preserved PSF and parent response")
    for artifact in job["artifacts"]:
        service.registry.blob_path(artifact["sha256"])
