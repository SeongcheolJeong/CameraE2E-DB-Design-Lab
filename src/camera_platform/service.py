"""Application use cases shared by HTTP, CLI, and the Python SDK."""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import io
import math
import platform
import sqlite3
from pathlib import Path

import numpy as np

from . import SCHEMA_VERSION, __version__
from .composer import check_assembly
from .contracts import (
    PROPERTY_DICTIONARY,
    AssemblyCreate,
    AssetCreate,
    ExperimentCreate,
    MeasurementCreate,
    ModelCreate,
    ReleaseCreate,
    RunCreate,
    TestCreate,
    ValidationCreate,
)
from .registry import ConflictError, SQLiteRegistry, canonical, digest
from .runtime import get_plugin, list_plugins, run_plugin, sensor_response_fields
from .validation import compare

NON_CAMERA_JOBS = {
    "camera_validation_esfr",
    "sensor_structure_study",
    "lens_psf_lut_camera",
    "lens_optical_psf_lut",
    "characterization", "isp_replay", "surrogate_training", "retained_isp_comparison",
    "retained_isp_joint_study", "retained_isp_observable_selection",
    "retained_isp_observable_holdout", "retained_isp_prospective_selection",
    "retained_isp_prospective_holdout",
    "spectral_scene_render", "spectral_scene_projection", "isp_study_cell", "readout_fit",
    "electrical_response_study", "raw_temporal_replay", "design_experiment",
    "r1_timed_acquisition",
    "design_isp_replay", "db_comparison", "catalog_quality_comparison",
    "physics_lut_sensitivity", "saved_sensor_quality", "saved_sensor_search",
    "lens_sensor_raw", "lens_sensor_streamed_raw", "lens_sensor_out_of_core_raw",
    "lens_sensor_registered_roi_disk_raw",
    "lens_sensor_frequency_target",
    "lens_geometric_pupil_density",
    "lens_dense_geometric_psf_candidate",
    "lens_dense_geometric_psf_candidate_v2",
    "lens_dense_geometric_psf_q4_rebin",
    "lens_sensor_dense_psf_sensitivity",
    "lens_sensor_q4_psf_sensitivity",
    "road_design_study",
    "road_hdr_study",
    "virtual_lab", "design_workshop", "reference_workshop", "design_asset_update",
    "calibration_lab", "calibration_lab_validation", "calibration_db_replay", "calibration_db_color_replay",
    "calibration_db_core_workflow",
    "calibration_unified_workflow",
    "pedestrian_braking_study",
    "lens_sensor_joint_scene_dense_psf_ladder",
    "lens_sensor_hdr", "lens_sensor_lfm",
    "lens_sensor_isp_replay", "lens_sensor_calibrated_isp", "lens_sensor_perception",
    "lens_sensor_raw_cohort", "lens_sensor_quality_study", "lens_sensor_isp_perception",
    "lens_sensor_isp_perception_cohort",
    "lens_sensor_pwm_hdr",
    "lens_sensor_adaptive_demosaic", "lens_sensor_legacy_isp",
    "lens_sensor_isp_detector",
    "saved_sensor_hdr", "saved_sensor_lfm", "retained_raw_dataset",
    "saved_sensor_isp_replay", "saved_sensor_calibrated_isp", "saved_sensor_perception",
}


class PreflightError(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__("Execution refused: compatibility or runtime contract is not satisfied")


def environment_fingerprint():
    paths = sorted(Path(__file__).parent.glob("*.py"))
    engine = Path(__file__).parent.parent / "pyisetcam"
    # Include all engine source files, including local uncommitted implementations.
    paths += sorted(engine.glob("*.py"))
    hashes = {
        str(p.relative_to(Path(__file__).parent.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
        if p.is_file()
    }
    versions = {}
    for name in (
        "numpy",
        "scipy",
        "scikit-image",
        "pillow",
        "pyisetcam",
        "rayoptics",
        "opticalglass",
        "transforms3d",
        "prysm",
        "setuptools",
    ):
        if name == "pyisetcam":
            # The source checkout and an older installed distribution can both
            # be on sys.path. Fingerprint the loaded engine, not whichever
            # dist-info importlib.metadata discovers first in this process.
            import pyisetcam

            versions[name] = pyisetcam.__version__
            continue
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "source-checkout"
    return {
        "platform_version": __version__,
        "python": platform.python_version(),
        "system": platform.platform(),
        "packages": versions,
        "source_hashes": hashes,
    }


class PlatformService:
    model_contract = ModelCreate

    def __init__(self, state_dir, workspace=None):
        self.registry = SQLiteRegistry(state_dir)
        self.workspace = Path(workspace or Path(__file__).resolve().parents[2]).resolve()
        self.loaded_environment = environment_fingerprint()

    @staticmethod
    def _data(contract, payload):
        return contract.model_validate(payload).model_dump()

    def create_asset(self, payload):
        data = self._data(AssetCreate, payload)
        return self.registry.put("assets", {**data, "approval_status": "draft"})

    def create_model(self, payload):
        data = self._data(ModelCreate, payload)
        if data["factory_condition_scope"] != "all_declared" and not data["psf_factory_job_id"]:
            raise ValueError("Optical condition scope requires a verified PSF Factory model")
        if data["psf_factory_job_id"]:
            from .factory import verify_psf_model_link

            verify_psf_model_link(self, data)
        elif data["factory_job_id"]:
            from .factory import verify_model_link

            verify_model_link(self, data)
        elif (
            data["source_uri"].startswith("factory://")
            or data["method_and_version"].startswith("rayoptics_lens_v1:")
            or data["method_and_version"].startswith("prysm_psf_v1:")
            or data["method_and_version"].startswith("prescription_psf_v1:")
            or data["factory_application_conditions"] is not None
            or "radiometry" in data["payload"]
            or "directional_response" in data["payload"]
        ):
            raise ValueError("Factory provenance requires its registered factory_job_id")
        asset = self.registry.get("assets", data["asset_id"])
        if asset["kind"] != data["kind"]:
            raise ValueError("Model kind differs from its parent asset")
        for parent in data["parents"]:
            # Model revision lineage points to immutable model revisions.
            ancestor = self.registry.get("models", parent)
            if ancestor["asset_id"] != data["asset_id"] or ancestor["kind"] != data["kind"]:
                raise ValueError("A model revision must retain its parent asset and model role")
            data["calibration_dataset_ids"] = sorted(
                set(data["calibration_dataset_ids"]) | set(ancestor["calibration_dataset_ids"])
            )
        from .observations import capture_content_ids

        measurements = self.registry.list("measurements")
        registered_dataset_ids = {
            i
            for m in measurements
            for i in m["dataset_ids"]
            if not i.startswith("raw-frame-sha256:")
        }
        for measurement in measurements:
            if measurement.get("raw_capture_id"):
                registered_dataset_ids.update(
                    capture_content_ids(
                        self.registry.get("captures", measurement["raw_capture_id"])
                    )
                )
        for measurement_id in data["calibration_dataset_ids"]:
            if measurement_id not in registered_dataset_ids:
                raise ValueError(f"Calibration dataset is not registered: {measurement_id}")
        self._validate_model_payload(data)
        from .gain_semantics import validate_model_declaration

        validate_model_declaration(data)
        if data["kind"] == "isp" and data["payload"].get("schema") == "camera_isp_profile_v2":
            from .isp_calibration import get_calibration

            if data["payload"]["calibration_id"] is not None:
                get_calibration(self, data["payload"]["calibration_id"])
        if data["kind"] == "isp" and data["payload"].get("schema") == "camera_isp_profile_v3":
            from .isp_charge_calibration import get

            if data["payload"]["calibration_id"] is not None:
                get(self, data["payload"]["calibration_id"])
        from .uncertainty import validate_record

        validate_record(data)
        from .native_model_evidence import store_responses

        response_artifacts = store_responses(self, data)
        return self.registry.put(
            "models",
            {
                **data,
                **({"response_artifacts": response_artifacts} if response_artifacts else {}),
                "approval_status": "draft",
                "factory": {
                    "method": data["method_and_version"].split(":", 1)[0]
                    if data["psf_factory_job_id"]
                    else "rayoptics_lens_v1"
                    if data["factory_job_id"]
                    else "response_table_import_v1",
                    "physical_validation": "not_established",
                    "schema_check": "passed",
                    "asset_hash": asset["content_hash"],
                },
            },
        )

    @staticmethod
    def _validate_model_payload(data):
        p, kind = data["payload"], data["kind"]
        generation_sensor = (
            kind == "sensor"
            and isinstance(p.get("native_pixel_response"), dict)
            and p["native_pixel_response"].get("schema")
            == "camera_native_generation_response_v1"
        )
        expected_effects = {
            "lens": {"lens_relative_illumination", "lens_incident_angle_distribution"},
            "sensor": {
                "sensor_effective_qe",
                "sensor_angular_response",
                "mean_dark_charge",
                "readout_clipping",
            },
            "isp": {"software_isp"},
        }[kind]
        if generation_sensor:
            expected_effects = (expected_effects - {"sensor_effective_qe"}) | {
                "sensor_charge_generation", "sensor_charge_collection"
            }
        if kind == "lens" and "psf" in p:
            expected_effects |= {"lens_psf", "pixel_aperture"}
        if set(data["included_effects"]) != expected_effects or len(
            data["included_effects"]
        ) != len(expected_effects):
            raise ValueError(
                f"{kind} v1 requires its actual implemented effects: {sorted(expected_effects)}"
            )
        required = {
            "lens": {"field_radius", "theta_deg", "angular_weights", "relative_illumination"},
            "sensor": {
                "wavelength_nm",
                "qe",
                "pixel_pitch_um",
                "sensor_angular_response",
                "readout",
            },
            "isp": {"implementation", "mode", "awb", "lens_shading_correction"},
        }[kind]
        if kind == "isp" and p.get("schema") == "camera_retained_code_isp_profile_v1":
            from .retained_isp import validate_profile

            validate_profile(p)
            return
        if kind == "isp" and p.get("schema") == "camera_isp_profile_v3":
            from .isp_v3 import validate_profile

            validate_profile(p)
            return
        if kind == "isp" and p.get("schema") == "camera_isp_profile_v2":
            from .isp_v2 import validate_profile

            validate_profile(p)
            return
        if kind == "isp" and p.get("schema") == "camera_isp_profile_v1":
            from .isp import validate_profile

            validate_profile(p)
            return
        if kind == "lens" and "wavelength_nm" in p:
            required = required | {"wavelength_nm"}
        if kind == "lens" and "radiometry" in p:
            required |= {"radiometry", "field_reference_radius_mm", "wavelength_nm"}
        if kind == "lens" and "directional_response" in p:
            required |= {"directional_response", "radiometry", "field_reference_radius_mm",
                         "wavelength_nm"}
        native_sensor = kind == "sensor" and "native_pixel_response" in p
        if native_sensor:
            required = {"wavelength_nm", "pixel_pitch_um", "readout", "native_pixel_response"}
            if generation_sensor or "charge_collection" in p:
                required.add("charge_collection")
        if kind == "lens" and "field_reference_radius_mm" in p:
            radius = p["field_reference_radius_mm"]
            if type(radius) not in (int, float) or not 1e-12 <= radius <= 1000000:
                raise ValueError("Lens field_reference_radius_mm must be positive finite mm")
            required |= {"field_reference_radius_mm"}
        if kind == "lens" and "psf" in p:
            required |= {"psf"}
            from .spatial import validate_psf

            validate_psf(p["psf"])
        if set(p) != required:
            raise ValueError(
                f"{kind} response-table v1 payload requires exactly {sorted(required)}"
            )
        if kind == "isp":
            if p != {
                "implementation": "pyisetcam",
                "mode": "fixed",
                "awb": "fixed",
                "lens_shading_correction": "bypass",
            }:
                raise ValueError(
                    "Only the declared fixed pyisetcam ISP is supported by this adapter"
                )
            return
        if native_sensor:
            PlatformService._validate_native_sensor(data)
            return

        def array(key, dims):
            a = np.asarray(p[key], dtype=float)
            if a.ndim != dims or not np.isfinite(a).all():
                raise ValueError(f"Invalid {kind}.{key} shape or nonfinite data")
            return a

        if kind == "lens":
            fields, theta = array("field_radius", 1), array("theta_deg", 1)
            spectral = "wavelength_nm" in p
            wave = array("wavelength_nm", 1) if spectral else None
            if spectral and not (
                2 <= len(wave) <= 2048
                and np.all(np.diff(wave) > 0)
                and wave[0] >= 200
                and wave[-1] <= 2500
                and data["spectral_range"] == {"min": wave[0], "max": wave[-1], "unit": "nm"}
            ):
                raise ValueError(
                    "Spectral lens requires ordered nm samples and exact declared coverage"
                )
            weights = array("angular_weights", 3 if spectral else 2)
            illumination = array("relative_illumination", 2 if spectral else 1)
            if not (
                2 <= len(fields) <= 256
                and fields[0] == 0
                and fields[-1] == 1
                and np.all(np.diff(fields) > 0)
            ):
                raise ValueError("Lens field_radius must increase from 0 to 1")
            if not (
                2 <= len(theta) <= 181
                and theta[0] >= 0
                and theta[-1] <= 89.9
                and np.all(np.diff(theta) > 0)
            ):
                raise ValueError("Lens angles require ordered degrees in [0,90)")
            if (
                weights.shape
                != ((len(wave), len(fields), len(theta)) if spectral else (len(fields), len(theta)))
                or (weights < 0).any()
                or not np.allclose(weights.sum(axis=-1), 1, rtol=0, atol=1e-8)
            ):
                raise ValueError(
                    "Angular power weights must be nonnegative, field-by-angle, and sum to one"
                )
            if (
                illumination.shape != ((len(wave), len(fields)) if spectral else fields.shape)
                or (illumination < 0).any()
                or not np.all(illumination[..., 0] == 1)
            ):
                raise ValueError(
                    "Relative illumination must match fields and normalize to 1 on axis"
                )
            if "radiometry" in p:
                response = p["radiometry"]
                keys = {
                    "schema", "wavelength_nm", "image_height_mm", "irradiance_per_radiance_sr",
                    "prescription_sha256", "stop_transmission_sha256", "stop_transmission",
                }
                finite = (isinstance(response, dict)
                          and response.get("schema") == "camera_lens_radiometry_v2")
                if finite:
                    from .finite_conjugate import validate_conjugate

                    keys.add("object_conjugate")
                    conjugate = validate_conjugate(response.get("object_conjugate"))
                    if data["properties"].get("object_conjugate") != conjugate:
                        raise ValueError("Finite Lens properties must retain its conjugate")
                if not isinstance(response, dict) or set(response) != keys:
                    raise ValueError("Absolute lens radiometry requires its exact contract")
                heights = np.asarray(response["image_height_mm"], dtype=float)
                absolute = np.asarray(response["irradiance_per_radiance_sr"], dtype=float)
                if (
                    response["schema"] not in {
                        "camera_lens_radiometry_v1", "camera_lens_radiometry_v2"
                    }
                    or response["wavelength_nm"] != p["wavelength_nm"]
                    or heights.shape != fields.shape
                    or not np.isfinite(heights).all()
                    or heights[0] != 0 or not np.all(np.diff(heights) > 0)
                    or heights[-1] != p["field_reference_radius_mm"]
                    or not np.allclose(heights / heights[-1], fields, rtol=0, atol=1e-12)
                    or absolute.shape != illumination.shape
                    or not np.isfinite(absolute).all()
                    or np.any(absolute <= 0) or np.any(absolute > np.pi)
                    or not np.allclose(
                        absolute / absolute[:, :1], illumination, rtol=1e-12, atol=1e-14
                    )
                    or response["stop_transmission_sha256"] != digest(response["stop_transmission"])
                ):
                    raise ValueError(
                        "Absolute radiometry coordinates, power or normalization disagree"
                    )
            if "directional_response" in p:
                from .lens_direction_response import validate_directional_response

                directional = validate_directional_response(p["directional_response"])
                radiometry = p["radiometry"]
                if (
                    directional["wavelength_nm"] != p["wavelength_nm"]
                    or directional["field_radius"] != p["field_radius"]
                    or directional["image_height_mm"] != radiometry["image_height_mm"]
                    or directional["field_reference_radius_mm"] != p["field_reference_radius_mm"]
                    or directional["prescription_sha256"] != radiometry["prescription_sha256"]
                    or directional["stop_transmission_sha256"]
                    != radiometry["stop_transmission_sha256"]
                    or directional.get("object_conjugate") != radiometry.get("object_conjugate")
                ):
                    raise ValueError(
                        "Joint directions must preserve exact radiometry axes and source"
                    )
                p["directional_response"] = directional
        else:
            wave, qe = array("wavelength_nm", 1), array("qe", 2)
            if not (
                2 <= len(wave) <= 2048
                and np.all(np.diff(wave) > 0)
                and 200 <= wave[0] < wave[-1] <= 2500
            ):
                raise ValueError("Sensor wavelength axis must be ordered in nm")
            if qe.shape != (3, len(wave)) or ((qe < 0) | (qe > 1)).any():
                raise ValueError("QE must be [R,G,B][wavelength] in [0,1]")
            angular = p["sensor_angular_response"]
            theta = np.asarray(angular["theta_deg"], dtype=float)
            response = np.asarray(angular["relative_response"], dtype=float)
            if (
                theta.ndim != 1
                or len(theta) < 2
                or len(theta) > 181
                or not np.isfinite(theta).all()
                or not np.all(np.diff(theta) > 0)
                or theta[0] != 0
                or theta[-1] > 89.9
            ):
                raise ValueError("Sensor angles must start at 0 and increase below 90 degrees")
            if (
                response.shape != (3, len(wave), len(theta))
                or not np.isfinite(response).all()
                or (response < 0).any()
                or not np.allclose(response[:, :, 0], 1)
            ):
                raise ValueError(
                    "Angular response is [channel,wavelength,angle], "
                    "relative to normal-incidence QE"
                )
            if (response * qe[:, :, None] > 1 + 1e-12).any():
                raise ValueError(
                    "Collected-electron QE cannot exceed one in this non-avalanche runtime"
                )
            probe = {
                "wavelength_nm": p["wavelength_nm"],
                "qe": p["qe"],
                "pixel_pitch_um": p["pixel_pitch_um"],
                "readout": p["readout"],
                "exposure_s": 0.004,
                "spectral_irradiance_w_m2_nm": [0.0] * len(wave),
            }
            issues = get_plugin("quick_estimate").validate_request(probe)
            if issues:
                raise PreflightError({"status": "FAIL", "issues": issues})

    @staticmethod
    def _validate_native_sensor(data):
        from .native_pixel_response import validate_response

        p = data["payload"]
        response = validate_response(p["native_pixel_response"])
        generation = response["schema"] == "camera_native_generation_response_v1"
        if generation or "charge_collection" in p:
            from .charge_collection import validate_collection, validate_pair

            validate_pair(response, p.get("charge_collection"))
            p["charge_collection"] = validate_collection(p["charge_collection"])
        wave, pitch = response["wavelength_nm"], response["pixel_pitch_um"]
        if (p["wavelength_nm"] != wave or type(p["pixel_pitch_um"]) not in (int, float)
                or p["pixel_pitch_um"] != pitch):
            raise ValueError("Native Sensor wavelength and pitch must match the absolute response")
        spectral = data["spectral_range"]
        accepted = data["properties"].get("accepted_angle_range")
        if (spectral != {"min": wave[0], "max": wave[-1], "unit": "nm"}
                or not isinstance(accepted, dict)
                or any(type(accepted.get(k)) not in (int, float) for k in ("min", "max"))
                or accepted != {"min": 0, "max": response["theta_deg"][-1], "unit": "deg"}
                or data["reference_plane"] != "sensor_surface"):
            raise ValueError(
                "Native Sensor spectral, angular and sensor_surface declarations must match"
            )
        raw, readout = data["raw_format"], p["readout"]
        if (not isinstance(readout, dict)
                or raw.get("cfa") != "RGGB" or raw.get("layout") != "bayer"
                or raw.get("packing") != "unpacked" or raw.get("hdr_streams") != 1
                or type(raw.get("hdr_streams")) is not int
                or type(raw.get("bit_depth")) is not int
                or type(raw.get("black_level")) not in (int, float)
                or raw.get("bit_depth") != readout.get("bit_depth")
                or raw.get("black_level") != readout.get("black_level_dn")):
            raise ValueError("Native Sensor RAW declaration must match the Bayer readout")
        p.update(wavelength_nm=wave, pixel_pitch_um=pitch, native_pixel_response=response)
        PlatformService._native_capture_geometry(data, [2, 2])
        probe = {"wavelength_nm": wave, "pixel_pitch_um": pitch, "readout": readout,
                 "native_pixel_response": response, "exposure_s": .004,
                 "spectral_irradiance_w_m2_nm": [0.] * len(wave)}
        if generation:
            from .runtime import _common

            # Registration checks the exact optical/readout input contract, not
            # a spatially collapsed estimate that cannot represent transport.
            _common({**probe, "charge_collection": p["charge_collection"]})
            return
        issues = get_plugin("quick_estimate").validate_request(probe)
        if issues:
            raise PreflightError({"status": "FAIL", "issues": issues})

    @staticmethod
    def _native_capture_geometry(sensor, output_size):
        properties, pitch = sensor["properties"], sensor["payload"]["pixel_pitch_um"]
        full = []
        for name in ("active_height", "active_width"):
            quantity = properties.get(name)
            if (not isinstance(quantity, dict) or set(quantity) != {"value", "unit"}
                    or quantity["unit"] != "mm" or type(quantity["value"]) not in (int, float)
                    or not 0 < quantity["value"] <= 1e9 or not math.isfinite(quantity["value"])):
                raise ValueError("Native Sensor requires positive finite active dimensions in mm")
            pixels = quantity["value"] * 1000 / pitch
            if not math.isfinite(pixels) or pixels < 2 or abs(pixels-round(pixels)) > 1e-6:
                raise ValueError("Native dimensions and pitch must define an integer sensor raster")
            full.append(round(pixels))
        if (not isinstance(output_size, (list, tuple)) or len(output_size) != 2
                or any(type(v) is not int or v < 1 or v > side
                       for v, side in zip(output_size, full, strict=True))):
            raise ValueError("Native output_size must fit the declared full sensor")
        origin = [(side-size)//2 for side, size in zip(full, output_size, strict=True)]
        return {"full_sensor_shape": full, "crop_origin": origin}

    def create_assembly(self, payload):
        data = self._data(AssemblyCreate, payload)
        models = [self.registry.get("models", identity) for identity in data["model_ids"]]
        if sorted(m["kind"] for m in models) != ["isp", "lens", "sensor"]:
            raise ValueError("An assembly pins one lens, one sensor, and one ISP model")
        return self.registry.put(
            "assemblies",
            {
                **data,
                "model_hashes": {m["id"]: m["content_hash"] for m in models},
                "compatibility": check_assembly(data, models),
            },
        )

    def create_test(self, payload):
        data = self._data(TestCreate, payload)
        from .uncertainty import validate_record

        validate_record(data, test=True)
        if data["parent_test_id"] is not None:
            self.registry.get("tests", data["parent_test_id"])
        fields = [
            data["observation_method"],
            data["observation_specification"],
            data["observation_shape"],
        ]
        if any(value is not None for value in fields) and not all(
            value is not None for value in fields
        ):
            raise ValueError(
                "An observation test must freeze the method, specification and raster shape"
            )
        if (data["observation_method"] or {}).get("id") == "control_response_observation_v1":
            from .control_response_service import validate_registered_test

            validate_registered_test(self, data)
        elif ((data["observation_method"] or {}).get("id")
              == "camera_temporal_signal_observation_v1"):
            from .temporal_signal_reference import validate_registered_test

            validate_registered_test(self, data)
        elif data["observation_method"] is not None:
            from .measurement import observation_method, observation_sampling_weights

            observation_sampling_weights(
                data["observation_specification"], data["observation_shape"], [0.0, 1.0]
            )
            expected_method = observation_method(
                data["observation_specification"],
                data["observation_shape"],
                dark_present=data["observation_method"].get("dark_correction") == "mean_dark_frame",
            )
            if data["observation_method"] != expected_method:
                raise ValueError("Frozen observation method does not match its specification")
            if data["observation_specification"].get("conditions") != data["conditions"]:
                raise ValueError("Observation conditions must match the frozen test")
            if (
                data["metric_id"] != "raw.channel_ratio"
                or data["unit"] != "1"
                or data["reference_plane"] != "sensor_raw"
                or set(data["observation_specification"].get("included_effects", []))
                != set(data["included_effects"])
            ):
                raise ValueError("Observation test must retain the RAW channel-ratio contract")
        if data["metric_id"] not in {p["id"] for p in PROPERTY_DICTIONARY}:
            raise ValueError("Register a metric contract before creating its test")
        tolerance = data["absolute_tolerance"]
        if (
            not isinstance(tolerance.get("value"), (int, float))
            or isinstance(tolerance["value"], bool)
            or tolerance["value"] < 0
            or tolerance.get("unit") != data["unit"]
        ):
            raise ValueError("Acceptance tolerance requires a nonnegative value in the metric unit")
        return self.registry.put("tests", data)

    def create_measurement(self, payload, *, original_bytes=None):
        data = self._data(MeasurementCreate, payload)
        self.registry.get("tests", data["test_id"])
        self.registry.get("assemblies", data["assembly_id"])
        if (
            (data.get("observation_method") or {}).get("id")
            in {"raw_roi_observation_v1", "control_response_observation_v1"}
            or data.get("response_observation") is not None
        ) and not data.get("raw_capture_id"):
            raise ValueError(
                "RAW observation measurements require their preserved capture analysis"
            )
        if data.get("raw_capture_id"):
            from .observations import verify_capture_measurement

            verify_capture_measurement(self, data)
        for existing in self.registry.list("measurements"):
            if set(data["dataset_ids"]) & set(existing["dataset_ids"]):
                raise ConflictError(
                    "Dataset identity is already registered; "
                    "preserve the original and use a new revision ID"
                )
        original = original_bytes if original_bytes is not None else canonical(data).encode()
        if original_bytes is not None:
            import json

            if self._data(MeasurementCreate, json.loads(original_bytes)) != data:
                raise ValueError("Original bytes do not match the measurement contract")
        artifact = self.registry.store_blob(original, "measurement.json", "application/json")
        return self.registry.put(
            "measurements",
            {
                **data,
                "original_artifact": artifact,
                "original_encoding": "verbatim_utf8_json"
                if original_bytes is not None
                else "canonical_contract_json",
                "approval_status": "unreviewed",
            },
        )

    def import_capture(self, metadata, raw_bytes, dark_bytes=None):
        from .observations import import_capture

        return import_capture(self, metadata, raw_bytes, dark_bytes)

    def compare_retained_raw_capture(self, payload):
        """Cold-read original RAW for a descriptive, non-persistent comparison."""
        from .retained_raw_capture_comparison import compare

        return compare(self, payload)

    def propose_calibration(self, payload):
        from .calibration_service import propose_calibration

        return propose_calibration(self, payload)

    def check(self, assembly_id):
        assembly = self.registry.get("assemblies", assembly_id)
        models = [self.registry.get("models", i) for i in assembly["model_ids"]]
        return check_assembly(assembly, models)

    def _prepare_run(self, payload):
        if self.loaded_environment != environment_fingerprint():
            raise ConflictError(
                "Executable source changed after startup; restart the API before submitting"
            )
        data = self._data(RunCreate, payload)
        if data["capture_program_id"] is not None and data["plugin_id"] not in {
            "hdr_bracket_v1", "shared_charge_dcg_v1", "reference_hdr_v1",
            "retained_charge_v1", "electrical_control_v1",
        }:
            raise ValueError("A capture program requires its matching electrical runtime")
        if data["plugin_id"] == "closed_loop_scene_v1":
            from .control_service import prepare_run

            return prepare_run(self, data)
        if data["control_program_id"] is not None:
            raise ValueError("A control program requires closed_loop_scene_v1")
        if data["plugin_id"] in {
            "temporal_scene_v1",
            "hdr_bracket_v1",
            "shared_charge_dcg_v1",
            "reference_hdr_v1",
            "retained_charge_v1",
            "electrical_control_v1",
        }:
            from .sequences import prepare_run

            return prepare_run(self, data)
        if data["scene_sequence_id"] or data["control_sequence_id"]:
            raise ValueError("Scene/control sequences require temporal_scene_v1")
        if data["plugin_id"] == "spatial_scene_v1":
            from .imaging import prepare_spatial

            return prepare_spatial(self, data)
        if data["scene_id"] is not None:
            raise ValueError("A spectral image input requires the spatial_scene_v1 runtime")
        assembly = self.registry.get("assemblies", data["assembly_id"])
        test = self.registry.get("tests", data["test_id"])
        from .demo import EFFECTS
        from .runtime import with_sensor_effects

        models = [self.registry.get("models", i) for i in assembly["model_ids"]]
        by_kind = {m["kind"]: m for m in models}

        if test.get("observation_method") and data["plugin_id"] in {
            "quick_estimate",
            "quick_estimate_v1",
        }:
            raise ValueError("ROI observation requires the spatial Response-DB runtime")
        expected_effects = (
            ["sensor_effective_qe", "mean_dark_charge", "readout_clipping"]
            if data["plugin_id"] in {"quick_estimate", "quick_estimate_v1"}
            else EFFECTS
        )
        expected_effects = with_sensor_effects(expected_effects, by_kind["sensor"]["payload"])
        if (
            test["metric_id"] != "raw.channel_ratio"
            or test["unit"] != "1"
            or test["reference_plane"] != "sensor_raw"
            or set(test["included_effects"]) != set(expected_effects)
        ):
            raise ValueError(
                "This runtime computes raw.channel_ratio in unit 1 at sensor_raw; "
                "select a test with matching implemented effects or another runtime"
            )
        from .native_model_evidence import verify_models

        response_artifacts = verify_models(self, models)
        if "psf" in by_kind["lens"]["payload"]:
            raise ValueError("A Lens model containing PSF requires the spatial_scene_v1 runtime")
        if by_kind["isp"]["payload"].get("schema") in {
            "camera_isp_profile_v1", "camera_isp_profile_v2", "camera_isp_profile_v3"
        }:
            raise ValueError(
                "Tunable ISP profiles require spatial simulation or preserved RAW replay"
            )
        parameters = data["parameters"]
        if set(parameters) - {"exposure_ms", "seed", "frames", "output_size"}:
            raise ValueError(
                "Unsupported parameter; create a new model/test revision for physical changes"
            )
        conditions = copy.deepcopy(test["conditions"])
        if "exposure_ms" in parameters:
            exposure = parameters["exposure_ms"]
            if (
                isinstance(exposure, bool)
                or not isinstance(exposure, (int, float))
                or not math.isfinite(exposure)
                or exposure <= 0
            ):
                raise ValueError("Exposure must be a positive finite number in ms")
            conditions["exposure_s"] = {"value": exposure / 1000, "unit": "s"}
            if conditions["exposure_s"] != test["conditions"][
                "exposure_s"
            ] and "conditions.exposure_s.value" in test.get("parameter_uncertainty", {}):
                raise ValueError(
                    "Create a new test revision to change an exposure with a declared uncertainty"
                )
        effective_test = {**copy.deepcopy(test), "conditions": conditions}
        if conditions != test["conditions"]:
            # A sweep is an explicit immutable test variant; never relabel the original conditions.
            effective_test = {
                **effective_test,
                "derived_from_test": test["id"],
                "id": test["id"] + ":" + digest(conditions)[:12],
            }
        report = check_assembly({**assembly, "conditions": conditions}, models)
        if report["status"] != "PASS":
            raise PreflightError(report)
        stimulus = test["stimulus"]
        if stimulus.get("reference_plane") != "sensor_surface":
            raise ValueError("Stimulus must explicitly contain sensor-surface spectral irradiance")
        if stimulus.get("wavelength_unit") != "nm":
            raise ValueError("Stimulus wavelength_unit must be nm; convert explicitly upstream")
        if stimulus.get("spectral_irradiance_unit") != "W/m^2/nm":
            raise ValueError("Stimulus spectral_irradiance_unit must be W/m^2/nm")
        sensor = by_kind["sensor"]["payload"]
        if stimulus["wavelength_nm"] != sensor["wavelength_nm"]:
            raise ValueError(
                "Stimulus and sensor wavelength coordinates differ; "
                "implicit resampling is forbidden"
            )
        # Current runtime consumes only this exact Bayer/ISP contract.
        from .demo import RAW_FORMAT

        if assembly["raw_format"] != RAW_FORMAT:
            raise ValueError(
                "This runtime supports only unpacked RGGB12 with black level 64, one stream"
            )
        if (
            sensor["readout"]["bit_depth"] != RAW_FORMAT["bit_depth"]
            or sensor["readout"]["black_level_dn"] != RAW_FORMAT["black_level"]
        ):
            raise ValueError("Readout payload disagrees with its declared RAW contract")
        required_units = {"exposure_s": "s", "temperature_c": "degC", "gain": "1"}
        for key, unit in required_units.items():
            if conditions.get(key, {}).get("unit") != unit:
                raise ValueError(f"{key} must be specified in {unit}")
        if conditions["gain"]["value"] != sensor["readout"]["gain"]:
            raise ValueError(
                "Readout gain differs from the fixed test; create a matching model revision"
            )
        request = {
            "wavelength_nm": sensor["wavelength_nm"],
            **sensor_response_fields(sensor),
            "pixel_pitch_um": sensor["pixel_pitch_um"],
            "readout": sensor["readout"],
            "spectral_irradiance_w_m2_nm": stimulus["spectral_irradiance_w_m2_nm"],
            "irradiance_reference_plane": "sensor_surface_on_axis",
            "exposure_s": conditions["exposure_s"]["value"],
            "temperature_c": conditions["temperature_c"]["value"],
            "lens": by_kind["lens"]["payload"],
            "seed": parameters.get("seed", 42),
            "frames": parameters.get("frames", 4),
            "output_size": parameters.get("output_size", [64, 64]),
        }
        if "native_pixel_response" in sensor and data["plugin_id"] == "response_db_v1":
            request.update(self._native_capture_geometry(by_kind["sensor"], request["output_size"]))
        from .uncertainty import collect_inputs

        request["parameter_uncertainty"] = collect_inputs(models, effective_test)
        plugin = get_plugin(data["plugin_id"])
        if test.get("observation_method") and plugin.plugin_id != "response_db_v1":
            raise ValueError("ROI observation requires the spatial Response-DB runtime")
        if test.get("observation_method"):
            from .observations import _validate_sensor_geometry

            _validate_sensor_geometry(
                assembly, models, test["observation_specification"], "simulated"
            )
        issues = plugin.validate_request(request)
        if issues:
            raise PreflightError({"status": "FAIL", "issues": issues})
        frozen = {
            "name": assembly["name"],
            "assembly_id": assembly["id"],
            "test_id": test["id"],
            "plugin_id": plugin.plugin_id,
            "parameters": parameters,
            "request": request,
            "assembly_snapshot": assembly,
            "model_snapshots": models,
            "test_snapshot": effective_test,
            "compatibility": report,
            "environment": environment_fingerprint(),
            "resources": plugin.estimate_resources(request),
            **({"input_artifacts": response_artifacts} if response_artifacts else {}),
        }
        return frozen, data["force"]

    def submit_run(self, payload):
        frozen, force = self._prepare_run(payload)
        return self.registry.submit(frozen, digest(frozen), force=force)

    def list_simulation_runs(self):
        return [
            r
            for r in self.registry.list_runs()
            if r.get("job_kind")
            not in NON_CAMERA_JOBS
        ]

    def list_replays(self):
        records = [r for r in self.registry.list_runs() if r.get("job_kind") == "isp_replay"]
        from .retained_isp_replay import verify

        for record in records:
            if record.get("plugin_id") == "retained_isp_replay_v1":
                verify(self, record)
        return records

    def list_factory_jobs(self):
        return [r for r in self.registry.list_runs() if r.get("job_kind") == "characterization"]

    def execute_run(self, job):
        if job.get("job_kind") == "camera_validation_esfr":
            from .esfr_lab import execute
            return execute(self, job)
        if job.get("job_kind") == "calibration_unified_workflow":
            from .calibration_unified import execute

            return execute(self, job)
        if job.get("job_kind") == "calibration_db_core_workflow":
            from .calibration_db_core import execute

            return execute(self, job)
        if job.get("job_kind") == "calibration_db_color_replay":
            from .calibration_db_color import execute

            return execute(self, job)
        if job.get("job_kind") == "calibration_db_replay":
            from .calibration_db import execute

            return execute(self, job)
        if job.get("job_kind") in {"calibration_lab", "calibration_lab_validation"}:
            from .calibration_lab import execute

            return execute(self, job)
        if job.get("job_kind") == "design_asset_update":
            from .design_asset_jobs import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_legacy_isp":
            from .lens_sensor_legacy_isp_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_adaptive_demosaic":
            from .lens_sensor_adaptive_demosaic_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_isp_detector":
            from .lens_sensor_isp_detector_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_quality_study":
            from .lens_sensor_quality_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_pwm_hdr":
            from .lens_sensor_pwm_hdr_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_raw_cohort":
            from .lens_sensor_raw_cohort_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_isp_perception":
            from .lens_sensor_isp_perception_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_isp_perception_cohort":
            from .lens_sensor_isp_perception_cohort_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_perception":
            from .lens_sensor_perception_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_hdr":
            from .lens_sensor_hdr_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_lfm":
            from .lens_sensor_lfm_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_isp_replay":
            from .lens_sensor_isp_replay_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_calibrated_isp":
            from .lens_sensor_calibrated_isp_job import execute

            return execute(self, job)
        if job.get("job_kind") == "saved_sensor_calibrated_isp":
            from .saved_sensor_calibrated_isp_job import execute

            return execute(self, job)
        if job.get("job_kind") == "saved_sensor_perception":
            from .saved_sensor_perception import execute

            return execute(self, job)
        if job.get("job_kind") == "saved_sensor_isp_replay":
            from .saved_sensor_isp_replay_job import execute

            return execute(self, job)
        if job.get("job_kind") == "retained_raw_dataset":
            from .retained_raw_dataset_job import execute

            return execute(self, job)
        if job.get("job_kind") == "saved_sensor_lfm":
            from .saved_sensor_lfm_job import execute

            return execute(self, job)
        if job.get("job_kind") == "saved_sensor_hdr":
            from .saved_sensor_hdr_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_raw":
            from .lens_sensor_raw_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_streamed_raw":
            from .lens_sensor_streamed_raw_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_out_of_core_raw":
            from .lens_sensor_out_of_core_raw_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_registered_roi_disk_raw":
            from .lens_sensor_registered_roi_disk_raw_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_frequency_target":
            from .lens_sensor_frequency_target_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_geometric_pupil_density":
            from .lens_geometric_pupil_density_job import execute

            return execute(self, job)
        if job.get("job_kind") == "sensor_structure_study":
            from .sensor_structure_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_psf_lut_camera":
            from .lens_psf_lut_camera import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_optical_psf_lut":
            from .lens_psf_lut_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_dense_geometric_psf_candidate":
            from .lens_dense_geometric_psf_candidate_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_dense_geometric_psf_candidate_v2":
            from .lens_dense_geometric_psf_candidate_v2_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_dense_geometric_psf_q4_rebin":
            from .lens_dense_geometric_psf_q4_rebin_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_dense_psf_sensitivity":
            from .lens_sensor_dense_psf_sensitivity_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_q4_psf_sensitivity":
            from .lens_sensor_q4_psf_sensitivity_job import execute

            return execute(self, job)
        if job.get("job_kind") == "road_hdr_study":
            from .road_hdr_job import execute
            return execute(self, job)
        if job.get("job_kind") == "design_workshop":
            from .design_workshop_job import execute
            return execute(self, job)
        if job.get("job_kind") == "reference_workshop":
            from .reference_workshop_job import execute

            return execute(self, job)
        if job.get("job_kind") == "virtual_lab":
            from .virtual_lab_job import execute
            return execute(self, job)
        if job.get("job_kind") == "road_design_study":
            from .road_design_job import execute

            return execute(self, job)
        if job.get("job_kind") == "pedestrian_braking_study":
            from .pedestrian_braking_job import execute

            return execute(self, job)
        if job.get("job_kind") == "lens_sensor_joint_scene_dense_psf_ladder":
            from .lens_sensor_joint_sampling_ladder_job import execute

            return execute(self, job)
        if job.get("job_kind") == "saved_sensor_search":
            from .saved_sensor_search_job import execute

            return execute(self, job)
        if job.get("job_kind") == "saved_sensor_quality":
            from .saved_sensor_quality_job import execute

            return execute(self, job)
        if job.get("job_kind") == "physics_lut_sensitivity":
            from .physics_lut_bridge import execute

            return execute(self, job)
        if job.get("job_kind") == "catalog_quality_comparison":
            from .quality_comparison_bridge import execute

            return execute(self, job)
        if job.get("job_kind") == "db_comparison":
            from .db_comparisons import execute

            return execute(self, job)
        if job.get("job_kind") == "design_experiment":
            from .design_experiments import execute

            return execute(self, job)
        if job.get("job_kind") == "r1_timed_acquisition":
            from .r1_timed_acquisition import execute

            return execute(self, job)
        if job.get("job_kind") == "hdr_lfm_reference_comparison":
            from .hdr_lfm_reference_job import execute

            return execute(self, job)
        if job.get("job_kind") == "design_isp_replay":
            from .design_isp_replay import execute

            return execute(self, job)
        if job.get("job_kind") == "raw_temporal_replay":
            from .raw_temporal_replay import execute

            return execute(self, job)
        if job.get("job_kind") == "electrical_response_study":
            from .electrical_response_study import execute

            return execute(self, job)
        if job.get("job_kind") == "readout_fit":
            from .readout_fits import execute

            return execute(self, job)
        """Worker entry: refuses replay if executable source/runtime has drifted."""
        if job.get("job_kind") == "isp_study_cell":
            from .isp_studies import execute

            return execute(self, job)
        if job.get("job_kind") == "spectral_scene_projection":
            from .scene_projection import execute

            return execute(self, job)
        if job.get("job_kind") == "spectral_scene_render":
            from .scene_render import execute

            return execute(self, job)
        if job.get("plugin_id") == "closed_loop_scene_v1":
            from .control_service import execute_run

            return execute_run(self, job)
        if job.get("job_kind") == "surrogate_training":
            from .surrogates import execute

            return execute(self, job)
        if job.get("plugin_id") in {
            "temporal_scene_v1",
            "hdr_bracket_v1",
            "shared_charge_dcg_v1",
            "reference_hdr_v1",
            "retained_charge_v1",
            "electrical_control_v1",
        }:
            from .sequences import execute_run

            return execute_run(self, job)
        if job.get("job_kind") == "retained_isp_comparison":
            from .retained_isp_comparison import execute

            return execute(self, job)
        if job.get("job_kind") == "retained_isp_joint_study":
            from .retained_isp_joint_workflow import execute

            return execute(self, job)
        if job.get("job_kind") == "retained_isp_observable_selection":
            from .retained_isp_observable_workflow import execute_selection

            return execute_selection(self, job)
        if job.get("job_kind") == "retained_isp_observable_holdout":
            from .retained_isp_observable_workflow import execute_holdout

            return execute_holdout(self, job)
        if job.get("job_kind") == "retained_isp_prospective_selection":
            from .retained_isp_prospective_workflow import execute_selection

            return execute_selection(self, job)
        if job.get("job_kind") == "retained_isp_prospective_holdout":
            from .retained_isp_prospective_workflow import execute_holdout

            return execute_holdout(self, job)
        if job.get("job_kind") == "isp_replay":
            from .imaging import execute_replay

            return execute_replay(self, job)
        if job.get("plugin_id") == "spatial_scene_v1":
            from .imaging import execute_spatial

            return execute_spatial(self, job)
        if job.get("job_kind") == "characterization":
            from .factory import execute

            return execute(self, job)
        if job["environment"] != environment_fingerprint():
            raise ValueError(
                "Worker environment differs from the frozen submission; submit a new run"
            )
        from .native_model_evidence import verify_models

        response_artifacts = verify_models(self, job["model_snapshots"])
        if response_artifacts:
            by_kind = {model["kind"]: model for model in job["model_snapshots"]}
            sensor, lens = by_kind["sensor"]["payload"], by_kind["lens"]["payload"]
            for name, expected in sensor_response_fields(sensor).items():
                if job["request"].get(name) != expected:
                    raise ValueError("Frozen native run request differs from its Sensor model")
            if any(job["request"].get(k) != sensor[k]
                   for k in ("wavelength_nm", "pixel_pitch_um", "readout")):
                raise ValueError("Frozen native run request differs from pinned Sensor parameters")
            if (job["request"].get("lens") != lens
                    or job.get("input_artifacts") != response_artifacts):
                raise ValueError("Frozen native run request or artifacts differ from pinned models")
            if "native_pixel_response" in sensor and job["plugin_id"] == "response_db_v1":
                geometry = self._native_capture_geometry(
                    by_kind["sensor"], job["request"]["output_size"]
                )
                if any(job["request"].get(k) != v for k, v in geometry.items()):
                    raise ValueError("Frozen native crop differs from the declared Sensor geometry")
        output = run_plugin(job["plugin_id"], job["request"])
        summary = output["summary"]
        prediction = summary["prediction"]
        prediction.update(
            {
                "id": job["id"],
                "conditions": job["test_snapshot"]["conditions"],
                "valid_domain": self._domain_intersection(job["model_snapshots"]),
                "calibration_dataset_ids": sorted(
                    {i for m in job["model_snapshots"] for i in m["calibration_dataset_ids"]}
                ),
                "dataset_ids": [job["id"]],
                "assembly_id": job["assembly_id"],
                "test_id": job["test_id"],
            }
        )
        if job["test_snapshot"].get("observation_method"):
            from .observations import observe_prediction

            prediction, observed = observe_prediction(summary, job["test_snapshot"])
            summary["prediction"] = prediction
            summary["observed_measurement"] = observed
        if (response_artifacts and "native_pixel_response" in job["request"]
                and job["plugin_id"] == "response_db_v1"):
            from .imaging import store_output

            return store_output(
                self, output, source_kind="simulated",
                geometry=summary.get("geometry", {"kind": "native_sensor", **geometry}),
                input_artifacts=response_artifacts,
            )
        artifacts = list(response_artifacts)
        array_units = {
            "field_channel_electrons": "electron/pixel",
            "mean_electrons": "electron/pixel",
            "radius_map": "1",
            "raw_frames": "DN",
            "rgb": "1",
            "raw_temporal_variance_dn2": "DN^2",
        }
        for name, array in output["arrays"].items():
            stream = io.BytesIO()
            np.save(stream, array, allow_pickle=False)
            artifact = self.registry.store_blob(
                stream.getvalue(), name + ".npy", "application/octet-stream"
            )
            artifact.update(
                shape=list(array.shape),
                dtype=str(array.dtype),
                unit=array_units[name],
                source_kind="simulated",
                spatial_sampling=("native_site_normal_incidence_point"
                                  if "native_pixel_response" in job["request"]
                                  else "normalized_field_representative_raster"),
            )
            artifacts.append(artifact)
        if "rgb" in output["arrays"]:
            from PIL import Image

            stream = io.BytesIO()
            Image.fromarray((np.clip(output["arrays"]["rgb"], 0, 1) * 255).astype(np.uint8)).save(
                stream, format="PNG"
            )
            artifacts.append(
                self.registry.store_blob(stream.getvalue(), "sensor-rgb.png", "image/png")
            )
        artifacts.append(
            self.registry.store_blob(
                canonical(summary).encode(), "summary.json", "application/json"
            )
        )
        return {
            **summary,
            "summary": summary,
            "prediction": prediction,
            "artifacts": artifacts,
            "verification": {
                "implementation_verification": "runtime_completed",
                "cross_tool_comparison": "not_performed",
                "physical_validation": "not_established",
            },
        }

    @staticmethod
    def _domain_intersection(models):
        domain = {}
        for model in models:
            for key, bounds in model["valid_domain"].items():
                if key not in domain:
                    domain[key] = copy.deepcopy(bounds)
                else:
                    if domain[key]["unit"] != bounds["unit"]:
                        raise ValueError("Domain units disagree")
                    domain[key]["min"] = max(domain[key]["min"], bounds["min"])
                    domain[key]["max"] = min(domain[key]["max"], bounds["max"])
        return domain

    def validate(self, payload):
        data = self._data(ValidationCreate, payload)
        run = self.registry.get_run(data["run_id"])
        if run.get("job_kind") in NON_CAMERA_JOBS:
            raise ValueError("Characterization cannot be used as a camera measurement prediction")
        if run["status"] != "succeeded":
            raise ConflictError("Only a completed run can be compared")
        measurement = self.registry.get("measurements", data["measurement_id"])
        self.registry.blob_path(measurement["original_artifact"]["sha256"])
        comparison_measurement = copy.deepcopy(measurement)
        if measurement.get("raw_capture_id"):
            from .observations import capture_content_ids, verify_capture_measurement

            capture = verify_capture_measurement(self, measurement)
            comparison_measurement["dataset_ids"] = sorted(
                set(measurement["dataset_ids"]) | set(capture_content_ids(capture))
            )
        test = self.registry.get("tests", data["test_id"])
        prediction = run["result"]["prediction"]
        report = compare(prediction, comparison_measurement, test)
        mismatch = []
        if data["test_id"] != run["test_id"] or measurement["test_id"] != data["test_id"]:
            mismatch.append(
                "Simulation, measurement and comparison must use the same test revision"
            )
        if measurement["assembly_id"] != run["assembly_id"]:
            if data["allow_calibrated_model"]:
                from .calibration_service import calibrated_assembly_equivalence

                try:
                    report["model_revision_comparison"] = calibrated_assembly_equivalence(
                        self, run["assembly_id"], measurement["assembly_id"]
                    )
                except (ValueError, KeyError) as exc:
                    mismatch.append(str(exc))
            else:
                mismatch.append(
                    "Measurement is associated with a different camera assembly revision"
                )
        if mismatch:
            report.update(
                status="INVALID_COMPARISON", physical_validation_pass=False, comparison_valid=False
            )
            report["issues"].extend(
                {"code": "identity_mismatch", "message": m, "severity": "error"} for m in mismatch
            )
        return self.registry.put(
            "validations",
            {
                **report,
                **data,
                "name": test["name"],
                "evidence_hashes": {
                    "run": run["content_hash"],
                    "measurement": measurement["content_hash"],
                    "test": test["content_hash"],
                },
            },
        )

    def create_experiment(self, payload):
        data = self._data(ExperimentCreate, payload)
        if len(data["assembly_ids"]) * len(data["exposure_ms"]) > 24:
            raise ValueError("An experiment is limited to 24 runs")
        requests = [
            {
                "assembly_id": a,
                "test_id": data["test_id"],
                "parameters": {"exposure_ms": e, "seed": data["seed"]},
            }
            for a in data["assembly_ids"]
            for e in data["exposure_ms"]
        ]
        # Validate the whole plan before submitting any work.
        prepared = [self._prepare_run(r) for r in requests]
        runs = [
            self.registry.submit(frozen, digest(frozen), force=force) for frozen, force in prepared
        ]
        return self.registry.put(
            "experiments",
            {
                **data,
                "run_ids": [r["id"] for r in runs],
                "purpose": (
                    "Condition-controlled sweep; no automatic product ranking or model approval"
                ),
            },
        )

    def plan_study(self, payload):
        from .studies import preview

        return preview(self, payload)

    def plan_surrogate(self, payload):
        from .surrogates import prepare

        return prepare(self, payload)

    def create_surrogate(self, payload):
        from .surrogates import submit

        return submit(self, payload)

    def predict_surrogate(self, identity, payload):
        from .surrogates import predict

        return predict(self, identity, payload)

    def create_study(self, payload):
        from .studies import submit

        return submit(self, payload)

    def analyze_study(self, identity):
        from .studies import analyze

        return analyze(self, identity)

    def create_release(self, payload):
        data = self._data(ReleaseCreate, payload)
        runs = [self.registry.get_run(i) for i in data["run_ids"]]
        if any(
            r.get("job_kind")
            in NON_CAMERA_JOBS
            for r in runs
        ):
            raise ValueError(
                "A camera release selects camera runs; factory evidence follows model lineage"
            )
        reports = [self.registry.get("validations", i) for i in data["validation_ids"]]
        blockers = []
        for run in runs:
            if run["status"] != "succeeded":
                blockers.append(
                    {
                        "code": "run_incomplete",
                        "run_id": run["id"],
                        "message": "Run did not complete successfully",
                    }
                )
                continue
            for artifact in run["artifacts"]:
                self.registry.blob_path(artifact["sha256"])
            if not any(
                r["run_id"] == run["id"] and r.get("physical_validation_pass") for r in reports
            ):
                blockers.append(
                    {
                        "code": "independent_validation_missing",
                        "run_id": run["id"],
                        "message": "Independent physical validation has not passed",
                    }
                )
            for m in run["model_snapshots"]:
                if m["license"].lower() in {"unknown", "unresolved", "missing"}:
                    blockers.append(
                        {
                            "code": "license_unresolved",
                            "model_id": m["id"],
                            "message": "Asset distribution rights are unresolved",
                        }
                    )
        # This local edition prepares reviewable packages; it never impersonates final approval.
        return self.registry.put(
            "releases",
            {
                **data,
                "status": "blocked" if blockers else "review_required",
                "blockers": blockers,
                "approval_status": "unapproved",
                "evidence": {
                    "runs": [
                        {
                            "id": r["id"],
                            "input_hash": r["content_hash"],
                            "cache_key": r["cache_key"],
                        }
                        for r in runs
                    ],
                    "validations": [
                        {"id": r["id"], "content_hash": r["content_hash"]} for r in reports
                    ],
                },
                "audit_head": self.registry.audit()["head_hash"],
            },
        )

    def export_run(self, identity):
        run = self.registry.get_run(identity)
        if run.get("job_kind") == "electrical_response_study":
            raise ValueError("Use the dedicated electrical response study evidence endpoint")
        if run.get("job_kind") == "readout_fit":
            raise ValueError("Use the dedicated response fitting evidence endpoint")
        if run.get("job_kind") == "spectral_scene_projection":
            for artifact in [*run["artifacts"], *run.get("input_artifacts", [])]:
                self.registry.blob_path(artifact["sha256"])
            return {
                "schema": SCHEMA_VERSION, "type": "scene_projection_evidence_package",
                "run": run, "package_hash": digest(run),
                "replay": (
                    "Preview and submit /api/v1/scene_projections using the pinned "
                    "source render and target projection"
                ),
            }
        if run.get("job_kind") == "spectral_scene_render":
            for artifact in [*run["artifacts"], *run.get("input_artifacts", [])]:
                self.registry.blob_path(artifact["sha256"])
            return {
                "schema": SCHEMA_VERSION,
                "type": "scene_render_evidence_package",
                "run": run,
                "package_hash": digest(run),
                "replay": "POST /api/v1/scene_renders with original request parameters "
                "and pinned assets",
            }
        if run.get("job_kind") == "surrogate_training":
            raise ValueError(
                "Use the surrogate endpoint to export its model and reference evidence"
            )
        if run.get("job_kind") == "retained_isp_comparison":
            raise ValueError("Use the dedicated retained ISP comparison evidence endpoint")
        if run.get("job_kind") == "retained_isp_joint_study":
            raise ValueError("Use the dedicated retained ISP Joint Study evidence endpoint")
        if run.get("job_kind") in {
            "retained_isp_observable_selection", "retained_isp_observable_holdout",
            "retained_isp_prospective_selection", "retained_isp_prospective_holdout",
        }:
            raise ValueError("Use the dedicated observable ISP optimization evidence endpoint")
        if run.get("job_kind") == "isp_replay":
            raise ValueError("Use the dedicated ISP replay evidence endpoint")
        if run.get("job_kind") == "isp_study_cell":
            raise ValueError("Use the owning ISP Study evidence endpoint")
        for artifact in run["artifacts"]:
            self.registry.blob_path(artifact["sha256"])
        pending = [m["id"] for m in run.get("model_snapshots", [])]
        lineage = {}
        while pending:
            model_id = pending.pop()
            if model_id in lineage:
                continue
            model = self.registry.get("models", model_id)
            lineage[model_id] = model
            pending.extend(model["parents"])
        factory_jobs = [
            self.registry.get_run(job_id)
            for job_id in sorted({
                m[key] for m in lineage.values()
                for key in ("factory_job_id", "psf_factory_job_id") if m.get(key)
            })
        ]
        for job in factory_jobs:
            for artifact in [*job.get("input_artifacts", []), *job["artifacts"]]:
                self.registry.blob_path(artifact["sha256"])
        content = {
            "run": run,
            "model_lineage": [lineage[i] for i in sorted(lineage)],
            "factory_jobs": factory_jobs,
        }
        return {
            "schema": SCHEMA_VERSION,
            "type": "run_evidence_package",
            **content,
            "package_hash": digest(content),
            "replay": (
                "POST /api/v1/runs using pinned assembly_id, test_id, plugin_id, "
                "scene_sequence_id, control_sequence_id, capture_program_id and parameters; "
                "restart the entire immutable sequence from zero, never resume external state"
                if run.get("plugin_id") in {"retained_charge_v1", "electrical_control_v1"}
                else "POST /api/v1/runs using pinned assembly_id, test_id, "
                "plugin_id, scene_id or scene_sequence_id/control_sequence_id when present, "
                "control_program_id for closed-loop programs, "
                "and parameters"
            ),
        }

    def export_replay(self, identity):
        replay = self.registry.get_run(identity)
        if replay.get("job_kind") != "isp_replay":
            raise ValueError("Selected job is not an ISP replay")
        if replay.get("plugin_id") == "retained_isp_replay_v1":
            from .retained_isp_replay import verify

            verify(self, replay)
        if replay.get("plugin_id") == "isp_replay_v3":
            from .isp_replay_v3 import verify

            verify(self, replay)
        source = replay["source"]["source_snapshot"]
        pending = [replay["isp_model_snapshot"]["id"]]
        pending += [m["id"] for m in source.get("model_snapshots", [])]
        lineage = {}
        while pending:
            identity = pending.pop()
            if identity in lineage:
                continue
            model = self.registry.get("models", identity)
            lineage[identity] = model
            pending.extend(model["parents"])
        factory_jobs = [
            self.registry.get_run(identity)
            for identity in sorted(
                {
                    m[key]
                    for m in lineage.values()
                    for key in ("factory_job_id", "psf_factory_job_id")
                    if m.get(key)
                }
            )
        ]
        for record in [replay, source, *factory_jobs]:
            for artifact in record.get("artifacts", []):
                self.registry.blob_path(artifact["sha256"])
        content = {
            "replay": replay,
            "source": source,
            "model_lineage": [lineage[i] for i in sorted(lineage)],
            "factory_jobs": factory_jobs,
        }
        imported_selections = []
        if (replay.get("plugin_id") == "isp_replay_v3"
                and replay["source"]["source_kind"] == "capture"):
            imported_selections.append(replay["source"])
        if replay.get("plugin_id") in {"isp_replay_v2", "isp_replay_v3"}:
            if replay["plugin_id"] == "isp_replay_v3":
                from .isp_charge_calibration import get as get_calibration
            else:
                from .isp_calibration import get_calibration

            calibration = replay.get("calibration_snapshot")
            if calibration:
                current = get_calibration(self, calibration["id"])
                if current != calibration:
                    raise ValueError("Replay calibration snapshot differs from its original")
                content["isp_calibration"] = current
                calibration_lineage, calibration_sources = {}, {}
                pending = [current["id"]]
                while pending:
                    calibration_id = pending.pop()
                    if calibration_id in calibration_lineage:
                        continue
                    ancestor = get_calibration(self, calibration_id)
                    calibration_lineage[calibration_id] = ancestor
                    pending.extend(ancestor["parents"])
                    reference = ancestor["reference_source"]
                    if (ancestor.get("schema") == "camera_charge_isp_calibration_v1"
                            and reference["kind"] == "capture"):
                        imported_selections.append({
                            "source_kind": "capture", "source_id": reference["id"],
                            "selector": reference["selector"],
                        })
                    key = reference["kind"] + ":" + reference["id"]
                    calibration_sources[key] = ancestor["source_snapshot"]
                content["isp_calibration_lineage"] = [
                    calibration_lineage[key] for key in sorted(calibration_lineage)
                ]
                content["isp_calibration_sources"] = [
                    calibration_sources[key] for key in sorted(calibration_sources)
                ]
                for record in content["isp_calibration_sources"]:
                    for artifact in record.get("artifacts", []):
                        self.registry.blob_path(artifact["sha256"])
        if imported_selections:
            from .isp_capture_source import evidence_closure

            imported = evidence_closure(self, imported_selections)
            content.update(imported)
            # Calibration fitting and parent acquisitions may share the sensor
            # while using a different lens assembly. Their factory ancestry is
            # part of the portable evidence, too.
            pending = [m["id"] for record in imported["nonlinear_acquisition_lineage"]
                       for m in record.get("model_snapshots", [])]
            pending.extend(r["sensor_model_id"] for r in imported["readout_calibrations"])
            while pending:
                model_id = pending.pop()
                if model_id in lineage:
                    continue
                model = self.registry.get("models", model_id)
                lineage[model_id] = model
                pending.extend(model["parents"])
            content["model_lineage"] = [lineage[k] for k in sorted(lineage)]
            content["factory_jobs"] = [
                self.registry.get_run(job_id)
                for job_id in sorted({m[key] for m in lineage.values()
                                      for key in ("factory_job_id", "psf_factory_job_id")
                                      if m.get(key)})
            ]
            for job in content["factory_jobs"]:
                for artifact in [*job.get("input_artifacts", []), *job.get("artifacts", [])]:
                    self.registry.blob_path(artifact["sha256"])
        return {
            "schema": SCHEMA_VERSION,
            "type": "isp_replay_evidence_package",
            **content,
            "package_hash": digest(content),
        }

    def export_release(self, identity):
        release = self.registry.get("releases", identity)
        self.registry.audit()
        validations = [self.registry.get("validations", i) for i in release["validation_ids"]]
        runs = [self.export_run(i) for i in release["run_ids"]]
        pending_models = [m["id"] for entry in runs for m in entry["run"]["model_snapshots"]]
        model_lineage = {}
        while pending_models:
            model_id = pending_models.pop()
            if model_id in model_lineage:
                continue
            model = self.registry.get("models", model_id)
            model_lineage[model_id] = model
            pending_models.extend(model["parents"])
        calibrations = [
            c
            for c in self.registry.list("calibrations")
            if c.get("candidate_model_id") in model_lineage
        ]
        factory_jobs = [
            self.registry.get_run(i)
            for i in sorted(
                {
                    m[key]
                    for m in model_lineage.values()
                    for key in ("factory_job_id", "psf_factory_job_id")
                    if m.get(key)
                }
            )
        ]
        for job in factory_jobs:
            for artifact in job["artifacts"]:
                self.registry.blob_path(artifact["sha256"])
        measurement_ids = {v["measurement_id"] for v in validations}
        measurement_ids.update(i for c in calibrations for i in c["measurement_ids"])
        measurements = [self.registry.get("measurements", i) for i in sorted(measurement_ids)]
        captures = [
            self.registry.get("captures", i)
            for i in sorted({m["raw_capture_id"] for m in measurements if m.get("raw_capture_id")})
        ]
        for measurement in measurements:
            self.registry.blob_path(measurement["original_artifact"]["sha256"])
            if measurement.get("raw_capture_id"):
                from .observations import verify_capture_measurement

                verify_capture_measurement(self, measurement)
        analyses = [
            self.registry.get("capture_analyses", i)
            for i in sorted(
                {m["capture_analysis_id"] for m in measurements if m.get("capture_analysis_id")}
            )
        ]
        # Include ancestors even when their measurement was not selected for this release.
        by_id = {a["id"]: a for a in analyses}
        pending = list(analyses)
        while pending:
            parent_id = pending.pop().get("parent_analysis_id")
            if parent_id and parent_id not in by_id:
                parent = self.registry.get("capture_analyses", parent_id)
                by_id[parent_id] = parent
                pending.append(parent)
        analyses = [by_id[i] for i in sorted(by_id)]
        for analysis in analyses:
            for artifact in analysis["artifacts"]:
                self.registry.blob_path(artifact["sha256"])
        for capture in captures:
            for artifact in capture["artifacts"]:
                self.registry.blob_path(artifact["sha256"])
        content = {
            "release": release,
            "runs": runs,
            "validations": validations,
            "measurements": measurements,
            "captures": captures,
            "capture_analyses": analyses,
            "calibrations": calibrations,
            "factory_jobs": factory_jobs,
            "model_lineage": [model_lineage[i] for i in sorted(model_lineage)],
            "measurement_tests": [
                self.registry.get("tests", i) for i in sorted({m["test_id"] for m in measurements})
            ],
        }
        return {
            "schema": SCHEMA_VERSION,
            "type": "release_evidence_package",
            **content,
            "package_hash": digest(content),
        }

    def legacy_catalog(self, query="", limit=40):
        path = self.workspace / "camerae2e_db/catalog.sqlite"
        if not path.is_file():
            return {"available": False, "components": [], "reason": "Legacy catalog is not present"}
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            counts = {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("components", "model_revisions", "simulation_runs", "artifacts")
            }
            columns = [r[1] for r in conn.execute("PRAGMA table_info(components)")]
            clauses = " OR ".join(
                f"CAST({c} AS TEXT) LIKE ?"
                for c in columns
                if c in ("model", "manufacturer", "component_id")
            )
            rows = conn.execute(
                "SELECT * FROM components" + (" WHERE " + clauses if query else "") + " LIMIT ?",
                (["%" + query + "%"] * clauses.count("?") if query else [])
                + [min(max(limit, 1), 100)],
            ).fetchall()
            return {
                "available": True,
                "read_only": True,
                "counts": counts,
                "components": [dict(r) for r in rows],
                "note": (
                    "Legacy catalog identity is preserved. "
                    "Importing a physical response requires an explicit model contract."
                ),
            }
        finally:
            conn.close()

    def create_case_review(self, payload):
        from .case_reviews import create_case_review

        return create_case_review(self, payload)

    def get_case_review(self, identity):
        from .case_reviews import get_case_review

        return get_case_review(self, identity)

    def list_case_reviews(self):
        from .case_reviews import list_case_reviews

        return list_case_reviews(self)

    def case_review_history(self, identity):
        from .case_reviews import case_review_history

        return case_review_history(self, identity)

    def bootstrap(self):
        data = {
            kind: self.registry.list(kind)
            for kind in (
                "assets",
                "models",
                "assemblies",
                "tests",
                "measurements",
                "validations",
                "releases",
                "experiments",
                "studies",
                "isp_studies",
                "isp_selections",
                "isp_references",
                "surrogate_predictions",
                "captures",
                "capture_analyses",
                "readout_calibrations",
                "readout_observations",
                "calibrations",
                "scene_sequences",
                "control_sequences",
                "control_policies",
                "control_programs",
                "capture_programs",
                "scene_assets",
            )
        }
        data["case_reviews"] = self.list_case_reviews()
        jobs = self.registry.list_runs()
        data["runs"] = [
            r
            for r in jobs
            if r.get("job_kind")
            not in NON_CAMERA_JOBS
        ]
        data["factory_jobs"] = [r for r in jobs if r.get("job_kind") == "characterization"]
        data["readout_fits"] = [r for r in jobs if r.get("job_kind") == "readout_fit"]
        data["replays"] = [r for r in jobs if r.get("job_kind") == "isp_replay"]
        data["isp_study_cells"] = [r for r in jobs if r.get("job_kind") == "isp_study_cell"]
        data["scene_renders"] = [r for r in jobs if r.get("job_kind") == "spectral_scene_render"]
        data["scene_projections"] = [
            r for r in jobs if r.get("job_kind") == "spectral_scene_projection"
        ]
        data["spectral_images"] = self.registry.list("spectral_images")
        data["surrogates"] = [r for r in jobs if r.get("job_kind") == "surrogate_training"]
        from .factory import list_plugins as factory_plugins

        return {
            "schema": SCHEMA_VERSION,
            "version": __version__,
            "workspace": str(self.workspace),
            "state_dir": str(self.registry.directory),
            **data,
            "counts": {k: len(v) for k, v in data.items()},
            "plugins": list_plugins()
            + [
                {
                    "plugin_id": "response_table_import_v1",
                    "role": "characterization",
                    "available": True,
                    "supports": ["validated_table_registration"],
                    "unsupported": ["automatic_physical_approval"],
                },
            ]
            + factory_plugins(),
            "property_dictionary": PROPERTY_DICTIONARY,
            "readiness": [
                {
                    "label": "통합 실행·증거 보존",
                    "status": "available",
                    "detail": "버전 고정 모델, 별도 Worker, RAW/RGB 및 해시 기반 산출물",
                },
                {
                    "label": "독립 실물 검증",
                    "status": "missing" if not data["measurements"] else "review_required",
                    "detail": "실측 원본·시험 조건·불확실도·독립 데이터가 필요합니다.",
                },
                {
                    "label": "기업 운영",
                    "status": "pending",
                    "detail": (
                        "현재 단일 호스트 로컬 에디션. SSO/RBAC, PostgreSQL/Object Store, "
                        "부하 검증이 후속 출시 조건입니다. 로컬 백업·복구를 지원합니다."
                    ),
                },
                {
                    "label": "외부 정밀 Solver",
                    "status": "limited",
                    "detail": (
                        "설치된 RayOptics는 제한된 렌즈 방사 측정, Meep은 평면층 광학 "
                        "특성화를 수행합니다. 실물 정확도와 전자 수집 QE는 미확립입니다."
                    ),
                },
            ],
            "demo": {
                "assembly_ids": [a["id"] for a in reversed(data["assemblies"])],
                "test_id": data["tests"][-1]["id"] if data["tests"] else None,
            },
        }
