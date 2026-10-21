"""Small synchronous client for humans, notebooks and agents using the same API."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx


class PlatformClient:
    def __init__(self, base_url="http://127.0.0.1:8770", *, timeout=30):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, trust_env=False)
        try:
            response = self.http.get("/api/v1/session")
            if response.status_code == 404:
                response = self.http.get("/api/v1/bootstrap")
            response.raise_for_status()
            bootstrap = response.json()
            if bootstrap.get("schema") != "camera_platform_v1":
                raise ValueError("Endpoint is not a Camera Platform server")
            self.http.headers["X-Platform-Token"] = bootstrap["token"]
        except Exception:
            self.http.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.http.close()

    def get(self, path):
        response = self.http.get("/api/v1/" + path.lstrip("/"))
        response.raise_for_status()
        return response.json()

    def post(self, path, payload):
        response = self.http.post("/api/v1/" + path.lstrip("/"), json=payload)
        response.raise_for_status()
        return response.json()

    def download_artifact(self, artifact, destination, *, max_bytes=128 * 1024 * 1024):
        """Stream a registered blob to disk and publish it only after SHA-256 verification."""
        if not isinstance(artifact, dict):
            raise ValueError("An artifact descriptor is required.")
        checksum = artifact.get("sha256")
        path = artifact.get("url")
        if (
            not isinstance(checksum, str) or len(checksum) != 64
            or any(char not in "0123456789abcdef" for char in checksum)
            or path != "/api/v1/artifacts/" + checksum
            or type(max_bytes) is not int or max_bytes <= 0
        ):
            raise ValueError("Artifact URL, checksum or download limit is invalid.")
        target = Path(destination)
        expected_bytes = artifact.get("bytes")
        if expected_bytes is not None and (
            type(expected_bytes) is not int or expected_bytes < 0
        ):
            raise ValueError("Artifact descriptor byte count is invalid.")
        if expected_bytes is not None and expected_bytes > max_bytes:
            raise ValueError("Artifact exceeds the declared download limit.")
        temporary = None
        size = 0
        digest = hashlib.sha256()
        try:
            # Same-directory staging makes final hard-link publication atomic
            # and never overwrites a pre-existing destination.
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{target.name}.", suffix=".part",
                dir=target.parent, delete=False,
            ) as output:
                temporary = Path(output.name)
                with self.http.stream("GET", path) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError("Artifact exceeds the declared download limit.")
                        output.write(chunk)
                        digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if expected_bytes is not None and size != expected_bytes:
                raise ValueError("Downloaded artifact differs from its byte count descriptor.")
            if digest.hexdigest() != checksum:
                raise ValueError("Downloaded artifact differs from its SHA-256 descriptor.")
            os.link(temporary, target)
            return target
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def submit(
        self,
        assembly_id,
        test_id,
        *,
        plugin_id="response_db",
        parameters=None,
        force=False,
        scene_id=None,
        scene_sequence_id=None,
        control_sequence_id=None,
        control_program_id=None,
        capture_program_id=None,
    ):
        return self.post(
            "runs",
            {
                "assembly_id": assembly_id,
                "test_id": test_id,
                "plugin_id": plugin_id,
                "parameters": parameters or {},
                "force": force,
                "scene_id": scene_id,
                "scene_sequence_id": scene_sequence_id,
                "control_sequence_id": control_sequence_id,
                **(
                    {"control_program_id": control_program_id}
                    if control_program_id is not None
                    else {}
                ),
                **(
                    {"capture_program_id": capture_program_id}
                    if capture_program_id is not None
                    else {}
                ),
            },
        )

    def reassess_sensor_structure(self, identity, policy=None):
        return self.post('sensor-structure-studies/'+identity+'/trend-assessments', {'policy':policy or {}})

    def submit_sensor_structure_trend(self, identity, policy=None):
        return self.post('sensor-structure-studies/'+identity+'/trend-studies', {'policy':policy or {}})

    def sensor_structure_template(self):
        return self.get("sensor-structure-studies/template")

    def submit_sensor_structure_study(self, specification):
        return self.post("sensor-structure-studies", specification)

    def sensor_structure_study(self, identity):
        return self.get("sensor-structure-studies/" + identity)

    def cancel_sensor_structure_study(self, identity):
        return self.post("sensor-structure-studies/" + identity + "/cancel", {})

    def import_sensor_structure_results(self, identity, directory):
        return self.post("sensor-structure-studies/" + identity + "/import", {"directory": directory})

    def job_status(self, identity, *, expected_kind=None):
        """Read the common lifecycle status without releasing typed results."""
        path = "jobs/" + identity
        if expected_kind is not None:
            path += "?" + urlencode({"expected_kind": expected_kind})
        return self.get(path)

    def wait(self, run_id, *, timeout=150, poll_interval=0.5, expected_kind=None):
        """Wait by job ID; return typed detail where this client has a getter.

        Returns verified detail when the job status declares a typed detail
        path. Other specialized jobs return the common lifecycle record; use
        their typed SDK getter for verified results and artifacts.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.job_status(run_id, expected_kind=expected_kind)
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                return self.get(job["detail_path"]) if job["detail_path"] else job
            time.sleep(poll_interval)
        raise TimeoutError(f"Run {run_id} is still active; use its ID to resume polling")

    def compare(self, run_id, measurement_id, test_id, *, allow_calibrated_model=False):
        return self.post(
            "validations",
            {
                "run_id": run_id,
                "measurement_id": measurement_id,
                "test_id": test_id,
                "allow_calibrated_model": allow_calibrated_model,
            },
        )

    def create_scene_asset(self, specification):
        return self.post("scene_assets", specification)

    def create_case_review(self, review):
        """Save a review revision; retain request_id when retrying an uncertain response."""
        return self.post("case_reviews", review)

    def list_case_reviews(self):
        return self.get("case_reviews")

    def get_case_review(self, identity):
        return self.get("case_reviews/" + identity)

    def case_review_history(self, identity):
        return self.get("case_reviews/" + identity + "/history")

    def render_scene(self, specification):
        return self.post("scene_renders", specification)

    def factory_template(self, plugin_id="rayoptics_lens_v1", *, request_version=1):
        """Load v1, passive-stop v2, joint-direction v3, or even-asphere v4 input."""
        from urllib.parse import urlencode

        return self.get(
            "templates/factory?"
            + urlencode(
                {
                    "plugin_id": plugin_id,
                    "request_version": request_version,
                }
            )
        )

    def lens_db_factory_pair_template(self):
        return self.get("lens-db-factory-pairs/template")

    def plan_lens_db_factory_pair(self, specification):
        return self.post("lens-db-factory-pairs/plan", specification)

    def submit_lens_db_factory_pair(self, specification, *, expected_plan_sha256):
        return self.post(
            "lens-db-factory-pairs",
            {
                "specification": specification,
                "expected_plan_sha256": expected_plan_sha256,
            },
        )

    def publish_lens_db_factory_pair(self, publication):
        """Register both completed Factory results as unvalidated draft Lens models."""
        return self.post("lens-db-factory-pairs/models", publication)

    def publish_native_response_bundle(
        self,
        bundle_zip,
        *,
        expected_manifest_sha256,
        lens_model_id,
        readout_model_id,
        name,
        revision,
        license,
    ):
        """Upload a source-bound native pixel response as a draft Sensor model.

        The server checks the ZIP members, manifest hash, exact Lens response,
        and Sensor DB identity before saving the source files and model.
        """
        path = Path(bundle_zip)
        with path.open("rb") as stream:
            response = self.http.post(
                "/api/v1/native-response-bundles/import",
                data={
                    "expected_manifest_sha256": expected_manifest_sha256,
                    "lens_model_id": lens_model_id,
                    "readout_model_id": readout_model_id,
                    "name": name,
                    "revision": revision,
                    "license": license,
                },
                files={"bundle": (path.name, stream, "application/zip")},
            )
        response.raise_for_status()
        return response.json()

    def native_sensor_data_request(
        self, *, lens_model_id, sensor_identifier, run_id=None
    ):
        """Review one saved Sensor revision against a registered Factory Lens."""
        parameters = {
            "lens_model_id": lens_model_id,
            "sensor_identifier": sensor_identifier,
        }
        if run_id is not None:
            parameters["run_id"] = run_id
        return self.get("native-sensor-data-requests?" + urlencode(parameters))

    def native_pixel_model_template(self, base_sensor_model_id):
        from urllib.parse import urlencode

        return self.get(
            "templates/native-pixel-model?"
            + urlencode(
                {
                    "base_sensor_model_id": base_sensor_model_id,
                }
            )
        )

    def preview_scene_camera(self, draft):
        return self.post("scene_authoring/camera-preview", draft)

    def save_scene_camera(self, preview):
        return self.post(
            "scene_authoring/cameras",
            {
                "draft": preview["draft"],
                "preview_sha256": preview["preview_sha256"],
            },
        )

    def wait_scene_render(self, job_id, *, timeout=150, poll_interval=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get("scene_renders/" + job_id)
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                return job
            time.sleep(poll_interval)
        raise TimeoutError(f"Scene render {job_id} is still active; resume polling this ID")

    def publish_scene_render(self, job_id, name, *, revision="1"):
        return self.post(
            "scene_render_publications", {"job_id": job_id, "name": name, "revision": revision}
        )

    def preview_scene_projection(self, specification):
        return self.post("scene_projections/preview", specification)

    def project_scene_radiance(self, preview):
        return self.post(
            "scene_projections",
            {
                "draft": preview["draft"],
                "preview_sha256": preview["preview_sha256"],
            },
        )

    def wait_scene_projection(self, job_id, *, timeout=120, poll_interval=0.25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get("scene_projections/" + job_id)
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                return job
            time.sleep(poll_interval)
        raise TimeoutError(f"Radiance projection {job_id} remains active; resume this ID")

    def publish_scene_projection(self, job_id, name, *, revision="1"):
        return self.post(
            "scene_projection_publications",
            {
                "job_id": job_id,
                "name": name,
                "revision": revision,
            },
        )

    def plan_study(self, specification):
        """Preflight every candidate without creating revisions or jobs."""
        return self.post("studies/plan", specification)

    def submit_study(self, plan):
        """Submit exactly the inspected specification/source/environment identity."""
        return self.post(
            "studies", {"specification": plan["specification"], "plan_hash": plan["plan_hash"]}
        )

    def wait_study(self, study_id, *, timeout=150, poll_interval=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            analysis = self.get("studies/" + study_id + "/analysis")
            if analysis["status"] == "complete":
                return analysis
            time.sleep(poll_interval)
        raise TimeoutError(f"Study {study_id} is still active; resume polling its ID")

    def isp_study_template(
        self, source_kind, source_id, calibration_id, baseline_isp_model_id, *, selector=None
    ):
        query = {
            "source_kind": source_kind,
            "source_id": source_id,
            "baseline_isp_model_id": baseline_isp_model_id,
        }
        if calibration_id is not None:
            query["calibration_id"] = calibration_id
        if selector is not None:
            query.update(version=2, selector=json.dumps(selector, separators=(",", ":")))
        return self.get("templates/isp-study?" + urlencode(query))

    def plan_isp_study(self, specification):
        """Inspect all fixed-RAW candidates without publishing records or jobs."""
        return self.post("isp-studies/plan", specification)

    def submit_isp_study(self, plan):
        """Retry an uncertain submission with this exact specification and plan hash."""
        return self.post(
            "isp-studies",
            {
                "specification": plan["specification"],
                "plan_hash": plan["plan_hash"],
            },
        )

    def wait_isp_study(self, study_id, *, timeout=150, poll_interval=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            analysis = self.get("isp-studies/" + study_id + "/analysis")
            if analysis["status"] == "complete":
                return analysis
            time.sleep(poll_interval)
        raise TimeoutError(f"ISP study {study_id} remains active; resume polling this identity")

    def cancel_isp_study(self, study_id):
        return self.post("isp-studies/" + study_id + "/cancel", {})

    def select_isp_candidate(self, study_id, candidate_id, rationale, analysis_sha256):
        return self.post(
            "isp-studies/" + study_id + "/selection",
            {
                "candidate_id": candidate_id,
                "rationale": rationale,
                "analysis_sha256": analysis_sha256,
            },
        )

    def import_isp_reference(self, metadata, image_path):
        """Preserve explicitly described independent, linear RGB reference NPY bytes."""
        import json
        from pathlib import Path

        path = Path(image_path)
        with path.open("rb") as stream:
            response = self.http.post(
                "/api/v1/isp-references/import",
                data={"metadata": json.dumps(metadata, allow_nan=False)},
                files={"image": (path.name, stream, "application/octet-stream")},
            )
        response.raise_for_status()
        return response.json()

    def isp_study_bundle(self, study_id):
        response = self.http.get("/api/v1/isp-studies/" + study_id + "/bundle")
        response.raise_for_status()
        return response.content

    def import_capture(self, metadata, raw_path, *, dark_path=None):
        """Upload explicit NPY acquisition files to this configured local platform."""
        import json
        from contextlib import ExitStack
        from pathlib import Path

        with ExitStack() as stack:
            raw = Path(raw_path)
            files = {
                "raw": (raw.name, stack.enter_context(raw.open("rb")), "application/octet-stream")
            }
            if dark_path is not None:
                dark = Path(dark_path)
                files["dark"] = (
                    dark.name,
                    stack.enter_context(dark.open("rb")),
                    "application/octet-stream",
                )
            response = self.http.post(
                "/api/v1/captures/import",
                files=files,
                data={"metadata": json.dumps(metadata, allow_nan=False)},
            )
            response.raise_for_status()
            return response.json()

    def characterize(self, payload):
        return self.post("factory/jobs", payload)

    def import_nonlinear_capture(self, metadata_path, raw_path):
        """Preserve nonlinear RAW and original JSON without binding a response calibration."""
        return self._import_nonlinear_files(
            "captures/nonlinear/import", metadata_file=metadata_path, raw=raw_path
        )

    def import_readout_calibration(self, metadata_path, dark_rate_path, evidence_path):
        """Import a separate response version with expected dark rate and charge-scale evidence."""
        return self._import_nonlinear_files(
            "readout-calibrations/import",
            metadata_file=metadata_path,
            dark_rate=dark_rate_path,
            evidence=evidence_path,
        )

    def _import_nonlinear_files(self, endpoint, **paths):
        from contextlib import ExitStack
        from pathlib import Path

        with ExitStack() as stack:
            files = {}
            for key, value in paths.items():
                path = Path(value)
                media = (
                    "application/json"
                    if key in {"metadata_file", "evidence"}
                    else "application/octet-stream"
                )
                files[key] = (path.name, stack.enter_context(path.open("rb")), media)
            response = self.http.post("/api/v1/" + endpoint, files=files)
        response.raise_for_status()
        return response.json()

    def import_readout_observations(self, metadata_path, evidence_path):
        """Preserve independent charge targets and their original reference documents."""
        return self._import_nonlinear_files(
            "readout-observations/import",
            metadata_file=metadata_path,
            evidence=evidence_path,
        )

    def plan_readout_fit(self, specification):
        return self.post("readout-fits/plan", specification)

    def submit_readout_fit(self, specification, plan_hash):
        return self.post(
            "readout-fits",
            {
                "specification": specification,
                "plan_hash": plan_hash,
            },
        )

    def publish_readout_fit(self, identity, *, name, revision, request_sha256, result_sha256):
        return self.post(
            "readout-fits/" + identity + "/publish",
            {
                "name": name,
                "revision": revision,
                "request_sha256": request_sha256,
                "result_sha256": result_sha256,
            },
        )

    def response_test_template(self, control_program_id, **options):
        """Prepare a common Test with a frozen RAW/control-log observation operator."""
        return self.post(
            "control_response_tests/template",
            {
                "control_program_id": control_program_id,
                **options,
            },
        )

    def import_control_response(self, metadata, raw_path):
        """Preserve native RAW plus explicit applied settings, clocks and missing frames."""
        import json
        from pathlib import Path

        raw = Path(raw_path)
        with raw.open("rb") as stream:
            response = self.http.post(
                "/api/v1/captures/control-response/import",
                files={"raw": (raw.name, stream, "application/octet-stream")},
                data={"metadata": json.dumps(metadata, allow_nan=False)},
            )
        response.raise_for_status()
        return response.json()

    def analyze_capture(
        self, capture_id, test_id, *, name, uncertainty, revision="1", parent_analysis_id=None
    ):
        """Create a new observation revision without registering another acquisition."""
        return self.post(
            "capture_analyses",
            {
                "capture_id": capture_id,
                "test_id": test_id,
                "name": name,
                "revision": revision,
                "uncertainty": uncertainty,
                "parent_analysis_id": parent_analysis_id,
            },
        )

    def import_control_response_file(self, metadata_path, raw_path):
        """Upload original UTF-8 JSON bytes, preserving whitespace and newline encoding."""
        from pathlib import Path

        metadata, raw = Path(metadata_path), Path(raw_path)
        with metadata.open("rb") as document, raw.open("rb") as pixels:
            response = self.http.post(
                "/api/v1/captures/control-response/import",
                files={
                    "metadata_file": (metadata.name, document, "application/json"),
                    "raw": (raw.name, pixels, "application/octet-stream"),
                },
            )
        response.raise_for_status()
        return response.json()

    def template(self, kind, **parameters):
        """Fetch an API template, including native scene/Test or ISP profile defaults."""
        response = self.http.get("/api/v1/templates/" + kind, params=parameters)
        response.raise_for_status()
        return response.json()

    def plan_surrogate(self, specification):
        return self.post("surrogates/plan", specification)

    def submit_surrogate(self, plan):
        return self.post(
            "surrogates", {"specification": plan["specification"], "plan_hash": plan["plan_hash"]}
        )

    def wait_surrogate(self, identity, *, timeout=150, poll_interval=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get("surrogates/" + identity)
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                return job
            time.sleep(poll_interval)
        raise TimeoutError(f"Surrogate {identity} is still active; resume polling its ID")

    def predict_surrogate(self, identity, points, units):
        return self.post("surrogates/" + identity + "/predict", {"points": points, "units": units})

    def import_spectral_image(self, metadata, image_path):
        """Upload an explicit spectral irradiance NPY; no RGB-to-spectrum conversion."""
        import json
        from pathlib import Path

        path = Path(image_path)
        with path.open("rb") as stream:
            response = self.http.post(
                "/api/v1/spectral_images/import",
                data={"metadata": json.dumps(metadata, allow_nan=False)},
                files={"image": (path.name, stream, "application/octet-stream")},
            )
        response.raise_for_status()
        return response.json()

    def create_spectral_fixture(self, payload):
        """Create a labeled synthetic ideal-image fixture with native sampling."""
        return self.post("spectral_images/fixture", payload)

    def create_temporal_fixture(self, payload):
        """Create an explicitly synthetic ideal-image timeline, not hardware evidence."""
        return self.post("scene_sequences/fixture", payload)

    def create_scene_sequence(self, payload):
        """Register fixed spectral-image references with their declared clock and timing."""
        return self.post("scene_sequences", payload)

    def create_control_sequence(self, payload):
        """Register recorded applied settings independently from any command evidence."""
        return self.post("control_sequences", payload)

    def submit_retained_charge(
        self,
        assembly_id,
        test_id,
        scene_sequence_id,
        control_sequence_id,
        capture_program_id,
        *,
        seed=42,
        noise_enabled=True,
        unmodulated_scene_sequence_id=None,
        capture_crop=None,
        field_radiometry_mode=None,
        force=False,
    ):
        """Submit an immutable retained sequence with an optional source-field RI mode."""
        parameters = {"seed": seed, "noise_enabled": noise_enabled}
        if unmodulated_scene_sequence_id is not None:
            parameters["unmodulated_scene_sequence_id"] = unmodulated_scene_sequence_id
        if capture_crop is not None:
            parameters["capture_crop"] = capture_crop
        if field_radiometry_mode is not None:
            parameters["field_radiometry_mode"] = field_radiometry_mode
        return self.submit(
            assembly_id,
            test_id,
            plugin_id="retained_charge_v1",
            scene_sequence_id=scene_sequence_id,
            control_sequence_id=control_sequence_id,
            capture_program_id=capture_program_id,
            parameters=parameters,
            force=force,
        )

    def submit_electrical_control(
        self,
        assembly_id,
        test_id,
        scene_sequence_id,
        control_sequence_id,
        capture_program_id,
        *,
        seed=42,
        noise_enabled=True,
        force=False,
    ):
        """Run RAW-observed exposure AE from a fresh retained-charge session."""
        return self.submit(
            assembly_id,
            test_id,
            plugin_id="electrical_control_v1",
            scene_sequence_id=scene_sequence_id,
            control_sequence_id=control_sequence_id,
            capture_program_id=capture_program_id,
            parameters={"seed": seed, "noise_enabled": noise_enabled},
            force=force,
        )

    def create_capture_program(self, payload):
        """Pin explicit electrical/LED/HDR model assumptions to one assembly."""
        return self.post("capture_programs", payload)

    def submit_hdr(
        self,
        assembly_id,
        test_id,
        scene_sequence_id,
        control_sequence_id,
        capture_program_id,
        *,
        seed=42,
        unmodulated_scene_sequence_id=None,
        capture_crop=None,
        field_radiometry_mode=None,
        force=False,
    ):
        """Sequential electrical RAW with optional explicitly authored light components."""
        parameters = {"seed": seed}
        if unmodulated_scene_sequence_id is not None:
            parameters["unmodulated_scene_sequence_id"] = unmodulated_scene_sequence_id
        if capture_crop is not None:
            parameters["capture_crop"] = capture_crop
        if field_radiometry_mode is not None:
            parameters["field_radiometry_mode"] = field_radiometry_mode
        return self.submit(
            assembly_id,
            test_id,
            plugin_id="hdr_bracket_v1",
            scene_sequence_id=scene_sequence_id,
            control_sequence_id=control_sequence_id,
            capture_program_id=capture_program_id,
            parameters=parameters,
            force=force,
        )

    def submit_dcg(
        self,
        assembly_id,
        test_id,
        scene_sequence_id,
        control_sequence_id,
        capture_program_id,
        *,
        seed=42,
        capture_crop=None,
        field_radiometry_mode=None,
        force=False,
    ):
        """One charge acquisition with correlated HCG/LCG readouts and RAW-only fusion."""
        parameters = {"seed": seed}
        if capture_crop is not None:
            parameters["capture_crop"] = capture_crop
        if field_radiometry_mode is not None:
            parameters["field_radiometry_mode"] = field_radiometry_mode
        return self.submit(
            assembly_id,
            test_id,
            plugin_id="shared_charge_dcg_v1",
            scene_sequence_id=scene_sequence_id,
            control_sequence_id=control_sequence_id,
            capture_program_id=capture_program_id,
            parameters=parameters,
            force=force,
        )

    def submit_reference_hdr(
        self,
        assembly_id,
        test_id,
        scene_sequence_id,
        control_sequence_id,
        capture_program_id,
        *,
        seed=42,
        capture_crop=None,
        force=False,
    ):
        """RAW fusion under constant light, limited by a valid reference exposure."""
        parameters = {"seed": seed}
        if capture_crop is not None:
            parameters["capture_crop"] = capture_crop
        return self.submit(
            assembly_id,
            test_id,
            plugin_id="reference_hdr_v1",
            scene_sequence_id=scene_sequence_id,
            control_sequence_id=control_sequence_id,
            capture_program_id=capture_program_id,
            parameters=parameters,
            force=force,
        )

    def create_control_policy(self, payload):
        """Register an immutable reusable RAW-feedback policy and its provenance."""
        return self.post("control_policies", payload)

    def check_control_program(self, payload):
        """Check whole control-domain coverage without creating records or jobs."""
        return self.post("control_programs/check", payload)

    def create_control_program(self, payload):
        return self.post("control_programs", payload)

    def submit_closed_loop(self, assembly_id, test_id, control_program_id, *, seed=42, force=False):
        return self.submit(
            assembly_id,
            test_id,
            plugin_id="closed_loop_scene_v1",
            control_program_id=control_program_id,
            parameters={"seed": seed},
            force=force,
        )

    def import_scene_sequence(self, document_text):
        """Preserve the supplied UTF-8 JSON text alongside its normalized contract."""
        return self.post("scene_sequences/import", {"document_text": document_text})

    def import_control_sequence(self, document_text):
        """Preserve per-frame applied-settings JSON text without normalization by the SDK."""
        return self.post("control_sequences/import", {"document_text": document_text})

    def submit_temporal(
        self,
        assembly_id,
        test_id,
        scene_sequence_id,
        control_sequence_id,
        *,
        seed=42,
        capture_crop=None,
        field_radiometry_mode=None,
        force=False,
    ):
        """Capture fixed applied settings with optional source-field radiometry."""
        parameters = {"seed": seed}
        if capture_crop is not None:
            parameters["capture_crop"] = capture_crop
        if field_radiometry_mode is not None:
            parameters["field_radiometry_mode"] = field_radiometry_mode
        return self.submit(
            assembly_id,
            test_id,
            plugin_id="temporal_scene_v1",
            parameters=parameters,
            force=force,
            scene_sequence_id=scene_sequence_id,
            control_sequence_id=control_sequence_id,
        )

    def retained_isp_sources(self):
        return self.get("retained-isp/sources")

    def compare_retained_raw_capture(self, run_id, capture_id, readout_id, roi):
        """Read-only original-code diagnostics; no physical validation result."""
        return self.post(
            "retained-isp/raw-capture-comparisons",
            {
                "schema": "camera_retained_raw_capture_comparison_v1",
                "run_id": run_id,
                "capture_id": capture_id,
                "readout_id": readout_id,
                "roi": roi,
            },
        )

    def design_experiment_template(self):
        """Available only in a dedicated design-experiments development state."""
        return self.get("design-experiments/template")

    def submit_design_experiment(self, specification):
        """Explicit finite-grid run; acknowledgement and all limits are required."""
        return self.post("design-experiments", specification)

    def design_experiment(self, identity):
        return self.get("design-experiments/" + identity)

    def list_design_experiments(self):
        return self.get("design-experiments")

    def r1_timed_acquisition_template(self):
        """Registered synthetic R1 scene/model timing request and limits."""
        return self.get("r1-timed-acquisitions/template")

    def submit_r1_timed_acquisition(self, specification):
        return self.post("r1-timed-acquisitions", specification)

    def r1_timed_acquisition(self, identity):
        return self.get("r1-timed-acquisitions/" + identity)

    def list_r1_timed_acquisitions(self):
        return self.get("r1-timed-acquisitions")

    def hdr_lfm_mode_readiness(self, identity):
        """Reverify one RAW/HDR Job and show exact mode/reference evidence gaps."""
        return self.get("hdr-lfm-mode-readiness/" + identity)

    def hdr_lfm_reference_template(self, source_hdr_job_id):
        """Get a source-bound independent RAW/reference intake contract."""
        return self.get("hdr-lfm-reference-comparisons/template/" + source_hdr_job_id)

    def submit_hdr_lfm_reference_comparison(
        self, metadata, *, measured_raw_path, measured_reference_rate_path,
        mode_log_path, waveform_log_path, reference_log_path,
    ):
        """Upload evidence bytes; the Worker compares only unregistered means."""
        from pathlib import Path

        from .registry import canonical

        files = {}
        for key, path_value in {
            "measured_raw": measured_raw_path,
            "measured_reference_rate": measured_reference_rate_path,
            "mode_log": mode_log_path,
            "waveform_log": waveform_log_path,
            "reference_log": reference_log_path,
        }.items():
            path = Path(path_value)
            files[key] = (path.name, path.read_bytes(), "application/octet-stream")
        response = self.http.post(
            "/api/v1/hdr-lfm-reference-comparisons",
            data={"metadata": canonical(metadata)}, files=files,
        )
        response.raise_for_status()
        return response.json()

    def hdr_lfm_reference_comparison(self, identity):
        return self.get("hdr-lfm-reference-comparisons/" + identity)

    def db_comparison_template(self, *, query=""):
        """Browse selectable saved lens and sensor revisions."""
        return self.get("db-comparisons/template?" + urlencode({"q": query}))

    def submit_db_comparison(self, specification):
        """Queue a pinned two-lens/two-sensor comparison."""
        return self.post("db-comparisons", specification)

    def db_comparison(self, identity):
        return self.get("db-comparisons/" + identity)

    def list_db_comparisons(self):
        return self.get("db-comparisons")

    def catalog_quality_template(self, *, query=""):
        """Browse source-catalog sensors for the separate quality estimate."""
        return self.get("catalog-quality-comparisons/template?" + urlencode({"q": query}))

    def submit_catalog_quality_comparison(self, specification):
        return self.post("catalog-quality-comparisons", specification)

    def catalog_quality_comparison(self, identity):
        return self.get("catalog-quality-comparisons/" + identity)

    def list_catalog_quality_comparisons(self):
        return self.get("catalog-quality-comparisons")

    def saved_sensor_quality_template(self, *, query=""):
        """Browse executable saved sensor run revisions for quality diagnostics."""
        return self.get("saved-sensor-quality/template?" + urlencode({"q": query}))

    def submit_saved_sensor_quality(self, specification):
        return self.post("saved-sensor-quality", specification)

    def saved_sensor_quality(self, identity):
        return self.get("saved-sensor-quality/" + identity)

    def list_saved_sensor_quality(self):
        return self.get("saved-sensor-quality")

    def saved_sensor_search_template(self, *, query=""):
        """Browse saved runs for bounded exposure/gain search."""
        return self.get("saved-sensor-search/template?" + urlencode({"q": query}))

    def submit_saved_sensor_search(self, specification):
        return self.post("saved-sensor-search", specification)

    def saved_sensor_search(self, identity):
        return self.get("saved-sensor-search/" + identity)

    def list_saved_sensor_searches(self):
        return self.get("saved-sensor-search")

    def lens_sensor_raw_template(self, *, query="", scope="central"):
        """Browse Lens × saved Sensor RAW, including bounded common-scene scopes."""
        return self.get("lens-sensor-raw/template?" + urlencode({"q": query, "scope": scope}))

    def lens_sensor_radiance_scene_template(self):
        """Read the explicit pre-lens spectral-radiance import contract."""
        return self.get("lens-sensor-radiance-scenes/template")

    def import_lens_sensor_radiance_scene(self, metadata, image_path):
        """Upload a spectral-radiance NPY; sensor-plane irradiance is invalid here."""
        path = Path(image_path)
        with path.open("rb") as stream:
            response = self.http.post(
                "/api/v1/lens-sensor-radiance-scenes/import",
                data={"metadata": json.dumps(metadata, allow_nan=False)},
                files={"image": (path.name, stream, "application/octet-stream")},
            )
        response.raise_for_status()
        return response.json()

    def lens_sensor_radiance_scene(self, identity):
        return self.get("lens-sensor-radiance-scenes/" + identity)

    def list_lens_sensor_radiance_scenes(self):
        return self.get("lens-sensor-radiance-scenes")

    def submit_lens_sensor_raw(self, specification):
        return self.post("lens-sensor-raw", specification)

    def lens_sensor_raw(self, identity):
        return self.get("lens-sensor-raw/" + identity)

    def list_lens_sensor_raw(self):
        return self.get("lens-sensor-raw")

    def lens_sensor_streamed_raw_template(self, *, query=""):
        path = "lens-sensor-streamed-raw/template"
        if query:
            path += "?" + urlencode({"q": query})
        return self.get(path)

    def lens_sensor_streamed_raw_sensor_option(self, identifier, run_id):
        return self.get("lens-sensor-streamed-raw/sensor-option?" + urlencode({
            "identifier": identifier, "run_id": run_id,
        }))

    def submit_lens_sensor_streamed_raw(self, specification):
        return self.post("lens-sensor-streamed-raw", specification)

    def lens_sensor_streamed_raw(self, identity):
        return self.get("lens-sensor-streamed-raw/" + identity)

    def list_lens_sensor_streamed_raw(self):
        return self.get("lens-sensor-streamed-raw")

    def lens_sensor_disk_raw_template(self, *, query=""):
        path = "lens-sensor-disk-raw/template"
        if query:
            path += "?" + urlencode({"q": query})
        return self.get(path)

    def lens_sensor_disk_raw_sensor_option(self, identifier, run_id):
        return self.get("lens-sensor-disk-raw/sensor-option?" + urlencode({
            "identifier": identifier, "run_id": run_id,
        }))

    def submit_lens_sensor_disk_raw(self, specification):
        return self.post("lens-sensor-disk-raw", specification)

    def lens_sensor_disk_raw(self, identity):
        return self.get("lens-sensor-disk-raw/" + identity)

    def list_lens_sensor_disk_raw(self):
        return self.get("lens-sensor-disk-raw")

    def wait_lens_sensor_disk_raw(self, identity, *, timeout=660, poll_interval=0.5):
        """Wait for a disk RAW job; allow beyond its 600 s default worker deadline."""
        return self.wait(
            identity, timeout=timeout, poll_interval=poll_interval,
            expected_kind="lens_sensor_out_of_core_raw",
        )

    def lens_sensor_registered_roi_disk_raw_template(self, *, query=""):
        path = "lens-sensor-registered-roi-disk-raw/template"
        if query:
            path += "?" + urlencode({"q": query})
        return self.get(path)

    def submit_lens_sensor_registered_roi_disk_raw(self, specification):
        return self.post("lens-sensor-registered-roi-disk-raw", specification)

    def lens_sensor_registered_roi_disk_raw(self, identity):
        return self.get("lens-sensor-registered-roi-disk-raw/" + identity)

    def list_lens_sensor_registered_roi_disk_raw(self):
        return self.get("lens-sensor-registered-roi-disk-raw")

    def wait_lens_sensor_registered_roi_disk_raw(
        self, identity, *, timeout=1860, poll_interval=0.5
    ):
        """Wait beyond the registered ROI RAW Worker's 1800 s default deadline."""
        return self.wait(
            identity, timeout=timeout, poll_interval=poll_interval,
            expected_kind="lens_sensor_registered_roi_disk_raw",
        )

    def lens_sensor_frequency_target_template(self):
        return self.get("lens-sensor-frequency-target/template")

    def lens_sensor_frequency_target_extended_sparse_roi_template(self):
        return self.get("lens-sensor-frequency-target/template/extended-sparse-roi")

    def submit_lens_sensor_frequency_target(self, specification):
        return self.post("lens-sensor-frequency-target", specification)

    def lens_sensor_frequency_target(self, identity):
        return self.get("lens-sensor-frequency-target/" + identity)

    def list_lens_sensor_frequency_target(self):
        return self.get("lens-sensor-frequency-target")

    def wait_lens_sensor_frequency_target(
        self, identity, *, timeout=1860, poll_interval=0.5
    ):
        return self.wait(
            identity, timeout=timeout, poll_interval=poll_interval,
            expected_kind="lens_sensor_frequency_target",
        )

    def wait_lens_sensor_frequency_target_extended_sparse_roi(
        self, identity, *, timeout=3660, poll_interval=0.5
    ):
        return self.wait_lens_sensor_frequency_target(
            identity, timeout=timeout, poll_interval=poll_interval,
        )

    def lens_geometric_pupil_density_template(self):
        return self.get("lens-geometric-pupil-density/template")

    def lens_psf_lut_template(self):
        return self.get("lens-psf-luts/template")

    def submit_lens_psf_camera(self, lut_run_id, **settings):
        return self.post("lens-psf-cameras", dict(lut_run_id=lut_run_id, **settings))

    def lens_psf_camera(self, identity):
        return self.get("lens-psf-cameras/" + identity)

    def submit_lens_psf_lut(self, specification):
        return self.post("lens-psf-luts", specification)

    def lens_psf_lut(self, identity):
        return self.get("lens-psf-luts/" + identity)

    def list_lens_psf_luts(self):
        return self.get("lens-psf-luts")

    def sample_lens_psf_lut(self, identity, *, angle_deg_xy, wavelength_nm,
                            pixel_pitch_um, kernel_size=129, source_offset_um_xy=(0.,0.)):
        return self.post("lens-psf-luts/"+identity+"/sample",dict(angle_deg_xy=angle_deg_xy,
            wavelength_nm=wavelength_nm,pixel_pitch_um=pixel_pitch_um,kernel_size=kernel_size,
            source_offset_um_xy=list(source_offset_um_xy)))

    def submit_lens_geometric_pupil_density(self, specification):
        return self.post("lens-geometric-pupil-density", specification)

    def lens_geometric_pupil_density(self, identity):
        return self.get("lens-geometric-pupil-density/" + identity)

    def list_lens_geometric_pupil_density(self):
        return self.get("lens-geometric-pupil-density")

    def wait_lens_geometric_pupil_density(
        self, identity, *, timeout=480, poll_interval=0.5
    ):
        return self.wait(
            identity, timeout=timeout, poll_interval=poll_interval,
            expected_kind="lens_geometric_pupil_density",
        )

    def lens_dense_geometric_psf_candidate_template(self):
        return self.get("lens-dense-geometric-psf-candidates/template")

    def submit_lens_dense_geometric_psf_candidate(self, specification):
        return self.post("lens-dense-geometric-psf-candidates", specification)

    def lens_dense_geometric_psf_candidate(self, identity):
        return self.get("lens-dense-geometric-psf-candidates/" + identity)

    def list_lens_dense_geometric_psf_candidates(self):
        return self.get("lens-dense-geometric-psf-candidates")

    def wait_lens_dense_geometric_psf_candidate(
        self, identity, *, timeout=960, poll_interval=0.5
    ):
        return self.wait(
            identity, timeout=timeout, poll_interval=poll_interval,
            expected_kind="lens_dense_geometric_psf_candidate",
        )

    def lens_dense_geometric_psf_candidate_v2_template(self):
        return self.get("lens-dense-geometric-psf-candidates-v2/template")

    def submit_lens_dense_geometric_psf_candidate_v2(self, specification):
        return self.post("lens-dense-geometric-psf-candidates-v2", specification)

    def lens_dense_geometric_psf_candidate_v2(self, identity):
        return self.get("lens-dense-geometric-psf-candidates-v2/" + identity)

    def list_lens_dense_geometric_psf_candidates_v2(self):
        return self.get("lens-dense-geometric-psf-candidates-v2")

    def wait_lens_dense_geometric_psf_candidate_v2(
        self, identity, *, timeout=960, poll_interval=0.5
    ):
        return self.wait(
            identity, timeout=timeout, poll_interval=poll_interval,
            expected_kind="lens_dense_geometric_psf_candidate_v2",
        )

    def lens_dense_geometric_psf_q4_rebin_template(self):
        return self.get("lens-dense-geometric-psf-q4-rebins/template")

    def submit_lens_dense_geometric_psf_q4_rebin(self, specification):
        return self.post("lens-dense-geometric-psf-q4-rebins", specification)

    def lens_dense_geometric_psf_q4_rebin(self, identity):
        return self.get("lens-dense-geometric-psf-q4-rebins/" + identity)

    def list_lens_dense_geometric_psf_q4_rebins(self):
        return self.get("lens-dense-geometric-psf-q4-rebins")

    def wait_lens_dense_geometric_psf_q4_rebin(
        self, identity, *, timeout=960, poll_interval=0.5
    ):
        return self.wait(
            identity, timeout=timeout, poll_interval=poll_interval,
            expected_kind="lens_dense_geometric_psf_q4_rebin",
        )

    def lens_sensor_dense_psf_sensitivity_template(self):
        return self.get("lens-sensor-dense-psf-sensitivity/template")

    def submit_lens_sensor_dense_psf_sensitivity(self, specification):
        return self.post("lens-sensor-dense-psf-sensitivity", specification)

    def lens_sensor_dense_psf_sensitivity(self, identity):
        return self.get("lens-sensor-dense-psf-sensitivity/" + identity)

    def list_lens_sensor_dense_psf_sensitivity(self):
        return self.get("lens-sensor-dense-psf-sensitivity")

    def wait_lens_sensor_dense_psf_sensitivity(
        self, identity, *, timeout=1860, poll_interval=0.5
    ):
        return self.wait(
            identity, timeout=timeout, poll_interval=poll_interval,
            expected_kind="lens_sensor_dense_psf_sensitivity",
        )

    def road_hdr_template(self):
        return self.get("road-hdr-studies/template")

    def design_workshop_catalog(self):
        return self.get("design-workshop/catalog")

    def reference_workshop_catalog(self):
        return self.get("reference-workshop/catalog")

    def create_reference_revision(self, specification, *, kind="camera", name="Reference revision", parent_id=None):
        return self.post("reference-workshop/revisions", {"specification": specification,
            "kind": kind, "name": name, "parent_id": parent_id})

    def submit_reference_workshop(self, specification):
        return self.post("reference-workshop/runs", specification)

    def submit_reference_sweep(self, specification, axes, *, name="Reference sweep"):
        return self.post("reference-workshop/sweeps", {"specification": specification,
            "axes": axes, "name": name})

    def replay_reference_workshop(self, source_run_id, isp, *, name="Reference same RAW ISP", source_candidate_id=None):
        return self.post("reference-workshop/replays", {"source_run_id": source_run_id,
            "name": name, "isp": isp, "source_candidate_id": source_candidate_id})

    def reference_workshop_run(self, identity):
        return self.get("reference-workshop/runs/" + identity)

    def submit_reference_isp_study(self, source_run_id, recipe, *, source_candidate_id=None,
                                   name=None, isp=None, constraints=None, parameter_axes=None):
        payload = {"source_run_id": source_run_id, "recipe": recipe}
        for key, value in (("source_candidate_id", source_candidate_id), ("name", name),
                           ("isp", isp), ("constraints", constraints),
                           ("parameter_axes", parameter_axes)):
            if value is not None:
                payload[key] = value
        return self.post("reference-workshop/isp-studies", payload)

    def preview_reference_isp(self, source_run_id, isp, *, field_id="outer-ne", chart="texture",
                              source_candidate_id=None, name="Reference ISP · one-frame preview"):
        """Process preserved frame zero only; no temporal-noise or selection claim."""
        return self.post("reference-workshop/isp-previews", {
            "source_run_id": source_run_id, "source_candidate_id": source_candidate_id,
            "isp": isp, "field_id": field_id, "chart": chart, "name": name,
        })

    def wait_reference_workshop(self, identity, *, timeout=7260, poll_interval=1):
        return self.wait(identity, timeout=timeout, poll_interval=poll_interval, expected_kind="reference_workshop")

    def submit_reference_pixel_study(self, specification):
        return self.post("reference-workshop/pixel-studies", specification)

    def search_design_assets(self, **filters):
        """Search source-pinned design evidence; model counts are not product counts."""
        return self.get("component-explorer/catalog" + ("?" + urlencode(filters) if filters else ""))

    def design_asset(self, identity):
        return self.get(f"component-explorer/assets/{identity}")

    def design_asset_result(self, identity, result_id):
        """Verify retained result/source bindings before opening its returned URL."""
        return self.get(f"component-explorer/assets/{identity}/results/{result_id}")

    def save_design_shortlist(self, selection):
        return self.post("component-explorer/shortlists", selection)

    def design_shortlists(self):
        return self.get("component-explorer/shortlists")

    def design_shortlist(self, identity):
        return self.get(f"component-explorer/shortlists/{identity}")

    def design_readiness(self, asset_id=None):
        path = f"assets/{asset_id}/readiness" if asset_id else "readiness"
        return self.get("component-explorer/" + path)

    def submit_design_asset_refresh(self):
        return self.post("component-explorer/refreshes", {})

    def design_asset_watch(self):
        return self.get("component-explorer/watch")

    def configure_design_asset_watch(self, *, enabled):
        response = self.http.put("/api/v1/component-explorer/watch", json={"enabled": enabled})
        response.raise_for_status()
        return response.json()

    def propose_design_asset_update(self, proposal):
        return self.post("component-explorer/proposals", proposal)

    def adopt_design_asset_proposal(self, identity):
        return self.post(f"component-explorer/proposals/{identity}/adopt", {})

    def submit_design_readiness_check(self, model_revision_id):
        return self.post("component-explorer/readiness-checks",
                         {"model_revision_id": model_revision_id})

    def submit_design_asset_trial(self, specification):
        return self.post("component-explorer/model-trials", specification)

    def db_design_options(self):
        """Available pinned DB models, acquired/default fields and prior study jobs."""
        return self.get("db-design/options")

    def submit_db_design_study(self, specification=None):
        """Compute selected DB prescriptions and sensors; use common wait/cancel."""
        return self.post("db-design/studies", specification or {})

    def submit_aperture_conditions(self, source_run_id, *, luminance_cd_m2=None,
                                   exposure_ms=None, selection_policy=None, name=None):
        """Recapture a saved aperture sweep over scene luminance/exposure conditions.

        Only noise-free optical/pixel rates are reused. Each condition produces
        new independent RAW, fixed-ISP output and a bounded aperture decision.
        Use the common wait(), cancel() and workshop file/bundle lifecycle.
        """
        spec = {"comparison_mode": "aperture_conditions", "source_run_id": source_run_id}
        for key, value in (("luminance_cd_m2", luminance_cd_m2), ("exposure_ms", exposure_ms),
                           ("selection_policy", selection_policy), ("name", name)):
            if value is not None:
                spec[key] = value
        return self.submit_db_design_study(spec)

    def submit_aperture_motion(self, source_run_id, *, luminance_cd_m2=None,
                              exposure_ms=None, object_speed_m_s=None,
                              object_distance_m=None, selection_policy=None, name=None):
        """Joint aperture/exposure selection under declared transverse target motion.

        Integrates saved channel response planes before fresh independent RAW.
        Speed/distance use a declared pinhole projection with a common valid ROI.
        Common wait(), cancel() and original artifact download remain available.
        """
        spec = {"comparison_mode": "aperture_motion", "source_run_id": source_run_id}
        for key, value in (("luminance_cd_m2", luminance_cd_m2), ("exposure_ms", exposure_ms),
                           ("object_speed_m_s", object_speed_m_s),
                           ("object_distance_m", object_distance_m),
                           ("selection_policy", selection_policy), ("name", name)):
            if value is not None:
                spec[key] = value
        return self.submit_db_design_study(spec)

    def design_asset_job(self, identity):
        return self.get(f"component-explorer/jobs/{identity}")

    def submit_shutter_motion(self, source_run_id, *, luminance_cd_m2=None,
                              exposure_ms=None, object_speed_m_s=None,
                              object_distance_m=None, readout_ms=None, f_numbers=None,
                              selection_policy=None, name=None):
        """Compare matched GS and RS using first-to-last-row scan of a 256-row probe.

        readout_ms is the row exposure-start span, not transfer latency or a
        measured product mode. Saved full channel rates are time-integrated
        before fresh RAW. Common wait/cancel/files/bundle lifecycle applies.
        """
        spec = {"comparison_mode": "shutter_motion", "source_run_id": source_run_id}
        for key, value in (("luminance_cd_m2", luminance_cd_m2), ("exposure_ms", exposure_ms),
                           ("object_speed_m_s", object_speed_m_s),
                           ("object_distance_m", object_distance_m), ("readout_ms", readout_ms),
                           ("f_numbers", f_numbers), ("selection_policy", selection_policy),
                           ("name", name)):
            if value is not None:
                spec[key] = value
        return self.submit_db_design_study(spec)

    def cancel_design_asset_job(self, identity):
        return self.post(f"component-explorer/jobs/{identity}/cancel", {})

    def submit_sensor_shutter(self, source_run_id, *, luminance_cd_m2=None,
                              exposure_ms=None, object_speed_m_s=None,
                              object_distance_m=None, line_time_us=None, f_numbers=None,
                              selection_policy=None, name=None, operating_mode_id=None,
                              fps=None, located_source_run_id=None):
        """Compare located sensor ROIs on one shared full-array row clock.

        Sensor dimensions and source documents are pinned by the backend.
        line_time_us contains 0 for the matched GS and positive declared timing
        assumptions; it does not assert a measured vendor operating mode.
        Peripheral optics are regenerated at the located sensor-plane point.
        operating_mode_id binds source/assumption timing, FPS and exposure
        limits. located_source_run_id reuses only hash-verified pre-noise rates.
        """
        spec = {"comparison_mode": "sensor_shutter", "source_run_id": source_run_id}
        for key, value in (("luminance_cd_m2", luminance_cd_m2), ("exposure_ms", exposure_ms),
                           ("object_speed_m_s", object_speed_m_s),
                           ("object_distance_m", object_distance_m), ("line_time_us", line_time_us),
                           ("f_numbers", f_numbers), ("selection_policy", selection_policy),
                           ("operating_mode_id", operating_mode_id), ("fps", fps),
                           ("located_source_run_id", located_source_run_id),
                           ("name", name)):
            if value is not None:
                spec[key] = value
        return self.submit_db_design_study(spec)

    def link_design_asset_validation(self, specification):
        return self.post("component-explorer/validation-links", specification)

    def reference_ircf_studies(self):
        return self.get("reference-workshop/ircf-studies")

    def submit_reference_ircf_study(self, specification):
        """Acquire filter-dependent RAW; compare two CCMs on each preserved capture."""
        return self.post("reference-workshop/ircf-studies", specification)

    def reference_ircf_study(self, identity):
        return self.get(f"reference-workshop/ircf-studies/{identity}")

    def reference_ircf_case(self, identity, case_id):
        return self.get(f"reference-workshop/ircf-studies/{identity}/cases/{case_id}")

    def save_reference_ircf_profile(self, identity, selection):
        return self.post(f"reference-workshop/ircf-studies/{identity}/profiles", selection)

    def submit_reference_ghost_study(self, specification):
        return self.post("reference-workshop/ghost-studies", specification)

    def reference_ghost_studies(self):
        return self.get("reference-workshop/ghost-studies")

    def reference_ghost_study(self, identity):
        return self.get(f"reference-workshop/ghost-studies/{identity}")

    def submit_reference_sensor_mode_study(self, specification):
        return self.post("reference-workshop/sensor-mode-studies", specification)

    def submit_reference_readout_study(self, recipe, *, settings=None, fields=None,
                                      charts=None, light_scales=None, name=None):
        """Run a source-pinned circuit assumption study using the common lifecycle.

        A changed readout generates new integer RAW from preserved mean response.
        It is not an ISP replay or a measured commercial sensor operating mode.
        """
        if recipe not in {"gain_budget", "noise_structure", "adc_headroom"}:
            raise ValueError("Choose gain_budget, noise_structure or adc_headroom.")
        defaults = {
            "gain_budget": (["uniform", "texture", "dynamic_range"], [0.01, 1.0]),
            "noise_structure": (["dark", "uniform", "texture"], [0.01]),
            "adc_headroom": (["uniform", "dynamic_range"], [1.0]),
        }
        payload = self.reference_sensor_mode_studies()["template"]
        payload.update(recipe=recipe, name=name or "Reference Readout · " + recipe,
                       reprocess_of=None)
        payload.update(charts=defaults[recipe][0], light_scales=defaults[recipe][1])
        for key, value in (("readout_lab", settings), ("fields", fields),
                           ("charts", charts), ("light_scales", light_scales)):
            if value is not None:
                payload[key] = value
        return self.submit_reference_sensor_mode_study(payload)

    def reference_sensor_mode_studies(self):
        return self.get("reference-workshop/sensor-mode-studies")

    def reference_sensor_mode_study(self, identity):
        return self.get(f"reference-workshop/sensor-mode-studies/{identity}")

    def reference_sensor_mode_case(self, identity, case_id):
        return self.get(f"reference-workshop/sensor-mode-studies/{identity}/cases/{case_id}")

    def save_reference_sensor_mode_profile(self, identity, selection):
        return self.post(f"reference-workshop/sensor-mode-studies/{identity}/profiles", selection)

    def reference_pixel_studies(self):
        return self.get("reference-workshop/pixel-studies")

    def reference_pixel_candidate(self, identity, candidate_id):
        return self.get(f"reference-workshop/pixel-studies/{identity}/candidates/{candidate_id}")

    def save_reference_pixel_comparison(self, identity, selection):
        return self.post(f"reference-workshop/pixel-studies/{identity}/comparisons", selection)

    def cancel_reference_workshop(self, identity):
        return self.post("reference-workshop/runs/" + identity + "/cancel", {})

    def create_design_revision(self, specification, *, kind="camera", name="Design revision", parent_id=None):
        return self.post("design-workshop/revisions", {"specification": specification,
            "kind": kind, "name": name, "parent_id": parent_id})

    def submit_design_workshop(self, specification):
        return self.post("design-workshop/runs", specification)

    def submit_design_sweep(self, specification, axes, *, name="Design sweep"):
        return self.post("design-workshop/sweeps", {"specification": specification,
            "axes": axes, "name": name})

    def replay_design_workshop(self, source_run_id, isp, *, name="Same RAW ISP replay", source_candidate_id=None):
        return self.post("design-workshop/replays", {"source_run_id": source_run_id,
            "name": name, "isp": isp, "source_candidate_id": source_candidate_id})

    def design_workshop_run(self, identity):
        return self.get("design-workshop/runs/" + identity)

    def wait_design_workshop(self, identity, *, timeout=7260, poll_interval=1):
        return self.wait(identity, timeout=timeout, poll_interval=poll_interval, expected_kind="design_workshop")

    def cancel_design_workshop(self, identity):
        return self.post("design-workshop/runs/" + identity + "/cancel", {})

    def virtual_lab_template(self):
        return self.get("virtual-lab/template")

    def calibration_lab_template(self, *, source_run=None, candidate=None, condition=None):
        params = {key: value for key, value in {
            "source_run": source_run, "candidate": candidate, "condition": condition,
        }.items() if value is not None}
        response = self.http.get("/api/v1/calibration-lab/template", params=params)
        response.raise_for_status()
        return response.json()

    def create_calibration_db_bridge(self, source_run, candidate, *, condition=None):
        source = {"run_id": source_run, "candidate_id": candidate}
        if condition is not None:
            source["condition_id"] = condition
        return self.post("calibration-lab/db-bridges", {"source": source})

    def calibration_db_bridge(self, identity):
        return self.get("calibration-lab/db-bridges/" + identity)

    def create_calibration_db_optical_profile(
        self, source_run, candidate, profile, *, condition=None, field_mapping=None,
        field_mapping_mode=None,
    ):
        source = {"run_id": source_run, "candidate_id": candidate}
        if condition is not None:
            source["condition_id"] = condition
        payload = {"source": source, "profile": profile}
        if field_mapping is not None:
            payload["field_mapping"] = field_mapping
        if field_mapping_mode is not None:
            payload["field_mapping_mode"] = field_mapping_mode
        return self.post("calibration-lab/db-optical-profiles", payload)

    def calibration_db_optical_profile(self, identity):
        return self.get("calibration-lab/db-optical-profiles/" + identity)

    def submit_calibration_db_replay(self, campaign_id, profile_id, *, seed=42):
        return self.post("calibration-lab/db-replays", {
            "campaign_id": campaign_id, "profile_id": profile_id,
            "parameter_scope": "electronics_only", "seed": seed,
        })

    def calibration_db_replay(self, identity):
        return self.get("calibration-lab/db-replays/" + identity)

    def cancel_calibration_db_replay(self, identity):
        return self.post("calibration-lab/db-replays/" + identity + "/cancel", {})

    def wait_calibration_db_replay(self, identity, *, timeout=1860, poll_interval=1):
        return self.wait(identity, timeout=timeout, poll_interval=poll_interval,
                         expected_kind="calibration_db_replay")

    def create_calibration_campaign(self, specification):
        return self.post("calibration-lab/campaigns", specification)

    def calibration_campaign(self, identity):
        return self.get("calibration-lab/campaigns/" + identity)

    def import_calibration_measurement(self, metadata, path=None):
        if path is None:
            return self.post("calibration-lab/measurements", metadata)
        path = Path(path)
        with path.open("rb") as stream:
            response = self.http.post(
                "/api/v1/calibration-lab/measurements/import",
                data={"metadata": json.dumps(metadata, ensure_ascii=False, allow_nan=False)},
                files={"file": (path.name, stream, "application/octet-stream")},
            )
        response.raise_for_status()
        return response.json()

    def submit_calibration_study(self, specification):
        return self.post("calibration-lab/studies", specification)

    def calibration_study(self, identity):
        return self.get("calibration-lab/studies/" + identity)

    def validate_calibration_study(self, identity, measurement_ids):
        return self.post("calibration-lab/studies/" + identity + "/validate",
                         {"measurement_ids": measurement_ids})

    def apply_calibration_study(self, identity, campaign_id):
        return self.post("calibration-lab/studies/" + identity + "/apply",
                         {"campaign_id": campaign_id})

    def activate_calibration_profile(self, campaign_id, profile_id):
        return self.post("calibration-lab/campaigns/" + campaign_id + "/activate",
                         {"profile_id": profile_id})

    def calibration_demo(self):
        return self.post("calibration-lab/demo", {})

    def create_calibration_completion_report(self, study_id, validation_id, profile_id):
        return self.post("calibration-lab/completion-reports", {
            "study_id": study_id, "validation_id": validation_id, "profile_id": profile_id,
        })

    def submit_virtual_lab(self, specification):
        return self.post("virtual-lab/runs", specification)

    def replay_virtual_lab(self, source_run_id, isp, *, name="Virtual Lab ISP replay"):
        return self.post("virtual-lab/replays", {"source_run_id": source_run_id, "name": name, "isp": isp})

    def virtual_lab_run(self, identity):
        return self.get("virtual-lab/runs/" + identity)

    def wait_virtual_lab(self, identity, *, timeout=3660, poll_interval=1):
        return self.wait(identity, timeout=timeout, poll_interval=poll_interval, expected_kind="virtual_lab")

    def cancel_virtual_lab(self, identity):
        return self.post("virtual-lab/runs/" + identity + "/cancel", {})

    def resume_virtual_lab(self, identity):
        """Resume a stopped wide acquisition from frozen, hash-verified native tiles."""
        return self.post("virtual-lab/runs/" + identity + "/resume", {})

    def freeze_road_hdr(self, package_path, *, dataset_root=None, version="v1.0-trainval"):
        payload = {"package_path": str(package_path)}
        if dataset_root is not None:
            payload.update(dataset_root=str(dataset_root), version=version)
        return self.post("road-hdr-studies/freeze", payload)

    def submit_road_hdr_acquisition(self, specification):
        if specification.get("action") not in {"first-photo", "acquire"}:
            raise ValueError("Choose an acquisition action.")
        return self.post("road-hdr-studies", specification)

    def submit_road_hdr_reprocessing(self, specification, *, profiles=None):
        payload = {**specification, "action": "reprocess"}
        if profiles is not None:
            payload["profiles"] = list(profiles)
        return self.post("road-hdr-studies", payload)

    def submit_road_hdr_evaluation(self, specification):
        return self.post("road-hdr-studies", {**specification, "action": "evaluate"})

    def road_hdr_study(self, identity):
        return self.get("road-hdr-studies/" + identity)

    def wait_road_hdr(self, identity, *, timeout=604860, poll_interval=5):
        return self.wait(identity, timeout=timeout, poll_interval=poll_interval,
                         expected_kind="road_hdr_study")

    def road_design_study_template(self):
        return self.get("road-design-studies/template")

    def pedestrian_braking_study_template(self):
        return self.get("pedestrian-braking-studies/template")

    def submit_pedestrian_braking_study(self, specification):
        return self.post("pedestrian-braking-studies", specification)

    def pedestrian_braking_study(self, identity):
        return self.get("pedestrian-braking-studies/" + identity)

    def wait_pedestrian_braking_study(self, identity, *, timeout=86460, poll_interval=5):
        return self.wait(identity, timeout=timeout, poll_interval=poll_interval,
                         expected_kind="pedestrian_braking_study")

    def submit_road_design_study(self, specification):
        return self.post("road-design-studies", specification)

    def road_design_study(self, identity):
        return self.get("road-design-studies/" + identity)

    def list_road_design_studies(self):
        return self.get("road-design-studies")

    def wait_road_design_study(self, identity, *, timeout=14460, poll_interval=1):
        return self.wait(identity, timeout=timeout, poll_interval=poll_interval,
                         expected_kind="road_design_study")

    def lens_sensor_q4_psf_sensitivity_template(self):
        return self.get("lens-sensor-q4-psf-sensitivity/template")

    def submit_lens_sensor_q4_psf_sensitivity(self, specification):
        return self.post("lens-sensor-q4-psf-sensitivity", specification)

    def lens_sensor_q4_psf_sensitivity(self, identity):
        return self.get("lens-sensor-q4-psf-sensitivity/" + identity)

    def list_lens_sensor_q4_psf_sensitivity(self):
        return self.get("lens-sensor-q4-psf-sensitivity")

    def wait_lens_sensor_q4_psf_sensitivity(
        self, identity, *, timeout=3660, poll_interval=0.5
    ):
        return self.wait(
            identity, timeout=timeout, poll_interval=poll_interval,
            expected_kind="lens_sensor_q4_psf_sensitivity",
        )

    def lens_sensor_joint_sampling_ladder_template(self):
        return self.get("lens-sensor-joint-sampling-ladder/template")

    def submit_lens_sensor_joint_sampling_ladder(self, specification):
        return self.post("lens-sensor-joint-sampling-ladder", specification)

    def lens_sensor_joint_sampling_ladder(self, identity):
        return self.get("lens-sensor-joint-sampling-ladder/" + identity)

    def list_lens_sensor_joint_sampling_ladder(self):
        return self.get("lens-sensor-joint-sampling-ladder")

    def wait_lens_sensor_joint_sampling_ladder(
        self, identity, *, timeout=3660, poll_interval=0.5
    ):
        return self.wait(
            identity, timeout=timeout, poll_interval=poll_interval,
            expected_kind="lens_sensor_joint_scene_dense_psf_ladder",
        )

    def inspect_lens_sensor_fov(
        self, lens_revision_id, sensor_identifier, sensor_run_id, *, active_mode=None
    ):
        """Check pinned geometric field support for a declared full active mode."""
        request = {
            "lens_revision_id": lens_revision_id,
            "sensor_identifier": sensor_identifier,
            "sensor_run_id": sensor_run_id,
        }
        if active_mode is not None:
            request["active_mode"] = active_mode
        return self.post("lens-sensor-fov/inspect", request)

    def register_lens_sensor_active_mode(
        self, sensor_identifier, sensor_run_id, active_mode,
        *, source_kind="engineering_declaration",
    ):
        """Store an unverified active-mode declaration with its sensor revision."""
        return self.post("lens-sensor-active-modes", {
            "sensor_identifier": sensor_identifier,
            "sensor_run_id": sensor_run_id,
            "active_mode": active_mode,
            "source_kind": source_kind,
        })

    def lens_sensor_active_mode(self, identity):
        return self.get("lens-sensor-active-modes/" + identity)

    def inspect_registered_lens_sensor_fov(
        self, lens_revision_id, sensor_identifier, sensor_run_id, declaration_id,
    ):
        """Inspect saved geometric field support from a pinned declaration ID."""
        return self.post("lens-sensor-fov/registered-inspect", {
            "lens_revision_id": lens_revision_id,
            "sensor_identifier": sensor_identifier,
            "sensor_run_id": sensor_run_id,
            "declaration_id": declaration_id,
        })

    def lens_sensor_legacy_isp_template(self, *, source_lens_sensor_job_id=""):
        """Browse original RAW eligible for model-derived CameraE2E color processing."""
        path = "lens-sensor-legacy-isp/template"
        if source_lens_sensor_job_id:
            path += "?" + urlencode({"source_lens_sensor_job_id": source_lens_sensor_job_id})
        return self.get(path)

    def submit_lens_sensor_legacy_isp(self, specification):
        return self.post("lens-sensor-legacy-isp", specification)

    def lens_sensor_legacy_isp(self, identity):
        return self.get("lens-sensor-legacy-isp/" + identity)

    def list_lens_sensor_legacy_isp(self):
        return self.get("lens-sensor-legacy-isp")

    def export_lens_sensor_legacy_isp(self, identity):
        return self.get("lens-sensor-legacy-isp/" + identity + "/export")

    def lens_sensor_quality_template(self):
        """Browse completed Lens x Sensor RAW jobs for a bounded 2 x 2 study."""
        return self.get("lens-sensor-quality-studies/template")

    def submit_lens_sensor_quality(self, specification):
        return self.post("lens-sensor-quality-studies", specification)

    def lens_sensor_quality(self, identity):
        return self.get("lens-sensor-quality-studies/" + identity)

    def list_lens_sensor_quality(self):
        return self.get("lens-sensor-quality-studies")

    def lens_sensor_hdr_template(self, *, query="", scope="central"):
        """Browse pinned Lens and Sensor revisions for static RAW HDR fusion."""
        return self.get("lens-sensor-hdr/template?" + urlencode({"q": query, "scope": scope}))

    def submit_lens_sensor_hdr(self, specification):
        return self.post("lens-sensor-hdr", specification)

    def lens_sensor_hdr(self, identity):
        return self.get("lens-sensor-hdr/" + identity)

    def list_lens_sensor_hdr(self):
        return self.get("lens-sensor-hdr")

    def lens_sensor_lfm_template(self, *, query="", scope="central"):
        """Browse pinned Lens and Sensor revisions for an authored PWM study."""
        return self.get("lens-sensor-lfm/template?" + urlencode({"q": query, "scope": scope}))

    def submit_lens_sensor_lfm(self, specification):
        return self.post("lens-sensor-lfm", specification)

    def lens_sensor_lfm(self, identity):
        return self.get("lens-sensor-lfm/" + identity)

    def list_lens_sensor_lfm(self):
        return self.get("lens-sensor-lfm")

    def lens_sensor_pwm_hdr_template(self):
        """Browse saved Lens PWM brackets for row-aware HDR reconstruction."""
        return self.get("lens-sensor-pwm-hdr/template")

    def submit_lens_sensor_pwm_hdr(self, specification):
        return self.post("lens-sensor-pwm-hdr", specification)

    def lens_sensor_pwm_hdr(self, identity):
        return self.get("lens-sensor-pwm-hdr/" + identity)

    def list_lens_sensor_pwm_hdr(self):
        return self.get("lens-sensor-pwm-hdr")

    def lens_sensor_perception_template(self):
        """Browse exact Lens x Sensor RAW datasets for a perception handoff."""
        return self.get("lens-sensor-perception/template")

    def submit_lens_sensor_perception(self, specification):
        return self.post("lens-sensor-perception", specification)

    def lens_sensor_perception(self, identity):
        return self.get("lens-sensor-perception/" + identity)

    def list_lens_sensor_perception(self):
        return self.get("lens-sensor-perception")

    def lens_sensor_raw_cohort_template(self):
        """Browse exact Lens RAW cohort exports, including imported user labels."""
        return self.get("lens-sensor-raw-cohorts/template")

    def submit_lens_sensor_raw_cohort(self, specification):
        return self.post("lens-sensor-raw-cohorts", specification)

    def lens_sensor_raw_cohort(self, identity):
        return self.get("lens-sensor-raw-cohorts/" + identity)

    def list_lens_sensor_raw_cohorts(self):
        return self.get("lens-sensor-raw-cohorts")

    def lens_sensor_isp_replay_template(self):
        """Browse saved Lens x Sensor RAW for strict native-CFA ISP replay."""
        return self.get("lens-sensor-isp-replays/template")

    def submit_lens_sensor_isp_replay(self, specification):
        return self.post("lens-sensor-isp-replays", specification)

    def lens_sensor_isp_replay(self, identity):
        return self.get("lens-sensor-isp-replays/" + identity)

    def list_lens_sensor_isp_replays(self):
        return self.get("lens-sensor-isp-replays")

    def lens_sensor_calibrated_isp_template(self, *, source_lens_sensor_job_id=""):
        """Get the exact Lens RAW binding for a separately supplied ISP calibration."""
        return self.get(
            "lens-sensor-calibrated-isp/template?"
            + urlencode({"source_lens_sensor_job_id": source_lens_sensor_job_id})
        )

    def import_lens_sensor_isp_calibration(self, calibration_path):
        """Upload exact Lens-bound calibration JSON bytes with its SHA-256."""
        from pathlib import Path

        path = Path(calibration_path)
        content = path.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        response = self.http.post(
            "/api/v1/lens-sensor-calibrated-isp/calibrations/import",
            files={"calibration": (path.name, content, "application/json")},
            data={"sha256": checksum},
        )
        response.raise_for_status()
        return response.json()

    def submit_lens_sensor_calibrated_isp(self, specification):
        return self.post("lens-sensor-calibrated-isp", specification)

    def lens_sensor_calibrated_isp(self, identity):
        return self.get("lens-sensor-calibrated-isp/" + identity)

    def list_lens_sensor_calibrated_isp(self):
        return self.get("lens-sensor-calibrated-isp")

    def lens_sensor_isp_perception_template(
        self, *, source_lens_sensor_job_id="", source_lens_sensor_isp_job_id=""
    ):
        """Get the exact candidate RGB binding for supplied detection evidence."""
        return self.get(
            "lens-sensor-isp-perception/template?"
            + urlencode({
                "source_lens_sensor_job_id": source_lens_sensor_job_id,
                "source_lens_sensor_isp_job_id": source_lens_sensor_isp_job_id,
            })
        )

    def import_lens_sensor_isp_perception_artifact(self, kind, document):
        """Store canonical annotation, prediction or model-manifest JSON."""
        from .registry import canonical

        encoded = canonical(document).encode("utf-8")
        return self.post(
            "lens-sensor-isp-perception/artifacts",
            {
                "kind": kind,
                "document": document,
                "expected_sha256": hashlib.sha256(encoded).hexdigest(),
            },
        )

    def submit_lens_sensor_isp_perception(self, specification):
        return self.post("lens-sensor-isp-perception", specification)

    def lens_sensor_isp_perception(self, identity):
        return self.get("lens-sensor-isp-perception/" + identity)

    def list_lens_sensor_isp_perception(self):
        return self.get("lens-sensor-isp-perception")

    def lens_sensor_isp_perception_cohort_template(self):
        """List completed frame evaluations and the diagnostic-only cohort contract."""
        return self.get("lens-sensor-isp-perception-cohorts/template")

    def submit_lens_sensor_isp_perception_cohort(self, specification):
        return self.post("lens-sensor-isp-perception-cohorts", specification)

    def lens_sensor_isp_perception_cohort(self, identity):
        return self.get("lens-sensor-isp-perception-cohorts/" + identity)

    def list_lens_sensor_isp_perception_cohorts(self):
        return self.get("lens-sensor-isp-perception-cohorts")

    def export_lens_sensor_isp_perception_cohort(self, identity):
        """Return the checked, self-contained JSON export for one completed cohort."""
        return self.get("lens-sensor-isp-perception-cohorts/" + identity + "/export")

    def lens_sensor_adaptive_demosaic_template(self, *, source_lens_sensor_job_id=""):
        """Get source-bound adaptive demosaic options for saved Lens RAW."""
        path = "lens-sensor-adaptive-demosaic/template"
        if source_lens_sensor_job_id:
            path += "?" + urlencode({"source_lens_sensor_job_id": source_lens_sensor_job_id})
        return self.get(path)

    def submit_lens_sensor_adaptive_demosaic(self, specification):
        return self.post("lens-sensor-adaptive-demosaic", specification)

    def lens_sensor_adaptive_demosaic(self, identity):
        return self.get("lens-sensor-adaptive-demosaic/" + identity)

    def list_lens_sensor_adaptive_demosaic(self):
        return self.get("lens-sensor-adaptive-demosaic")

    def export_lens_sensor_adaptive_demosaic(self, identity):
        return self.get("lens-sensor-adaptive-demosaic/" + identity + "/export")

    def lens_sensor_isp_detector_template(
        self, *, source_lens_sensor_job_id="", source_lens_sensor_isp_job_id=""
    ):
        """Get exact saved ISP RGB sources for offline detector execution."""
        path = "lens-sensor-isp-detectors/template"
        query = {
            key: value for key, value in {
                "source_lens_sensor_job_id": source_lens_sensor_job_id,
                "source_lens_sensor_isp_job_id": source_lens_sensor_isp_job_id,
            }.items() if value
        }
        return self.get(path + ("?" + urlencode(query) if query else ""))

    def submit_lens_sensor_isp_detector(self, specification):
        return self.post("lens-sensor-isp-detectors", specification)

    def lens_sensor_isp_detector(self, identity):
        return self.get("lens-sensor-isp-detectors/" + identity)

    def list_lens_sensor_isp_detector(self):
        return self.get("lens-sensor-isp-detectors")

    def export_lens_sensor_isp_detector(self, identity):
        return self.get("lens-sensor-isp-detectors/" + identity + "/export")

    def saved_sensor_hdr_template(self, *, query=""):
        """Browse pinned sensor runs for a static RAW bracket with explicit limits."""
        return self.get("saved-sensor-hdr/template?" + urlencode({"q": query}))

    def submit_saved_sensor_hdr(self, specification):
        return self.post("saved-sensor-hdr", specification)

    def saved_sensor_hdr(self, identity):
        return self.get("saved-sensor-hdr/" + identity)

    def list_saved_sensor_hdr(self):
        return self.get("saved-sensor-hdr")

    def saved_sensor_lfm_template(self, *, query=""):
        """Browse pinned sensor runs for bounded PWM phase-dependent RAW diagnostics."""
        return self.get("saved-sensor-lfm/template?" + urlencode({"q": query}))

    def submit_saved_sensor_lfm(self, specification):
        return self.post("saved-sensor-lfm", specification)

    def saved_sensor_lfm(self, identity):
        return self.get("saved-sensor-lfm/" + identity)

    def list_saved_sensor_lfm(self):
        return self.get("saved-sensor-lfm")

    def retained_raw_dataset_template(
        self, *, source_kind="saved_sensor_quality", source_job_id=""
    ):
        """Browse completed quality or Lens × Sensor RAW jobs for exact export."""
        query = {"source_kind": source_kind}
        if source_job_id:
            query["source_job_id"] = source_job_id
        return self.get("retained-raw-datasets/template?" + urlencode(query))

    def import_retained_raw_annotation(self, document, expected_sha256=None):
        """Import one source-bound annotation; the server normalizes its hash."""
        payload = {"document": document}
        if expected_sha256 is not None:
            payload["expected_sha256"] = expected_sha256
        return self.post("retained-raw-datasets/annotations", payload)

    def submit_retained_raw_dataset(self, specification):
        return self.post("retained-raw-datasets", specification)

    def retained_raw_dataset(self, identity):
        return self.get("retained-raw-datasets/" + identity)

    def list_retained_raw_datasets(self):
        return self.get("retained-raw-datasets")

    def saved_sensor_isp_replay_template(self):
        """Browse retained quality RAW for a bounded existing ISP v2 replay."""
        return self.get("saved-sensor-isp-replays/template")

    def submit_saved_sensor_isp_replay(self, specification):
        return self.post("saved-sensor-isp-replays", specification)

    def saved_sensor_isp_replay(self, identity):
        return self.get("saved-sensor-isp-replays/" + identity)

    def list_saved_sensor_isp_replays(self):
        return self.get("saved-sensor-isp-replays")

    def saved_sensor_calibrated_isp_template(
        self, *, source_quality_job_id="", sensor_report_index=0, case_index=0
    ):
        """Get the exact calibration binding for one completed quality RAW case."""
        return self.get(
            "saved-sensor-calibrated-isp/template?"
            + urlencode(
                {
                    "source_quality_job_id": source_quality_job_id,
                    "sensor_report_index": sensor_report_index,
                    "case_index": case_index,
                }
            )
        )

    def import_saved_sensor_isp_calibration(self, calibration_path):
        """Upload exact JSON bytes; the server checks the supplied SHA-256."""
        from pathlib import Path

        path = Path(calibration_path)
        with path.open("rb") as stream:
            content = stream.read()
        checksum = hashlib.sha256(content).hexdigest()
        response = self.http.post(
            "/api/v1/saved-sensor-calibrated-isp/calibrations/import",
            files={"calibration": (path.name, content, "application/json")},
            data={"sha256": checksum},
        )
        response.raise_for_status()
        return response.json()

    def submit_saved_sensor_calibrated_isp(self, specification):
        return self.post("saved-sensor-calibrated-isp", specification)

    def saved_sensor_calibrated_isp(self, identity):
        return self.get("saved-sensor-calibrated-isp/" + identity)

    def list_saved_sensor_calibrated_isp(self):
        return self.get("saved-sensor-calibrated-isp")

    def saved_sensor_perception_template(self, *, quality_job_id="", sensor_run_id="", scene=""):
        """Get a pinned RGB image binding and external-prediction study contract."""
        return self.get(
            "saved-sensor-perception/template?"
            + urlencode(
                {
                    "quality_job_id": quality_job_id,
                    "sensor_run_id": sensor_run_id,
                    "scene": scene,
                }
            )
        )

    def import_saved_sensor_perception_artifact(self, kind, document):
        """Store a small canonical JSON label, prediction or model manifest."""
        encoded = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return self.post(
            "saved-sensor-perception/artifacts",
            {
                "kind": kind,
                "document": document,
                "expected_sha256": hashlib.sha256(encoded).hexdigest(),
            },
        )

    def submit_saved_sensor_perception(self, specification):
        return self.post("saved-sensor-perception", specification)

    def saved_sensor_perception(self, identity):
        return self.get("saved-sensor-perception/" + identity)

    def list_saved_sensor_perception(self):
        return self.get("saved-sensor-perception")

    def physics_lut_template(self):
        """Inspect bounded FDTD optical and TCAD collection proxy options."""
        return self.get("physics-lut/template")

    def submit_physics_lut_sensitivity(self, specification):
        return self.post("physics-lut", specification)

    def physics_lut_sensitivity(self, identity):
        return self.get("physics-lut/" + identity)

    def list_physics_lut_sensitivities(self):
        return self.get("physics-lut")

    def raw_temporal_sources(self):
        return self.get("raw-temporal/sources")

    def submit_raw_temporal(
        self,
        source_id,
        readout_id,
        *,
        cadence_ns,
        config,
        acknowledge_period_two_risk=False,
        name="Opt-in RAW temporal comparison",
    ):
        """Explicit research replay. Never change the original acquisition or ISP."""
        return self.post(
            "raw-temporal/replays",
            {
                "schema": "camera_raw_temporal_replay_v1",
                "name": name,
                "source_id": source_id,
                "readout_id": readout_id,
                "cadence_ns": cadence_ns,
                "config": config,
                "acknowledge_period_two_risk": acknowledge_period_two_risk,
            },
        )

    def raw_temporal_replay(self, identity):
        return self.get("raw-temporal/replays/" + identity)

    def retained_isp_template(self, source_id, readout_id):
        return self.get(
            "templates/retained-isp-model?"
            + urlencode({"source_id": source_id, "readout_id": readout_id})
        )

    def submit_retained_isp(
        self,
        source_id,
        readout_id,
        isp_model_id,
        *,
        name="Retained RAW code-domain ISP",
        force=False,
    ):
        return self.post(
            "replays",
            {
                "schema": "camera_retained_code_isp_replay_v1",
                "name": name,
                "source_kind": "run",
                "source_id": source_id,
                "isp_model_id": isp_model_id,
                "selector": {
                    "schema": "camera_retained_raw_selection_v1",
                    "readout_id": readout_id,
                },
                "force": force,
            },
        )

    def plan_retained_isp_comparison(self, specification):
        return self.post("retained-isp/comparisons/plan", specification)

    def temporal_signal_reference_template(self, source_id, readout_id, isp_model_id):
        return self.get(
            "retained-isp/temporal-references/template?"
            + urlencode(
                {
                    "source_id": source_id,
                    "readout_id": readout_id,
                    "isp_model_id": isp_model_id,
                }
            )
        )

    def import_temporal_signal_reference(self, metadata, image_path):
        from pathlib import Path

        path = Path(image_path)
        with path.open("rb") as stream:
            response = self.http.post(
                "/api/v1/retained-isp/temporal-references/import",
                data={"metadata": json.dumps(metadata, allow_nan=False)},
                files={"image": (path.name, stream, "application/octet-stream")},
            )
        response.raise_for_status()
        return response.json()

    def temporal_signal_references(self):
        return self.get("retained-isp/temporal-references")

    def temporal_signal_reference(self, identity):
        return self.get("retained-isp/temporal-references/" + identity)

    def temporal_signal_test_template(self, reference_id):
        return self.get("retained-isp/temporal-references/" + reference_id + "/test-template")

    def submit_retained_isp_comparison(self, plan):
        return self.post(
            "retained-isp/comparisons",
            {
                "specification": plan["frozen_request"]["specification"],
                "plan_hash": plan["plan_hash"],
            },
        )

    def retained_isp_comparison(self, identity):
        return self.get("retained-isp/comparisons/" + identity)

    def export_retained_isp_comparison(self, identity):
        return self.get("retained-isp/comparisons/" + identity + "/export")

    def retained_isp_comparison_bundle(self, identity):
        response = self.http.get("/api/v1/retained-isp/comparisons/" + identity + "/bundle")
        response.raise_for_status()
        return response.content

    def plan_retained_isp_joint_study(self, specification):
        return self.post("retained-isp/joint-studies/plan", specification)

    def submit_retained_isp_joint_study(self, plan):
        return self.post(
            "retained-isp/joint-studies",
            {
                "specification": plan["frozen_request"]["specification"],
                "plan_hash": plan["plan_hash"],
            },
        )

    def retained_isp_joint_studies(self):
        return self.get("retained-isp/joint-studies")

    def retained_isp_joint_study(self, identity):
        return self.get("retained-isp/joint-studies/" + identity)

    def export_retained_isp_joint_study(self, identity):
        return self.get("retained-isp/joint-studies/" + identity + "/export")

    def retained_isp_joint_study_bundle(self, identity):
        response = self.http.get("/api/v1/retained-isp/joint-studies/" + identity + "/bundle")
        response.raise_for_status()
        return response.content

    def plan_observable_isp_selection(self, specification):
        return self.post("retained-isp/observable-optimizer/selections/plan", specification)

    def submit_observable_isp_selection(self, plan):
        return self.post(
            "retained-isp/observable-optimizer/selections",
            {
                "specification": plan["frozen_request"]["specification"],
                "plan_hash": plan["plan_hash"],
            },
        )

    def observable_isp_selection(self, identity):
        return self.get("retained-isp/observable-optimizer/selections/" + identity)

    def observable_isp_selection_export(self, identity):
        return self.get("retained-isp/observable-optimizer/selections/" + identity + "/export")

    def plan_observable_isp_holdout(self, selection_job_id):
        return self.post(
            "retained-isp/observable-optimizer/holdouts/plan",
            {
                "selection_job_id": selection_job_id,
            },
        )

    def submit_observable_isp_holdout(self, plan):
        return self.post(
            "retained-isp/observable-optimizer/holdouts",
            {
                "selection_job_id": plan["frozen_request"]["selection_job_id"],
                "plan_hash": plan["plan_hash"],
            },
        )

    def observable_isp_holdout(self, identity):
        return self.get("retained-isp/observable-optimizer/holdouts/" + identity)

    def observable_isp_holdout_export(self, identity):
        return self.get("retained-isp/observable-optimizer/holdouts/" + identity + "/export")

    def plan_prospective_isp_selection(self, specification):
        return self.post("retained-isp/prospective-optimizer/selections/plan", specification)

    def submit_prospective_isp_selection(self, plan):
        return self.post(
            "retained-isp/prospective-optimizer/selections",
            {
                "specification": plan["frozen_request"]["specification"],
                "plan_hash": plan["plan_hash"],
            },
        )

    def prospective_isp_selections(self):
        return self.get("retained-isp/prospective-optimizer/selections")

    def prospective_isp_selection(self, identity):
        return self.get("retained-isp/prospective-optimizer/selections/" + identity)

    def export_prospective_isp_selection(self, identity):
        return self.get("retained-isp/prospective-optimizer/selections/" + identity + "/export")

    def plan_prospective_isp_holdout(self, selection_job_id, holdout_joint_study_id):
        return self.post(
            "retained-isp/prospective-optimizer/holdouts/plan",
            {
                "selection_job_id": selection_job_id,
                "holdout_joint_study_id": holdout_joint_study_id,
            },
        )

    def submit_prospective_isp_holdout(self, plan):
        return self.post(
            "retained-isp/prospective-optimizer/holdouts",
            {
                "selection_job_id": plan["frozen_request"]["selection_job_id"],
                "holdout_joint_study_id": plan["frozen_request"]["holdout_joint_study_id"],
                "plan_hash": plan["plan_hash"],
            },
        )

    def prospective_isp_holdouts(self):
        return self.get("retained-isp/prospective-optimizer/holdouts")

    def prospective_isp_holdout(self, identity):
        return self.get("retained-isp/prospective-optimizer/holdouts/" + identity)

    def export_prospective_isp_holdout(self, identity):
        return self.get("retained-isp/prospective-optimizer/holdouts/" + identity + "/export")

    def replay_sources(self):
        return self.get("replay-sources")

    def electrical_response_template(
        self, assembly_id, scene_sequence_id, control_sequence_id, capture_program_id
    ):
        return self.get(
            "electrical-response/template?"
            + urlencode(
                {
                    "assembly_id": assembly_id,
                    "scene_sequence_id": scene_sequence_id,
                    "control_sequence_id": control_sequence_id,
                    "capture_program_id": capture_program_id,
                }
            )
        )

    def plan_electrical_response(self, specification):
        return self.post("electrical-response/studies/plan", specification)

    def submit_electrical_response(self, plan, *, force=False):
        return self.post(
            "electrical-response/studies",
            {
                "specification": {**plan["frozen_request"]["specification"], "force": force},
                "plan_hash": plan["plan_hash"],
            },
        )

    def electrical_response(self, identity):
        return self.get("electrical-response/studies/" + identity)

    def cancel_electrical_response(self, identity):
        return self.post("electrical-response/studies/" + identity + "/cancel", {})

    def export_electrical_response(self, identity):
        return self.get("electrical-response/studies/" + identity + "/export")

    def electrical_response_bundle(self, identity):
        response = self.http.get("/api/v1/electrical-response/studies/" + identity + "/bundle")
        response.raise_for_status()
        return response.content

    def isp_calibration_template(self, source_kind, source_id, *, selector=None):
        query = {
            "source_kind": source_kind,
            "source_id": source_id,
        }
        if selector is not None:
            query.update(version=2, selector=json.dumps(selector, separators=(",", ":")))
        return self.get("templates/isp-calibration?" + urlencode(query))

    def import_isp_calibration(self, document):
        """Import declared calibration evidence; this does not fit or approve it."""
        return self.post("isp-calibrations", document)

    def advanced_isp_template(self, source_kind, source_id, *, calibration_id=None, selector=None):
        query = {
            "version": 3 if selector is not None else 2,
            "source_kind": source_kind,
            "source_id": source_id,
        }
        if selector is not None:
            query["selector"] = json.dumps(selector, separators=(",", ":"))
        if calibration_id is not None:
            query["calibration_id"] = calibration_id
        return self.get("templates/isp-profile?" + urlencode(query))

    def submit_replay(
        self,
        source_kind,
        source_id,
        isp_model_id,
        *,
        name="Preserved RAW ISP replay",
        force=False,
        selector=None,
    ):
        """Process preserved RAW through an ISP model without changing acquisition."""
        return self.post(
            "replays",
            {
                "name": name,
                "source_kind": source_kind,
                "source_id": source_id,
                "isp_model_id": isp_model_id,
                "force": force,
                **(
                    {"schema": "camera_isp_replay_v3", "selector": selector}
                    if selector is not None
                    else {}
                ),
            },
        )

    def wait_replay(self, job_id, *, timeout=150, poll_interval=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get("replays/" + job_id)
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                return job
            time.sleep(poll_interval)
        raise TimeoutError(f"ISP replay {job_id} is still active")

    def export_replay(self, job_id):
        return self.get("replays/" + job_id + "/export")

    def replay_bundle(self, job_id):
        """Return the verified evidence ZIP bytes; writing a destination is explicit."""
        response = self.http.get("/api/v1/replays/" + job_id + "/bundle")
        response.raise_for_status()
        return response.content

    def wait_characterization(self, job_id, *, timeout=150, poll_interval=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get("factory/jobs/" + job_id)
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                return job
            time.sleep(poll_interval)
        raise TimeoutError(f"Characterization {job_id} is still active")
