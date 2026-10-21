"""Read-only bridge from archived prescription camera runs to response_chain.

This preserves the supplied ideal-plane irradiance (absolute lens throughput
already included), sensor, field PSF and source identities. Loading an archive
does not register a model, grant applicability or certify physical accuracy.
"""

from __future__ import annotations

import hashlib
import io
import json
from copy import deepcopy
from pathlib import Path

import numpy as np

from .electrical import default_electrical_model
from .registry import canonical, digest
from .spatial import validate_psf

MAX_JSON_BYTES = 64 * 1024**2
MAX_BLOB_BYTES = 128 * 1024**2


class ArchivedPrescription:
    """Load only named, hash-verified files; never open a writable registry."""

    def __init__(self, record_path: Path, state_dir: Path, *, expected_sha256: str):
        self.path = Path(record_path).resolve()
        self.blobs = (Path(state_dir).resolve() / "blobs").resolve()
        if self.path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("Archived run exceeds the bounded JSON size")
        content = self.path.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_sha256:
            raise ValueError("Archived run differs from the retained evidence summary")
        self.record = json.loads(content)
        self.read_hashes: dict[Path, str] = {self.path: actual}
        if (
            self.record.get("plugin_id") != "spatial_scene_v1"
            or self.record.get("status") != "succeeded"
        ):
            raise ValueError("Require a completed archived spatial camera acquisition")
        self.models = {m["kind"]: m for m in self.record["model_snapshots"]}
        self.lens = self.models["lens"]
        self.sensor = self.models["sensor"]
        self.scene = self.record["scene_snapshot"]
        self.request = deepcopy(self.record["request"])
        self.psf = validate_psf(self.request["psf"])
        if self.psf["schema"] != "field_intensity_psf_v1" or not self.lens.get(
            "psf_factory_job_id"
        ):
            raise ValueError("Require a prescription-derived field PSF, not a declared pupil")
        payload = deepcopy(self.lens["payload"])
        if canonical(payload.pop("psf")) != canonical(self.psf):
            raise ValueError("Archived field PSF differs from the pinned Lens")
        if canonical(payload) != canonical(self.request["lens"]):
            raise ValueError("Archived lens response differs from the pinned Lens")
        for key in ("readout", "pixel_pitch_um", "wavelength_nm", "qe", "sensor_angular_response"):
            if key in self.request and canonical(self.request[key]) != canonical(
                self.sensor["payload"][key]
            ):
                raise ValueError("Archived sensor response differs: " + key)
        if self.scene.get("projection_lens_model_id") != self.lens["id"]:
            raise ValueError("Ideal irradiance projection belongs to another Lens")
        image = self.array("ideal_spectral_irradiance.npy")
        # The scene's first spectral-image is its published image. Later entries
        # include immutable parent closure (e.g. the clear-stop projection).
        # This is the same selection rule used by imaging._scene_input.
        projected = next(a for a in self.scene["artifacts"] if a["name"] == "spectral-image.npy")
        scene_image = np.load(io.BytesIO(self.content(projected)), allow_pickle=False)
        if not np.array_equal(image, scene_image):
            raise ValueError("Original ideal irradiance differs from its projected input")
        if (
            list(image.shape) != self.scene["shape"]
            or not np.isfinite(image).all()
            or np.any(image < 0)
        ):
            raise ValueError("Original scene array shape or values differ")
        self.request["scene"]["spectral_irradiance"] = image.copy()
        if not np.array_equal(self.array("psf_intensity.npy"), np.asarray(self.psf["values"])):
            raise ValueError("Archived PSF array differs from its model samples")
        self.electrical_model = default_electrical_model(self.request["readout"])
        self.factory_inputs = self.json_artifacts("factory-input.json")
        self.characterizations = self.json_artifacts("characterization.json")
        from .lens_factory import validate_request
        from .prescription_identity import sampling_independent_binding
        from .prescription_psf import validate_request as validate_psf_request

        radiometry = self.request["lens"]["radiometry"]
        ray_inputs = [
            f
            for f in self.factory_inputs
            if f.get("plugin_id") == "rayoptics_lens_v1"
            and digest(validate_request(f["request"])) == radiometry["prescription_sha256"]
        ]
        if len(ray_inputs) != 1:
            raise ValueError("Cannot identify one original radiometry prescription input")
        self.prescription = validate_request(ray_inputs[0]["request"])
        psf_inputs = []
        for candidate_input in self.factory_inputs:
            if candidate_input.get("plugin_id") != "prescription_psf_v1":
                continue
            try:
                candidate_request = validate_psf_request(candidate_input["request"])
                binding = sampling_independent_binding(
                    self.prescription, candidate_request["prescription"]
                )
            except (KeyError, ValueError):
                continue
            psf_inputs.append((candidate_input, candidate_request, binding))
        if len(psf_inputs) != 1:
            raise ValueError("Cannot identify matching ray/PSF prescription inputs")
        psf_input, psf_request, self.prescription_binding = psf_inputs[0]
        if (
            self.lens["properties"].get("physical_prescription_binding")
            != self.prescription_binding
        ):
            raise ValueError(
                "Archived Lens physical-prescription binding differs from Factory inputs"
            )
        self.psf_request = psf_input["request"]
        if ray_inputs[0]["asset_id"] != psf_input["asset_id"]:
            raise ValueError("Ray and PSF Factory inputs use different design assets")
        if canonical(self.prescription["stop_transmission"]) != canonical(
            radiometry["stop_transmission"]
        ):
            raise ValueError("Radiometry stop differs from the actual Factory prescription")
        self.design_asset_id = ray_inputs[0]["asset_id"]
        self._verify_optical_response(psf_request)

    def _verify_optical_response(self, psf_request: dict) -> None:
        """Bind every reused optical response to the retained Factory outputs.

        A matching prescription input alone does not prove that the archived
        throughput, relative illumination, angle weights or PSF came from it.
        These are original, hash-checked characterization blobs, not a new ray
        trace or a claim of optical accuracy.
        """
        from .finite_conjugate import application_conditions
        from .model_conditions import fixed_factory_conditions

        def characterization(request, label):
            matching = [
                item
                for item in self.characterizations
                if item.get("summary", {}).get("normalized_request") == request
            ]
            if len(matching) != 1:
                raise ValueError(f"Require one preserved {label} Factory characterization")
            item = matching[0]
            if (
                item["summary"].get("request_sha256") != digest(request)
                or item["summary"].get("status") != "PROPOSED"
            ):
                raise ValueError(f"Preserved {label} Factory outcome differs from its input")
            return item

        ray = characterization(self.prescription, "radiometry")
        psf = characterization(psf_request, "PSF")
        lens_response = {key: value for key, value in self.lens["payload"].items() if key != "psf"}
        if canonical(ray.get("model_candidate")) != canonical(lens_response):
            raise ValueError(
                "Archived throughput, RI or angle response differs from Factory output"
            )
        if canonical(psf.get("model_candidate")) != canonical(self.lens["payload"]["psf"]):
            raise ValueError("Archived field PSF differs from Factory output")
        radiometry = lens_response["radiometry"]
        if (
            ray["summary"].get("irradiance_per_radiance_sr")
            != radiometry["irradiance_per_radiance_sr"]
        ):
            raise ValueError("Archived absolute throughput differs from Factory summary")
        if psf["summary"].get("prescription_sha256") != digest(psf_request["prescription"]):
            raise ValueError("Archived PSF summary differs from its original prescription")
        pupil = {"value": psf["summary"].get("geometric_reference_f_number"), "unit": "1"}
        assembly = self.record["assembly_snapshot"]
        conditions = assembly["conditions"]
        if (
            self.record["assembly_id"] != assembly["id"]
            or self.lens["id"] not in assembly["model_ids"]
            or self.lens["properties"].get("f_number") != pupil
            or self.lens["properties"].get("psf_pupil_f_number") != pupil
            or conditions.get("f_number") != pupil
        ):
            raise ValueError("Archived aperture differs from the PSF pupil or camera condition")
        if self.lens["properties"].get("focal_length") != {
            "value": psf_request["reference_focal_length_mm"],
            "unit": "mm",
        }:
            raise ValueError("Archived focal reference differs from the PSF Factory input")
        if self.lens["properties"].get("psf_object_conjugate") != psf_request["object_conjugate"]:
            raise ValueError("Archived PSF object conjugate differs from Factory input")
        fixed = fixed_factory_conditions(self.lens)
        if not fixed or any(conditions.get(key) != value for key, value in fixed.items()):
            raise ValueError("Archived camera conditions differ from the fixed optical response")
        if self.prescription["schema"] == "lens_prescription_v5":
            conjugate = self.prescription["object_conjugate"]
            if (
                radiometry.get("object_conjugate") != conjugate
                or self.lens["properties"].get("object_conjugate") != conjugate
                or any(
                    conditions.get(key) != value
                    for key, value in application_conditions(conjugate).items()
                )
            ):
                raise ValueError(
                    "Archived finite optical response has a different object conjugate"
                )

    def artifacts(self, name: str) -> list[dict]:
        entries = {}
        for item in self.record["artifacts"] + self.record.get("input_artifacts", []):
            if item["name"] == name:
                entries[item["sha256"]] = item
        if not entries:
            raise ValueError("Archived evidence lacks " + name)
        return list(entries.values())

    def content(self, artifact: dict) -> bytes:
        checksum = artifact["sha256"]
        if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError("Malformed blob identity")
        path = self.blobs / checksum
        if path.is_symlink() or path.stat().st_size > MAX_BLOB_BYTES:
            raise ValueError("Require a bounded original blob, not a link")
        value = path.read_bytes()
        if hashlib.sha256(value).hexdigest() != checksum or len(value) != artifact["bytes"]:
            raise ValueError("Original blob checksum/length differs: " + artifact["name"])
        self.read_hashes[path] = checksum
        return value

    def array(self, name: str) -> np.ndarray:
        entries = self.artifacts(name)
        if len(entries) != 1:
            raise ValueError("Ambiguous archived array: " + name)
        array = np.load(io.BytesIO(self.content(entries[0])), allow_pickle=False)
        if not isinstance(array, np.ndarray) or array.dtype.hasobject:
            raise ValueError("Require an original numeric NPY")
        return array

    def json_artifacts(self, name: str) -> list[dict]:
        return [json.loads(self.content(a)) for a in self.artifacts(name)]

    def rate_request(self) -> dict:
        return {
            k: deepcopy(v)
            for k, v in self.request.items()
            if k not in {"exposure_s", "frames", "isp_profile"}
        }

    def identity(self) -> dict:
        render = self.scene["source_render_snapshot"]
        return {
            "run_id": self.record["id"],
            "archive_sha256": self.read_hashes[self.path],
            "lens_model_id": self.lens["id"],
            "lens_response_sha256": digest(self.lens["payload"]),
            "psf_factory_job_id": self.lens["psf_factory_job_id"],
            "sensor_model_id": self.sensor["id"],
            "sensor_payload_sha256": digest(self.sensor["payload"]),
            "scene_render_id": render["id"],
            "scene_render_content_hash": render.get("content_hash"),
            "projection_lens_model_id": self.scene["projection_lens_model_id"],
            "sampling": self.scene["sampling"],
            "spectral_axis_nm": self.request["wavelength_nm"],
            "physical_instance": self.lens.get("physical_instance"),
            "design_asset_id": self.design_asset_id,
            "prescription_sha256": digest(self.prescription),
            "source_uri": self.lens.get("source_uri"),
            "physical_validation": "not_established",
        }

    def assert_unchanged(self) -> None:
        for path, checksum in self.read_hashes.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise ValueError("Read-only evidence changed during comparison: " + str(path))


def require_same_design_scene(cases: list[ArchivedPrescription]) -> None:
    """Changing only stop transmission must not silently replace geometry or scene."""
    if len(cases) < 2:
        raise ValueError("Require at least two optical conditions")
    first = cases[0]
    geometry = {k: v for k, v in first.prescription.items() if k != "stop_transmission"}
    for case in cases[1:]:
        if case.design_asset_id != first.design_asset_id or canonical(
            {k: v for k, v in case.prescription.items() if k != "stop_transmission"}
        ) != canonical(geometry):
            raise ValueError("Stop comparison must retain the same prescription geometry")
        for key in (
            "sensor_payload_sha256",
            "scene_render_id",
            "scene_render_content_hash",
            "sampling",
        ):
            if case.identity()[key] != first.identity()[key]:
                raise ValueError("Stop comparison changed source identity: " + key)
        # Source irradiance differs by design, but the archived on-axis radiance
        # recovered from each projection must be the same input field.
        a = np.asarray(first.request["lens"]["radiometry"]["irradiance_per_radiance_sr"])[:, 0]
        b = np.asarray(case.request["lens"]["radiometry"]["irradiance_per_radiance_sr"])[:, 0]
        if not np.allclose(
            first.request["scene"]["spectral_irradiance"] / a,
            case.request["scene"]["spectral_irradiance"] / b,
            rtol=2e-12,
            atol=0,
        ):
            raise ValueError("Archived projections are not from the same supplied radiance")
