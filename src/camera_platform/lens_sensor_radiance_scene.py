"""Immutable object-side spectral radiance for the Lens DB x Sensor DB path.

``spectral_images`` is an *image-plane irradiance* contract and cannot be
reused here: the pyisetcam raytrace optics must receive scene radiance before
its own F-number, geometric PSF and (currently assumed) radiometry are applied.
"""

from __future__ import annotations

import hashlib
import io
import math
from typing import Any

import numpy as np

from pyisetcam.scene import scene_create, scene_interpolate_w, scene_set
from pyisetcam.utils import energy_to_quanta

from .registry import ConflictError, IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_object_radiance_scene_v1"
COLLECTION = "lens_sensor_radiance_scenes"
REFERENCE_PLANE = "object_directional_radiance_before_lens"
UNIT = "W/m^2/sr/nm"
SAMPLING = "rectilinear_tangent_plane_centered_v1"
TARGET_WAVELENGTH_NM = (450.0, 550.0, 650.0)
MAX_NPY_BYTES = 32 * 1024 * 1024
_FIELDS = frozenset({
    "schema", "name", "revision", "source_kind", "source_uri", "license",
    "method_and_version", "physical_instance", "parents", "wavelength_nm",
    "unit", "reference_plane", "sampling_convention", "horizontal_fov_deg",
    "object_distance_m", "included_effects",
})


def template() -> dict[str, Any]:
    """Return an explicit import contract, not a physical-scene claim."""
    return {
        "schema": SCHEMA,
        "name": "Supplied object-side spectral radiance",
        "revision": "1",
        "source_kind": "simulated",
        "source_uri": "user-supplied://declare-original-source",
        "license": "Declare actual rights for this source",
        "method_and_version": "declare-radiance-generation-method-and-version",
        "physical_instance": None,
        "parents": [],
        "wavelength_nm": list(TARGET_WAVELENGTH_NM),
        "unit": UNIT,
        "reference_plane": REFERENCE_PLANE,
        "sampling_convention": SAMPLING,
        "horizontal_fov_deg": 0.12,
        "object_distance_m": "infinity",
        "included_effects": [],
    }


def _finite_number(value: Any, name: str, low: float, high: float) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].")
    return result


def metadata_contract(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("Declare exactly the object-radiance scene metadata fields.")
    if value["schema"] != SCHEMA or value["unit"] != UNIT or value[
        "reference_plane"
    ] != REFERENCE_PLANE or value["sampling_convention"] != SAMPLING:
        raise ValueError("Object-radiance schema, units, reference plane or sampling differs.")
    if value["included_effects"] != []:
        raise ValueError("Object radiance must not contain lens, sensor or ISP effects.")
    result = dict(value)
    for key, upper in (
        ("name", 200), ("revision", 80), ("source_uri", 2000),
        ("license", 500), ("method_and_version", 500),
    ):
        item = value[key]
        if not isinstance(item, str) or not 1 <= len(item.strip()) <= upper:
            raise ValueError(f"{key} requires 1..{upper} characters.")
        result[key] = item.strip()
    for key in ("source_uri", "license", "method_and_version"):
        if result[key] == template()[key]:
            raise ValueError(f"Replace the {key} template placeholder with actual provenance.")
    if not isinstance(value["source_kind"], str) or value[
        "source_kind"
    ] not in {"measured", "simulated", "vendor", "estimated"}:
        raise ValueError("source_kind must be measured, simulated, vendor or estimated.")
    instance = value["physical_instance"]
    if instance is not None and (
        not isinstance(instance, str) or not 1 <= len(instance.strip()) <= 500
    ):
        raise ValueError("physical_instance must be null or a nonempty string.")
    if value["source_kind"] == "measured" and not instance:
        raise ValueError("Measured radiance requires a physical_instance identity.")
    result["physical_instance"] = instance.strip() if instance else None
    parents = value["parents"]
    if (
        not isinstance(parents, list) or len(parents) > 100
        or any(not isinstance(x, str) or not 1 <= len(x.strip()) <= 256 for x in parents)
    ):
        raise ValueError("parents must be at most 100 nonempty source identifiers.")
    result["parents"] = [item.strip() for item in parents]
    waves = value["wavelength_nm"]
    if not isinstance(waves, list) or not 3 <= len(waves) <= 16:
        raise ValueError("Supply 3..16 spectral radiance samples.")
    result["wavelength_nm"] = [
        _finite_number(item, "wavelength_nm", 200.0, 2500.0) for item in waves
    ]
    if (
        any(b <= a for a, b in zip(
            result["wavelength_nm"], result["wavelength_nm"][1:], strict=False
        ))
        or result["wavelength_nm"][0] > TARGET_WAVELENGTH_NM[0]
        or result["wavelength_nm"][-1] < TARGET_WAVELENGTH_NM[-1]
    ):
        raise ValueError("Increasing spectral support must cover 450, 550 and 650 nm.")
    result["horizontal_fov_deg"] = _finite_number(
        value["horizontal_fov_deg"], "horizontal_fov_deg", 0.001, 30.0
    )
    distance = value["object_distance_m"]
    result["object_distance_m"] = (
        "infinity" if distance == "infinity" else
        _finite_number(distance, "object_distance_m", 0.05, 1e9)
    )
    return result


def load_radiance_npy(content: bytes) -> np.ndarray:
    """Bound NPY allocation and reject implicit RGB, NaN and sensor-plane data."""
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_NPY_BYTES:
        raise ValueError("Radiance NPY must be nonempty and at most 32 MiB.")
    stream = io.BytesIO(content)
    try:
        version = np.lib.format.read_magic(stream)
        if version == (1, 0):
            shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream)
        elif version == (2, 0):
            shape, fortran, dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError("Radiance input supports NPY versions 1 and 2.")
    except (EOFError, UnicodeError, ValueError) as exc:
        raise ValueError("Radiance input is not a supported NPY array.") from exc
    if (
        dtype.hasobject or dtype.fields or dtype.kind != "f"
        or dtype.itemsize not in (4, 8) or fortran
    ):
        raise ValueError("Radiance NPY requires C-order float32/64 samples.")
    if (
        len(shape) != 3 or not 3 <= shape[2] <= 16
        or any(type(n) is not int or not 32 <= n <= 256 or n % 2 for n in shape[:2])
    ):
        raise ValueError("Radiance NPY shape must be [32..256 even,32..256 even,3..16].")
    if math.prod(shape) * dtype.itemsize > MAX_NPY_BYTES or (
        stream.tell() + math.prod(shape) * dtype.itemsize != len(content)
    ):
        raise ValueError("Radiance NPY bytes disagree with its bounded header.")
    array = np.load(io.BytesIO(content), allow_pickle=False)
    if not np.isfinite(array).all() or np.any(array < 0):
        raise ValueError("Radiance samples must be finite and nonnegative.")
    # This is a numerical overflow guard, not a physical brightness threshold.
    if np.any(array > 1e12):
        raise ValueError("Radiance exceeds the supported numerical input range.")
    return np.ascontiguousarray(array)


def _artifact(record: dict[str, Any]) -> dict[str, Any]:
    artifacts = record.get("artifacts")
    if (
        not isinstance(artifacts, list) or len(artifacts) != 2
        or [item.get("name") for item in artifacts] != [
            "object-spectral-radiance.npy", "object-spectral-radiance-metadata.json"
        ]
    ):
        raise IntegrityError("Radiance scene lacks its original NPY and metadata artifacts.")
    return artifacts[0]


def import_radiance_scene(service: Any, metadata: dict[str, Any], content: bytes) -> dict[str, Any]:
    data = metadata_contract(metadata)
    array = load_radiance_npy(content)
    if len(data["wavelength_nm"]) != array.shape[2]:
        raise ValueError("Radiance wavelength axis must match the NPY band count.")
    # Reject all-zero data: it cannot establish a meaningful optical response
    # and does not justify an expensive strict-PSF run.
    if not np.any(array > 0):
        raise ValueError("Radiance scene has no nonzero spectral samples.")
    original = service.registry.store_blob(
        content, "object-spectral-radiance.npy", "application/octet-stream"
    )
    metadata_artifact = service.registry.store_blob(
        canonical(data).encode(),
        "object-spectral-radiance-metadata.json", "application/json"
    )
    vertical_fov = math.degrees(2.0 * math.atan(
        math.tan(math.radians(data["horizontal_fov_deg"]) / 2.0)
        * array.shape[0] / array.shape[1]
    ))
    document = {
        **data,
        "shape": list(array.shape),
        "vertical_fov_deg": vertical_fov,
        "source_sha256": original["sha256"],
        "artifacts": [original, metadata_artifact],
        "approval_status": "draft",
        "physical_validation": "not_established",
        "source_semantics": "user_declared_object_radiance_not_verified_ground_truth",
    }
    identity = "lens-sensor-radiance-scene-" + digest({
        "metadata": data, "source_sha256": original["sha256"]
    })
    try:
        return service.registry.put(COLLECTION, document, identity=identity)
    except ConflictError as exc:
        existing = service.registry.get(COLLECTION, identity)
        if {key: val for key, val in existing.items()
                if key not in {"id", "created_at", "content_hash"}} != document:
            raise IntegrityError(
                "Radiance scene identity collides with different content."
            ) from exc
        return get_radiance_scene(service, identity)


def load_radiance_scene(service: Any, identity: str) -> tuple[dict[str, Any], np.ndarray]:
    record = service.registry.get(COLLECTION, identity)
    metadata = metadata_contract({key: record[key] for key in _FIELDS})
    source = _artifact(record)
    source_path = service.registry.blob_path(source["sha256"])
    content = source_path.read_bytes()
    if (
        hashlib.sha256(content).hexdigest() != source.get("sha256")
        or source.get("sha256") != record.get("source_sha256")
        or source.get("bytes") != len(content)
    ):
        raise IntegrityError("Radiance scene original bytes differ from the frozen source.")
    array = load_radiance_npy(content)
    if list(array.shape) != record.get("shape") or len(metadata["wavelength_nm"]) != array.shape[2]:
        raise IntegrityError("Radiance scene shape or spectral axis differs from its source.")
    if not np.any(array > 0):
        raise IntegrityError("Radiance scene has no nonzero source samples.")
    vertical_fov = math.degrees(2.0 * math.atan(
        math.tan(math.radians(metadata["horizontal_fov_deg"]) / 2.0)
        * array.shape[0] / array.shape[1]
    ))
    if not math.isclose(record.get("vertical_fov_deg", -1), vertical_fov, rel_tol=1e-12):
        raise IntegrityError("Radiance scene vertical FOV differs from its source geometry.")
    metadata_bytes = service.registry.blob_path(record["artifacts"][1]["sha256"]).read_bytes()
    if metadata_bytes != canonical(metadata).encode():
        raise IntegrityError("Radiance scene metadata artifact differs from its record.")
    return record, array


def get_radiance_scene(service: Any, identity: str) -> dict[str, Any]:
    record, _ = load_radiance_scene(service, identity)
    return record


def list_radiance_scenes(service: Any) -> list[dict[str, Any]]:
    return [get_radiance_scene(service, row["id"]) for row in service.registry.list(COLLECTION)]


def source_identity(record: dict[str, Any]) -> dict[str, Any]:
    """Small job pin; immutable registry record itself remains the full source."""
    return {
        "id": record["id"], "content_hash": record["content_hash"],
        "source_sha256": record["source_sha256"],
        "shape": record["shape"],
        "wavelength_nm": record["wavelength_nm"],
        "horizontal_fov_deg": record["horizontal_fov_deg"],
        "object_distance_m": record["object_distance_m"],
        "reference_plane": record["reference_plane"],
        "unit": record["unit"],
    }


def scene_from_radiance(record: dict[str, Any], array: np.ndarray, *, assets: Any):
    """Adapt user radiance to pyisetcam Scene without luminance normalization."""
    waves = np.asarray(record["wavelength_nm"], dtype=float)
    targets = np.asarray(TARGET_WAVELENGTH_NM, dtype=float)
    energy = np.empty((*array.shape[:2], len(targets)), dtype=float)
    for index, wave in enumerate(targets):
        upper = int(np.searchsorted(waves, wave, side="left"))
        if upper < waves.size and waves[upper] == wave:
            energy[..., index] = array[..., upper]
        else:
            lower = upper - 1
            fraction = (wave - waves[lower]) / (waves[upper] - waves[lower])
            energy[..., index] = (1.0 - fraction) * array[..., lower] + fraction * array[..., upper]
    photons = energy_to_quanta(energy, targets)
    if not np.isfinite(photons).all():
        raise ValueError("Radiance-to-photon conversion is outside the numerical domain.")
    scene = scene_create("uniform ee", array.shape[:2], asset_store=assets)
    scene_interpolate_w(scene, targets, preserve_luminance=False, asset_store=assets)
    scene_set(scene, "photons", photons)
    scene_set(scene, "fov", record["horizontal_fov_deg"])
    distance = record["object_distance_m"]
    scene_set(scene, "distance", math.inf if distance == "infinity" else distance)
    mean_energy = np.mean(energy, axis=(0, 1))
    scene.fields["illuminant_format"] = "spectral"
    scene.fields["illuminant_energy"] = mean_energy
    scene.fields["illuminant_photons"] = energy_to_quanta(mean_energy, targets)
    scene.fields["illuminant_comment"] = "spatial_mean_supplied_radiance_not_independent_illuminant"
    scene.fields["source_type"] = "registered_object_spectral_radiance"
    scene.fields["source_sha256"] = record["source_sha256"]
    scene.metadata["lens_sensor_radiance_scene"] = source_identity(record)
    return scene
