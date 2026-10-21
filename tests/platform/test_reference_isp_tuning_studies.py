"""Tuning compares full fields without rewarding dimming or incomplete previews."""

from copy import deepcopy

import pytest
from test_reference_isp_studies import candidate, source_configuration, study

from camera_platform import reference_isp_studies as studies
from camera_platform import reference_workshop_job as job
from camera_platform.registry import digest


def lit(identity, *, brightness=1, **kwargs):
    result = candidate(identity, **kwargs)
    for field in result["fields"]:
        field["tests"]["isp_lab"]["uniform_mean_luma"] = brightness
    return result


def test_default_tuning_is_twelve_distinct_replays_including_explicit_baseline():
    specification = study("parameter_tuning")
    profiles = studies.profiles(specification)
    assert len(profiles) == specification["candidate_count"] == 12
    assert len({digest(p) for _, p in profiles}) == 12
    assert profiles[0][1]["lsc_tuning"] == {"strength": 1, "gain_cap": 4}
    assert profiles[0][1]["raw_denoise"]["strength"] == 0
    assert profiles[0][1]["linear_sharpen"]["amount"] == 0
    for _, profile in profiles:
        for name in ("wb", "ccm", "tone_gamma", "display_white_level"):
            assert profile[name] == source_configuration()["isp"][name]


def test_axes_include_noise_floor_sharpen_threshold_cap_and_keep_baseline():
    specification = study("parameter_tuning", parameter_axes={
        "raw_denoise.range_floor_dn2": [1, 16],
        "linear_sharpen.threshold": [0.002],
        "linear_sharpen.max_delta": [0.02],
        "lsc_tuning.gain_cap": [2],
    })
    profiles = studies.profiles(specification)
    assert len(profiles) == 3
    assert profiles[1][1]["lsc_tuning"]["gain_cap"] == 2
    assert profiles[2][1]["raw_denoise"]["range_floor_dn2"] == 16


def test_all_seven_axes_produce_saveable_configuration_names():
    axes = {**studies.TUNING_AXES, "raw_denoise.range_floor_dn2": [1],
            "linear_sharpen.threshold": [.01], "linear_sharpen.max_delta": [.05],
            "lsc_tuning.gain_cap": [4]}
    for label, profile in studies.profiles(study("parameter_tuning", parameter_axes=axes)):
        job.specification({**source_configuration(), "name": label, "isp": profile})
        assert len(label + " · 첫 RAW 미리 계산") <= 160


@pytest.mark.parametrize("axes", [
    {}, {"wb": [1]}, {"raw_denoise.strength": [True]},
    {"raw_denoise.strength": [float("nan")]},
    {"raw_denoise.strength": [0.2, 0.2]}, {"lsc_tuning.gain_cap": [5]},
    {"raw_denoise.strength": [0.1, 0.2, 0.3], "linear_sharpen.amount": [0.1, 0.2],
     "lsc_tuning.strength": [0.8, 0.9]},  # 12 plus mandatory baseline
])
def test_invalid_unbounded_or_ambiguous_axes_rejected(axes):
    with pytest.raises(ValueError):
        study("parameter_tuning", parameter_axes=axes)


def test_noise_reduction_by_dimming_does_not_win():
    baseline, dark = lit("base"), lit("dark", noise=1, brightness=0.8)
    result = studies.select([baseline, dark], study("parameter_tuning"))
    assert result["selected_candidate_id"] == "base"
    assert "brightness_outside_baseline_limits" in result["rows"][1]["reasons"]


def test_small_dimming_within_brightness_tolerance_is_not_denoising_gain():
    result = studies.select([lit("base"), lit("darker", noise=9.8, brightness=.98, texture=.98)],
                            study("parameter_tuning"))
    assert result["selected_candidate_id"] == "base"
    assert result["rows"][1]["reasons"] == ["brightness_normalized_noise_gain_below_limit"]
    assert result["rows"][1]["brightness_normalized_noise_improvement"] == pytest.approx(0)


def test_uniformity_constraint_and_missing_brightness_fail_closed():
    baseline, changed = lit("base"), lit("changed", noise=1)
    changed["fields"][0]["tests"]["isp_lab"]["uniform_mean_luma"] = 0.96
    result = studies.select([baseline, changed], study(
        "parameter_tuning", constraints={"uniformity_increase_max": 0.02}))
    assert result["rows"][1]["reasons"] == ["field_uniformity_regression_above_limit"]
    changed["fields"][0]["tests"]["isp_lab"]["uniform_mean_luma"] = None
    result = studies.select([baseline, changed], study("parameter_tuning"))
    assert "missing_or_invalid_brightness_metrics" in result["rows"][1]["reasons"]


def test_complete_bright_texture_preserving_candidate_can_win():
    result = studies.select([lit("base"), lit("candidate", noise=9, texture=.97)],
                            study("parameter_tuning"))
    assert result["selected_candidate_id"] == "candidate"
    assert result["noise_improvement_fraction"] == pytest.approx(.1)


def test_partial_preview_cannot_become_study_source():
    record = {"status": "succeeded", "result": {"summary": {
        "preview_scope": {"frames": 1}, "configuration": source_configuration()}}}
    with pytest.raises(ValueError, match="complete capture"):
        job._source_summary(record)


def test_tuning_failure_in_any_field_stays_in_candidate_denominator():
    baseline, incomplete = lit("base"), lit("incomplete", noise=1)
    incomplete["fields"].pop()
    result = studies.select([baseline, incomplete], study("parameter_tuning"))
    assert len(result["rows"]) == 2
    assert result["selected_candidate_id"] == "base"
    assert not result["rows"][1]["eligible"]


def test_tuning_does_not_mutate_source_or_axes():
    source = source_configuration()
    axes = deepcopy(studies.TUNING_AXES)
    before = deepcopy(source)
    studies.specification({"recipe": "parameter_tuning", "parameter_axes": axes}, source)
    assert source == before and axes == studies.TUNING_AXES


def test_existing_recipe_stays_full_lsc_even_after_tuning_source():
    from camera_platform import reference_isp

    profile = reference_isp.default_profile()
    profile["lsc_tuning"] = {"strength": .2, "gain_cap": 1.2}
    for recipe in ("noise_detail", "demosaic", "color_recalibration"):
        assert "lsc_tuning" not in study(recipe, isp=profile)["isp"]


def test_preview_job_has_common_lifecycle_and_cannot_become_full_source(tmp_path, monkeypatch):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from camera_platform import reference_isp
    from camera_platform import reference_workshop_engine as engine
    from camera_platform.api import create_app

    monkeypatch.setenv("CAMERA_PLATFORM_WORKSHOP_ROOT", str(tmp_path / "ssd"))
    monkeypatch.setattr(job, "reference_snapshot", lambda *_: {"source": "fixture"})

    def capture(workspace, directory, spec, cache, on_progress=None):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "raw.npy").write_bytes(b"untouched integer RAW fixture")
        return {"configuration": spec, "fields": []}

    def preview(source, directory, spec, on_progress=None, *, field_id, chart, **kwargs):
        assert field_id == "outer-ne" and chart == "texture"
        assert (source / "raw.npy").read_bytes() == b"untouched integer RAW fixture"
        (directory / "raw.npy").write_bytes((source / "raw.npy").read_bytes())
        return {"configuration": spec, "fields": [], "raw_preserved": True,
                "preview_scope": {"field_id": field_id, "chart": chart, "frames": 1}}

    monkeypatch.setattr(engine, "run", capture)
    monkeypatch.setattr(engine, "preview", preview)
    monkeypatch.setattr(engine, "report", lambda *args: None)
    app = create_app(tmp_path / "state", workspace=Path(__file__).parents[2],
                     start_worker=False, seed_demo=False)
    service = app.state.service

    def finish(identity):
        claimed = service.registry.claim()
        assert claimed["id"] == identity
        service.registry.finish(identity, result=service.execute_run(claimed))

    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        source = http.post("/api/v1/reference-workshop/runs", json=source_configuration()).json()
        finish(source["id"])
        request = {"source_run_id": source["id"], "isp": reference_isp.default_profile(),
                   "field_id": "outer-ne", "chart": "texture"}
        response = http.post("/api/v1/reference-workshop/isp-previews", json=request)
        assert response.status_code == 202, response.text
        preview_id = response.json()["id"]
        finish(preview_id)
        response = http.get("/api/v1/reference-workshop/runs/" + preview_id)
        assert response.status_code == 200
        summary = response.json()["result"]["summary"]
        assert summary["preview_scope"]["source"]["run_id"] == source["id"]
        response = http.post("/api/v1/reference-workshop/isp-studies", json={
            "source_run_id": preview_id, "recipe": "parameter_tuning"})
        assert response.status_code == 422
        assert "complete capture" in response.text
        request["name"] = "cancelled preview"
        pending = http.post("/api/v1/reference-workshop/isp-previews", json=request).json()
        response = http.post("/api/v1/reference-workshop/runs/" + pending["id"] + "/cancel",
                             json={})
        assert response.status_code == 200, response.text
        assert service.registry.get_run(pending["id"])["status"] == "cancelled"
