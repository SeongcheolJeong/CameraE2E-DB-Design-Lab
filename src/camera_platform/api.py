"""Versioned local API. Human UI and agent SDK share these use cases."""

from __future__ import annotations

import fcntl
import json
import secrets
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from . import __version__
from .capture_analysis import CaptureAnalysisRequest
from .case_contracts import CaseReviewCreate
from .contracts import (
    AssemblyCreate,
    AssetCreate,
    CalibrationCreate,
    CaptureImport,
    CaptureProgramCreate,
    ControlSequenceCreate,
    ExperimentCreate,
    FactoryJobCreate,
    FactoryModelCreate,
    MeasurementCreate,
    MeasurementImport,
    ModelCreate,
    ReleaseCreate,
    ReplayCreate,
    RunCreate,
    SceneSequenceCreate,
    SpectralFixtureCreate,
    SpectralImageImport,
    TemporalFixtureCreate,
    TestCreate,
    ValidationCreate,
)
from .control_contracts import ControlPolicyCreate, ControlProgramCreate
from .control_observation import ResponseTestRequest
from .demo import measurement_template, model_template, seed
from .finite_plane import PlaneFixtureCreate
from .isp_replay_v3 import ReplayCreateV3
from .lens_db_factory_publication import FactoryPairPublicationCreate, publish_pair
from .projection_contracts import SceneProjectionCreate, SceneProjectionSubmit
from .registry import ConflictError, IntegrityError
from .retained_isp_replay import ReplayCreate as RetainedISPReplayCreate
from .scene_camera import SceneCameraDraft, SceneCameraSave
from .scene_contracts import RenderPublish, SceneAssetCreate, SceneRenderCreate
from .service import NON_CAMERA_JOBS, PlatformService, PreflightError
from .study_contracts import StudyPlan, StudySubmit
from .surrogate_contracts import SurrogatePlan, SurrogatePredict, SurrogateSubmit


def create_app(state_dir=None, workspace=None, *, start_worker=True, seed_demo=True):
    root = Path(workspace or Path(__file__).resolve().parents[2]).resolve()
    service = PlatformService(state_dir or root / "outputs/camera-platform", root)
    with (service.registry.directory / "initialization.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if seed_demo:
            seed(service)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        process = None
        log = None
        from .design_asset_jobs import Watcher

        watcher = Watcher(service)
        app.state.design_asset_watcher = watcher
        if start_worker:
            log = (service.registry.directory / "worker-supervisor.log").open("ab")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "camera_platform.worker",
                    "--state-dir",
                    str(service.registry.directory),
                    "--workspace",
                    str(root),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                start_new_session=True,
            )
        app.state.worker = process
        if start_worker:
            watcher.start()
        try:
            yield
        finally:
            watcher.stop()
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if log:
                log.close()

    app = FastAPI(
        title="Camera System Engineering & Validation",
        version=__version__,
        lifespan=lifespan,
        description=(
            "Local single-host engineering edition. Immutable revisions, "
            "explicit physics, and independent validation gates."
        ),
    )
    app.state.service = service
    app.state.worker = None

    from .calibration_lab import install_routes as install_calibration_lab_routes

    install_calibration_lab_routes(app, service)
    from .example_gallery import install_routes as install_example_routes
    install_example_routes(app, service)
    from .esfr_lab import install_routes as install_esfr_routes
    install_esfr_routes(app, service)
    from .esfr_measured_workflow import install_routes as install_esfr_measured_routes
    install_esfr_measured_routes(app, service)

    # Specific research endpoints must precede the generic collection routes.
    from .raw_temporal_replay import install_routes as install_raw_temporal_routes

    install_raw_temporal_routes(app, service)
    from .design_experiments import install_routes as install_design_experiment_routes

    install_design_experiment_routes(app, service)
    from .r1_timed_acquisition import install_routes as install_r1_timed_routes

    install_r1_timed_routes(app, service)
    from .db_comparisons import install_routes as install_db_comparison_routes

    install_db_comparison_routes(app, service)
    from .quality_comparison_bridge import install_routes as install_quality_comparison_routes

    install_quality_comparison_routes(app, service)
    from .saved_sensor_quality_job import install_routes as install_saved_sensor_quality_routes

    install_saved_sensor_quality_routes(app, service)
    from .saved_sensor_search_job import install_routes as install_saved_sensor_search_routes

    install_saved_sensor_search_routes(app, service)
    from .lens_sensor_raw_job import install_routes as install_lens_sensor_raw_routes

    install_lens_sensor_raw_routes(app, service)
    from .lens_sensor_streamed_raw_job import install_routes as install_streamed_raw_routes

    install_streamed_raw_routes(app, service)
    from .lens_sensor_out_of_core_raw_job import install_routes as install_disk_raw_routes

    install_disk_raw_routes(app, service)
    from .lens_sensor_registered_roi_disk_raw_job import (
        install_routes as install_registered_roi_disk_raw_routes,
    )

    install_registered_roi_disk_raw_routes(app, service)
    from .lens_sensor_frequency_target_job import install_routes as install_frequency_target_routes

    install_frequency_target_routes(app, service)
    from .lens_geometric_pupil_density_job import install_routes as install_pupil_density_routes

    install_pupil_density_routes(app, service)
    from .lens_psf_lut_job import install_routes as install_lens_psf_lut_routes

    install_lens_psf_lut_routes(app, service)
    from .sensor_structure_job import install_routes as install_sensor_structure_routes
    install_sensor_structure_routes(app, service)
    from .lens_psf_lut_camera import install_routes as install_lens_psf_camera_routes
    install_lens_psf_camera_routes(app, service)
    from .lens_dense_geometric_psf_candidate_job import (
        install_routes as install_dense_geometric_psf_candidate_routes,
    )

    install_dense_geometric_psf_candidate_routes(app, service)
    from .lens_dense_geometric_psf_candidate_v2_job import (
        install_routes as install_dense_geometric_psf_candidate_v2_routes,
    )

    install_dense_geometric_psf_candidate_v2_routes(app, service)
    from .lens_dense_geometric_psf_q4_rebin_job import (
        install_routes as install_dense_geometric_psf_q4_rebin_routes,
    )

    install_dense_geometric_psf_q4_rebin_routes(app, service)
    from .lens_sensor_dense_psf_sensitivity_job import (
        install_routes as install_dense_psf_sensitivity_routes,
    )

    install_dense_psf_sensitivity_routes(app, service)
    from .lens_sensor_q4_psf_sensitivity_job import (
        install_routes as install_q4_psf_sensitivity_routes,
    )

    install_q4_psf_sensitivity_routes(app, service)
    from .road_design_job import install_routes as install_road_design_routes

    install_road_design_routes(app, service)
    from .road_hdr_job import install_routes as install_road_hdr_routes
    install_road_hdr_routes(app, service)
    from .virtual_lab_job import install_routes as install_virtual_lab_routes
    install_virtual_lab_routes(app, service)
    from .design_workshop_job import install_routes as install_workshop_routes
    from .reference_workshop_job import install_routes as install_reference_workshop_routes
    install_workshop_routes(app, service)
    install_reference_workshop_routes(app, service)
    from .pedestrian_braking_job import install_routes as install_pedestrian_braking_routes

    install_pedestrian_braking_routes(app, service)
    from .lens_sensor_joint_sampling_ladder_job import (
        install_routes as install_joint_sampling_ladder_routes,
    )

    install_joint_sampling_ladder_routes(app, service)
    from .lens_sensor_fov_feasibility import install_routes as install_lens_sensor_fov_routes

    install_lens_sensor_fov_routes(app, service)
    from .lens_sensor_active_mode_asset import install_routes as install_active_mode_routes

    install_active_mode_routes(app, service)
    from .lens_sensor_legacy_isp_job import install_routes as install_lens_legacy_isp_routes

    install_lens_legacy_isp_routes(app, service)
    from .lens_db_factory_workflow import install_routes as install_lens_db_factory_routes

    install_lens_db_factory_routes(app, service)

    @app.post(
        "/api/v1/native-response-bundles/import", status_code=201,
        tags=["sensor_model_factory"],
    )
    def native_response_bundle_import(
        expected_manifest_sha256: Annotated[str, Form()],
        lens_model_id: Annotated[str, Form()],
        readout_model_id: Annotated[str, Form()],
        name: Annotated[str, Form()],
        revision: Annotated[str, Form()],
        license: Annotated[str, Form()],
        bundle: Annotated[UploadFile, File()],
    ):
        from .native_response_publication import (
            extract_native_response_bundle,
            publish_native_response_bundle,
        )

        # The local API buffers multipart bodies; keep its limit below the
        # direct Python publication limit. Never accept a server-local path.
        max_upload_bytes = 64 * 1024 * 1024
        content = bundle.file.read(max_upload_bytes + 1)
        if len(content) > max_upload_bytes:
            raise HTTPException(413, "Native response ZIP is limited to 64 MiB")
        with TemporaryDirectory(dir=service.registry.directory) as directory:
            archive = Path(directory) / "bundle.zip"
            archive.write_bytes(content)
            bundle_dir = extract_native_response_bundle(archive, Path(directory) / "bundle")
            if sum(path.stat().st_size for path in bundle_dir.rglob("*") if path.is_file()) > (
                128 * 1024 * 1024
            ):
                raise HTTPException(413, "Native response ZIP expands beyond 128 MiB")
            return publish_native_response_bundle(
                service,
                bundle_dir,
                expected_manifest_sha256=expected_manifest_sha256,
                lens_model_id=lens_model_id,
                readout_model_id=readout_model_id,
                name=name,
                revision=revision,
                license=license,
            )

    @app.get("/api/v1/native-sensor-data-requests", tags=["sensor_model_factory"])
    def native_sensor_data_request_get(
        lens_model_id: str, sensor_identifier: str, run_id: str | None = None,
    ):
        """Explain missing native response for one registered Factory Lens and DB run."""
        from .native_response_publication import _qualified_lens
        from .native_sensor_readiness import native_sensor_data_request

        lens = _qualified_lens(service, lens_model_id)
        request = native_sensor_data_request(
            sensor_identifier,
            psf=lens["payload"]["psf"],
            directional_response=lens["payload"]["directional_response"],
            repository_root=service.workspace,
            run_id=run_id,
        )
        return {
            **request,
            "registered_lens_binding": {
                "model_id": lens["id"],
                "model_content_hash": lens["content_hash"],
            },
        }

    @app.get("/api/v1/lens-sensor-radiance-scenes/template", tags=["lens_sensor_raw"])
    def lens_sensor_radiance_scene_template():
        from .lens_sensor_radiance_scene import template

        return template()

    @app.post(
        "/api/v1/lens-sensor-radiance-scenes/import", status_code=201,
        tags=["lens_sensor_raw"],
    )
    def lens_sensor_radiance_scene_import(
        metadata: Annotated[str, Form()], image: Annotated[UploadFile, File()],
    ):
        from .lens_sensor_radiance_scene import MAX_NPY_BYTES, import_radiance_scene

        if len(metadata.encode("utf-8")) > 1_048_576:
            raise HTTPException(413, "Radiance scene metadata is limited to 1 MiB")
        content = image.file.read(MAX_NPY_BYTES + 1)
        if len(content) > MAX_NPY_BYTES:
            raise HTTPException(413, "Radiance scene NPY is limited to 32 MiB")
        return import_radiance_scene(service, json.loads(metadata), content)

    @app.get("/api/v1/lens-sensor-radiance-scenes", tags=["lens_sensor_raw"])
    def lens_sensor_radiance_scene_list():
        from .lens_sensor_radiance_scene import list_radiance_scenes

        return list_radiance_scenes(service)

    @app.get("/api/v1/lens-sensor-radiance-scenes/{identity}", tags=["lens_sensor_raw"])
    def lens_sensor_radiance_scene_get(identity: str):
        from .lens_sensor_radiance_scene import get_radiance_scene

        return get_radiance_scene(service, identity)

    from .lens_sensor_quality_job import install_routes as install_lens_sensor_quality_routes

    install_lens_sensor_quality_routes(app, service)
    from .lens_sensor_hdr_job import install_routes as install_lens_sensor_hdr_routes

    install_lens_sensor_hdr_routes(app, service)
    from .lens_sensor_lfm_job import install_routes as install_lens_sensor_lfm_routes

    install_lens_sensor_lfm_routes(app, service)
    from .lens_sensor_pwm_hdr_job import install_routes as install_lens_sensor_pwm_hdr_routes

    install_lens_sensor_pwm_hdr_routes(app, service)
    from .hdr_lfm_mode_readiness import install_routes as install_hdr_lfm_readiness_routes
    from .hdr_lfm_reference_job import install_routes as install_hdr_lfm_reference_routes

    install_hdr_lfm_readiness_routes(app, service)
    install_hdr_lfm_reference_routes(app, service)
    from .lens_sensor_perception_job import install_routes as install_lens_sensor_perception_routes

    install_lens_sensor_perception_routes(app, service)
    from .lens_sensor_raw_cohort_job import install_routes as install_lens_sensor_raw_cohort_routes

    install_lens_sensor_raw_cohort_routes(app, service)
    from .lens_sensor_isp_replay_job import install_routes as install_lens_sensor_isp_routes

    install_lens_sensor_isp_routes(app, service)
    from .lens_sensor_calibrated_isp_job import install_routes as install_lens_calibrated_isp_routes

    install_lens_calibrated_isp_routes(app, service)
    from .lens_sensor_isp_perception_job import install_routes as install_lens_isp_perception_routes

    install_lens_isp_perception_routes(app, service)
    from .lens_sensor_isp_perception_cohort_job import (
        install_routes as install_lens_isp_perception_cohort_routes,
    )

    install_lens_isp_perception_cohort_routes(app, service)
    from .lens_sensor_adaptive_demosaic_job import (
        install_routes as install_lens_adaptive_demosaic_routes,
    )

    install_lens_adaptive_demosaic_routes(app, service)
    from .lens_sensor_isp_detector_job import install_routes as install_lens_isp_detector_routes

    install_lens_isp_detector_routes(app, service)
    from .saved_sensor_hdr_job import install_routes as install_saved_sensor_hdr_routes

    install_saved_sensor_hdr_routes(app, service)
    from .saved_sensor_lfm_job import install_routes as install_saved_sensor_lfm_routes

    install_saved_sensor_lfm_routes(app, service)
    from .retained_raw_dataset_job import install_routes as install_retained_raw_dataset_routes

    install_retained_raw_dataset_routes(app, service)
    from . import saved_sensor_isp_replay_job

    saved_sensor_isp_replay_job.install_routes(app, service)
    from . import saved_sensor_calibrated_isp_job

    saved_sensor_calibrated_isp_job.install_routes(app, service)
    from .saved_sensor_perception import install_routes as install_saved_sensor_perception_routes

    install_saved_sensor_perception_routes(app, service)
    from .physics_lut_bridge import install_routes as install_physics_lut_routes

    install_physics_lut_routes(app, service)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.headers.get("host", "")
        try:
            hostname = urlsplit("http://" + host).hostname
        except ValueError:
            hostname = None
        if hostname not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse(
                {"error": "This edition accepts loopback hosts only"}, status_code=403
            )
        origin = request.headers.get("origin")
        if origin and origin != f"http://{host}":
            return JSONResponse({"error": "Cross-origin access is not allowed"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if not secrets.compare_digest(request.headers.get("x-platform-token", ""), token):
                return JSONResponse(
                    {"error": "A current local platform token is required"}, status_code=403
                )
            binary_import = request.url.path in {
                "/api/v1/camera-validation/esfr/captures",
                "/api/v1/calibration-lab/measurements/import",
                "/api/v1/captures/import",
                "/api/v1/captures/control-response/import",
                "/api/v1/captures/nonlinear/import",
                "/api/v1/readout-calibrations/import",
                "/api/v1/readout-observations/import",
                "/api/v1/spectral_images/import",
                "/api/v1/isp-references/import",
                "/api/v1/retained-isp/temporal-references/import",
                "/api/v1/saved-sensor-calibrated-isp/calibrations/import",
                "/api/v1/lens-sensor-calibrated-isp/calibrations/import",
                "/api/v1/lens-sensor-lfm/timing-modes/import",
                "/api/v1/lens-sensor-radiance-scenes/import",
                "/api/v1/native-response-bundles/import",
                "/api/v1/hdr-lfm-reference-comparisons",
            }
            content_type = request.headers.get("content-type", "").split(";")[0]
            expected_type = "multipart/form-data" if binary_import else "application/json"
            if content_type != expected_type:
                return JSONResponse(
                    {"error": f"{expected_type} content type required"}, status_code=415
                )
            # Bound the stream, not only Content-Length (chunked uploads also count).
            blocks = []
            length = 0
            async for block in request.stream():
                length += len(block)
                limit = (
                    256 * 1024 * 1024 + 65536
                    if request.url.path == "/api/v1/camera-validation/esfr/captures" else
                    2 * 1024 * 1024 + 64 * 1024
                    if request.url.path in {
                        "/api/v1/saved-sensor-calibrated-isp/calibrations/import",
                        "/api/v1/lens-sensor-calibrated-isp/calibrations/import",
                    }
                    else 68 * 1024 * 1024 if binary_import else 2_097_152
                )
                if length > limit:
                    return JSONResponse(
                        {"error": "Upload exceeds the endpoint size limit"}, status_code=413
                    )
                blocks.append(block)
            request._body = b"".join(blocks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if not request.url.path.startswith(("/docs", "/redoc")):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            )
        return response

    @app.exception_handler(PreflightError)
    async def preflight_error(request, exc):
        return JSONResponse({"error": str(exc), "preflight": exc.report}, status_code=422)

    @app.exception_handler(ConflictError)
    async def conflict_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(IntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.exception_handler(ValidationError)
    async def schema_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.get("/api/v1/health", tags=["operations"])
    def health():
        worker = app.state.worker
        return {
            "schema": "camera_platform_v1",
            "status": "ok",
            "workspace": str(root),
            "state_dir": str(service.registry.directory),
            "worker": "running" if worker and worker.poll() is None else "external_or_stopped",
        }

    @app.get("/api/v1/bootstrap", tags=["workbench"])
    def bootstrap():
        return {**service.bootstrap(), "token": token}

    @app.get("/api/v1/session", tags=["workbench"])
    def session():
        from . import __version__

        return {
            "schema": "camera_platform_v1",
            "version": __version__,
            "token": token,
            "workspace": str(root),
            "state_dir": str(service.registry.directory),
        }

    @app.get("/api/v1/workbench", tags=["workbench"])
    def workbench():
        from .workbench import bootstrap

        return {**bootstrap(service), "token": token}

    @app.get("/api/v1/workbench/{collection}/{identity}", tags=["workbench"])
    def workbench_detail(collection: str, identity: str):
        from .workbench import detail

        return detail(service, collection, identity)

    @app.get("/api/v1/templates/factory", tags=["model_factory"])
    def factory_template(plugin_id: str = "rayoptics_lens_v1", request_version: int = 1):
        from .factory import template

        return template(service, plugin_id, request_version=request_version)

    @app.post("/api/v1/case_reviews", status_code=201, tags=["engineering_cases"])
    def case_review_create(payload: CaseReviewCreate):
        return service.create_case_review(payload)

    @app.get("/api/v1/case_reviews", tags=["engineering_cases"])
    def case_review_list():
        return service.list_case_reviews()

    @app.get("/api/v1/case_reviews/{identity}/history", tags=["engineering_cases"])
    def case_review_history(identity: str):
        return service.case_review_history(identity)

    @app.get("/api/v1/case_reviews/{identity}", tags=["engineering_cases"])
    def case_review_get(identity: str):
        return service.get_case_review(identity)

    @app.get("/api/v1/templates/spectral-image", tags=["spatial_imaging"])
    def spectral_image_template(assembly_id: str | None = None):
        from .imaging import spectral_template

        return spectral_template(service, assembly_id)

    @app.get("/api/v1/templates/spatial-scene", tags=["spatial_imaging"])
    def spatial_scene_template(assembly_id: str | None = None):
        from .imaging import fixture_template

        return fixture_template(service, assembly_id)

    @app.get("/api/v1/templates/spatial-test", tags=["spatial_imaging"])
    def spatial_test_template(assembly_id: str, scene_id: str):
        from .imaging import spatial_test_template

        return spatial_test_template(service, assembly_id, scene_id)

    @app.get("/api/v1/templates/isp-profile", tags=["isp_replay"])
    def isp_profile_template(assembly_id: str | None = None, version: int = 1,
                             source_kind: str | None = None, source_id: str | None = None,
                             calibration_id: str | None = None, selector: str | None = None):
        if version == 3:
            from .isp_replay_v3 import template

            if not source_kind or not source_id or not selector:
                raise ValueError("Charge ISP template requires an explicit retained RAW selection")
            return template(service, source_kind=source_kind, source_id=source_id,
                            selector=json.loads(selector), calibration_id=calibration_id)
        if version == 2:
            from .isp_replay_v2 import template

            if not source_kind or not source_id:
                raise ValueError("Advanced ISP template needs its retained RAW source")
            return template(service, source_kind=source_kind, source_id=source_id,
                            calibration_id=calibration_id)
        if version != 1:
            raise ValueError("Unknown ISP profile version")
        from .imaging import isp_template

        return isp_template(service, assembly_id)

    @app.get("/api/v1/templates/isp-calibration", tags=["isp_replay"])
    def isp_calibration_template(source_kind: str, source_id: str, version: int = 1,
                                 selector: str | None = None):
        if version == 2:
            from .isp_charge_calibration import template

            if not selector:
                raise ValueError("Charge LSC calibration requires an explicit RAW selection")
            return template(service, source_kind, source_id, json.loads(selector))
        if version != 1:
            raise ValueError("Unknown ISP calibration version")
        from .isp_calibration import template

        return template(service, source_kind, source_id)

    @app.post("/api/v1/isp-calibrations", status_code=201, tags=["isp_replay"])
    def isp_calibration_import(payload: dict):
        if payload.get("schema") == "camera_charge_isp_calibration_v1":
            from .isp_charge_calibration import create

            return create(service, payload)
        from .isp_calibration import import_calibration

        return import_calibration(service, payload)

    @app.get("/api/v1/isp-calibrations", tags=["isp_replay"])
    def isp_calibrations():
        return [isp_calibration(r["id"])
                for r in service.registry.list("isp_calibrations")]

    @app.get("/api/v1/isp-calibrations/{identity}", tags=["isp_replay"])
    def isp_calibration(identity: str):
        record = service.registry.get("isp_calibrations", identity)
        if record.get("schema") == "camera_charge_isp_calibration_v1":
            from .isp_charge_calibration import get

            return get(service, identity)
        from .isp_calibration import get_calibration

        return get_calibration(service, identity)

    @app.get("/api/v1/templates/temporal-scene", tags=["temporal_imaging"])
    def temporal_scene_template(assembly_id: str | None = None):
        from .sequences import scene_template

        return scene_template(service, assembly_id)

    @app.get("/api/v1/templates/temporal-fixture", tags=["temporal_imaging"])
    def temporal_fixture_template(assembly_id: str | None = None):
        from .sequences import fixture_template

        return fixture_template(service, assembly_id)

    @app.get("/api/v1/templates/control-sequence", tags=["temporal_imaging"])
    def control_sequence_template(assembly_id: str, scene_sequence_id: str):
        from .sequences import control_template

        return control_template(service, assembly_id, scene_sequence_id)

    @app.get("/api/v1/templates/temporal-test", tags=["temporal_imaging"])
    def temporal_test_template(
        assembly_id: str,
        scene_sequence_id: str,
        control_sequence_id: str,
        capture_program_id: str | None = None,
        unmodulated_scene_sequence_id: str | None = None,
        capture_row: int | None = None,
        capture_col: int | None = None,
        capture_height: int | None = None,
        capture_width: int | None = None,
    ):
        from .sequences import test_template

        roi = (capture_row, capture_col, capture_height, capture_width)
        if any(value is not None for value in roi) and any(value is None for value in roi):
            raise ValueError("Receiver ROI requires row, col, height and width together")
        capture_crop = (
            {"crop_origin": [capture_row, capture_col],
             "shape": [capture_height, capture_width]}
            if all(value is not None for value in roi) else None
        )
        return test_template(
            service, assembly_id, scene_sequence_id, control_sequence_id, capture_program_id,
            unmodulated_scene_sequence_id, capture_crop,
        )

    @app.get("/api/v1/templates/capture-program", tags=["electrical_hdr"])
    def capture_program_template(assembly_id: str, mode: str = "independent_bracket",
                                 control_sequence_id: str | None = None):
        from .capture_programs import template

        return template(service, assembly_id, mode, control_sequence_id)

    @app.post("/api/v1/capture_programs", status_code=201, tags=["electrical_hdr"])
    def create_capture_program(payload: CaptureProgramCreate):
        from .capture_programs import create

        return create(service, payload.model_dump())

    @app.get("/api/v1/templates/control-policy", tags=["camera_control"])
    def control_policy_template(scene_sequence_id: str):
        from .control_service import policy_template

        return policy_template(service, scene_sequence_id)

    @app.get("/api/v1/templates/control-program", tags=["camera_control"])
    def control_program_template(
        assembly_id: str, scene_sequence_id: str, control_policy_id: str, frame_count: int = 8
    ):
        from .control_service import program_template

        return program_template(
            service, assembly_id, scene_sequence_id, control_policy_id, frame_count
        )

    @app.get("/api/v1/templates/closed-loop-test", tags=["camera_control"])
    def closed_loop_test_template(control_program_id: str):
        from .control_service import test_template

        return test_template(service, control_program_id)

    @app.get("/api/v1/templates/native-pixel-model", tags=["contracts"])
    def native_pixel_model_template(base_sensor_model_id: str):
        from .native_templates import native_model_template

        return native_model_template(service, base_sensor_model_id)

    @app.get("/api/v1/templates/isp-study", tags=["isp_experiments"])
    def isp_study_template(
        source_kind: str, source_id: str, baseline_isp_model_id: str,
        calibration_id: str | None = None, version: int = 1, selector: str | None = None
    ):
        if version == 2:
            from .isp_studies_v2 import template

            if not selector:
                raise ValueError("Charge ISP Study requires an explicit RAW selection")
            return template(service, source_kind, source_id, calibration_id,
                            baseline_isp_model_id, json.loads(selector))
        if version != 1 or not calibration_id:
            raise ValueError("ISP Study v1 requires its calibration ID")
        from .isp_studies import template

        return template(service, source_kind, source_id, calibration_id, baseline_isp_model_id)

    @app.get("/api/v1/templates/nonlinear-capture", tags=["measurement"])
    def nonlinear_capture_template(
        assembly_id: str, test_id: str, readout_mode: str = "sequential_frames"
    ):
        from .nonlinear_captures import template

        return template(service, assembly_id, test_id, readout_mode=readout_mode)

    @app.get("/api/v1/templates/readout-calibration", tags=["measurement"])
    def readout_calibration_template(sensor_model_id: str, mode: str = "sequential_frames"):
        from .readout_calibrations import template

        return template(service, sensor_model_id, mode=mode)

    @app.get("/api/v1/templates/readout-observation", tags=["measurement"])
    def readout_observation_template(capture_id: str):
        from .readout_observations import template

        return template(service, capture_id)

    @app.get("/api/v1/templates/retained-isp-model", tags=["retained_isp"])
    def retained_isp_template(source_id: str, readout_id: str):
        from .retained_isp_replay import template

        return template(service, source_id=source_id, readout_id=readout_id)

    @app.get("/api/v1/templates/{kind}", tags=["contracts"])
    def template(kind: str):
        if kind == "measurement":
            return measurement_template(service)
        if kind == "model":
            return model_template(service)
        if kind == "capture":
            from .observations import capture_template

            return capture_template(service)
        raise HTTPException(404, "Unknown template")

    @app.post("/api/v1/assets", status_code=201, tags=["registry"])
    def asset(payload: AssetCreate):
        return service.create_asset(payload.model_dump())

    @app.post("/api/v1/models", status_code=201, tags=["model_factory"])
    def model(payload: ModelCreate):
        return service.create_model(payload.model_dump())

    @app.post("/api/v1/assemblies", status_code=201, tags=["composer"])
    def assembly(payload: AssemblyCreate):
        return service.create_assembly(payload.model_dump())

    @app.post("/api/v1/assemblies/check", tags=["composer"])
    def assembly_check(payload: dict):
        return service.check(payload["assembly_id"])

    @app.post("/api/v1/assemblies/condition-space", tags=["composer"])
    def assembly_condition_space(payload: dict):
        from .condition_space import analyze

        return analyze(service, payload)

    @app.post("/api/v1/tests", status_code=201, tags=["validation"])
    def test(payload: TestCreate):
        return service.create_test(payload.model_dump())

    @app.post("/api/v1/measurements", status_code=201, tags=["validation"])
    def measurement(payload: MeasurementCreate):
        return service.create_measurement(payload.model_dump())

    @app.post("/api/v1/measurements/import", status_code=201, tags=["validation"])
    def measurement_import(payload: MeasurementImport):
        import json

        return service.create_measurement(
            json.loads(payload.document_text), original_bytes=payload.document_text.encode("utf-8")
        )

    @app.post("/api/v1/runs", status_code=202, tags=["runtime"])
    def run(payload: RunCreate):
        return service.submit_run(payload.model_dump())

    @app.post("/api/v1/captures/import", status_code=201, tags=["measurement"])
    def capture_import(
        metadata: Annotated[str, Form()],
        raw: Annotated[UploadFile, File()],
        dark: Annotated[UploadFile | None, File()] = None,
    ):
        from .observations import MAX_CAPTURE_BYTES

        if len(metadata.encode()) > 1_048_576:
            raise HTTPException(413, "Acquisition metadata is limited to 1 MiB")
        data = CaptureImport.model_validate_json(metadata).model_dump()
        raw_bytes = raw.file.read(MAX_CAPTURE_BYTES + 1)
        dark_bytes = dark.file.read(MAX_CAPTURE_BYTES + 1) if dark is not None else None
        return service.import_capture(data, raw_bytes, dark_bytes)

    @app.post("/api/v1/control_response_tests/template", tags=["camera_control"])
    def response_test_template(payload: ResponseTestRequest):
        from .control_response_service import test_template

        return test_template(service, payload.model_dump())

    def original_import_metadata(metadata, metadata_file):
        import json

        if (metadata is None) == (metadata_file is None):
            raise HTTPException(
                422, "Provide exactly one metadata string or original metadata_file"
            )
        original = metadata_file.file.read(1_048_577) if metadata_file else metadata.encode()
        if len(original) > 1_048_576:
            raise HTTPException(413, "Import metadata is limited to 1 MiB")
        return json.loads(original.decode("utf-8", errors="strict")), original

    @app.post("/api/v1/captures/nonlinear/import", status_code=201, tags=["measurement"])
    def nonlinear_capture_import(
        raw: Annotated[UploadFile, File()],
        metadata: Annotated[str | None, Form()] = None,
        metadata_file: Annotated[UploadFile | None, File()] = None,
    ):
        from .nonlinear_capture_contracts import MAX_CAPTURE_BYTES
        from .nonlinear_captures import import_capture

        data, original = original_import_metadata(metadata, metadata_file)
        return import_capture(
            service, data, raw.file.read(MAX_CAPTURE_BYTES + 1), metadata_bytes=original
        )

    @app.post("/api/v1/readout-calibrations/import", status_code=201, tags=["measurement"])
    def readout_calibration_import(
        dark_rate: Annotated[UploadFile, File()],
        evidence: Annotated[UploadFile, File()],
        metadata: Annotated[str | None, Form()] = None,
        metadata_file: Annotated[UploadFile | None, File()] = None,
    ):
        from .readout_calibrations import import_calibration

        data, original = original_import_metadata(metadata, metadata_file)
        evidence_bytes = evidence.file.read(2 * 1024 * 1024 + 1)
        if len(evidence_bytes) > 2 * 1024 * 1024:
            raise HTTPException(413, "Original response evidence is limited to 2 MiB")
        return import_calibration(
            service, data, dark_rate.file.read(64 * 1024 * 1024 + 1),
            evidence_bytes, metadata_bytes=original,
        )

    @app.get("/api/v1/readout-calibrations", tags=["measurement"])
    def readout_calibration_list():
        return service.registry.list("readout_calibrations")

    @app.get("/api/v1/readout-calibrations/{identity}", tags=["measurement"])
    def readout_calibration_get(identity: str):
        from .readout_calibrations import get

        return get(service, identity)

    @app.post("/api/v1/readout-observations/import", status_code=201, tags=["measurement"])
    def readout_observation_import(
        evidence: Annotated[UploadFile, File()],
        metadata: Annotated[str | None, Form()] = None,
        metadata_file: Annotated[UploadFile | None, File()] = None,
    ):
        from .readout_observations import import_observations

        data, original = original_import_metadata(metadata, metadata_file)
        evidence_bytes = evidence.file.read(2 * 1024 * 1024 + 1)
        if len(evidence_bytes) > 2 * 1024 * 1024:
            raise HTTPException(413, "Original charge-reference evidence is limited to 2 MiB")
        return import_observations(service, data, evidence_bytes, metadata_bytes=original)

    @app.get("/api/v1/readout-observations", tags=["measurement"])
    def readout_observations_list():
        return service.registry.list("readout_observations")

    @app.get("/api/v1/readout-observations/{identity}", tags=["measurement"])
    def readout_observations_get(identity: str):
        from .readout_observations import get

        return get(service, identity)

    @app.post("/api/v1/readout-fits/plan", tags=["model_factory"])
    def readout_fit_plan(payload: dict):
        from .readout_fits import plan

        return plan(service, payload)

    @app.post("/api/v1/readout-fits", status_code=202, tags=["model_factory"])
    def readout_fit_submit(payload: dict):
        from .readout_fits import submit

        if set(payload) != {"specification", "plan_hash"}:
            raise ValueError("Readout fitting requires a checked specification and plan_hash")
        return submit(service, payload["specification"], plan_hash=payload["plan_hash"])

    @app.get("/api/v1/readout-fits", tags=["model_factory"])
    def readout_fit_list():
        return [r for r in service.registry.list_runs() if r.get("job_kind") == "readout_fit"]

    @app.get("/api/v1/readout-fits/{identity}", tags=["model_factory"])
    def readout_fit_get(identity: str):
        from .readout_fits import get_job

        return get_job(service, identity)

    @app.post("/api/v1/readout-fits/{identity}/cancel", tags=["model_factory"])
    def readout_fit_cancel(identity: str):
        readout_fit_get(identity)
        return service.registry.cancel(identity)

    @app.post("/api/v1/readout-fits/{identity}/publish", status_code=201, tags=["model_factory"])
    def readout_fit_publish(identity: str, payload: dict):
        from .readout_fitted_calibrations import publish

        return publish(service, identity, payload)

    @app.get("/api/v1/readout-fits/{identity}/export", tags=["evidence"])
    def readout_fit_export(identity: str):
        from .readout_fits import export

        return export(service, identity)

    @app.get("/api/v1/readout-fits/{identity}/bundle", tags=["evidence"])
    def readout_fit_bundle(identity: str):
        from .readout_fits import bundle

        target = bundle(service, identity)
        return FileResponse(
            target, media_type="application/zip", filename=f"{identity}.zip",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    @app.post("/api/v1/capture_analyses", status_code=201, tags=["measurement"])
    def capture_analysis(payload: CaptureAnalysisRequest):
        from .capture_analysis import create

        return create(service, payload.model_dump())

    @app.post("/api/v1/captures/control-response/import", status_code=201, tags=["measurement"])
    def control_response_import(
        raw: Annotated[UploadFile, File()],
        metadata: Annotated[str | None, Form()] = None,
        metadata_file: Annotated[UploadFile | None, File()] = None,
    ):
        from .control_observation import ResponseCaptureImport
        from .control_response_service import import_capture
        from .observations import MAX_CAPTURE_BYTES

        if (metadata is None) == (metadata_file is None):
            raise HTTPException(
                422, "Provide exactly one metadata string or original metadata_file"
            )
        original = metadata_file.file.read(1_048_577) if metadata_file else metadata.encode()
        if len(original) > 1_048_576:
            raise HTTPException(413, "Acquisition metadata is limited to 1 MiB")
        metadata = original.decode("utf-8", errors="strict")
        data = ResponseCaptureImport.model_validate_json(metadata).model_dump()
        return import_capture(
            service, data, raw.file.read(MAX_CAPTURE_BYTES + 1), original_text=metadata
        )

    @app.get("/api/v1/control_response_tests/{test_id}/acquisition-template", tags=["measurement"])
    def control_response_acquisition_template(test_id: str):
        from .control_response_service import acquisition_template

        return acquisition_template(service, test_id)

    @app.post("/api/v1/scene_assets", status_code=201, tags=["scene_rendering"])
    def scene_asset_create(payload: SceneAssetCreate):
        from .scene_render import create_asset

        return create_asset(service, payload.model_dump())

    @app.get("/api/v1/scene_renders/catalog", tags=["scene_rendering"])
    def scene_render_catalog():
        from .scene_render import catalog

        return catalog()

    @app.get("/api/v1/scene_authoring/templates", tags=["scene_rendering"])
    def scene_authoring_templates():
        from .scene_templates import templates

        return templates(service)

    @app.get("/api/v1/scene_authoring/render-template", tags=["scene_rendering"])
    def scene_authoring_render_template(scene_id: str, assembly_id: str):
        from .scene_templates import render_template

        return render_template(service, scene_id, assembly_id)

    @app.get("/api/v1/scene_authoring/camera-template", tags=["scene_rendering"])
    def scene_camera_template(scene_id: str, assembly_id: str):
        from .scene_camera import template

        return template(service, scene_id, assembly_id)

    @app.post("/api/v1/scene_authoring/camera-preview", tags=["scene_rendering"])
    def scene_camera_preview(payload: SceneCameraDraft):
        from .scene_camera import preview

        return preview(service, payload.model_dump())

    @app.post("/api/v1/scene_authoring/cameras", status_code=201, tags=["scene_rendering"])
    def scene_camera_save(payload: SceneCameraSave):
        from .scene_camera import save

        return save(service, payload.model_dump())

    @app.post("/api/v1/scene_renders", status_code=202, tags=["scene_rendering"])
    def scene_render_submit(payload: SceneRenderCreate):
        from .scene_render import submit

        return submit(service, payload.model_dump())

    @app.get("/api/v1/scene_renders", tags=["scene_rendering"])
    def scene_render_list():
        return [
            r for r in service.registry.list_runs() if r.get("job_kind") == "spectral_scene_render"
        ]

    @app.get("/api/v1/scene_renders/{identity}", tags=["scene_rendering"])
    def scene_render_get(identity: str):
        from .scene_render import get_job

        return get_job(service, identity)

    @app.post("/api/v1/scene_renders/{identity}/cancel", tags=["scene_rendering"])
    def scene_render_cancel(identity: str):
        from .scene_render import get_job

        get_job(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/scene_renders/{identity}/bundle", tags=["scene_rendering"])
    def scene_render_bundle(identity: str):
        from .evidence import build_bundle
        from .scene_render import get_job

        get_job(service, identity)
        path = build_bundle(service, "scene_renders", identity)
        return FileResponse(
            path,
            media_type="application/zip",
            filename="scene-render-evidence.zip",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    @app.post("/api/v1/scene_render_publications", status_code=201, tags=["scene_rendering"])
    def scene_render_publish(payload: RenderPublish):
        from .scene_render import publish

        return publish(service, payload.model_dump())

    @app.get("/api/v1/scene_authoring/projection-template", tags=["scene_projection"])
    def scene_projection_template(source_render_id: str, assembly_id: str):
        from .scene_projection import template

        return template(service, source_render_id, assembly_id)

    @app.post("/api/v1/scene_projections/preview", tags=["scene_projection"])
    def scene_projection_preview(payload: SceneProjectionCreate):
        from .scene_projection import preview

        return preview(service, payload.model_dump())

    @app.post("/api/v1/scene_projections", status_code=202, tags=["scene_projection"])
    def scene_projection_submit(payload: SceneProjectionSubmit):
        from .scene_projection import submit

        return submit(service, payload.model_dump())

    @app.get("/api/v1/scene_projections", tags=["scene_projection"])
    def scene_projection_list():
        return [r for r in service.registry.list_runs()
                if r.get("job_kind") == "spectral_scene_projection"]

    @app.get("/api/v1/scene_projections/{identity}", tags=["scene_projection"])
    def scene_projection_get(identity: str):
        from .scene_projection import get_job

        return get_job(service, identity)

    @app.post("/api/v1/scene_projections/{identity}/cancel", tags=["scene_projection"])
    def scene_projection_cancel(identity: str):
        from .scene_projection import get_job

        get_job(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/scene_projections/{identity}/bundle", tags=["scene_projection"])
    def scene_projection_bundle(identity: str):
        from .evidence import build_bundle
        from .scene_projection import get_job

        get_job(service, identity)
        path = build_bundle(service, "scene_projections", identity)
        return FileResponse(path, media_type="application/zip",
                            filename="radiance-projection-evidence.zip",
                            background=BackgroundTask(path.unlink, missing_ok=True))

    @app.post("/api/v1/scene_projection_publications", status_code=201, tags=["scene_projection"])
    def scene_projection_publish(payload: RenderPublish):
        from .scene_projection import publish

        return publish(service, payload.model_dump())

    @app.post("/api/v1/spectral_images/import", status_code=201, tags=["spatial_imaging"])
    def spectral_image_import(
        metadata: Annotated[str, Form()],
        image: Annotated[UploadFile, File()],
    ):
        from .imaging import MAX_IMAGE_BYTES, import_spectral

        if len(metadata.encode("utf-8")) > 1_048_576:
            raise HTTPException(413, "Spectral image metadata is limited to 1 MiB")
        data = SpectralImageImport.model_validate_json(metadata).model_dump()
        content = image.file.read(MAX_IMAGE_BYTES + 1)
        if len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "Spectral NPY is limited to 64 MiB")
        return import_spectral(service, data, content)

    @app.post("/api/v1/spectral_images/fixture", status_code=201, tags=["spatial_imaging"])
    def spectral_fixture(payload: SpectralFixtureCreate):
        from .imaging import create_fixture

        return create_fixture(service, payload.model_dump())

    @app.post("/api/v1/spectral_images/finite_plane", status_code=201, tags=["spatial_imaging"])
    def finite_plane_fixture(payload: PlaneFixtureCreate):
        from .finite_plane import create

        return create(service, payload.model_dump())

    @app.post("/api/v1/scene_sequences/fixture", status_code=201, tags=["temporal_imaging"])
    def temporal_fixture(payload: TemporalFixtureCreate):
        from .sequences import create_fixture

        return create_fixture(service, payload.model_dump())

    @app.post("/api/v1/scene_sequences", status_code=201, tags=["temporal_imaging"])
    def scene_sequence(payload: SceneSequenceCreate):
        from .sequences import create_sequence

        return create_sequence(service, "scene_sequences", payload.model_dump())

    @app.post("/api/v1/control_sequences", status_code=201, tags=["temporal_imaging"])
    def control_sequence(payload: ControlSequenceCreate):
        from .sequences import create_sequence

        return create_sequence(service, "control_sequences", payload.model_dump())

    @app.post("/api/v1/scene_sequences/import", status_code=201, tags=["temporal_imaging"])
    def scene_sequence_import(payload: MeasurementImport):
        from .sequences import import_sequence

        return import_sequence(service, "scene_sequences", payload.document_text)

    @app.post("/api/v1/control_sequences/import", status_code=201, tags=["temporal_imaging"])
    def control_sequence_import(payload: MeasurementImport):
        from .sequences import import_sequence

        return import_sequence(service, "control_sequences", payload.document_text)

    @app.post("/api/v1/control_policies", status_code=201, tags=["camera_control"])
    def control_policy(payload: ControlPolicyCreate):
        from .control_service import create_policy

        return create_policy(service, payload.model_dump())

    @app.post("/api/v1/control_programs/check", tags=["camera_control"])
    def control_program_check(payload: ControlProgramCreate):
        from .control_service import check_program

        return check_program(service, payload.model_dump())

    @app.post("/api/v1/control_programs", status_code=201, tags=["camera_control"])
    def control_program(payload: ControlProgramCreate):
        from .control_service import create_program

        return create_program(service, payload.model_dump())

    @app.post("/api/v1/control_policies/import", status_code=201, tags=["camera_control"])
    def control_policy_import(payload: MeasurementImport):
        from .control_service import import_record

        return import_record(service, "control_policies", payload.document_text)

    @app.post("/api/v1/control_programs/import", status_code=201, tags=["camera_control"])
    def control_program_import(payload: MeasurementImport):
        from .control_service import import_record

        return import_record(service, "control_programs", payload.document_text)

    @app.get("/api/v1/retained-isp/workbench", tags=["retained_isp"])
    def retained_isp_workbench():
        from .retained_isp_workbench import build

        return build(service)

    @app.get("/api/v1/retained-isp/sources", tags=["retained_isp"])
    def retained_isp_sources():
        from .retained_isp_replay import choices

        return choices(service)

    @app.post("/api/v1/retained-isp/raw-capture-comparisons", tags=["retained_isp"])
    def retained_raw_capture_comparison(payload: dict):
        return service.compare_retained_raw_capture(payload)

    @app.get("/api/v1/retained-isp/temporal-references/template", tags=["retained_isp"])
    def temporal_reference_template(source_id: str, readout_id: str, isp_model_id: str):
        from .temporal_signal_reference import template

        return template(service, source_id, readout_id, isp_model_id)

    @app.post("/api/v1/retained-isp/temporal-references/import", status_code=201,
              tags=["retained_isp"])
    def temporal_reference_import(
        metadata: Annotated[str, Form()], image: Annotated[UploadFile, File()]
    ):
        from .temporal_signal_reference import (
            MAX_METADATA_BYTES,
            MAX_REFERENCE_BYTES,
            import_reference,
        )

        if len(metadata.encode()) > MAX_METADATA_BYTES:
            raise HTTPException(413, "Temporal reference metadata exceeds its byte limit")
        return import_reference(
            service, json.loads(metadata), image.file.read(MAX_REFERENCE_BYTES + 1)
        )

    @app.get("/api/v1/retained-isp/temporal-references", tags=["retained_isp"])
    def temporal_reference_list():
        from .temporal_signal_reference import get_reference

        return [get_reference(service, record["id"])
                for record in service.registry.list("temporal_signal_references")]

    @app.get("/api/v1/retained-isp/temporal-references/{identity}/test-template",
             tags=["retained_isp"])
    def temporal_reference_test_template(identity: str):
        from .temporal_signal_reference import test_template

        return test_template(service, identity)

    @app.get("/api/v1/retained-isp/temporal-references/{identity}", tags=["retained_isp"])
    def temporal_reference_get(identity: str):
        from .temporal_signal_reference import get_reference

        return get_reference(service, identity)

    @app.post("/api/v1/retained-isp/comparisons/plan", tags=["retained_isp"])
    def retained_isp_comparison_plan(payload: dict):
        from .retained_isp_comparison import plan

        return plan(service, payload)

    @app.post("/api/v1/retained-isp/comparisons", status_code=202, tags=["retained_isp"])
    def retained_isp_comparison_submit(payload: dict):
        from .retained_isp_comparison import submit

        if set(payload) != {"specification", "plan_hash"}:
            raise ValueError("Retained comparison submission requires specification and plan_hash")
        return submit(service, payload["specification"], payload["plan_hash"])

    @app.get("/api/v1/retained-isp/comparisons", tags=["retained_isp"])
    def retained_isp_comparison_list():
        from .retained_isp_comparison import list_jobs

        return list_jobs(service)

    @app.get("/api/v1/retained-isp/comparisons/{identity}", tags=["retained_isp"])
    def retained_isp_comparison_get(identity: str):
        from .retained_isp_comparison import get

        return get(service, identity)

    @app.post("/api/v1/retained-isp/comparisons/{identity}/cancel", tags=["retained_isp"])
    def retained_isp_comparison_cancel(identity: str):
        from .retained_isp_comparison import cancel

        return cancel(service, identity)

    @app.get("/api/v1/retained-isp/comparisons/{identity}/export", tags=["evidence"])
    def retained_isp_comparison_export(identity: str):
        from .retained_isp_comparison import export_manifest

        return export_manifest(service, identity)

    @app.get("/api/v1/retained-isp/comparisons/{identity}/bundle", tags=["evidence"])
    def retained_isp_comparison_bundle(identity: str):
        from .retained_isp_comparison import export_bundle

        target = export_bundle(service, identity)
        return FileResponse(target, media_type="application/zip", filename=f"{identity}.zip",
                            background=BackgroundTask(target.unlink, missing_ok=True))

    @app.post("/api/v1/retained-isp/joint-studies/plan", tags=["retained_isp"])
    def retained_isp_joint_plan(payload: dict):
        from .retained_isp_joint_workflow import plan

        return plan(service, payload)

    @app.post("/api/v1/retained-isp/joint-studies", status_code=202,
              tags=["retained_isp"])
    def retained_isp_joint_submit(payload: dict):
        from .retained_isp_joint_workflow import submit

        if set(payload) != {"specification", "plan_hash"}:
            raise ValueError("Joint Study submission requires specification and plan_hash")
        return submit(service, payload["specification"], payload["plan_hash"])

    @app.get("/api/v1/retained-isp/joint-studies", tags=["retained_isp"])
    def retained_isp_joint_list():
        from .retained_isp_joint_workflow import list_jobs

        return list_jobs(service)

    @app.get("/api/v1/retained-isp/joint-studies/{identity}", tags=["retained_isp"])
    def retained_isp_joint_get(identity: str):
        from .retained_isp_joint_workflow import get

        return get(service, identity)

    @app.post("/api/v1/retained-isp/joint-studies/{identity}/cancel",
              tags=["retained_isp"])
    def retained_isp_joint_cancel(identity: str):
        from .retained_isp_joint_workflow import cancel

        return cancel(service, identity)

    @app.get("/api/v1/retained-isp/joint-studies/{identity}/export", tags=["evidence"])
    def retained_isp_joint_export(identity: str):
        from .retained_isp_joint_workflow import export_manifest

        return export_manifest(service, identity)

    @app.get("/api/v1/retained-isp/joint-studies/{identity}/bundle", tags=["evidence"])
    def retained_isp_joint_bundle(identity: str):
        from .retained_isp_joint_workflow import export_bundle

        target = export_bundle(service, identity)
        return FileResponse(target, media_type="application/zip", filename=f"{identity}.zip",
                            background=BackgroundTask(target.unlink, missing_ok=True))

    @app.post("/api/v1/retained-isp/observable-optimizer/selections/plan",
              tags=["retained_isp"])
    def observable_isp_selection_plan(payload: dict):
        from .retained_isp_observable_workflow import plan_selection

        return plan_selection(service, payload)

    @app.post("/api/v1/retained-isp/observable-optimizer/selections", status_code=202,
              tags=["retained_isp"])
    def observable_isp_selection_submit(payload: dict):
        from .retained_isp_observable_workflow import submit_selection

        if set(payload) != {"specification", "plan_hash"}:
            raise ValueError("Observable ISP selection needs specification and plan_hash")
        return submit_selection(service, payload["specification"], payload["plan_hash"])

    @app.get("/api/v1/retained-isp/observable-optimizer/selections",
             tags=["retained_isp"])
    def observable_isp_selection_list():
        from .retained_isp_observable_workflow import list_selection_jobs

        return list_selection_jobs(service)

    @app.get("/api/v1/retained-isp/observable-optimizer/selections/{identity}",
             tags=["retained_isp"])
    def observable_isp_selection_get(identity: str):
        from .retained_isp_observable_workflow import get_selection_job

        return get_selection_job(service, identity)

    @app.get("/api/v1/retained-isp/observable-optimizer/selections/{identity}/export",
             tags=["evidence"])
    def observable_isp_selection_export(identity: str):
        from .retained_isp_observable_workflow import export_selection

        return export_selection(service, identity)

    @app.post("/api/v1/retained-isp/observable-optimizer/holdouts/plan",
              tags=["retained_isp"])
    def observable_isp_holdout_plan(payload: dict):
        from .retained_isp_observable_workflow import plan_holdout

        if set(payload) != {"selection_job_id"}:
            raise ValueError("Observable ISP holdout planning needs selection_job_id")
        return plan_holdout(service, payload["selection_job_id"])

    @app.post("/api/v1/retained-isp/observable-optimizer/holdouts", status_code=202,
              tags=["retained_isp"])
    def observable_isp_holdout_submit(payload: dict):
        from .retained_isp_observable_workflow import submit_holdout

        if set(payload) != {"selection_job_id", "plan_hash"}:
            raise ValueError("Observable ISP holdout needs selection_job_id and plan_hash")
        return submit_holdout(service, payload["selection_job_id"], payload["plan_hash"])

    @app.get("/api/v1/retained-isp/observable-optimizer/holdouts",
             tags=["retained_isp"])
    def observable_isp_holdout_list():
        from .retained_isp_observable_workflow import list_holdout_jobs

        return list_holdout_jobs(service)

    @app.get("/api/v1/retained-isp/observable-optimizer/holdouts/{identity}",
             tags=["retained_isp"])
    def observable_isp_holdout_get(identity: str):
        from .retained_isp_observable_workflow import get_holdout_job

        return get_holdout_job(service, identity)

    @app.get("/api/v1/retained-isp/observable-optimizer/holdouts/{identity}/export",
             tags=["evidence"])
    def observable_isp_holdout_export(identity: str):
        from .retained_isp_observable_workflow import export_holdout

        return export_holdout(service, identity)

    @app.post("/api/v1/retained-isp/prospective-optimizer/selections/plan",
              tags=["retained_isp"])
    def prospective_isp_selection_plan(payload: dict):
        from .retained_isp_prospective_workflow import plan_selection

        return plan_selection(service, payload)

    @app.post("/api/v1/retained-isp/prospective-optimizer/selections", status_code=202,
              tags=["retained_isp"])
    def prospective_isp_selection_submit(payload: dict):
        from .retained_isp_prospective_workflow import submit_selection

        if set(payload) != {"specification", "plan_hash"}:
            raise ValueError("Prospective ISP selection needs specification and plan_hash")
        return submit_selection(service, payload["specification"], payload["plan_hash"])

    @app.get("/api/v1/retained-isp/prospective-optimizer/selections",
             tags=["retained_isp"])
    def prospective_isp_selection_list():
        from .retained_isp_prospective_workflow import list_selection_jobs

        return list_selection_jobs(service)

    @app.get("/api/v1/retained-isp/prospective-optimizer/selections/{identity}",
             tags=["retained_isp"])
    def prospective_isp_selection_get(identity: str):
        from .retained_isp_prospective_workflow import get_selection_job

        return get_selection_job(service, identity)

    @app.get("/api/v1/retained-isp/prospective-optimizer/selections/{identity}/export",
             tags=["evidence"])
    def prospective_isp_selection_export(identity: str):
        from .retained_isp_prospective_workflow import export_selection

        return export_selection(service, identity)

    @app.post("/api/v1/retained-isp/prospective-optimizer/holdouts/plan",
              tags=["retained_isp"])
    def prospective_isp_holdout_plan(payload: dict):
        from .retained_isp_prospective_workflow import plan_holdout

        if set(payload) != {"selection_job_id", "holdout_joint_study_id"}:
            raise ValueError("Prospective holdout needs selection and Joint Study IDs")
        return plan_holdout(
            service, payload["selection_job_id"], payload["holdout_joint_study_id"],
        )

    @app.post("/api/v1/retained-isp/prospective-optimizer/holdouts", status_code=202,
              tags=["retained_isp"])
    def prospective_isp_holdout_submit(payload: dict):
        from .retained_isp_prospective_workflow import submit_holdout

        if set(payload) != {"selection_job_id", "holdout_joint_study_id", "plan_hash"}:
            raise ValueError("Prospective holdout needs selection, Joint Study and plan_hash")
        return submit_holdout(
            service, payload["selection_job_id"], payload["holdout_joint_study_id"],
            payload["plan_hash"],
        )

    @app.get("/api/v1/retained-isp/prospective-optimizer/holdouts",
             tags=["retained_isp"])
    def prospective_isp_holdout_list():
        from .retained_isp_prospective_workflow import list_holdout_jobs

        return list_holdout_jobs(service)

    @app.get("/api/v1/retained-isp/prospective-optimizer/holdouts/{identity}",
             tags=["retained_isp"])
    def prospective_isp_holdout_get(identity: str):
        from .retained_isp_prospective_workflow import get_holdout_job

        return get_holdout_job(service, identity)

    @app.get("/api/v1/retained-isp/prospective-optimizer/holdouts/{identity}/export",
             tags=["evidence"])
    def prospective_isp_holdout_export(identity: str):
        from .retained_isp_prospective_workflow import export_holdout

        return export_holdout(service, identity)

    @app.get("/api/v1/electrical-response/template", tags=["electrical_response"])
    def electrical_response_template(assembly_id: str, scene_sequence_id: str,
                                     control_sequence_id: str, capture_program_id: str):
        from .electrical_response_study import template

        return template(service, assembly_id, scene_sequence_id, control_sequence_id,
                        capture_program_id)

    @app.post("/api/v1/electrical-response/studies/plan", tags=["electrical_response"])
    def electrical_response_plan(payload: dict):
        from .electrical_response_study import plan

        return plan(service, payload)

    @app.post("/api/v1/electrical-response/studies", status_code=202, tags=["electrical_response"])
    def electrical_response_submit(payload: dict):
        from .electrical_response_study import submit

        if set(payload) != {"specification", "plan_hash"}:
            raise ValueError("Electrical response submission requires specification and plan_hash")
        return submit(service, payload["specification"], payload["plan_hash"])

    @app.get("/api/v1/electrical-response/studies", tags=["electrical_response"])
    def electrical_response_list():
        from .electrical_response_study import list_jobs

        return list_jobs(service)

    @app.get("/api/v1/electrical-response/studies/{identity}", tags=["electrical_response"])
    def electrical_response_get(identity: str):
        from .electrical_response_study import get

        return get(service, identity)

    @app.post("/api/v1/electrical-response/studies/{identity}/cancel", tags=["electrical_response"])
    def electrical_response_cancel(identity: str):
        from .electrical_response_study import cancel

        return cancel(service, identity)

    @app.get("/api/v1/electrical-response/studies/{identity}/export", tags=["evidence"])
    def electrical_response_export(identity: str):
        from .electrical_response_study import export_manifest

        return export_manifest(service, identity)

    @app.get("/api/v1/electrical-response/studies/{identity}/bundle", tags=["evidence"])
    def electrical_response_bundle(identity: str):
        from .electrical_response_study import export_bundle

        target = export_bundle(service, identity)
        return FileResponse(target, media_type="application/zip", filename=f"{identity}.zip",
                            background=BackgroundTask(target.unlink, missing_ok=True))

    @app.post("/api/v1/lfm/gain-screens", status_code=201, tags=["electrical_response"])
    def lfm_gain_screen_submit(payload: dict):
        from .lfm_gain_screen import submit

        return submit(service, payload)

    @app.post("/api/v1/lfm/candidate-plans", tags=["electrical_response"])
    def lfm_candidate_plan(payload: dict):
        from .lfm_candidate_plan import analyze

        return analyze(service, payload)

    @app.get("/api/v1/lfm/gain-screens", tags=["electrical_response"])
    def lfm_gain_screen_list():
        from .lfm_gain_screen import list_records

        return list_records(service)

    @app.get("/api/v1/lfm/gain-screens/{identity}", tags=["electrical_response"])
    def lfm_gain_screen_get(identity: str):
        from .lfm_gain_screen import get

        return get(service, identity)

    @app.get("/api/v1/replay-sources", tags=["isp_replay"])
    def replay_sources():
        from .imaging import replay_sources

        return replay_sources(service)

    @app.post("/api/v1/replays", status_code=202, tags=["isp_replay"])
    def replay_submit(payload: ReplayCreate | ReplayCreateV3 | RetainedISPReplayCreate):
        from .imaging import submit_replay

        return submit_replay(service, payload.model_dump())

    @app.get("/api/v1/replays", tags=["isp_replay"])
    def replay_jobs():
        return service.list_replays()

    @app.get("/api/v1/replays/{identity}", tags=["isp_replay"])
    def replay_job(identity: str):
        job = service.registry.get_run(identity)
        if job.get("job_kind") != "isp_replay":
            raise HTTPException(404, "Selected job is not an ISP replay")
        if job.get("plugin_id") == "retained_isp_replay_v1":
            from .retained_isp_replay import verify

            verify(service, job)
        if job.get("plugin_id") == "isp_replay_v3":
            from .isp_replay_v3 import verify

            verify(service, job)
        return job

    @app.post("/api/v1/replays/{identity}/cancel", tags=["isp_replay"])
    def replay_cancel(identity: str):
        replay_job(identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/replays/{identity}/export", tags=["evidence"])
    def replay_export(identity: str):
        replay_job(identity)
        return service.export_replay(identity)

    @app.get("/api/v1/replays/{identity}/bundle", tags=["evidence"])
    def replay_bundle(identity: str):
        from .evidence import build_bundle

        replay_job(identity)
        target = build_bundle(service, "replays", identity)
        return FileResponse(
            target,
            media_type="application/zip",
            filename=f"{identity}.zip",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    @app.post("/api/v1/calibrations", status_code=201, tags=["model_factory"])
    def calibration(payload: CalibrationCreate):
        return service.propose_calibration(payload.model_dump())

    @app.get("/api/v1/factory/plugins", tags=["model_factory"])
    def factory_plugins():
        from .factory import list_plugins

        return list_plugins()

    @app.post("/api/v1/factory/jobs", status_code=202, tags=["model_factory"])
    def factory_submit(payload: FactoryJobCreate):
        from .factory import submit

        return submit(service, payload.model_dump())

    @app.get("/api/v1/factory/jobs", tags=["model_factory"])
    def factory_jobs():
        return service.list_factory_jobs()

    @app.get("/api/v1/factory/jobs/{identity}", tags=["model_factory"])
    def factory_job(identity: str):
        from .factory import get_job

        return get_job(service, identity)

    @app.post("/api/v1/factory/jobs/{identity}/cancel", tags=["model_factory"])
    def factory_cancel(identity: str):
        from .factory import get_job

        get_job(service, identity)
        return service.registry.cancel(identity)

    @app.post("/api/v1/factory/models", status_code=201, tags=["model_factory"])
    def factory_model(payload: FactoryModelCreate):
        from .factory import publish

        return publish(service, payload.model_dump())

    @app.post("/api/v1/lens-db-factory-pairs/models", status_code=201,
              tags=["model_factory"])
    def lens_db_factory_pair_model(payload: FactoryPairPublicationCreate):
        return publish_pair(service, payload.model_dump())

    @app.post("/api/v1/runs/{identity}/cancel", tags=["runtime"])
    def cancel(identity: str):
        return service.registry.cancel(identity)

    @app.post("/api/v1/experiments", status_code=201, tags=["experiments"])
    def experiment(payload: ExperimentCreate):
        return service.create_experiment(payload.model_dump())

    from .surrogates import catalog as surrogate_catalog
    from .surrogates import get_model as surrogate_model
    from .surrogates import list_models as surrogate_models

    @app.get("/api/v1/surrogates/catalog", tags=["surrogates"])
    def surrogate_capabilities():
        return surrogate_catalog()

    @app.post("/api/v1/surrogates/plan", tags=["surrogates"])
    def plan_surrogate(payload: SurrogatePlan):
        return service.plan_surrogate(payload.model_dump())

    @app.post("/api/v1/surrogates", status_code=201, tags=["surrogates"])
    def create_surrogate(payload: SurrogateSubmit):
        return service.create_surrogate(payload.model_dump())

    @app.get("/api/v1/surrogates", tags=["surrogates"])
    def list_surrogates():
        return surrogate_models(service)

    @app.get("/api/v1/surrogates/{identity}", tags=["surrogates"])
    def get_surrogate(identity: str):
        return surrogate_model(service, identity)

    @app.post("/api/v1/surrogates/{identity}/predict", status_code=201, tags=["surrogates"])
    def predict_surrogate(identity: str, payload: SurrogatePredict):
        return service.predict_surrogate(identity, payload.model_dump())

    from .studies import catalog as study_catalog

    @app.get("/api/v1/studies/catalog", tags=["experiments"])
    def study_parameters():
        return study_catalog()

    @app.post("/api/v1/studies/plan", tags=["experiments"])
    def plan_study(payload: StudyPlan):
        return service.plan_study(payload.model_dump())

    @app.post("/api/v1/studies", status_code=201, tags=["experiments"])
    def create_study(payload: StudySubmit):
        return service.create_study(payload.model_dump())

    @app.get("/api/v1/studies/{identity}/analysis", tags=["experiments"])
    def analyze_study(identity: str):
        return service.analyze_study(identity)

    @app.get("/api/v1/isp-studies/catalog", tags=["isp_experiments"])
    def isp_study_catalog(version: int = 1):
        if version == 2:
            from .isp_studies_v2 import catalog

            return catalog()
        if version != 1:
            raise ValueError("Unknown ISP Study version")
        from .isp_studies import catalog

        return catalog()

    @app.post("/api/v1/isp-studies/plan", tags=["isp_experiments"])
    def plan_isp_study(payload: dict):
        from .isp_studies import plan

        return plan(service, payload)

    @app.post("/api/v1/isp-studies", status_code=201, tags=["isp_experiments"])
    def submit_isp_study(payload: dict):
        from .isp_studies import submit

        return submit(service, payload)

    @app.get("/api/v1/isp-studies", tags=["isp_experiments"])
    def list_isp_studies():
        from .isp_studies import get_study

        return [get_study(service, r["id"]) for r in service.registry.list("isp_studies")]

    @app.get("/api/v1/isp-studies/{identity}", tags=["isp_experiments"])
    def get_isp_study(identity: str):
        from .isp_studies import get_study

        return get_study(service, identity)

    @app.get("/api/v1/isp-study-cells/{identity}", tags=["isp_experiments"])
    def get_isp_study_cell(identity: str):
        from .isp_studies import get_cell

        return get_cell(service, identity)

    @app.get("/api/v1/isp-studies/{identity}/analysis", tags=["isp_experiments"])
    def analyze_isp_study(identity: str):
        from .isp_studies import analyze

        return analyze(service, identity)

    @app.post("/api/v1/isp-studies/{identity}/cancel", tags=["isp_experiments"])
    def cancel_isp_study(identity: str):
        from .isp_studies import cancel

        return cancel(service, identity)

    @app.post("/api/v1/isp-studies/{identity}/selection", status_code=201,
              tags=["isp_experiments"])
    def select_isp_candidate(identity: str, payload: dict):
        from .isp_studies import select_candidate

        return select_candidate(service, identity, payload)

    @app.get("/api/v1/isp-studies/{identity}/export", tags=["evidence"])
    def export_isp_study(identity: str):
        from .isp_studies import export_manifest

        return export_manifest(service, identity)

    @app.get("/api/v1/isp-studies/{identity}/bundle", tags=["evidence"])
    def isp_study_bundle(identity: str):
        from .isp_studies import export_bundle

        target = export_bundle(service, identity)
        return FileResponse(
            target, media_type="application/zip", filename=f"isp-study-{identity}.zip",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    @app.get("/api/v1/isp-selections", tags=["isp_experiments"])
    def list_isp_selections():
        from .isp_studies import get_selection

        return [get_selection(service, r["id"]) for r in service.registry.list("isp_selections")]

    @app.get("/api/v1/isp-selections/{identity}", tags=["isp_experiments"])
    def get_isp_selection(identity: str):
        from .isp_studies import get_selection

        return get_selection(service, identity)

    @app.post("/api/v1/isp-references/import", status_code=201, tags=["isp_experiments"])
    def import_isp_reference(
        metadata: Annotated[str, Form()], image: Annotated[UploadFile, File()]
    ):
        import json

        from .isp_studies import import_reference

        if len(metadata.encode()) > 1_048_576:
            raise HTTPException(413, "ISP reference metadata is limited to 1 MiB")
        return import_reference(service, json.loads(metadata), image.file.read(64 * 1024**2 + 1))

    @app.get("/api/v1/isp-references", tags=["isp_experiments"])
    def list_isp_references():
        from .isp_studies import get_reference

        return [get_reference(service, r["id"]) for r in service.registry.list("isp_references")]

    @app.get("/api/v1/isp-references/{identity}", tags=["isp_experiments"])
    def get_isp_reference(identity: str):
        from .isp_studies import get_reference

        return get_reference(service, identity)

    @app.post("/api/v1/validations", status_code=201, tags=["validation"])
    def validation(payload: ValidationCreate):
        return service.validate(payload.model_dump())

    @app.post("/api/v1/releases", status_code=201, tags=["release_control"])
    def release(payload: ReleaseCreate):
        return service.create_release(payload.model_dump())

    @app.get("/api/v1/runs/{identity}/export", tags=["evidence"])
    def export_run(identity: str):
        return service.export_run(identity)

    @app.get("/api/v1/releases/{identity}/export", tags=["evidence"])
    def export_release(identity: str):
        return service.export_release(identity)

    @app.get("/api/v1/runs/{identity}/bundle", tags=["evidence"])
    def run_bundle(identity: str):
        from .evidence import build_bundle

        target = build_bundle(service, "runs", identity)
        return FileResponse(
            target,
            media_type="application/zip",
            filename=f"{identity}.zip",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    @app.get("/api/v1/releases/{identity}/bundle", tags=["evidence"])
    def release_bundle(identity: str):
        from .evidence import build_bundle

        target = build_bundle(service, "releases", identity)
        return FileResponse(
            target,
            media_type="application/zip",
            filename=f"{identity}.zip",
            background=BackgroundTask(target.unlink, missing_ok=True),
        )

    @app.get("/api/v1/artifacts/{identity}", tags=["evidence"])
    def artifact(identity: str):
        binding = service.registry.artifact_owner(identity)
        entry = binding["descriptor"]
        if binding["kind"] == "models":
            from .native_model_evidence import verify_models

            if entry not in verify_models(service, [binding["owner"]]):
                raise IntegrityError("Artifact differs from its registered response model")
        return FileResponse(
            binding["path"],
            media_type=entry["media_type"],
            filename=entry["name"],
            content_disposition_type="inline"
            if entry["media_type"] == "image/png"
            else "attachment",
        )

    @app.get("/api/v1/audit", tags=["operations"])
    def audit():
        return service.registry.audit()

    @app.get("/api/v1/legacy-catalog", tags=["registry"])
    def legacy_catalog(q: str = "", limit: int = 40):
        return service.legacy_catalog(q, limit)

    # Specific saved-decision routes must precede /api/v1/{kind}/{identity}.
    from .decision_review import install_routes as install_decision_review

    install_decision_review(app, service.workspace)

    from .component_explorer import install_routes as install_component_explorer

    install_component_explorer(app, service)
    from .design_asset_jobs import install_routes as install_design_asset_jobs

    install_design_asset_jobs(app, service)

    kinds = {
        "assets",
        "models",
        "assemblies",
        "tests",
        "measurements",
        "validations",
        "releases",
        "experiments",
        "studies",
        "surrogate_predictions",
        "captures",
        "capture_analyses",
        "readout_calibrations",
        "readout_observations",
        "calibrations",
        "spectral_images",
        "scene_assets",
        "scene_sequences",
        "control_sequences",
        "control_policies",
        "control_programs",
        "capture_programs",
    }

    @app.get("/api/v1/jobs/{identity}", tags=["jobs"])
    def job_status(identity: str, expected_kind: str | None = None):
        """Common lifecycle view; detailed evidence stays on its typed route."""
        job = service.registry.get_run(identity)
        kind = job.get("job_kind") or "simulation_run"
        if expected_kind is not None and expected_kind != kind:
            raise ValueError("Selected job kind does not match expected kind")
        typed_paths = {
            "sensor_structure_study": "sensor-structure-studies",
            "lens_psf_lut_camera": "lens-psf-cameras",
            "lens_optical_psf_lut": "lens-psf-luts",
            "design_experiment": "design-experiments",
            "r1_timed_acquisition": "r1-timed-acquisitions",
            "db_comparison": "db-comparisons",
            "catalog_quality_comparison": "catalog-quality-comparisons",
            "saved_sensor_quality": "saved-sensor-quality",
            "saved_sensor_search": "saved-sensor-search",
            "lens_sensor_raw": "lens-sensor-raw",
            "lens_sensor_streamed_raw": "lens-sensor-streamed-raw",
            "lens_sensor_out_of_core_raw": "lens-sensor-disk-raw",
            "lens_sensor_registered_roi_disk_raw": "lens-sensor-registered-roi-disk-raw",
            "lens_sensor_frequency_target": "lens-sensor-frequency-target",
            "lens_geometric_pupil_density": "lens-geometric-pupil-density",
            "lens_dense_geometric_psf_candidate": "lens-dense-geometric-psf-candidates",
            "lens_dense_geometric_psf_candidate_v2": "lens-dense-geometric-psf-candidates-v2",
            "lens_dense_geometric_psf_q4_rebin": "lens-dense-geometric-psf-q4-rebins",
            "lens_sensor_dense_psf_sensitivity": "lens-sensor-dense-psf-sensitivity",
            "lens_sensor_q4_psf_sensitivity": "lens-sensor-q4-psf-sensitivity",
            "road_design_study": "road-design-studies",
            "road_hdr_study": "road-hdr-studies",
            "virtual_lab": "virtual-lab/runs",
            "calibration_lab": "calibration-lab/studies",
            "calibration_lab_validation": "calibration-lab/studies",
            "calibration_db_replay": "calibration-lab/db-replays",
            "calibration_db_color_replay": "db-design/color-replays",
            "design_workshop": "design-workshop/runs",
            "reference_workshop": "reference-workshop/runs",
            "design_asset_update": "component-explorer/jobs",
            "pedestrian_braking_study": "pedestrian-braking-studies",
            "lens_sensor_joint_scene_dense_psf_ladder": "lens-sensor-joint-sampling-ladder",
            "lens_sensor_legacy_isp": "lens-sensor-legacy-isp",
            "lens_sensor_quality_study": "lens-sensor-quality-studies",
            "lens_sensor_hdr": "lens-sensor-hdr",
            "lens_sensor_lfm": "lens-sensor-lfm",
            "lens_sensor_pwm_hdr": "lens-sensor-pwm-hdr",
            "hdr_lfm_reference_comparison": "hdr-lfm-reference-comparisons",
            "lens_sensor_perception": "lens-sensor-perception",
            "lens_sensor_raw_cohort": "lens-sensor-raw-cohorts",
            "lens_sensor_isp_replay": "lens-sensor-isp-replays",
            "lens_sensor_calibrated_isp": "lens-sensor-calibrated-isp",
            "lens_sensor_isp_perception": "lens-sensor-isp-perception",
            "lens_sensor_isp_perception_cohort": "lens-sensor-isp-perception-cohorts",
            "lens_sensor_adaptive_demosaic": "lens-sensor-adaptive-demosaic",
            "lens_sensor_isp_detector": "lens-sensor-isp-detectors",
            "saved_sensor_hdr": "saved-sensor-hdr",
            "saved_sensor_lfm": "saved-sensor-lfm",
            "retained_raw_dataset": "retained-raw-datasets",
            "saved_sensor_isp_replay": "saved-sensor-isp-replays",
            "saved_sensor_calibrated_isp": "saved-sensor-calibrated-isp",
            "saved_sensor_perception": "saved-sensor-perception",
            "physics_lut_sensitivity": "physics-lut",
        }
        if kind in typed_paths:
            detail_path = typed_paths[kind] + "/" + identity
        elif kind in NON_CAMERA_JOBS:
            detail_path = None
        else:
            detail_path = "runs/" + identity
        return {
            "schema": "camera_job_status_v1",
            "id": job["id"],
            "job_kind": kind,
            "status": job["status"],
            "error": job.get("error"),
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "detail_path": detail_path,
        }

    @app.get("/api/v1/{kind}", tags=["registry"])
    def listing(kind: str):
        if kind == "runs":
            return service.list_simulation_runs()
        if kind not in kinds:
            raise HTTPException(404, "Unknown collection")
        return service.registry.list(kind)

    @app.get("/api/v1/{kind}/{identity}", tags=["registry"])
    def get_record(kind: str, identity: str):
        if kind == "runs":
            run = service.registry.get_run(identity)
            if run.get("job_kind") in {
                "sensor_structure_study",
                "lens_psf_lut_camera",
                "lens_optical_psf_lut",
                "characterization",
                "isp_replay",
                "surrogate_training",
                "spectral_scene_render",
                "spectral_scene_projection",
                "isp_study_cell",
                "readout_fit",
                "retained_isp_comparison",
                "electrical_response_study",
                "design_experiment",
                "r1_timed_acquisition",
                "db_comparison",
                "catalog_quality_comparison",
                "saved_sensor_quality",
                "saved_sensor_search",
                "lens_sensor_raw",
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
                "virtual_lab",
                "design_workshop",
                "reference_workshop",
                "pedestrian_braking_study",
                "lens_sensor_joint_scene_dense_psf_ladder",
                "lens_sensor_legacy_isp",
                "lens_sensor_quality_study",
                "lens_sensor_hdr",
                "lens_sensor_lfm",
                "lens_sensor_pwm_hdr",
                "hdr_lfm_reference_comparison",
                "lens_sensor_perception",
                "lens_sensor_raw_cohort",
                "lens_sensor_isp_replay",
                "lens_sensor_calibrated_isp",
                "lens_sensor_isp_perception",
                "lens_sensor_isp_perception_cohort",
                "lens_sensor_adaptive_demosaic",
                "lens_sensor_isp_detector",
                "saved_sensor_hdr",
                "saved_sensor_lfm",
                "retained_raw_dataset",
                "saved_sensor_isp_replay",
                "saved_sensor_calibrated_isp",
                "saved_sensor_perception",
                "physics_lut_sensitivity",
            }:
                raise HTTPException(
                    404, "Use the factory, replay or surrogate endpoint for this job kind"
                )
            return run
        if kind not in kinds:
            raise HTTPException(404, "Unknown collection")
        record = service.registry.get(kind, identity)
        if kind == "readout_calibrations":
            from .readout_calibrations import get

            return get(service, identity)
        if kind == "readout_observations":
            from .readout_observations import get

            return get(service, identity)
        if kind == "captures" and record.get("schema") == "camera_nonlinear_capture_v1":
            from .nonlinear_captures import get

            return get(service, identity)
        if kind == "capture_programs" and record.get("payload", {}).get("schema") in {
            "camera_capture_program_v2", "camera_dcg_capture_program_v2",
            "camera_reference_hdr_capture_program_v2", "camera_retained_charge_capture_program_v1",
            "camera_retained_charge_capture_program_v2",
            "camera_retained_charge_capture_program_v3",
            "camera_retained_charge_capture_program_v4",
            "camera_retained_charge_capture_program_v5",
            "camera_electrical_control_capture_program_v1",
            "camera_electrical_control_capture_program_v2",
            "camera_electrical_control_capture_program_v3",
        }:
            from .capture_programs import runtime_for_payload, verify_program

            verify_program(service, record, runtime_for_payload(record["payload"]))
        return record

    static = Path(__file__).parent / "static"

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    @app.get("/{filename}", include_in_schema=False)
    def static_file(filename: str):
        if filename not in {
            "esfr-entry.js",
            "camera-validation-esfr.html",
            "esfr-measured.html", "esfr-measured.js", "esfr-measured.css", "esfr-measured-guide.html",
            "camera-validation-esfr.js",
            "camera-validation-esfr.css",
            "calibration-lab.html",
            "calibration-lab.js",
            "calibration-lab.css",
            "calibration-color.html",
            "calibration-color.js",
            "calibration-color.css",
            "calibration-db-core.html",
            "calibration-unified.html",
            "calibration-unified.js",
            "calibration-db-core.js",
            "calibration-db-core.css",
            "calibration-db-color.html",
            "calibration-db-color.js",
            "calibration-db-color.css",
            "calibration-db-color-status.js",
            "index.html",
            "design-experiments.html",
            "design-experiments.js",
            "design-experiments.css",
            "db-comparisons.html",
            "db-comparisons.js",
            "db-comparisons.css",
            "saved-sensor-workbench.html",
            "saved-sensor-workbench.js",
            "saved-sensor-workbench.css",
            "integration-workbench.html",
            "integration-workbench.js",
            "integration-workbench.css",
            "frequency-target.html",
            "frequency-target.js",
            "frequency-target.css",
            "raw-temporal.html",
            "road-design.html",
            "road-hdr.html",
            "road-hdr.js",
            "design-workshop.html",
            "design-workshop.js",
            "design-workshop.css",
            "reference-workshop.html",
            "reference-workshop.js",
            "reference-workshop.css",
            "reference-sensor-modes.js",
            "reference-ircf.html",
            "reference-ircf.js",
            "reference-ircf.css",
            "design-library.html",
            "design-library.js",
            "design-library.css",
            "design-readiness.js",
            "design-readiness.css",
            "db-design.html",
            "db-design.js",
            "db-design.css",
            "reference-ghost.html",
            "reference-ghost.js",
            "virtual-lab.html",
            "sensor-structure.html",
            "sensor-structure.js",
            "sensor-structure-trend.js",
            "lens-psf-lut.html",
            "lens-psf-lut.js",
            "lens-psf-lut.css",
            "virtual-lab.css",
            "virtual-lab.js",
            "road-design.js",
            "pedestrian-braking.html",
            "pedestrian-braking.js",
            "raw-temporal.js",
            "decision-review.html",
            "decision-review.js",
            "decision-review.css",
            "app.js",
            "dcg.js",
            "isp-v2.js",
            "retained-isp.js",
            "electrical-control.js",
            "electrical-response.js",
            "electrical-response-controller.js",
            "isp-studies.js",
            "style.css",
            "studies.js",
            "surrogates.js",
            "workbench.js",
            "controls.js",
            "control-response.js",
            "capture-analysis.js",
            "response-import.js",
            "nonlinear-captures.js",
            "readout-fitting.js",
            "scenes.js",
            "cases.js",
        }:
            raise HTTPException(404)
        return FileResponse(static / filename)

    return app
