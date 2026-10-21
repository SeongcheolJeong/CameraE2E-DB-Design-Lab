"""IRCF contracts/lifecycle on bounded artifact fixtures, not optical performance tests."""

from __future__ import annotations

import copy
import io
import json
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import reference_ircf_engine as engine
from camera_platform import reference_ircf_job as job
from camera_platform import reference_ircf_optics as optics
from camera_platform import reference_ircf_spectra as spectra
from camera_platform import reference_sensor_job
from camera_platform import reference_workshop_job as workshop
from camera_platform.api import create_app
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient
from camera_platform.virtual_lab_job import sha256


def first_pair_spec():
    spec = job.template()
    spec.update(fields=["center"], charts=["color"], illuminants=["day"],
                filters=["baseline", "nir_leak_1pct"], include_angle_diagnostic=False)
    return spec


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(schema="product_ircf"),
    lambda s: s.update(fields=["center", "center"]),
    lambda s: s.update(fields=[]),
    lambda s: s.update(charts=["night_photo"]),
    lambda s: s.update(illuminants=["unknown"]),
    lambda s: s.update(filters=["baseline", "baseline"]),
    lambda s: s.update(wavelength_step_nm=20),
    lambda s: s.update(wavelength_step_nm=True),
    lambda s: s.update(nir_scale=float("nan")),
    lambda s: s.update(nir_scale=True),
    lambda s: s.update(frames=1),
    lambda s: s.update(exposure_ms=8),
    lambda s: s.update(gain=2),
    lambda s: s.update(luminance_cd_m2=2000),
    lambda s: s.update(effective_index=float("inf")),
    lambda s: s.update(effective_index=1.1),
    lambda s: s.update(angular_mode="object_field_angle"),
    lambda s: s.update(include_angle_diagnostic=1),
    lambda s: s.update(seed=True),
    lambda s: s.update(seed=-1),
    lambda s: s["source"].update(manifest_sha256="latest"),
    lambda s: s["source"].update(candidate_id="../candidate-01"),
    lambda s: s["isp"].update(display_white_level=float("nan")),
    lambda s: s["isp"].update(tone_gamma=True),
    lambda s: s.update(automatic_fallback=True),
])
def test_schema_rejects_unsupported_and_ambiguous_conditions(mutate):
    baseline = job.template()
    assert job.specification(baseline) == baseline
    changed = copy.deepcopy(baseline)
    mutate(changed)
    with pytest.raises(ValueError):
        job.specification(changed)


def test_full_study_and_first_pair_have_exact_distinct_case_programs():
    spec = job.template()
    frozen = copy.deepcopy(spec)
    full = job.case_specs(job.specification(spec))
    assert spec == frozen
    assert len(full) == 34
    main = [c for c in full if c["angular_mode"] == "pupil"]
    diagnostics = [c for c in full if c["angular_mode"] == "independent"]
    assert len(main) == 32 and len(diagnostics) == 2
    assert len({tuple(c[k] for k in ("field_id", "illuminant", "chart", "filter_id"))
                for c in main}) == 32
    assert {c["illuminant"] for c in diagnostics} == {"day", "ill_a"}
    assert all(c["field_id"] == "outer-ne" and c["chart"] == "uniform"
               and c["filter_id"] == "baseline" for c in diagnostics)
    assert [c["id"] for c in full] == [f"case-{i:03d}" for i in range(34)]
    assert len({c["seed_offset"] + i for c in full for i in range(8)}) == 34 * 8
    pair = job.case_specs(job.specification(first_pair_spec()))
    assert len(pair) == 2
    assert [c["filter_id"] for c in pair] == ["baseline", "nir_leak_1pct"]
    for change in ({"fields": ["center"]}, {"charts": ["color"]},
                   {"filters": ["nir_leak_1pct"]}, {"angular_mode": "independent"}):
        selected = {**spec, **change}
        cases = job.case_specs(selected)
        assert len(cases) == (len(selected["fields"]) * len(selected["charts"])
                              * len(selected["illuminants"]) * len(selected["filters"]))


def test_source_requires_pinned_unchanged_lens(monkeypatch, tmp_path):
    capture = {"configuration": {"lens": workshop.defaults()["lens"]}}
    seen = []

    def load(service, spec, verify=False):
        seen.append((spec, verify))
        return tmp_path, capture

    monkeypatch.setattr(reference_sensor_job, "_load_source", load)
    assert job.source(None, first_pair_spec(), verify=True) == (tmp_path, capture)
    assert seen[0][0]["charts"] == ["uniform", "color"] and seen[0][1]
    capture["configuration"]["lens"]["sensor_shift_um"] = 10
    with pytest.raises(ValueError, match="unchanged patent"):
        job.source(None, first_pair_spec())


@pytest.fixture
def fixture_app(tmp_path, monkeypatch):
    """Exercise queue/files/SDK with lightweight declared artifacts, not fake optics claims."""
    monkeypatch.setenv("CAMERA_PLATFORM_WORKSHOP_ROOT", str(tmp_path / "external"))
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "raw.bin").write_bytes(b"unchanged original source")
    original_sha = sha256(source_root / "raw.bin")
    source_checks = []

    def source(service, spec, verify=False):
        assert sha256(source_root / "raw.bin") == original_sha
        source_checks.append(verify)
        return source_root, {"configuration": workshop.defaults()}

    def solve(workspace, cache, step_nm=10, on_progress=None):
        folder = tmp_path / "optical-fixture"
        folder.mkdir(exist_ok=True)
        np.savez_compressed(folder / "optics.npz", fixture=np.array([step_nm]))
        fields = [{"id": field, "status": "succeeded"} for field in job.FIELDS]
        return {"result": {"fields": fields, "revision": "fixture-optics"}}, folder

    def calibrations(solution, assets, response, fields, filters, n_eff):
        return {}, {f["id"]: np.ones((2, 2)) for f in fields}, {"fixture": True}

    def run_case(root, item, spec, solution, assets, pixel, matrices, lsc, progress):
        progress()
        folder = root / "cases" / item["id"]
        folder.mkdir(parents=True)
        # Small integer array is an artifact fixture, intentionally not a sensor simulation.
        np.savez_compressed(folder / "capture.npz", raw_frames=np.zeros((8, 4, 4), np.uint16))
        for ccm in ("baseline", "refit"):
            engine.png(folder / f"{ccm}.png", np.zeros((4, 4, 3)))
        metrics = {"nir_fraction": 0.02, "delta_e00_mean": None,
                   "delta_e00_max": None, "color_valid_patches": 0,
                   "color_total_patches": 24, "temporal_noise_e": None}
        case = {**item, "metrics": {"baseline": metrics, "refit": metrics},
                "files": {"raw": f"cases/{item['id']}/capture.npz",
                          **{ccm + "_display": f"cases/{item['id']}/{ccm}.png"
                             for ccm in ("baseline", "refit")}},
                "ccm_reuses_same_raw": True, "fixture_only": True}
        (folder / "case.json").write_text(json.dumps(case))
        return case

    monkeypatch.setattr(job, "source", source)
    monkeypatch.setattr(job, "fingerprint", lambda: {"fixture": "frozen"})
    monkeypatch.setattr(optics, "solve", solve)
    monkeypatch.setattr(spectra, "load_assets", lambda *a, **kw: {"metadata": {"fixture": True}})
    monkeypatch.setattr(spectra, "build_pixel_response", lambda *a, **kw:
                        {"response": {}, "metadata": {"fixture": True}})
    monkeypatch.setattr(engine, "calibrations", calibrations)
    monkeypatch.setattr(engine, "run_case", run_case)
    app = create_app(tmp_path / "state", workspace=Path(__file__).parents[2],
                     start_worker=False, seed_demo=False)
    return app, source_checks


def _client(http):
    http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
    client = object.__new__(PlatformClient)
    client.http = http
    return client


def _finish(service, record):
    claimed = service.registry.claim()
    assert claimed["id"] == record["id"]
    return service.registry.finish(record["id"], result=service.execute_run(claimed))


@pytest.mark.parametrize("full", [False, True])
def test_sdk_submit_wait_cases_profile_files_bundle_and_cancel(fixture_app, full):
    app, checks = fixture_app
    service = app.state.service
    with TestClient(app, raise_server_exceptions=False) as http:
        client = _client(http)
        spec = job.template() if full else first_pair_spec()
        record = client.submit_reference_ircf_study(spec)
        assert client.submit_reference_ircf_study(spec)["id"] == record["id"]
        assert len(client.reference_ircf_studies()["studies"]) == 1
        _finish(service, record)
        completed = client.wait(record["id"], timeout=1, poll_interval=0)
        assert completed["status"] == "succeeded"
        waited = client.wait_reference_workshop(record["id"], timeout=1, poll_interval=0)
        assert waited["id"] == record["id"]
        assert client.reference_ircf_study(record["id"])["status"] == "succeeded"
        summary = completed["result"]["summary"]
        assert len(summary["cases"]) == (34 if full else 2)
        assert summary["new_raw_frames"] == (272 if full else 16)
        assert summary["ccm_reprocessing_reused_raw"] and summary["raw_preserved"]
        assert summary["physical_validation"] == "not_established"
        a = client.reference_ircf_case(record["id"], "case-000")
        b = client.reference_ircf_case(record["id"], "case-001")
        assert b["metrics"]["refit"]["delta_e00_mean"] is None
        assert b["metrics"]["refit"]["color_valid_patches"] == 0
        for case in (a, b):
            assert http.get(summary["files_base"] + case["files"]["raw"]).status_code == 200
        profile = client.save_reference_ircf_profile(record["id"], {
            "name": "Same field filter/CCM comparison", "case_id": "case-001",
            "baseline_case_id": "case-000", "ccm_a": "baseline", "ccm_b": "refit",
            "stage": "display",
        })
        profile_url = job.ROUTE + f"/{record['id']}/profiles/{profile['id']}"
        assert http.get(profile_url).json()["selection"]["case_id"] == "case-001"
        report = http.get(summary["report_url"])
        assert report.status_code == 200 and "N/A" in report.text
        bundle = http.get(summary["bundle_url"])
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            assert "manifest.json" in archive.namelist()
            assert "cases/case-000/capture.npz" in archive.namelist()
        pending = client.submit_reference_ircf_study({**spec, "name": "Cancelled IRCF"})
        assert client.cancel_reference_workshop(pending["id"])["status"] == "cancelled"
        assert client.wait(pending["id"], timeout=1, poll_interval=0)["status"] == "cancelled"
        assert service.registry.claim() is None
        assert checks.count(True) >= 2
        case_path = workshop._directory(service, record["id"]) / "cases/case-000/case.json"
        case_path.write_text(case_path.read_text() + " ")
        with pytest.raises(httpx.HTTPStatusError):
            client.reference_ircf_case(record["id"], "case-000")
        assert http.get(profile_url).status_code >= 400


def test_storage_estimate_scales_and_refuses_exhausted_budget(fixture_app, monkeypatch):
    app, _ = fixture_app
    service = app.state.service
    pair = job.submit(service, first_pair_spec())
    full = job.submit(service, job.template())
    assert full["estimated_bytes"] - pair["estimated_bytes"] == 32 * 96 * 1024**2
    monkeypatch.setattr(job.storage, "status", lambda service:
                        {"remaining_bytes": 1, "volume_free_bytes": 10**12})
    with pytest.raises(ValueError, match="SSD budget"):
        job.submit(service, first_pair_spec())


def test_changed_source_code_stops_before_any_artifacts(fixture_app, monkeypatch):
    app, _ = fixture_app
    service = app.state.service
    record = job.submit(service, first_pair_spec())
    claimed = service.registry.claim()
    monkeypatch.setattr(job, "fingerprint", lambda: {"fixture": "changed"})
    with pytest.raises(IntegrityError, match="code or storage"):
        job.execute(service, claimed)
    assert not (workshop._directory(service, record["id"]) / "manifest.json").exists()


def test_execution_failure_cannot_publish_completed_result(fixture_app, monkeypatch):
    app, _ = fixture_app
    service = app.state.service
    record = job.submit(service, first_pair_spec())
    claimed = service.registry.claim()

    def failure(*args, **kwargs):
        raise ValueError("IRCF fixture intentionally failed during acquisition")

    monkeypatch.setattr(engine, "run_case", failure)
    with pytest.raises(ValueError, match="intentionally failed"):
        service.execute_run(claimed)
    finished = service.registry.finish(record["id"], error="intentionally failed")
    assert finished["status"] == "failed"
    directory = workshop._directory(service, record["id"])
    assert not (directory / "manifest.json").exists()
    assert not (directory / "result.json").exists()
    with pytest.raises(ValueError, match="completed case"):
        job.case_detail(service, record["id"], "case-000")


def test_cancel_during_optics_stops_before_capture(fixture_app, monkeypatch):
    app, _ = fixture_app
    service = app.state.service
    record = job.submit(service, first_pair_spec())
    claimed = service.registry.claim()
    original = optics.solve

    def cancel(*args, **kwargs):
        service.registry.cancel(record["id"])
        return original(*args, **kwargs)

    monkeypatch.setattr(optics, "solve", cancel)
    with pytest.raises(RuntimeError, match="cancelled"):
        service.execute_run(claimed)
    assert service.registry.get_run(record["id"])["status"] == "cancelled"
    assert not (workshop._directory(service, record["id"]) / "cases").exists()


def test_profile_rejects_mixed_conditions(fixture_app):
    app, _ = fixture_app
    service = app.state.service
    spec = first_pair_spec()
    spec.update(illuminants=["day", "ill_a"], filters=["baseline"])
    record = job.submit(service, spec)
    _finish(service, record)
    with pytest.raises(ValueError, match="same field, illuminant and chart"):
        job.save_profile(service, record["id"], {
            "name": "Invalid cross-light comparison", "case_id": "case-001",
            "baseline_case_id": "case-000", "ccm_a": "baseline", "ccm_b": "refit",
            "stage": "display",
        })


def test_invalid_color_support_keeps_missing_noise_and_delta_e_unavailable(monkeypatch):
    shape = (256, 256)
    capture = {
        "electron_frames": np.zeros((8, *shape)),
        "valid_frames": np.zeros((8, *shape), bool),
        "variance_e2": np.ones(shape),
        "well_saturated_frames": np.ones((8, *shape), bool),
        "adc_saturated_frames": np.ones((8, *shape), bool),
    }
    monkeypatch.setattr(engine, "process_electrons", lambda *a, **kw: {
        "linear": np.zeros((*shape, 3)), "display": np.zeros((*shape, 3)),
        "valid": np.zeros(shape, bool),
    })
    assets = {"illuminants": {"day": {"photons_per_m2_sr_s_nm": np.ones(2)}},
              "xyz_per_photon": np.ones((2, 3)), "macbeth_reflectance": np.ones((2, 24))}
    result = engine.processed_metrics(capture, np.eye(3), np.ones(shape),
                                      first_pair_spec(), assets, "day", "color")
    metrics = result["metrics"]
    assert metrics["mean_electrons"] is None
    assert metrics["temporal_noise_e"] is None
    assert metrics["delta_e00_mean"] is None
    assert metrics["delta_e00_max"] is None
    assert metrics["color_valid_patches"] == 0 and metrics["color_total_patches"] == 24
    assert result["patch_delta_e"] == [None] * 24
    assert metrics["valid_fraction"] == 0
