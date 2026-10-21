"""A pinned Lens DB and saved Sensor DB produce one attributable PWM RAW study."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.hdr_lfm_reference_packet import (
    MODE_SCHEMA as REFERENCE_MODE_LOG_SCHEMA,
)
from camera_platform.hdr_lfm_reference_packet import (
    REFERENCE_SCHEMA as REFERENCE_RATE_LOG_SCHEMA,
)
from camera_platform.hdr_lfm_reference_packet import (
    WAVEFORM_SCHEMA as REFERENCE_WAVEFORM_LOG_SCHEMA,
)
from camera_platform.lens_sensor_lfm_job import (
    default_spec,
    specification,
)
from camera_platform.lens_sensor_lfm_job import prepare as prepare_lfm
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService
from camera_platform.worker import execute

WORKSPACE = Path(__file__).resolve().parents[2]


def test_joint_lfm_requires_real_revisions_and_explicit_approximation():
    spec = default_spec()
    spec.update(
        lens_revision_id="lens-revision",
        sensor_revision={"identifier": "sensor", "run_id": "saved-run"},
    )
    with pytest.raises(ValueError, match="Accept approximate"):
        specification(spec)
    spec["accept_approximate_lens_radiometry"] = True
    spec["acknowledge_simulation_limits"] = True
    assert specification(spec)["sensor_size"] == [8, 8]
    with pytest.raises(ValueError, match="even"):
        specification(dict(spec, sensor_size=[9, 8]))
    with pytest.raises(ValueError, match="ascending"):
        specification(dict(spec, exposure_s=[0.004, 0.001]))


def test_real_joint_lfm_api_sdk_worker_and_retained_raw(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        template = client.lens_sensor_lfm_template()
        lens = next(
            row for row in template["lens_catalog"]["revisions"]
            if row["preview_status"] == "ready"
        )
        sensor = next(
            row for row in template["sensor_catalog"]
            if row["report_code"] == "DEF-2512-801"
        )
        spec = template["specification"]
        spec.update(
            lens_revision_id=lens["revision_id"],
            sensor_revision={"identifier": "DEF-2512-801", "run_id": sensor["run_id"]},
            accept_approximate_lens_radiometry=True,
            acknowledge_simulation_limits=True,
        )
        queued = client.submit_lens_sensor_lfm(spec)
        status = client.job_status(queued["id"], expected_kind="lens_sensor_lfm")
        assert status["detail_path"] == f"lens-sensor-lfm/{queued['id']}"
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(queued["id"], expected_kind="lens_sensor_lfm", timeout=5)
        assert finished["status"] == "succeeded", finished.get("error")
        summary = finished["result"]["summary"]
        report = summary["report"]
        assert report["source_boundary"]["lens_geometric_psf_used"] is True
        assert report["source_boundary"]["saved_sensor_simulation_run_used"] is True
        assert report["source_boundary"][
            "saved_fpn_parameters_applied_to_generated_virtual_window_map"
        ] is True
        assert report["source_boundary"]["saved_fpn_parameter_provenance"] == (
            "saved_simulation_profile_assumptions"
        )
        assert report["source_boundary"]["sensor_readout_profile"] == (
            "generic_assumption"
        )
        assert report["source_boundary"]["sensor_readout_classification"] == (
            "unmeasured_assumption"
        )
        assert report["source_boundary"]["measured_fpn_map_reused"] is False
        assert report["source_boundary"]["fpn_physical_validation"] == "not_established"
        assert report["controls"]["saved_fpn_parameter_assumptions"]["prnu_fraction"] == (
            0.005
        )
        provenance = report["geometry_provenance"]
        assert provenance["configured_sensor_scope"] == "virtual_simulation_window_only"
        assert provenance["configured_sensor_size"] == [8, 8]
        assert provenance["saved_native_array_size"] == [32, 32]
        assert provenance["saved_native_array_crop_origin"] is None
        assert provenance["saved_native_array_crop_mapping"] == "not_established"
        assert provenance["saved_native_cfa"]["pattern"] == [[1, 2], [2, 3]]
        assert summary["native_cfa"] == provenance["saved_native_cfa"]
        assert report["linearity_check"]["status"] == "passed"
        assert len(report["cases"]) == 2
        npz = transport.get(report["artifacts"]["npz"]["url"]).content
        with np.load(io.BytesIO(npz), allow_pickle=False) as arrays:
            raw = arrays["original_pwm_raw_dn"]
            constant = arrays["original_constant_raw_dn"]
            assert raw.dtype == np.dtype("int32")
            assert raw.shape == (2, 4, 8, 8)
            assert constant.shape == (2, 8, 8)
        assert client.lens_sensor_lfm(queued["id"])["result"]["summary"] == summary


def test_quad_bayer_lfm_template_retains_actual_native_cfa(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    service = PlatformService(tmp_path / "state", WORKSPACE)
    from camera_platform.lens_catalog_bridge import lens_revision_catalog
    from camera_platform.sensor_db_bridge import sensor_revision_catalog

    lens = next(
        row for row in lens_revision_catalog(catalog_root=WORKSPACE / "camerae2e_db")["revisions"]
        if row["preview_status"] == "ready"
    )
    sensor = next(
        row for row in sensor_revision_catalog(root=WORKSPACE / "camerae2e_db")
        if row["report_code"] == "DEP-2209-801" and row["model"] == "db_estimate_v1"
    )
    spec = default_spec()
    spec.update(
        lens_revision_id=lens["revision_id"],
        sensor_revision={"identifier": "DEP-2209-801", "run_id": sensor["run_id"]},
        accept_approximate_lens_radiometry=True,
        acknowledge_simulation_limits=True,
    )
    frozen = prepare_lfm(service, spec)
    assert frozen["native_cfa"]["pattern"] == [
        [1, 1, 2, 2], [1, 1, 2, 2], [2, 2, 3, 3], [2, 2, 3, 3],
    ]


def test_imported_timing_mode_drives_saved_lfm_and_hdr_worker_chain(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    proof_dir = os.environ.get("CAMERA_PLATFORM_HDR_LFM_PROOF_DIR")
    proof_root = Path(proof_dir).resolve() if proof_dir else None
    if proof_root is not None:
        proof_root.mkdir(parents=True, exist_ok=True)
    state_dir = (proof_root / f"state-{uuid4().hex}" if proof_root is not None
                 else tmp_path / "timed-state")
    app = create_app(state_dir, WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        template = client.lens_sensor_lfm_template(scope="central_bounded_timed")
        lens = next(row for row in template["lens_catalog"]["revisions"]
                    if row["preview_status"] == "ready")
        sensor = next(row for row in template["sensor_catalog"]
                      if row["report_code"] == "DEF-2512-801")
        spec = template["specification"]
        spec.update(
            lens_revision_id=lens["revision_id"],
            sensor_revision={"identifier": sensor["report_code"], "run_id": sensor["run_id"]},
            accept_approximate_lens_radiometry=True,
            acknowledge_simulation_limits=True,
        )
        mode_template = transport.post(
            "/api/v1/lens-sensor-lfm/timing-modes/template",
            json={
                "sensor_revision": spec["sensor_revision"],
                "sensor_size": spec["sensor_size"],
                "exposure_s": spec["exposure_s"],
            },
        )
        assert mode_template.status_code == 200, mode_template.text
        suggested = mode_template.json()
        assert suggested["source_sensor_timing_available"] is False
        mode = suggested["timing_mode_document_template"]
        mode["mode_id"] = "authored-40us-test"
        mode["timing"]["line_time_s"] = 40e-6
        content = json.dumps(mode, sort_keys=True, separators=(",", ":")).encode()
        sha = hashlib.sha256(content).hexdigest()
        imported = transport.post(
            "/api/v1/lens-sensor-lfm/timing-modes/import",
            data={"sha256": sha},
            files={"timing_mode": ("timing-mode.json", content, "application/json")},
        )
        assert imported.status_code == 201, imported.text
        assert imported.json()["timing_mode_sha256"] == sha
        assert imported.json()["record_id"] == f"timing-mode-{sha}"
        downloaded = transport.get(imported.json()["artifact"]["url"])
        assert downloaded.status_code == 200
        assert downloaded.content == content
        imported_again = transport.post(
            "/api/v1/lens-sensor-lfm/timing-modes/import",
            data={"sha256": sha},
            files={"timing_mode": ("timing-mode.json", content, "application/json")},
        )
        assert imported_again.status_code == 201
        assert imported_again.json()["record_id"] == imported.json()["record_id"]
        source_bytes = b"declared vendor timing table"
        sourced = copy.deepcopy(mode)
        sourced["mode_id"] = "source-pinned-40us-test"
        sourced["provenance"].update(
            source_kind="vendor_document_declared",
            source_uri="fixture://vendor-timing-table",
            source_document_sha256=hashlib.sha256(source_bytes).hexdigest(),
        )
        sourced_content = json.dumps(
            sourced, sort_keys=True, separators=(",", ":")
        ).encode()
        sourced_sha = hashlib.sha256(sourced_content).hexdigest()
        sourced_import = transport.post(
            "/api/v1/lens-sensor-lfm/timing-modes/import",
            data={"sha256": sourced_sha},
            files={
                "timing_mode": ("source-pinned.json", sourced_content, "application/json"),
                "source_evidence": (
                    "timing-table.bin", source_bytes, "application/octet-stream"
                ),
            },
        )
        assert sourced_import.status_code == 201
        for descriptor, expected in (
            (sourced_import.json()["artifact"], sourced_content),
            (sourced_import.json()["source_evidence"], source_bytes),
        ):
            evidence = transport.get(descriptor["url"])
            assert evidence.status_code == 200
            assert evidence.content == expected
        assert imported.json()["verification"]["timing_value"] == (
            "declared_not_independently_verified"
        )
        incompatible = copy.deepcopy(mode)
        incompatible["binding"]["exposure_s"][1] = 0.008
        bad_content = json.dumps(
            incompatible, sort_keys=True, separators=(",", ":")
        ).encode()
        bad_sha = hashlib.sha256(bad_content).hexdigest()
        bad_import = transport.post(
            "/api/v1/lens-sensor-lfm/timing-modes/import",
            data={"sha256": bad_sha},
            files={"timing_mode": ("wrong-mode.json", bad_content, "application/json")},
        )
        assert bad_import.status_code == 201, bad_import.text
        before_jobs = len(app.state.service.registry.list_runs())
        rejected = transport.post(
            "/api/v1/lens-sensor-lfm",
            json={**spec, "timing_mode_sha256": bad_sha},
        )
        assert rejected.status_code == 422
        assert "Timing mode differs" in rejected.text
        assert len(app.state.service.registry.list_runs()) == before_jobs
        # Execute the source-pinned declaration, not the earlier authored
        # fixture. Its bytes are pinned, while the asserted numeric value is
        # still deliberately unverified.
        spec["timing_mode_sha256"] = sourced_sha
        queued = client.submit_lens_sensor_lfm(spec)
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(queued["id"], expected_kind="lens_sensor_lfm", timeout=5)
        assert finished["status"] == "succeeded", finished.get("error")
        summary = client.lens_sensor_lfm(queued["id"])["result"]["summary"]
        report = summary["report"]
        assert report["schema"] == "camerae2e_lens_sensor_pwm_lfm_v3"
        assert report["controls"]["line_time_s"] == 40e-6
        assert report["controls"]["timing_mode_sha256"] == sourced_sha
        assert report["controls"]["timing_mode"] == sourced
        assert report["source_boundary"]["timing_value_verification"] == (
            "declared_not_independently_verified"
        )
        lfm_readiness = client.hdr_lfm_mode_readiness(queued["id"])
        assert lfm_readiness["model_execution"]["raw_shape"] == [2, 4, 8, 8]
        assert lfm_readiness["reference_comparison"]["status"] == "not_available"
        assert lfm_readiness["next_workflows"][0]["kind"] == (
            "reconstruct_original_pwm_raw"
        )
        assert report["cases"][0]["phases"][0]["row_timing"]["row_exposure_start_s"][1] == (
            40e-6
        )
        with np.load(io.BytesIO(transport.get(
            report["artifacts"]["npz"]["url"]
        ).content), allow_pickle=False) as evidence:
            original_raw = evidence["original_pwm_raw_dn"].copy()
        hdr_spec = client.lens_sensor_pwm_hdr_template()["specification"]
        hdr_spec.update(
            source_lfm_job_id=queued["id"], acknowledge_simulation_limits=True
        )
        hdr_queued = client.submit_lens_sensor_pwm_hdr(hdr_spec)
        assert app.state.service.registry.claim()["id"] == hdr_queued["id"]
        execute(app.state.service, hdr_queued["id"])
        hdr = client.wait(
            hdr_queued["id"], expected_kind="lens_sensor_pwm_hdr", timeout=5
        )
        assert hdr["status"] == "succeeded", hdr.get("error")
        hdr_summary = hdr["result"]["summary"]
        assert hdr_summary["report"]["source_lfm_schema"] == report["schema"]
        assert hdr_summary["report"]["waveform"]["line_time_s"] == 40e-6
        with np.load(io.BytesIO(transport.get(
            hdr_summary["evidence"]["npz"]["url"]
        ).content), allow_pickle=False) as evidence:
            np.testing.assert_array_equal(evidence["original_pwm_raw_dn"], original_raw)

        readiness = client.hdr_lfm_mode_readiness(hdr_queued["id"])
        assert readiness["model_execution"]["status"] == (
            "source_artifacts_hash_reverified"
        )
        assert readiness["reference_comparison"]["status"] == "intake_available"
        assert readiness["product_hdr_lfm_decision"]["status"] == "not_established"
        assert readiness["sensor_operating_point"]["source_conditions"][
            "readout_rate_fps"
        ] is None
        intake = client.hdr_lfm_reference_template(hdr_queued["id"])
        packet = intake["metadata"]
        packet["physical_instance"] = "fixture-only-not-a-camera"
        packet["instrument"] = {"id": "fixture", "calibration_revision": "fixture-1"}
        packet["mode"]["native_crop_origin_row_col"] = [0, 0]
        packet["raw_acquisition"]["source_uri"] = "fixture://raw-acquisition"
        packet["reference_rate"]["source_uri"] = "fixture://reference-rate"
        packet["acknowledge_comparison_limits"] = True
        files = {
            "measured_raw": io.BytesIO(),
            "measured_reference_rate": io.BytesIO(),
        }
        fixture_raw = original_raw.copy()
        fixture_raw[0, 0, 0, 0] += 1
        fixture_rate = np.full((4, 8, 8), 100.0, dtype=np.float64)
        np.save(files["measured_raw"], fixture_raw, allow_pickle=False)
        np.save(files["measured_reference_rate"], fixture_rate, allow_pickle=False)

        def encoded(document):
            return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()

        def sha(content):
            return hashlib.sha256(content).hexdigest()

        raw_bytes = files["measured_raw"].getvalue()
        rate_bytes = files["measured_reference_rate"].getvalue()
        for key in ("mode_log", "waveform_log", "reference_log"):
            packet["sources"][key]["uri"] = "fixture://" + key
        frame_order = []
        for index in range(8):
            exposure_index, phase_index = divmod(index, 4)
            fraction = packet["stimulus"]["phase_fractions_of_period"][phase_index]
            frame_order.append({
                "array_index": index, "frame_id": f"synthetic-raw-{index}",
                "acquisition_order": index,
                "row_zero_timestamp_ns": index * 1_000_000,
                "exposure_index": exposure_index, "phase_index": phase_index,
                "exposure_s": packet["raw_acquisition"]["exposure_s"][exposure_index],
                "phase_fraction_of_period": fraction,
                "row_zero_phase_s": fraction * packet["stimulus"]["pwm_period_s"],
                "raw_frame_sha256": sha(
                    np.ascontiguousarray(fixture_raw[exposure_index, phase_index])
                    .tobytes(order="C")
                ),
            })
        mode_log = {
            "schema": REFERENCE_MODE_LOG_SCHEMA,
            "source_uri": packet["sources"]["mode_log"]["uri"],
            "source_hdr_job_id": packet["source_hdr_job_id"],
            "source_hdr_report_sha256": packet["source_hdr_report_sha256"],
            "sensor_revision": copy.deepcopy(packet["sensor_revision"]),
            "physical_instance": packet["physical_instance"],
            "instrument": copy.deepcopy(packet["instrument"]),
            "mode": copy.deepcopy(packet["mode"]),
            "raw_acquisition": copy.deepcopy(packet["raw_acquisition"]),
            "raw_file_sha256": sha(raw_bytes),
            "native_cfa_pattern": copy.deepcopy(summary["native_cfa"]["pattern"]),
            "native_cfa_tile_origin_row_col": [0, 0],
            "frame_order": frame_order,
        }
        files["mode_log"] = encoded(mode_log)
        waveform_log = {
            "schema": REFERENCE_WAVEFORM_LOG_SCHEMA,
            "source_uri": packet["sources"]["waveform_log"]["uri"],
            "source_hdr_job_id": packet["source_hdr_job_id"],
            "source_hdr_report_sha256": packet["source_hdr_report_sha256"],
            "mode_id": packet["mode"]["mode_id"],
            "timing_mode_sha256": packet["mode"]["timing_mode_sha256"],
            "mode_log_sha256": sha(files["mode_log"]),
            "raw_file_sha256": sha(raw_bytes),
            "line_time_s": packet["mode"]["line_time_s"],
            "phase_alignment": packet["mode"]["phase_alignment"],
            "stimulus": copy.deepcopy(packet["stimulus"]),
        }
        files["waveform_log"] = encoded(waveform_log)
        reference_log = {
            "schema": REFERENCE_RATE_LOG_SCHEMA,
            "source_uri": packet["sources"]["reference_log"]["uri"],
            "source_hdr_job_id": packet["source_hdr_job_id"],
            "source_hdr_report_sha256": packet["source_hdr_report_sha256"],
            "mode_log_sha256": sha(files["mode_log"]),
            "waveform_log_sha256": sha(files["waveform_log"]),
            "raw_file_sha256": sha(raw_bytes),
            "reference_rate_file_sha256": sha(rate_bytes),
            "reference_rate": copy.deepcopy(packet["reference_rate"]),
            "source_acquisition_id": "synthetic-reference-not-physical",
            "phase_order": [
                {
                    "phase_index": phase,
                    "phase_fraction_of_period": packet["stimulus"][
                        "phase_fractions_of_period"
                    ][phase],
                    "reference_phase_sha256": sha(
                        np.ascontiguousarray(fixture_rate[phase]).tobytes(order="C")
                    ),
                }
                for phase in range(4)
            ],
            "calibration": {
                "reference_instrument_id": "synthetic-separate-meter",
                "calibration_revision": "synthetic-cal-1",
                "method_and_version": "synthetic-unverified-v1",
                "calibration_document_sha256": sha(b"synthetic calibration fixture"),
                "relative_standard_uncertainty": 0.1,
                "applied_to_reference_array": True,
                "uses_uploaded_raw": False,
                "uses_model_output": False,
            },
        }
        files["reference_log"] = encoded(reference_log)
        for key in ("mode_log", "waveform_log", "reference_log"):
            packet["sources"][key] = {
                "uri": "fixture://" + key,
                "sha256": hashlib.sha256(files[key]).hexdigest(),
            }
        mismatched = copy.deepcopy(packet)
        mismatched["sources"]["waveform_log"]["sha256"] = "0" * 64
        rejected_reference = transport.post(
            "/api/v1/hdr-lfm-reference-comparisons",
            data={"metadata": json.dumps(mismatched)},
            files={key: (key, value.getvalue() if isinstance(value, io.BytesIO)
                         else value, "application/octet-stream")
                   for key, value in files.items()},
        )
        assert rejected_reference.status_code == 422
        assert len(app.state.service.registry.list_runs()) == 2
        contradictory = copy.deepcopy(packet)
        bad_waveform = copy.deepcopy(waveform_log)
        bad_waveform["line_time_s"] = 50e-6
        bad_files = dict(files, waveform_log=encoded(bad_waveform))
        contradictory["sources"]["waveform_log"]["sha256"] = sha(
            bad_files["waveform_log"]
        )
        rejected_content = transport.post(
            "/api/v1/hdr-lfm-reference-comparisons",
            data={"metadata": json.dumps(contradictory)},
            files={key: (key, value.getvalue() if isinstance(value, io.BytesIO)
                         else value, "application/octet-stream")
                   for key, value in bad_files.items()},
        )
        assert rejected_content.status_code == 422
        assert "waveform_log.line_time_s" in rejected_content.text
        assert len(app.state.service.registry.list_runs()) == 2
        uploaded = transport.post(
            "/api/v1/hdr-lfm-reference-comparisons",
            data={"metadata": json.dumps(packet)},
            files={key: (key, value.getvalue() if isinstance(value, io.BytesIO)
                         else value, "application/octet-stream")
                   for key, value in files.items()},
        )
        assert uploaded.status_code == 202, uploaded.text
        assert app.state.service.registry.claim()["id"] == uploaded.json()["id"]
        execute(app.state.service, uploaded.json()["id"])
        comparison = client.wait(
            uploaded.json()["id"], expected_kind="hdr_lfm_reference_comparison", timeout=5
        )
        assert comparison["status"] == "succeeded", comparison.get("error")
        result = comparison["result"]["summary"]
        assert result["comparison_scope"] == "unregistered_global_means_only"
        assert result["packet_consistency"]["internal_consistency"] == (
            "verified_against_uploaded_arrays_and_frozen_job"
        )
        assert result["packet_consistency"]["source_authenticity"] == (
            "declared_not_independently_verified"
        )
        qualification = result["registration_qualification"]
        assert qualification["status"] == (
            "blocked_pending_independent_registration_evidence"
        )
        assert qualification["verified_packet_bindings"]["model_native_crop_mapping"] == (
            "not_established"
        )
        assert qualification["declared_timing_window"]["waveform_trace_verified"] is False
        assert qualification["requirements"]["pixelwise_error_available"] is False
        assert qualification["requirements"]["measured_fusion_error_available"] is False
        assert result["physical_validation"] == "not_established"
        assert result["product_hdr_lfm_decision"] == "inconclusive"
        assert client.hdr_lfm_reference_comparison(uploaded.json()["id"])["result"] == (
            comparison["result"]
        )
        if proof_root is not None:
            # Opt-in persistent evidence from actual saved Jobs. All supplied
            # acquisition/reference inputs here are synthetic test fixtures.
            proof = {
                "schema": "camerae2e_hdr_lfm_bounded_readiness_proof_v1",
                "state_dir": str(state_dir.relative_to(WORKSPACE)),
                "scope": "bounded_api_sdk_worker_test_fixture_not_physical_measurement",
                "test": "tests/platform/test_lens_sensor_lfm_job.py::"
                "test_imported_timing_mode_drives_saved_lfm_and_hdr_worker_chain",
                "sensor_revision": spec["sensor_revision"],
                "source_timing": {
                    "mode_id": sourced["mode_id"],
                    "mode_json_sha256": sourced_sha,
                    "source_document_sha256": hashlib.sha256(source_bytes).hexdigest(),
                    "source_uri": sourced["provenance"]["source_uri"],
                    "value_verification": "declared_not_independently_verified",
                },
                "saved_jobs": {
                    "pwm_raw": {
                        "id": queued["id"], "status": finished["status"],
                        "npz_sha256": report["artifacts"]["npz"]["sha256"],
                        "readiness": {
                            "model_execution": lfm_readiness["model_execution"]["status"],
                            "reference_comparison": lfm_readiness["reference_comparison"]["status"],
                            "mode_blocker_codes": [block["code"] for block in
                                                   lfm_readiness["mode_execution"]["blockers"]],
                        },
                    },
                    "pwm_hdr": {
                        "id": hdr_queued["id"], "status": hdr["status"],
                        "npz_sha256": hdr_summary["evidence"]["npz"]["sha256"],
                        "original_pwm_raw_sha256": hdr_summary["report"][
                            "original_pwm_raw_sha256"
                        ],
                        "readiness": {
                            "model_execution": readiness["model_execution"]["status"],
                            "reference_comparison": readiness["reference_comparison"]["status"],
                            "mode_blocker_codes": [block["code"] for block in
                                                   readiness["mode_execution"]["blockers"]],
                            "product_hdr_lfm_decision": readiness[
                                "product_hdr_lfm_decision"
                            ]["status"],
                        },
                    },
                    "reference_comparison": {
                        "id": uploaded.json()["id"], "status": comparison["status"],
                        "input_sha256": {
                            key: hashlib.sha256(
                                value.getvalue() if isinstance(value, io.BytesIO) else value
                            ).hexdigest() for key, value in files.items()
                        },
                        "comparison_scope": result["comparison_scope"],
                        "source_authenticity": result["source_authenticity"],
                        "physical_validation": result["physical_validation"],
                        "product_hdr_lfm_decision": result["product_hdr_lfm_decision"],
                    },
                },
                "source_audit": "hdr-lfm-sensor-mode-audit.json",
            }
            (proof_root / "hdr-lfm-mode-readiness-bounded.json").write_text(
                json.dumps(proof, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
