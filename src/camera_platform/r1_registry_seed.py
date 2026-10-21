"""Register a bounded, solver-free R1 temporal/HDR/DCG example.

The R1 design bundle is a separate, hash-pinned catalog.  This bridge never
pretends that its archived optical response was published by the live Factory:
the supplied, precomputed PSF/RI/angular response becomes a Registry Lens,
while each ideal spectral image receives the bundle's on-axis E/L exactly once.
The original radiometry and component hashes remain recorded as provenance.
"""

from __future__ import annotations

import hashlib
import io
import os
from copy import deepcopy

import numpy as np

from . import capture_programs, imaging, sequences
from .demo import RAW_FORMAT
from .project_mode import R1_MODE, read as read_project_mode
from .r1_design_assets import BUNDLE_ENV, catalog, load_sources
from .registry import IntegrityError, digest

SEED_VERSION = "r1-registry-pilot-v1"
MODEL_ID = "r1-stop-clear-v1"
FLAT_ID = "r1-flat-v1"
CHECKER_ID = "r1-checker-v1"
_PROVENANCE = {
    "source_kind": "estimated",
    "license": "MIT; authored synthetic CameraE2E R1 engineering example",
    "physical_instance": None,
    "parents": [],
}
_DOMAIN = {
    # This is a small installed pilot.  The design-experiment catalog has a
    # different exposure grid; it must not silently widen this Registry path.
    "exposure_s": {"min": 0.004, "max": 0.008, "unit": "s"},
    "temperature_c": {"min": 25.0, "max": 25.0, "unit": "degC"},
    "gain": {"min": 1.0, "max": 1.0, "unit": "1"},
}
_CONDITIONS = {
    "exposure_s": {"value": 0.004, "unit": "s"},
    "temperature_c": {"value": 25.0, "unit": "degC"},
    "gain": {"value": 1.0, "unit": "1"},
    "light_id": {"value": "r1-packaged-ideal-image-v1", "unit": "1"},
}


def _uri(manifest_hash: str, suffix: str) -> str:
    return f"r1-bundle://{manifest_hash}/{SEED_VERSION}/{suffix}"


def _existing(service, kind: str, source_uri: str):
    matches = [item for item in service.registry.list(kind)
               if item.get("source_uri") == source_uri]
    if len(matches) > 1:
        raise IntegrityError(f"Duplicate R1 {kind} seed source: {source_uri}")
    return matches[0] if matches else None


def _ensure(service, kind: str, fields: dict, create):
    existing = _existing(service, kind, fields["source_uri"])
    if existing is None:
        return create(fields)
    for key, value in fields.items():
        if existing.get(key) != value:
            raise IntegrityError(f"R1 {kind} seed differs from its pinned {key}")
    if existing.get("physical_validation", "not_established") != "not_established":
        raise IntegrityError("R1 seed cannot acquire physical-validation status")
    return existing


def _ensure_test(service, fields: dict):
    matches = [item for item in service.registry.list("tests")
               if item.get("name") == fields["name"]
               and item.get("revision") == fields["revision"]]
    if len(matches) > 1:
        raise IntegrityError("Duplicate R1 pilot Test")
    if not matches:
        return service.create_test(fields)
    existing = matches[0]
    for key, value in fields.items():
        if existing.get(key) != value:
            raise IntegrityError(f"R1 pilot Test differs from its pinned {key}")
    return existing


def _model_fields(manifest_hash: str, role: str, asset_id: str,
                  payload: dict, *, included_effects: list[str], properties=None):
    # This pilot deliberately has a fixed two-sample axis; ensure() checks it
    # against the packaged model before any registry mutation.
    wave = [500.0, 600.0]
    return {
        **_PROVENANCE,
        "source_uri": _uri(manifest_hash, f"{MODEL_ID}/model/{role}"),
        "method_and_version": SEED_VERSION,
        "name": f"R1 등록 예제 · {role}",
        "revision": SEED_VERSION,
        "asset_id": asset_id,
        "kind": role,
        "reference_plane": "sensor_raw" if role == "isp" else "sensor_surface",
        "included_effects": included_effects,
        "valid_domain": deepcopy(_DOMAIN),
        "spectral_range": {"min": wave[0], "max": wave[-1], "unit": "nm"},
        "properties": properties or {},
        "raw_format": deepcopy(RAW_FORMAT) if role == "sensor" else {},
        "input_raw_format": deepcopy(RAW_FORMAT) if role == "isp" else {},
        "uncertainty": {"kind": "unknown"},
        "calibration_dataset_ids": [],
        "payload": payload,
    }


def _image(service, case, assembly, manifest_hash: str, scene_id: str):
    request = case.rate_request()
    scene_sha256 = case.identity()["scene_sha256"]
    source = np.asarray(request["scene"]["spectral_irradiance"], dtype="<f8")
    if source.shape != (16, 16, 2):
        raise ValueError("R1 registry pilot requires its frozen 16x16x2 ideal image")
    stream = io.BytesIO()
    np.save(stream, source, allow_pickle=False)
    content = stream.getvalue()
    metadata = imaging.spectral_template(service, assembly["id"])
    metadata.update(
        name=f"R1 등록 장면 · {scene_id} · E/L 적용 이상 영상",
        revision=SEED_VERSION,
        source_kind="estimated",
        source_uri=_uri(manifest_hash, f"{MODEL_ID}/image/{scene_id}/{scene_sha256}"),
        license=_PROVENANCE["license"],
        method_and_version="r1_packaged_radiance_times_on_axis_EL_once:1",
        sampling=deepcopy(request["scene"]["sampling"]),
    )
    existing = _existing(service, "spectral_images", metadata["source_uri"])
    if existing is None:
        return imaging.import_spectral(service, metadata, content)
    for key, value in metadata.items():
        if key != "asset_id" and existing.get(key) != value:
            raise IntegrityError(f"R1 image differs from its pinned {key}")
    artifact = next((item for item in existing.get("artifacts", [])
                     if item.get("name") == "spectral-image.npy"), None)
    if artifact is None or artifact["sha256"] != hashlib.sha256(content).hexdigest():
        raise IntegrityError("R1 image differs from its pinned ideal-plane array")
    service.registry.blob_path(artifact["sha256"])
    return existing


def _sequence(service, assembly, manifest_hash: str, name: str, images: list[dict]):
    template = sequences.scene_template(service, assembly["id"])
    template.update(
        name=f"R1 등록 {name} 시간 장면",
        revision=SEED_VERSION,
        source_uri=_uri(manifest_hash, f"{MODEL_ID}/sequence/{name}"),
        license=_PROVENANCE["license"],
        method_and_version=f"{SEED_VERSION}:{name}",
    )
    if name == "static":
        template["payload"]["knots"] = [
            {"time_ns": t, "spectral_image_id": images[0]["id"]}
            for t in (0, 80_000_000)
        ]
    else:
        template["payload"]["knots"] = [
            {"time_ns": i * 5_000_000, "spectral_image_id": images[i % 2]["id"]}
            for i in range(17)
        ]
    return _ensure(service, "scene_sequences", template,
                   lambda fields: sequences.create_sequence(service, "scene_sequences", fields))


def _control(service, assembly, scene, manifest_hash: str, name: str):
    template = sequences.control_template(service, assembly["id"], scene["id"])
    if len(template["payload"]["frames"]) != 4:
        raise ValueError("R1 temporal pilot requires exactly four recorded frames")
    for index, frame in enumerate(template["payload"]["frames"]):
        frame["applied"]["exposure_ns"] = (4 if index % 2 == 0 else 8) * 1_000_000
    template.update(
        name=f"R1 등록 {name} 4프레임 적용 설정",
        revision=SEED_VERSION,
        source_uri=_uri(manifest_hash, f"{MODEL_ID}/control/{name}"),
        license=_PROVENANCE["license"],
        method_and_version=f"{SEED_VERSION}:recorded_applied_settings",
    )
    return _ensure(service, "control_sequences", template,
                   lambda fields: sequences.create_sequence(service, "control_sequences", fields))


def _program(service, assembly, manifest_hash: str, mode: str):
    template = capture_programs.template(service, assembly["id"], mode=mode)
    template.update(
        name=f"R1 등록 {'독립 reset 정적 HDR' if mode == 'independent_bracket' else '공유 전하 DCG'}",
        revision=SEED_VERSION,
        source_uri=_uri(manifest_hash, f"{MODEL_ID}/program/{mode}"),
        license=_PROVENANCE["license"],
        method_and_version=f"{SEED_VERSION}:{mode}",
    )
    if template["payload"].get("waveform") is not None:
        raise ValueError("R1 registered static pilots must not enable periodic light")
    return _ensure(service, "capture_programs", template,
                   lambda fields: capture_programs.create(service, fields))


def _test(service, assembly, scene, control, manifest_hash: str, name: str, program=None):
    fields = sequences.test_template(service, assembly["id"], scene["id"], control["id"],
                                     program["id"] if program else None)
    fields.update(name=f"R1 등록 {name} 시험 · {manifest_hash[:12]}", revision=SEED_VERSION)
    return _ensure_test(service, fields)


def ensure(service):
    """Idempotently seed one registered R1-only pilot into the current project.

    This is deliberately separate from the general demo and never runs in a
    primary or unmarked state.  Reopening checks the packaged source and the
    existing record fields instead of silently replacing a changed revision.
    """
    marker = read_project_mode(service.registry.directory)
    if marker is None or marker["mode"] != R1_MODE:
        return None
    if os.environ.get(BUNDLE_ENV):
        raise ValueError("Registered R1 modes require the installed package bundle, not an override")
    inventory = catalog(service.workspace)
    manifest_hash = inventory["manifest_sha256"]
    cases = {
        scene_id: load_sources(service.workspace, [MODEL_ID], scene_id)[MODEL_ID]
        for scene_id in (FLAT_ID, CHECKER_ID)
    }
    identity = cases[FLAT_ID].identity()
    if identity["model_id"] != MODEL_ID or identity["physical_validation"] != "not_established":
        raise ValueError("R1 pilot source identity differs from its supported model")
    model_identity = {key: value for key, value in identity.items()
                      if key not in {"scene_id", "scene_revision", "scene_sha256",
                                     "scene_source_uri"}}
    request = cases[FLAT_ID].rate_request()
    if (request["wavelength_nm"] != [500.0, 600.0]
            or request["scene"]["sampling"]["full_sensor_shape"] != [128, 128]
            or request["scene"]["sampling"]["crop_origin"] != [56, 56]
            or request["pixel_pitch_um"] != 3.0):
        raise ValueError("R1 pilot source lies outside its frozen installed envelope")

    asset_ids = {}
    for role in ("lens", "sensor", "isp"):
        asset = _ensure(service, "assets", {
            **_PROVENANCE,
            "source_uri": _uri(manifest_hash, f"{MODEL_ID}/asset/{role}"),
            "method_and_version": SEED_VERSION,
            "name": f"R1 등록 예제 · {role} 자산",
            "revision": SEED_VERSION,
            "kind": role,
            "description": "Hash-pinned synthetic R1 source; physical accuracy unvalidated",
            "properties": {"r1_source_identity": model_identity},
        }, service.create_asset)
        asset_ids[role] = asset["id"]

    lens_payload = deepcopy(request["lens"])
    original_radiometry = lens_payload.pop("radiometry")
    lens_payload["psf"] = deepcopy(request["psf"])
    lens = _ensure(service, "models", _model_fields(
        manifest_hash, "lens", asset_ids["lens"], lens_payload,
        included_effects=["lens_relative_illumination", "lens_incident_angle_distribution",
                          "lens_psf", "pixel_aperture"],
        properties={
            # The archived response is sampled only to this field radius and
            # angular limit.  Do not inherit the generic demo's wider claims.
            "image_circle_diameter": {
                "value": 2 * original_radiometry["image_height_mm"][-1], "unit": "mm"
            },
            "incident_angle_range": {
                "min": request["lens"]["theta_deg"][0],
                "max": request["lens"]["theta_deg"][-1], "unit": "deg"
            },
            "r1_source_identity": model_identity,
            "original_radiometry_sha256": digest(original_radiometry),
            "on_axis_throughput_location": "frozen_ideal_spectral_image_once",
        },
    ), service.create_model)
    sensor = _ensure(service, "models", _model_fields(
        manifest_hash, "sensor", asset_ids["sensor"], {
            "wavelength_nm": request["wavelength_nm"],
            "qe": request["qe"],
            "pixel_pitch_um": request["pixel_pitch_um"],
            "sensor_angular_response": request["sensor_angular_response"],
            "readout": request["readout"],
        }, included_effects=["sensor_effective_qe", "sensor_angular_response",
                             "mean_dark_charge", "readout_clipping"],
        properties={
            "active_width": {"value": 0.384, "unit": "mm"},
            "active_height": {"value": 0.384, "unit": "mm"},
            "accepted_angle_range": {"min": 0.0, "max": 30.0, "unit": "deg"},
            "r1_source_identity": model_identity,
        },
    ), service.create_model)
    isp = _ensure(service, "models", _model_fields(
        manifest_hash, "isp", asset_ids["isp"], {
            "implementation": "pyisetcam", "mode": "fixed", "awb": "fixed",
            "lens_shading_correction": "bypass",
        }, included_effects=["software_isp"],
        properties={"r1_source_identity": model_identity},
    ), service.create_model)
    assembly_fields = {
        "name": "R1 등록 2파장 카메라 · clear",
        "revision": SEED_VERSION,
        "model_ids": [lens["id"], sensor["id"], isp["id"]],
        "conditions": deepcopy(_CONDITIONS),
        "spectral_range": {"min": 500.0, "max": 600.0, "unit": "nm"},
        "raw_format": deepcopy(RAW_FORMAT),
        "description": ("R1 hash-pinned 16×16 2-band pilot. Optical E/L is in the frozen "
                        "ideal image once; supplied PSF/RI/angular response is in the Lens. "
                        "No physical accuracy or arbitrary composition claim."),
    }
    assemblies = [record for record in service.registry.list("assemblies")
                  if record.get("name") == assembly_fields["name"]
                  and record.get("revision") == SEED_VERSION]
    if len(assemblies) > 1:
        raise IntegrityError("Duplicate R1 pilot Assembly")
    if assemblies:
        assembly = assemblies[0]
        for key, value in assembly_fields.items():
            if assembly.get(key) != value:
                raise IntegrityError(f"R1 pilot Assembly differs from its pinned {key}")
    else:
        assembly = service.create_assembly(assembly_fields)
    if service.check(assembly["id"])["status"] != "PASS":
        raise ValueError("R1 pilot Assembly no longer passes its registered contract")

    images = {scene_id: _image(service, cases[scene_id], assembly, manifest_hash, scene_id)
              for scene_id in (FLAT_ID, CHECKER_ID)}
    static_scene = _sequence(service, assembly, manifest_hash, "static", [images[FLAT_ID]])
    changing_scene = _sequence(service, assembly, manifest_hash, "changing",
                               [images[FLAT_ID], images[CHECKER_ID]])
    static_control = _control(service, assembly, static_scene, manifest_hash, "static")
    changing_control = _control(service, assembly, changing_scene, manifest_hash, "changing")
    hdr = _program(service, assembly, manifest_hash, "independent_bracket")
    dcg = _program(service, assembly, manifest_hash, "shared_charge")
    tests = {
        "temporal": _test(service, assembly, changing_scene, changing_control,
                          manifest_hash, "시간 촬영"),
        "hdr": _test(service, assembly, static_scene, static_control, manifest_hash,
                     "독립 reset HDR", hdr),
        "dcg": _test(service, assembly, static_scene, static_control, manifest_hash,
                     "공유 전하 DCG", dcg),
    }
    return {
        "schema": SEED_VERSION,
        "project_id": marker["project_id"],
        "bundle_manifest_sha256": manifest_hash,
        "source_identity": identity,
        "assembly_id": assembly["id"],
        "spectral_image_ids": {key: value["id"] for key, value in images.items()},
        "scene_sequence_ids": {"static": static_scene["id"], "changing": changing_scene["id"]},
        "control_sequence_ids": {"static": static_control["id"],
                                 "changing": changing_control["id"]},
        "capture_program_ids": {"hdr": hdr["id"], "dcg": dcg["id"]},
        "test_ids": {key: value["id"] for key, value in tests.items()},
        "physical_validation": "not_established",
        "limitations": ["Two wavelength bins, 16x16 crop, one registered optical/electrical pair",
                        "Changing scene is authored ideal-image interpolation, not 3D motion",
                        "HDR is independent reset; DCG shares one charge per acquisition",
                        "No PWM/LFM, retained charge, vendor or physical equivalence"],
    }
