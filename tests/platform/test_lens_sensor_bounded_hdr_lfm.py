"""Opt-in strict PSF streaming for pinned joint HDR and PWM/LFM jobs."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import lens_sensor_hdr_bridge as hdr_bridge
from camera_platform import lens_sensor_hdr_job as hdr_job
from camera_platform import lens_sensor_lfm_job as lfm_job
from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient
from camera_platform.sensor_db_bridge import sensor_revision_catalog
from camera_platform.service import PlatformService
from camera_platform.worker import execute as execute_worker

WORKSPACE = Path(__file__).resolve().parents[2]
ROOT = WORKSPACE / "camerae2e_db"
P0014_REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
P0248_REVISION = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"


def _sensor() -> dict:
    if not (ROOT / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    return next(
        row for row in sensor_revision_catalog(root=ROOT)
        if row["report_code"] == "DEF-2512-801" and row["model"] == "db_estimate_v1"
    )


def _spec(module, lens_id: str) -> dict:
    sensor = _sensor()
    spec = module.default_bounded_spec()
    spec.update(
        lens_revision_id=lens_id,
        sensor_revision={"identifier": "DEF-2512-801", "run_id": sensor["run_id"]},
        sensor_size=[8, 8],
        accept_approximate_lens_radiometry=True,
    )
    if module is lfm_job:
        spec["acknowledge_simulation_limits"] = True
    return spec


def test_bounded_preflight_is_frozen_before_expensive_optics(tmp_path, monkeypatch):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    for module in (hdr_job, lfm_job):
        assert set(module.default_spec()) == set(module.default_bounded_spec())
        spec = _spec(module, P0014_REVISION)
        frozen = module.prepare(service, spec)
        budget = frozen["resource_preflight"]
        assert frozen["plugin_id"] == module.BOUNDED_SCHEMA
        assert budget["scope"] == "central_bounded_strict_raytrace"
        assert budget["status"] == "within_development_budget"
        assert budget["legacy_complete_float32_stack_gib"] > 6
        assert budget["estimated_bounded_working_gib"] < 3
        assert budget["execution_guaranteed"] is False

    def block_before_optics(_resource):
        raise ValueError("resource_blocked: test admission policy")

    def never_load_optics(*_args, **_kwargs):
        pytest.fail("lens optics loaded before bounded resource admission")

    monkeypatch.setattr(hdr_bridge, "require_bounded_psf_budget", block_before_optics)
    monkeypatch.setattr(hdr_bridge, "lens_patent_raytrace_optics", never_load_optics)
    sensor = _sensor()
    with pytest.raises(ValueError, match="resource_blocked"):
        hdr_bridge.evaluate_lens_sensor_hdr_bracket(
            P0014_REVISION, "DEF-2512-801", sensor_run_id=sensor["run_id"],
            root=ROOT, workspace=WORKSPACE, sensor_size=(8, 8),
            accept_approximate_lens_radiometry=True,
            psf_compute_mode="bounded_streaming",
        )


def test_bounded_hdr_p0014_real_job_retains_raw_and_psf_evidence(tmp_path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    spec = _spec(hdr_job, P0014_REVISION)
    queued = hdr_job.submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    result = hdr_job.execute(service, queued)
    service.registry.finish(queued["id"], result=result, error=None)
    finished = hdr_job.get(service, queued["id"])
    assert finished["status"] == "succeeded"
    summary = finished["result"]["summary"]
    report = summary["report"]
    assert summary["resource_preflight"] == queued["resource_preflight"]
    assert report["resource_preflight"] == queued["resource_preflight"]
    assert report["psf_compute_mode"] == "bounded_streaming"
    assert report["psf_execution"]["mode"] == "strict_bounded_streaming"
    assert report["psf_execution"]["complete_precomputed_stack"] is False
    assert report["fusion"]["valid_pixels"] > 0
    with np.load(
        service.registry.blob_path(summary["evidence"]["npz"]["sha256"]),
        allow_pickle=False,
    ) as evidence:
        assert evidence["original_raw_dn"].shape == (2, 8, 8)
        assert evidence["original_raw_dn"].dtype == np.dtype("int32")


def test_bounded_lfm_job_retains_original_raw_and_psf_evidence(tmp_path):
    service = PlatformService(tmp_path / "state", WORKSPACE)
    spec = _spec(lfm_job, P0248_REVISION)
    queued = lfm_job.submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    result = lfm_job.execute(service, queued)
    service.registry.finish(queued["id"], result=result, error=None)
    finished = lfm_job.get(service, queued["id"])
    assert finished["status"] == "succeeded"
    summary = finished["result"]["summary"]
    report = summary["report"]
    assert summary["resource_preflight"] == queued["resource_preflight"]
    assert report["resource_preflight"] == queued["resource_preflight"]
    assert report["psf_compute_mode"] == "bounded_streaming"
    assert report["psf_execution"]["mode"] == "strict_bounded_streaming"
    assert report["psf_execution"]["complete_precomputed_stack"] is False
    npz_content = service.registry.blob_path(report["artifacts"]["npz"]["sha256"]).read_bytes()
    with np.load(io.BytesIO(npz_content), allow_pickle=False) as evidence:
        assert evidence["original_pwm_raw_dn"].shape == (2, 4, 8, 8)
        assert evidence["original_pwm_raw_dn"].dtype == np.dtype("int32")


def test_bounded_lfm_original_raw_flows_into_saved_pwm_hdr_fusion(tmp_path):
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        service = app.state.service
        lfm_spec = _spec(lfm_job, P0248_REVISION)
        assert client.lens_sensor_lfm_template(scope="central_bounded")["specification"][
            "schema"
        ] == lfm_job.BOUNDED_SCHEMA
        source = client.submit_lens_sensor_lfm(lfm_spec)
        assert client.job_status(source["id"], expected_kind="lens_sensor_lfm")[
            "status"
        ] == "queued"
        assert service.registry.claim()["id"] == source["id"]
        execute_worker(service, source["id"])
        source = client.wait(source["id"], expected_kind="lens_sensor_lfm", timeout=5)
        assert source["status"] == "succeeded", source.get("error")
        assert client.lens_sensor_lfm(source["id"])["content_hash"] == source["content_hash"]
        hdr_spec = client.lens_sensor_pwm_hdr_template()["specification"]
        hdr_spec.update(
            source_lfm_job_id=source["id"], acknowledge_simulation_limits=True
        )
        queued = client.submit_lens_sensor_pwm_hdr(hdr_spec)
        assert client.job_status(queued["id"], expected_kind="lens_sensor_pwm_hdr")[
            "status"
        ] == "queued"
        assert service.registry.claim()["id"] == queued["id"]
        execute_worker(service, queued["id"])
        saved = client.wait(queued["id"], expected_kind="lens_sensor_pwm_hdr", timeout=5)
        assert client.lens_sensor_pwm_hdr(saved["id"])["content_hash"] == saved["content_hash"]
        report = saved["result"]["summary"]["report"]
        assert saved["status"] == "succeeded", saved.get("error")
        assert report["source_lfm_schema"] == "camerae2e_lens_sensor_pwm_lfm_v2"
        assert report["source_psf_compute_mode"] == "bounded_streaming"
        assert report["fused_hdr_pixels"] > 0
        assert report["fused_hdr_pixels"] < np.prod(report["original_pwm_raw_shape"][1:])
        with np.load(
            service.registry.blob_path(
                source["result"]["summary"]["report"]["artifacts"]["npz"]["sha256"]
            ),
            allow_pickle=False,
        ) as source_evidence:
            original = np.asarray(source_evidence["original_pwm_raw_dn"])
        with np.load(
            service.registry.blob_path(
                saved["result"]["summary"]["evidence"]["npz"]["sha256"]
            ),
            allow_pickle=False,
        ) as hdr_evidence:
            np.testing.assert_array_equal(hdr_evidence["original_pwm_raw_dn"], original)
        assert report["original_pwm_raw_sha256"] == hashlib.sha256(original.tobytes()).hexdigest()


def test_bounded_templates_have_distinct_opt_in_schema(tmp_path):
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        for endpoint, old_schema, bounded_schema in (
            ("lens-sensor-hdr", hdr_job.SCHEMA, hdr_job.BOUNDED_SCHEMA),
            ("lens-sensor-lfm", lfm_job.SCHEMA, lfm_job.BOUNDED_SCHEMA),
        ):
            legacy = transport.get(f"/api/v1/{endpoint}/template").json()
            bounded = transport.get(
                f"/api/v1/{endpoint}/template", params={"scope": "central_bounded"}
            ).json()
            assert legacy["specification"]["schema"] == old_schema
            assert bounded["specification"]["schema"] == bounded_schema
            assert set(legacy["specification"]) == set(bounded["specification"])
            assert bounded["resource_contract"]["execution_guaranteed"] is False
