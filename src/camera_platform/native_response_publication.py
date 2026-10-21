"""Publish a source-pinned native Sensor response as a restricted research draft.

The source qualification is asserted by the bundle author. A separate Sensor
model supplies the readout and full-raster geometry for the *same declared*
component revision. The resulting composite has no physical validation or
product-ranking authority. One exact Factory Lens is bound at publication;
another Lens needs a separately reviewed Sensor model revision.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import AssetCreate, ModelCreate
from .native_pixel_response import validate_response
from .native_response_assets import (
    MAX_MANIFEST_BYTES,
    MAX_QUALIFICATION_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_SOURCE_FILES,
    inspect_native_response_bundle,
    load_eligible_native_response_bundle,
)
from .registry import ConflictError, IntegrityError, digest

SCHEMA = "camera_native_response_publication_v1"
METHOD = "native_source_bundle_import:1"
MAX_ARCHIVE_BYTES = 512_000_000
MAX_UNCOMPRESSED_BYTES = 512_000_000
MAX_ARCHIVE_MEMBERS = MAX_SOURCE_FILES + 8
MAX_COMPRESSION_RATIO = 200
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_LIMITATION = (
    "Source-reported angular response qualification and same-declared-revision "
    "readout pairing support a research calculation only. Independent physical "
    "accuracy, Lens/Sensor product ranking and commercial release are not established."
)


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Archive member requires a relative POSIX path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Archive member escapes the bundle root")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError("Archive member escapes the bundle root")
    return path.as_posix()


def extract_native_response_bundle(zip_path: str | Path, dest_dir: str | Path) -> Path:
    """Expand a bounded upload ZIP with no links, path escape or silent extras."""
    archive = Path(zip_path)
    if not archive.is_file() or not 0 < archive.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise ValueError("Native response ZIP is empty or exceeds the compressed byte limit")
    target = Path(dest_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise ValueError("Native response extraction destination must be empty")
    try:
        with zipfile.ZipFile(archive) as source:
            infos = source.infolist()
            if not 1 <= len(infos) <= MAX_ARCHIVE_MEMBERS:
                raise ValueError("Native response ZIP exceeds the member count limit")
            files: list[tuple[zipfile.ZipInfo, str]] = []
            seen: set[str] = set()
            total = 0
            for item in infos:
                relative = _relative(item.filename.rstrip("/"))
                if relative in seen:
                    raise ValueError("Duplicate native response ZIP path")
                seen.add(relative)
                mode = item.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG):
                    raise ValueError("Native response ZIP cannot contain links or special files")
                if item.flag_bits & 1:
                    raise ValueError("Encrypted native response ZIP members are unsupported")
                if item.is_dir():
                    continue
                if item.file_size <= 0 or item.compress_size <= 0:
                    raise ValueError("Native response ZIP has an empty or invalid file")
                if item.file_size > MAX_UNCOMPRESSED_BYTES:
                    raise ValueError("Native response ZIP file exceeds the byte limit")
                if item.file_size > item.compress_size * MAX_COMPRESSION_RATIO:
                    raise ValueError("Native response ZIP compression ratio exceeds the limit")
                total += item.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise ValueError("Native response ZIP exceeds the uncompressed byte limit")
                files.append((item, relative))
            if not any(relative == "manifest.json" for _, relative in files):
                raise ValueError("Native response ZIP needs a root manifest.json")
            manifest_info = next(item for item, relative in files
                                 if relative == "manifest.json")
            if manifest_info.file_size > MAX_MANIFEST_BYTES:
                raise ValueError("Native response manifest exceeds its byte limit")
            for item, relative in files:
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with source.open(item) as reader, destination.open("xb") as writer:
                    while block := reader.read(1024 * 1024):
                        written += len(block)
                        if written > item.file_size:
                            raise ValueError("Native response ZIP size changed during extraction")
                        writer.write(block)
                if written != item.file_size:
                    raise ValueError("Native response ZIP member was truncated")
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("Invalid native response ZIP") from exc
    try:
        manifest = json.loads((target / "manifest.json").read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Native response manifest must be UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Native response manifest must be a JSON object")
    described = {"manifest.json"}
    for field in ("response", "qualification"):
        descriptor = manifest.get(field)
        if not isinstance(descriptor, dict):
            raise ValueError(f"Native response manifest lacks {field}")
        described.add(_relative(descriptor.get("path")))
    listed_sources = manifest.get("source_files")
    if not isinstance(listed_sources, list) or not 1 <= len(listed_sources) <= MAX_SOURCE_FILES:
        raise ValueError("Native response manifest requires bounded source_files")
    for item in listed_sources:
        if not isinstance(item, dict):
            raise ValueError("Native response source descriptor is invalid")
        described.add(_relative(item.get("path")))
    if described != {relative for _, relative in files}:
        raise ValueError("Native response ZIP has missing or unlisted files")
    return target


def _pinned_bytes(root: Path, relative: str, expected_sha: str, max_bytes: int) -> bytes:
    relative = _relative(relative)
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Native response source escaped the bundle root")
    if not 0 < path.stat().st_size <= max_bytes:
        raise ValueError("Native response source exceeds its byte limit")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha:
        raise ValueError("Native response source bytes changed after inspection")
    return content


def _qualified_lens(service, identity: str) -> dict:
    lens = service.registry.get("models", identity)
    if lens.get("kind") != "lens" or not lens.get("psf_factory_job_id"):
        raise ValueError("Native response publication needs a PSF Factory Lens model")
    from .native_model_evidence import verify_models

    verify_models(service, [lens])
    if not {"psf", "directional_response"} <= set(lens.get("payload", {})):
        raise ValueError("Factory Lens lacks its joint PSF and incident directions")
    return lens


def _readout_model(service, identity: str, sensor: dict, response: dict) -> dict:
    model = service.registry.get("models", identity)
    if model.get("kind") != "sensor" or model.get("method_and_version") == METHOD:
        raise ValueError("Readout source must be a separate existing Sensor model")
    asset = service.registry.get("assets", model["asset_id"])
    identity_pair = (sensor["component_id"], sensor["revision_id"])
    actual_pair = (asset.get("properties", {}).get("component_id"),
                   asset.get("properties", {}).get("revision_id"))
    if actual_pair != identity_pair:
        raise ValueError("Readout Sensor asset lacks the same declared component/revision ID")
    if model["payload"].get("pixel_pitch_um") != response["pixel_pitch_um"]:
        raise ValueError("Readout Sensor pixel pitch differs from native response")
    if model.get("raw_format", {}).get("cfa") != "RGGB":
        raise ValueError("Native response requires a matching RGGB readout source")
    from .native_model_evidence import verify_models

    verify_models(service, [model])
    return model


@contextmanager
def _publication_lock(service):
    with (Path(service.registry.directory) / ".native-response-publication.lock").open(
        "a+b"
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _model_data(asset_id: str, asset: dict, readout: dict, lens: dict,
                response: dict, name: str, revision: str) -> dict:
    props = copy.deepcopy(readout["properties"])
    props["accepted_angle_range"] = {
        "min": 0, "max": response["theta_deg"][-1], "unit": "deg"
    }
    props["native_response_bundle"] = {
        "manifest_sha256": asset["properties"]["native_response_bundle"]["manifest_sha256"],
        "readout_model_id": readout["id"],
        "readout_model_hash": readout["content_hash"],
        "qualified_lens_model_id": lens["id"],
        "qualified_lens_model_hash": lens["content_hash"],
        "readout_relationship": "same_declared_sensor_component_revision",
    }
    return ModelCreate.model_validate({
        "name": name,
        "revision": revision,
        "asset_id": asset_id,
        "kind": "sensor",
        "reference_plane": "sensor_surface",
        "included_effects": [
            "sensor_effective_qe", "sensor_angular_response",
            "mean_dark_charge", "readout_clipping",
        ],
        "valid_domain": copy.deepcopy(readout["valid_domain"]),
        "spectral_range": {
            "min": response["wavelength_nm"][0],
            "max": response["wavelength_nm"][-1],
            "unit": "nm",
        },
        "properties": props,
        "raw_format": copy.deepcopy(readout["raw_format"]),
        "input_raw_format": copy.deepcopy(readout["input_raw_format"]),
        "uncertainty": {"kind": "unknown"},
        "parameter_uncertainty": {},
        "calibration_dataset_ids": [],
        "payload": {
            "wavelength_nm": response["wavelength_nm"],
            "pixel_pitch_um": response["pixel_pitch_um"],
            "readout": copy.deepcopy(readout["payload"]["readout"]),
            "native_pixel_response": response,
        },
        "source_kind": "estimated",
        "source_uri": asset["source_uri"],
        "license": asset["license"],
        "method_and_version": METHOD,
        "physical_instance": None,
        "parents": [],
    }).model_dump()


def publish_native_response_bundle(
    service, bundle_dir: str | Path, *, expected_manifest_sha256: str,
    lens_model_id: str, readout_model_id: str, name: str, revision: str,
    license: str,
) -> dict[str, Any]:
    """Pin all original bytes and publish one Lens/readout-bound draft Sensor."""
    if not isinstance(expected_manifest_sha256, str) or not _SHA.fullmatch(
        expected_manifest_sha256
    ):
        raise ValueError("Expected manifest SHA-256 is required")
    from .service import environment_fingerprint

    if service.loaded_environment != environment_fingerprint():
        raise ValueError("Executable source changed after service startup")
    lens = _qualified_lens(service, lens_model_id)
    root = Path(bundle_dir).expanduser().resolve(strict=True)
    response, inspection = load_eligible_native_response_bundle(
        root, psf=lens["payload"]["psf"],
        directional_response=lens["payload"]["directional_response"],
    )
    if inspection["provenance"]["manifest_sha256"] != expected_manifest_sha256:
        raise ValueError("Native response manifest changed after review")
    readout = _readout_model(service, readout_model_id, inspection["sensor"], response)
    if response["schema"] != "camera_native_pixel_response_v1":
        raise ValueError("Generation-only response needs a separate charge collection model")
    manifest = json.loads((root / "manifest.json").read_bytes())
    sources = manifest["source_files"]
    core = [
        ("manifest.json", "manifest.json", expected_manifest_sha256,
         MAX_MANIFEST_BYTES),
        ("response.json", manifest["response"]["path"],
         inspection["response"]["sha256"], MAX_RESPONSE_BYTES),
        ("qualification.json", manifest["qualification"]["path"],
         inspection["provenance"]["qualification_sha256"], MAX_QUALIFICATION_BYTES),
    ]
    if len({entry[2] for entry in core}) != len(core):
        raise ValueError("Native response core artifacts must have distinct bytes")
    unique_source_sha: dict[str, int] = {}
    source_entries = []
    descriptors = list(core)
    for item in sources:
        checksum = item["sha256"]
        if checksum in {entry[2] for entry in core}:
            raise ValueError("Native response original source aliases a core artifact")
        if checksum not in unique_source_sha:
            unique_source_sha[checksum] = len(descriptors)
            descriptors.append((
                f"source-{len(descriptors) - 3:03d}", item["path"], checksum,
                MAX_UNCOMPRESSED_BYTES,
            ))
        source_entries.append({
            "path": item["path"], "role": item["role"], "sha256": checksum,
            "artifact_index": unique_source_sha[checksum],
        })
    total = 0
    originals = []
    for artifact_name, relative, checksum, limit in descriptors:
        content = _pinned_bytes(root, relative, checksum, limit)
        total += len(content)
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Native response bundle exceeds the total byte limit")
        originals.append((artifact_name, relative, content))
    seen_indices: set[int] = set()
    for item in source_entries:
        index = item["artifact_index"]
        if index in seen_indices:
            content = _pinned_bytes(
                root, item["path"], item["sha256"], MAX_UNCOMPRESSED_BYTES
            )
            total += len(content)
            if total > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Native response bundle exceeds the total byte limit")
        seen_indices.add(index)
    identity = "asset-native-response-" + digest({
        "manifest_sha256": expected_manifest_sha256,
        "component_id": inspection["sensor"]["component_id"],
        "revision_id": inspection["sensor"]["revision_id"],
    })
    source_uri = "native-response-bundle://sha256/" + expected_manifest_sha256
    asset_properties = {
        "native_response_bundle": {
            "schema": inspection["schema"],
            "sensor": inspection["sensor"],
            "manifest_sha256": expected_manifest_sha256,
            "response_sha256": inspection["response"]["sha256"],
            "qualification_sha256": inspection["provenance"]["qualification_sha256"],
            "source_files": source_entries,
            "inspection": inspection,
        }
    }
    asset_data = AssetCreate.model_validate({
        "name": name + " source bundle", "kind": "sensor",
        "revision": inspection["sensor"]["revision_id"],
        "description": _LIMITATION,
        "source_kind": inspection["response"]["source"]["kind"],
        "source_uri": source_uri, "license": license,
        "method_and_version": METHOD, "physical_instance": None, "parents": [],
        "properties": asset_properties,
    }).model_dump()
    with _publication_lock(service):
        artifacts = [
            service.registry.store_blob(content, artifact_name, "application/json"
                                        if artifact_name in {
                                            "manifest.json", "response.json",
                                            "qualification.json"
                                        } else "application/octet-stream")
            for artifact_name, _, content in originals
        ]
        complete_asset = {
            **asset_data, "approval_status": "draft",
            "native_response_source_artifacts": artifacts,
        }
        try:
            asset = service.registry.get("assets", identity)
            if {k: v for k, v in asset.items() if k not in {
                "id", "created_at", "content_hash"
            }} != complete_asset:
                raise ConflictError("Native response source asset identity changed")
        except KeyError:
            asset = service.registry.put("assets", complete_asset, identity=identity)
        for descriptor in artifacts:
            binding = service.registry.artifact_binding(
                descriptor["sha256"], owner_kind="assets", owner_id=asset["id"]
            )
            if binding["descriptor"] != descriptor:
                raise IntegrityError("Native source blob lost exact Sensor asset ownership")
        model_data = _model_data(asset["id"], asset, readout, lens,
                                 response, name, revision)
        service._validate_model_payload(model_data)
        matches = [
            model for model in service.registry.list("models")
            if model.get("asset_id") == asset["id"]
            and model.get("properties", {}).get("native_response_bundle", {}).get(
                "qualified_lens_model_id"
            ) == lens["id"]
        ]
        if len(matches) > 1:
            raise ConflictError("Native response has ambiguous Lens-bound draft revisions")
        if matches:
            model = matches[0]
            if (model.get("approval_status") != "draft"
                    or {k: model.get(k) for k in ModelCreate.model_fields} != model_data):
                raise ConflictError("Native response draft differs from its reviewed inputs")
            verify_native_source_model(service, model)
        else:
            model = service.create_model(model_data)
            verify_native_source_model(service, model)
    return {
        "schema": SCHEMA, "asset_id": asset["id"], "model_id": model["id"],
        "source": asset_properties["native_response_bundle"],
        "qualified_lens_model_id": lens["id"],
        "readout_model_id": readout["id"],
        "approval_status": "draft", "physical_validation": "not_established",
        "product_ranking_allowed": False, "commercial_release_allowed": False,
        "limitation": _LIMITATION,
    }


def verify_native_source_model(service, model: dict, selected_models=()) -> None:
    """Revalidate frozen sources, exact Lens/readout binding and source support."""
    if model.get("method_and_version") != METHOD:
        return
    if model.get("kind") != "sensor" or model.get("approval_status") != "draft":
        raise ValueError("Native source model must remain a draft Sensor")
    if service.registry.get("models", model["id"]) != model:
        raise ValueError("Native source model differs from its immutable record")
    asset = service.registry.get("assets", model["asset_id"])
    props = asset.get("properties", {}).get("native_response_bundle", {})
    binding = model.get("properties", {}).get("native_response_bundle", {})
    if (
        asset.get("kind") != "sensor" or asset.get("approval_status") != "draft"
        or asset.get("method_and_version") != METHOD
        or asset.get("source_uri") != model.get("source_uri")
        or asset.get("source_uri") !=
        "native-response-bundle://sha256/" + str(props.get("manifest_sha256"))
        or model.get("source_kind") != "estimated"
        or model.get("physical_instance") is not None
        or model.get("parents") != []
        or binding.get("manifest_sha256") != props.get("manifest_sha256")
        or binding.get("readout_relationship") !=
        "same_declared_sensor_component_revision"
    ):
        raise ValueError("Native source model lost its draft bundle identity")
    lens = _qualified_lens(service, binding["qualified_lens_model_id"])
    readout = _readout_model(
        service, binding["readout_model_id"], props["sensor"],
        model["payload"]["native_pixel_response"],
    )
    if (
        lens["content_hash"] != binding.get("qualified_lens_model_hash")
        or readout["content_hash"] != binding.get("readout_model_hash")
        or model["payload"]["readout"] != readout["payload"]["readout"]
        or model["raw_format"] != readout["raw_format"]
        or model["valid_domain"] != readout["valid_domain"]
    ):
        raise ValueError("Native source model lost its exact readout/Lens binding")
    selected_lenses = [m for m in selected_models if m.get("kind") == "lens"]
    if selected_lenses and (
        len(selected_lenses) != 1 or selected_lenses[0]["id"] != lens["id"]
    ):
        raise ValueError("Native source response was qualified for another Lens model")
    artifacts = asset.get("native_response_source_artifacts")
    sources = props.get("source_files")
    if not isinstance(artifacts, list) or not isinstance(sources, list):
        raise ValueError("Native source bundle lost its original artifact manifest")
    manifest = json.loads(service.registry.blob_path(artifacts[0]["sha256"]).read_bytes())
    core_paths = ["manifest.json", manifest["response"]["path"],
                  manifest["qualification"]["path"]]
    with tempfile.TemporaryDirectory(dir=service.registry.directory) as directory:
        root = Path(directory)
        paths = [(relative, artifacts[index])
                 for index, relative in enumerate(core_paths)]
        paths.extend((entry["path"], artifacts[entry["artifact_index"]])
                     for entry in sources)
        for relative, descriptor in paths:
            relative = _relative(relative)
            owner = service.registry.artifact_binding(
                descriptor["sha256"], owner_kind="assets", owner_id=asset["id"]
            )
            if owner["descriptor"] != descriptor:
                raise IntegrityError("Native response original source owner differs")
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(owner["path"], destination)
        inspection = inspect_native_response_bundle(
            root, psf=lens["payload"]["psf"],
            directional_response=lens["payload"]["directional_response"],
        )
    if inspection != props.get("inspection") or inspection["status"] != "research_eligible":
        raise ValueError("Native source bundle no longer passes its original support gate")
    source_response = validate_response(json.loads(
        service.registry.blob_path(artifacts[1]["sha256"]).read_bytes()
    ))
    if model["payload"]["native_pixel_response"] != source_response:
        raise ValueError("Native model response differs from its original source bytes")
    expected = _model_data(asset["id"], asset, readout, lens, source_response,
                           model["name"], model["revision"])
    if {key: model.get(key) for key in ModelCreate.model_fields} != expected:
        raise ValueError("Native Sensor declaration differs from exact source/readout binding")
