"""Hash-pinned, portable model and scene inputs for R1 design experiments.

The bundle is a registered set of engineering examples. Its manifest is the
registration boundary: callers choose model and scene IDs, never historical
QA report paths or content-addressed blobs from another registry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from importlib import resources
from pathlib import Path

import numpy as np

from .electrical import default_electrical_model, validate_electrical_model
from .registry import canonical

BUNDLE_ENV = "CAMERAE2E_R1_ASSET_BUNDLE"
BUNDLE_SCHEMA = "camerae2e_r1_design_bundle_v1"
MODEL_SCHEMA = "camerae2e_r1_design_model_v1"
COMPOSITION_SCHEMA = "camerae2e_r1_design_composition_v1"
SCENE_SCHEMA = "camerae2e_r1_design_scene_v1"
COMPONENT_SCHEMAS = {
    "optics": "camerae2e_r1_optics_v1",
    "pixels": "camerae2e_r1_pixel_v1",
    "electricals": "camerae2e_r1_electrical_v1",
}
COMPONENT_KEYS = {"optics": "optics_id", "pixels": "pixel_id",
                  "electricals": "electrical_id"}
_ID = re.compile(r"[a-z][a-z0-9-]{2,80}\Z")
_FILE = re.compile(r"(?:models|scenes|optics|pixels|electricals)/[a-z0-9-]+\.json\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_INPUT_BYTES = 8 * 1024 * 1024


def bundle_root(workspace: str | Path) -> Path:
    """An installed bundle is the default; an absolute external bundle can override.

    The environment points to a self-contained bundle directory and is never
    persisted as a source identity. The bundle's content hash is persisted.
    """
    selected = os.environ.get(BUNDLE_ENV)
    packaged = resources.files("camera_platform").joinpath("data/r1")
    if not selected and not isinstance(packaged, Path):
        raise ValueError("R1 examples require a filesystem-installed package")
    path = Path(selected) if selected else packaged
    if not path.is_absolute() and selected:
        raise ValueError(f"{BUNDLE_ENV} must be an absolute directory")
    if path.is_symlink() or not path.is_dir():
        raise FileNotFoundError(f"R1 example bundle missing: {path}")
    return path.resolve()


def _read(path: Path, *, expected: str | None, max_bytes: int) -> tuple[dict, str, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("R1 bundle entry must be a regular file")
    if path.stat().st_size > max_bytes:
        raise ValueError("R1 bundle entry exceeds its byte limit")
    content = path.read_bytes()
    if len(content) > max_bytes:
        raise ValueError("R1 bundle entry exceeds its byte limit")
    checksum = hashlib.sha256(content).hexdigest()
    if expected is not None and checksum != expected:
        raise ValueError("R1 bundle entry differs from its manifest hash")

    def unique_fields(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("R1 bundle JSON has duplicate fields")
            value[key] = item
        return value

    value = json.loads(content, object_pairs_hook=unique_fields)
    if not isinstance(value, dict):
        raise ValueError("R1 bundle JSON must be an object")
    return value, checksum, len(content)


def _inventory(workspace: str | Path) -> tuple[Path, dict, str]:
    root = bundle_root(workspace)
    manifest, manifest_hash, _ = _read(root / "manifest.json", expected=None,
                                       max_bytes=_MAX_MANIFEST_BYTES)
    if (set(manifest) != {"schema", "bundle_id", "bundle_version", "models", "scenes",
                          "components", "physical_validation", "distribution_rights"}
            or manifest["schema"] != BUNDLE_SCHEMA
            or manifest["physical_validation"] != "not_established"
            or not _ID.fullmatch(manifest["bundle_id"])
            or not isinstance(manifest["bundle_version"], str)
            or not manifest["bundle_version"]):
        raise ValueError("Invalid R1 bundle manifest")
    rights = manifest["distribution_rights"]
    if (not isinstance(rights, dict)
            or set(rights) != {"status", "license", "notice", "evidence", "scope"}
            or any(not isinstance(rights[key], str) or not rights[key]
                   for key in ("status", "license", "notice", "scope"))
            or not isinstance(rights["evidence"], list)
            or not rights["evidence"]):
        raise ValueError("R1 bundle requires a declared distribution-rights basis")
    if (not isinstance(manifest["components"], dict)
            or set(manifest["components"]) != set(COMPONENT_SCHEMAS)):
        raise ValueError("R1 bundle requires registered optics, pixel and electrical catalogs")
    for group in ("models", "scenes", *COMPONENT_SCHEMAS):
        entries = (manifest[group] if group in {"models", "scenes"}
                   else manifest["components"][group])
        if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
            raise ValueError("R1 bundle requires bounded model and scene catalogs")
        ids = set()
        for entry in entries:
            required = {"id", "revision", "name", "file", "sha256", "bytes"}
            if group == "models":
                required.update({"kind", "components"})
            if (not isinstance(entry, dict)
                    or set(entry) != required
                    or not all(isinstance(entry[k], str) for k in (
                        "id", "revision", "name", "file", "sha256"))
                    or not _ID.fullmatch(entry["id"])
                    or not _FILE.fullmatch(entry["file"])
                    or not _HASH.fullmatch(entry["sha256"])
                    or type(entry["bytes"]) is not int
                    or not 0 < entry["bytes"] <= _MAX_INPUT_BYTES
                    or not entry["revision"] or not entry["name"]
                    or entry["id"] in ids):
                raise ValueError("Invalid R1 bundle catalog entry")
            if entry["file"].split("/")[0] != group:
                raise ValueError("R1 bundle catalog entry is in the wrong group")
            if group == "models" and (
                entry["kind"] not in {"legacy_full", "composition"}
                or not isinstance(entry["components"], dict)
                or set(entry["components"]) != set(COMPONENT_KEYS.values())
                or any(not isinstance(value, str) or not _ID.fullmatch(value)
                       for value in entry["components"].values())
            ):
                raise ValueError("R1 model requires three registered component IDs")
            ids.add(entry["id"])
    for model in manifest["models"]:
        for group, key in COMPONENT_KEYS.items():
            available = {item["id"] for item in manifest["components"][group]}
            if model["components"][key] not in available:
                raise ValueError("R1 model references an unregistered component")
    return root, manifest, manifest_hash


def _entry(root: Path, item: dict, schema: str) -> tuple[dict, Path, str]:
    path = root / item["file"]
    if path.parent.is_symlink():
        raise ValueError("R1 bundle folder must not be a symlink")
    data, checksum, size = _read(path, expected=item["sha256"], max_bytes=_MAX_INPUT_BYTES)
    if (size != item["bytes"] or data.get("schema") != schema
            or data.get("id") != item["id"] or data.get("revision") != item["revision"]
            or data.get("name") != item["name"]
            or data.get("physical_validation") != "not_established"):
        raise ValueError("R1 bundle entry differs from its registered metadata")
    return data, path, checksum


def _components(root: Path, manifest: dict, refs: dict):
    documents, paths, identities = {}, {}, {}
    for group, key in COMPONENT_KEYS.items():
        item = next(entry for entry in manifest["components"][group]
                    if entry["id"] == refs[key])
        document, path, checksum = _entry(root, item, COMPONENT_SCHEMAS[group])
        documents[group] = document
        paths[path] = checksum
        identities[group] = {"id": item["id"], "revision": item["revision"],
                             "sha256": checksum}
    optical, pixel, electrical = (documents[group] for group in COMPONENT_KEYS)
    if (set(optical) != {"schema", "id", "revision", "name", "physical_validation",
                         "lens", "psf", "wavelength_nm", "origin"}
            or set(pixel) != {"schema", "id", "revision", "name", "physical_validation",
                             "qe", "sensor_angular_response", "pixel_pitch_um",
                             "wavelength_nm", "cfa", "origin"}
            or set(electrical) != {"schema", "id", "revision", "name",
                                  "physical_validation", "readout", "electrical_model",
                                  "cfa", "origin"}):
        raise ValueError("R1 component fields differ from their v1 contracts")
    wavelengths = np.asarray(optical["wavelength_nm"])
    qe = np.asarray(pixel["qe"])
    angle = pixel["sensor_angular_response"]
    relative = np.asarray(angle.get("relative_response")) if isinstance(angle, dict) else None
    theta = np.asarray(angle.get("theta_deg")) if isinstance(angle, dict) else None
    if (wavelengths.ndim != 1 or not 2 <= wavelengths.size <= 16
            or not np.isfinite(wavelengths.astype(float)).all()
            or not np.array_equal(wavelengths, optical["lens"].get("wavelength_nm"))
            or not np.array_equal(wavelengths, optical["psf"].get("wavelength_nm"))
            or not np.array_equal(wavelengths, pixel["wavelength_nm"])
            or type(pixel["pixel_pitch_um"]) not in (int, float)
            or pixel["pixel_pitch_um"] <= 0
            or optical["psf"].get("sample_pitch_um") != pixel["pixel_pitch_um"]
            or pixel["cfa"] != "RGGB" or electrical["cfa"] != "RGGB"
            or qe.shape != (3, wavelengths.size)
            or not np.isfinite(qe.astype(float)).all()
            or np.any(qe < 0) or np.any(qe > 1)
            or relative is None or theta is None or theta.ndim != 1
            or relative.shape != (3, wavelengths.size, theta.size)
            or not np.isfinite(relative.astype(float)).all()
            or np.any(relative < 0)):
        raise ValueError("R1 optical, pixel and electrical component contracts differ")
    expected = default_electrical_model(electrical["readout"])
    validated = validate_electrical_model(electrical["electrical_model"])
    if canonical(validated) != canonical(expected):
        raise ValueError("R1 electrical model differs from its declared readout")
    return documents, paths, identities


def _require_baseline(model: dict, parts: dict) -> None:
    """The original full model must equal its newly assigned component triple."""
    request = model["request"]
    optical, pixel, electrical = (parts[group] for group in COMPONENT_KEYS)
    fields = (
        (request["lens"], optical["lens"]),
        (request["psf"], optical["psf"]),
        (request["wavelength_nm"], optical["wavelength_nm"]),
        (request["qe"], pixel["qe"]),
        (request["sensor_angular_response"], pixel["sensor_angular_response"]),
        (request["pixel_pitch_um"], pixel["pixel_pitch_um"]),
        (request["readout"], electrical["readout"]),
    )
    if any(canonical(before) != canonical(after) for before, after in fields):
        raise ValueError("R1 legacy model differs from its registered components")


def _resolved_model(root: Path, manifest: dict, item: dict):
    schema = MODEL_SCHEMA if item["kind"] == "legacy_full" else COMPOSITION_SCHEMA
    document, path, checksum = _entry(root, item, schema)
    parts, paths, identities = _components(root, manifest, item["components"])
    paths[path] = checksum
    if item["kind"] == "legacy_full":
        _require_baseline(document, parts)
        return document, checksum, parts["electricals"]["electrical_model"], paths, identities
    if (set(document) != {"schema", "id", "revision", "name",
                         "physical_validation", "base_model_id", "components"}
            or document.get("components") != item["components"]
            or not isinstance(document.get("base_model_id"), str)):
        raise ValueError("R1 composition differs from its registered components")
    bases = [entry for entry in manifest["models"]
             if entry["id"] == document["base_model_id"] and entry["kind"] == "legacy_full"]
    if len(bases) != 1:
        raise ValueError("R1 composition requires one registered full base model")
    base_item = bases[0]
    base, base_path, base_hash = _entry(root, base_item, MODEL_SCHEMA)
    base_parts, base_paths, _ = _components(root, manifest, base_item["components"])
    _require_baseline(base, base_parts)
    paths.update(base_paths)
    paths[base_path] = base_hash
    assembled = deepcopy(base)
    assembled.update(id=document["id"], revision=document["revision"],
                     name=document["name"])
    request = assembled["request"]
    request["lens"] = deepcopy(parts["optics"]["lens"])
    request["psf"] = deepcopy(parts["optics"]["psf"])
    request["wavelength_nm"] = deepcopy(parts["optics"]["wavelength_nm"])
    request["pixel_pitch_um"] = parts["pixels"]["pixel_pitch_um"]
    request["qe"] = deepcopy(parts["pixels"]["qe"])
    request["sensor_angular_response"] = deepcopy(parts["pixels"]["sensor_angular_response"])
    request["readout"] = deepcopy(parts["electricals"]["readout"])
    assembled["origin"] = {"base_model": deepcopy(base["origin"]),
                           "component_sources": {group: deepcopy(part["origin"])
                                                 for group, part in parts.items()}}
    return assembled, checksum, parts["electricals"]["electrical_model"], paths, identities


def catalog(workspace: str | Path) -> dict:
    root, manifest, manifest_hash = _inventory(workspace)
    # Reading every listed object catches a missing or corrupt second choice
    # before presenting the catalog to a user.
    scenes = [_entry(root, item, SCENE_SCHEMA)[0] for item in manifest["scenes"]]
    for item in manifest["models"]:
        model, _, electrical_model, _, identities = _resolved_model(root, manifest, item)
        for scene in scenes:
            PortableDesignSource(model, scene, bundle={}, paths={},
                                 electrical_model=electrical_model, components=identities)
    for group, schema in COMPONENT_SCHEMAS.items():
        for item in manifest["components"][group]:
            _entry(root, item, schema)
    return {key: manifest[key] for key in ("schema", "bundle_id", "bundle_version")} | {
        "manifest_sha256": manifest_hash,
        "distribution_rights": {key: manifest["distribution_rights"][key]
                                for key in ("status", "license", "scope")},
        "models": [{key: item[key] for key in ("id", "revision", "name", "components")}
                   for item in manifest["models"]],
        "scenes": [{key: item[key] for key in ("id", "revision", "name")}
                   for item in manifest["scenes"]],
        "components": {
            group: [{key: item[key] for key in ("id", "revision", "name")}
                    for item in manifest["components"][group]]
            for group in COMPONENT_SCHEMAS
        },
    }


class PortableDesignSource:
    def __init__(self, model: dict, scene: dict, *, bundle: dict,
                 paths: dict[Path, str], electrical_model: dict, components: dict):
        self.model = model
        self.scene = scene
        self.bundle = bundle
        self.read_hashes = paths
        self.components = components
        self._validate()
        self.electrical_model = validate_electrical_model(electrical_model)

    def _validate(self) -> None:
        request = self.model["request"]
        contract = self.model["scene_contract"]
        if (not isinstance(request, dict) or "scene" in request
                or not isinstance(contract, dict)
                or set(contract) != {"wavelength_nm", "sampling", "reference_plane", "unit"}
                or not np.array_equal(request["wavelength_nm"], self.scene["wavelength_nm"])
                or not np.array_equal(contract["wavelength_nm"], self.scene["wavelength_nm"])
                or canonical(contract["sampling"]) != canonical(self.scene["sampling"])
                or contract["reference_plane"] != "sensor_ideal_image_before_psf_ri"
                or contract["unit"] != "W/m^2/nm"):
            raise ValueError("R1 model and scene spectral/sampling contracts differ")
        radiance = np.asarray(self.scene["spectral_radiance_w_m2_sr_nm"], dtype=float)
        sampling = contract["sampling"]
        if (radiance.ndim != 3 or radiance.shape[-1] != len(request["wavelength_nm"])
                or not 1 <= radiance.shape[0] <= 512 or not 1 <= radiance.shape[1] <= 512
                or radiance.size > 512 * 512 * 32 or not np.isfinite(radiance).all()
                or np.any(radiance < 0)
                or not isinstance(sampling, dict)
                or sampling.get("pixel_pitch_um") != request["pixel_pitch_um"]):
            raise ValueError("R1 scene radiance has unsupported shape, value or sampling")
        throughput = np.asarray(
            request["lens"]["radiometry"]["irradiance_per_radiance_sr"], dtype=float
        )
        if (throughput.ndim != 2 or throughput.shape[0] != radiance.shape[-1]
                or not np.isfinite(throughput).all() or np.any(throughput < 0)):
            raise ValueError("R1 model lacks compatible optical throughput")
        self._radiance = radiance
        self._on_axis_transfer = throughput[:, 0]

    def _scene_request(self, radiance: np.ndarray) -> dict:
        request = deepcopy(self.model["request"])
        request["scene"] = {
            "schema": "spectral_ideal_image_v1",
            "included_effects": ["on_axis_lens_throughput"],
            "reference_plane": "sensor_ideal_image_before_psf_ri",
            "sampling": deepcopy(self.scene["sampling"]),
            "source": {"kind": self.scene["source"]["kind"],
                       "description": self.scene["source"]["description"]},
            "unit": "W/m^2/nm",
            "wavelength_nm": deepcopy(self.scene["wavelength_nm"]),
            "spectral_irradiance": radiance * self._on_axis_transfer,
        }
        return request

    def rate_request(self) -> dict:
        return self._scene_request(self._radiance)

    def radiance_request(self, scale: float, evaluation_index: int | None) -> dict:
        """Perturb the scene before applying wavelength-dependent lens E/L."""
        radiance = self._radiance.copy()
        if evaluation_index is not None:
            if evaluation_index % 2 == 0:
                radiance = radiance[::-1].copy()
            else:
                radiance = (radiance[:, ::-1].copy()
                            * np.linspace(1.15, .85, radiance.shape[-1]))
        return self._scene_request(radiance * scale)

    def identity(self) -> dict:
        return {
            "bundle_id": self.bundle["bundle_id"],
            "bundle_version": self.bundle["bundle_version"],
            "bundle_manifest_sha256": self.bundle["manifest_sha256"],
            "model_id": self.model["id"],
            "model_revision": self.model["revision"],
            "scene_id": self.scene["id"],
            "scene_revision": self.scene["revision"],
            "model_sha256": self.bundle["model_sha256"],
            "scene_sha256": self.bundle["scene_sha256"],
            "scene_source_uri": self.scene["source"]["source_uri"],
            "components": deepcopy(self.components),
            "distribution_rights": deepcopy(self.bundle["distribution_rights"]),
            "origin": deepcopy(self.model["origin"]),
            "physical_validation": "not_established",
        }

    def assert_unchanged(self) -> None:
        for path, checksum in self.read_hashes.items():
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise ValueError("R1 example input changed during comparison")


def load_sources(
    workspace: str | Path, model_ids: list[str], scene_id: str
) -> dict[str, PortableDesignSource]:
    root, manifest, manifest_hash = _inventory(workspace)
    models = {item["id"]: item for item in manifest["models"]}
    scenes = {item["id"]: item for item in manifest["scenes"]}
    if not model_ids or any(model_id not in models for model_id in model_ids):
        raise ValueError("Choose registered R1 model IDs")
    if scene_id not in scenes:
        raise ValueError("Choose a registered R1 scene ID")
    scene, scene_path, scene_hash = _entry(root, scenes[scene_id], SCENE_SCHEMA)
    output = {}
    for model_id in model_ids:
        model, model_hash, electrical_model, model_paths, component_ids = _resolved_model(
            root, manifest, models[model_id]
        )
        info = {"bundle_id": manifest["bundle_id"],
                "bundle_version": manifest["bundle_version"],
                "manifest_sha256": manifest_hash,
                "distribution_rights": {key: manifest["distribution_rights"][key]
                                        for key in ("status", "license")},
                "model_sha256": model_hash, "scene_sha256": scene_hash}
        output[model_id] = PortableDesignSource(
            model, scene, bundle=info, electrical_model=electrical_model,
            components=component_ids,
            paths={root / "manifest.json": manifest_hash, scene_path: scene_hash,
                   **model_paths},
        )
    return output
