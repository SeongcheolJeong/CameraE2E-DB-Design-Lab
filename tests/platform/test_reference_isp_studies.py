"""Preserved-source recipes select complete camera evidence, not fortunate ROIs."""

from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camera_platform import reference_isp
from camera_platform import reference_isp_studies as studies
from camera_platform import reference_workshop_job as job
from camera_platform.api import create_app
from camera_platform.registry import digest

FIELDS = (
    "center",
    "mid-right",
    "mid-top",
    "mid-left",
    "mid-bottom",
    "outer-ne",
    "outer-nw",
    "outer-sw",
    "outer-se",
)


def source_configuration():
    source = job.defaults()
    source["isp"].update(
        wb=[1.2, 0.9, 0.8],
        ccm=[[1.1, -0.1, 0], [0, 1, 0], [0, -0.1, 1.1]],
        tone_gamma=1.8,
        display_white_level=10,
    )
    return source


def study(recipe="noise_detail", **options):
    return studies.specification(
        {"source_run_id": "run-" + "a" * 32, "recipe": recipe, **options},
        source_configuration(),
    )


def candidate(identity, *, noise=10, texture=1, halo=0.01, color=2):
    return {
        "id": identity,
        "status": "succeeded",
        "fields": [
            {
                "id": name,
                "status": "succeeded",
                "tests": {
                    "isp_lab": {"temporal_noise_luma": noise, "texture_retention": texture,
                        "metric_support": {k: {"eligible": True}
                                           for k in ("color", "resolution", "texture")}},
                    "color": {"delta_e00_mean": color},
                    "resolution": {
                        "sites": {
                            "center": {
                                "processed_system": {
                                    "status": "ok",
                                    "overshoot_fraction": halo,
                                    "undershoot_fraction": halo / 2,
                                }
                            }
                        }
                    },
                },
            }
            for name in FIELDS
        ],
    }


def test_noise_recipe_has_six_distinct_profiles_and_fixed_source_controls():
    source = source_configuration()
    before = deepcopy(source)
    authored = reference_isp.default_profile()
    authored.update(wb=[2, 2, 2], tone_gamma=4, display_white_level=1)
    authored["ccm"] = [[2, 0, 0], [0, 2, 0], [0, 0, 2]]
    authored["raw_denoise"]["strength"] = 1
    authored["linear_sharpen"]["amount"] = 1
    selected = studies.specification(
        {"source_run_id": "run-" + "a" * 32, "recipe": "noise_detail", "isp": authored}, source
    )
    profiles = studies.profiles(selected)
    assert len(profiles) == len({digest(p) for _, p in profiles}) == 6
    assert {(p["raw_denoise"]["strength"], p["linear_sharpen"]["amount"]) for _, p in profiles} == {
        (n, s) for n in (0, 0.4, 0.8) for s in (0, 0.3)
    }
    for _, p in profiles:
        reference_isp.validate_profile(p)
        assert p["lsc_enabled"] is True
        assert p["demosaic_method"] == "adaptive_laplacian"
        for key in ("wb", "ccm", "tone_gamma", "display_white_level"):
            assert p[key] == source["isp"][key]
    profiles[0][1]["wb"][0] = 5
    assert profiles[1][1]["wb"][0] == 1.2
    assert source == before


@pytest.mark.parametrize(
    "override",
    [
        {"lens": {"curvature_scale": 1.005}},
        {"recipe": "arbitrary"},
        {"constraints": {"unknown": 0}},
        {"constraints": {"texture_retention_min": True}},
        {"constraints": {"halo_increase_max": float("nan")}},
        {"constraints": {"noise_improvement_min": 1.01}},
    ],
)
def test_recipe_refuses_new_acquisition_controls_and_ambiguous_constraints(override):
    request = {"source_run_id": "run-" + "a" * 32, "recipe": "noise_detail", **override}
    with pytest.raises(ValueError):
        studies.specification(request, source_configuration())


def test_demosaic_recipe_changes_only_algorithm_and_never_claims_a_winner():
    request = study("demosaic")
    profiles = studies.profiles(request)
    assert [p["demosaic_method"] for _, p in profiles] == [
        "bilinear",
        "laplacian",
        "adaptive_laplacian",
    ]
    fixed = [dict(p, demosaic_method="fixed") for _, p in profiles]
    assert fixed[0] == fixed[1] == fixed[2]
    result = studies.select([candidate("a"), candidate("b", noise=1)], request)
    assert result["status"] == "comparison_only"
    assert result["selected_candidate_id"] is None


def test_color_recipe_requires_immutable_id_and_preserves_candidate_profiles():
    request = study("color_recalibration")
    for absent in (None, "", 123, True):
        with pytest.raises(ValueError):
            studies.profiles(request, absent)
    profiles = studies.profiles(request, "reference-color-" + "1" * 32)
    assert len(profiles) == 2
    assert profiles[0][1]["color_calibration_id"] is None
    assert profiles[1][1]["color_calibration_id"] == "reference-color-" + "1" * 32
    for _, p in profiles:
        reference_isp.validate_profile(p)
        assert p["wb"] == [1, 1, 1]
        assert p["ccm"] == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    profiles[1][1]["wb"][0] = 2
    assert profiles[0][1]["wb"] == [1, 1, 1]
    assert request["isp"]["wb"] == [1.2, 0.9, 0.8]


def test_select_minimizes_worst_field_noise_not_mean_or_best_field():
    base = candidate("candidate-00")
    tempting = candidate("candidate-01", noise=1)
    tempting["fields"][-1]["tests"]["isp_lab"]["temporal_noise_luma"] = 9
    robust = candidate("candidate-02", noise=8)
    result = studies.select([base, tempting, robust], study())
    assert result["status"] == "recommended_within_demo"
    assert result["selected_candidate_id"] == "candidate-02"
    assert [r["noise"] for r in result["rows"]] == [10, 9, 8]
    assert result["noise_improvement_fraction"] == pytest.approx(0.2)


@pytest.mark.parametrize(
    "option,reason",
    [
        ({"texture": 0.94}, "texture_below_limit"),
        ({"halo": 0.061}, "additional_halo_above_limit"),
        ({"color": 3.01}, "color_regression_above_limit"),
    ],
)
def test_faster_noise_reduction_cannot_override_texture_halo_or_color_constraints(option, reason):
    result = studies.select([candidate("base"), candidate("bad", noise=1, **option)], study())
    assert result["selected_candidate_id"] == "base"
    assert result["rows"][1]["eligible"] is False
    assert reason in result["rows"][1]["reasons"]


def test_missing_candidate_field_is_excluded_without_shrinking_common_comparison():
    base, incomplete = candidate("base"), candidate("incomplete", noise=1)
    incomplete["fields"].pop()
    result = studies.select([base, incomplete], study())
    assert result["selected_candidate_id"] == "base"
    assert result["rows"][1]["eligible"] is False


def test_identical_missing_field_in_baseline_and_candidate_is_not_a_complete_bench():
    base, incomplete = candidate("base"), candidate("incomplete", noise=1)
    base["fields"].pop()
    incomplete["fields"].pop()
    result = studies.select([base, incomplete], study())
    assert result["status"] == "inconclusive"
    assert result["selected_candidate_id"] is None


@pytest.mark.parametrize("value", [None, float("nan"), -1])
def test_invalid_field_noise_cannot_win(value):
    base, invalid = candidate("base"), candidate("invalid", noise=1)
    invalid["fields"][0]["tests"]["isp_lab"]["temporal_noise_luma"] = value
    result = studies.select([base, invalid], study())
    assert result["selected_candidate_id"] == "base"
    assert result["rows"][1]["eligible"] is False


def test_sub_one_percent_gain_retains_baseline():
    result = studies.select([candidate("base"), candidate("tiny", noise=9.95)], study())
    assert result["status"] == "baseline_retained"
    assert result["selected_candidate_id"] == "base"
    assert result["noise_improvement_fraction"] == 0


@pytest.mark.parametrize("recipe,count", [("noise_detail", 6), ("parameter_tuning", 12)])
def test_source_bound_sweep_replays_all_six_candidates_without_new_capture(
    tmp_path, monkeypatch, recipe, count
):
    from camera_platform import reference_workshop_engine as engine

    monkeypatch.setenv("CAMERA_PLATFORM_WORKSHOP_ROOT", str(tmp_path / "external"))
    monkeypatch.setattr(job, "reference_snapshot", lambda *_: {"source": "fixture"})
    calls = []

    def capture(workspace, directory, spec, cache, on_progress=None):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "raw.npy").write_bytes(b"immutable RAW fixture")
        calls.append(("capture", None))
        return {"configuration": spec, "fields": [], "raw_preserved": True}

    def replay(source, directory, spec, on_progress=None, **kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        content = (source / "raw.npy").read_bytes()
        assert content == b"immutable RAW fixture"
        (directory / "raw.npy").write_bytes(content)
        calls.append(("replay", deepcopy(spec["isp"])))
        result = candidate("temporary", noise=10 - spec["isp"]["raw_denoise"]["strength"])
        result.update(configuration=spec, raw_preserved=True)
        return result

    monkeypatch.setattr(engine, "run", capture)
    monkeypatch.setattr(engine, "replay", replay)
    monkeypatch.setattr(
        engine,
        "report",
        lambda result, directory: (directory / "report.html").write_text("fixture"),
    )
    app = create_app(
        tmp_path / "state", workspace=Path(__file__).parents[2], start_worker=False, seed_demo=False
    )
    service = app.state.service

    def finish(identity):
        claimed = service.registry.claim()
        assert claimed["id"] == identity
        service.registry.finish(identity, result=service.execute_run(claimed))

    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        response = http.post("/api/v1/reference-workshop/runs", json=source_configuration())
        assert response.status_code == 202, response.text
        source = response.json()
        finish(source["id"])
        path = job._directory(service, source["id"]) / "raw.npy"
        original = path.read_bytes()
        response = http.post(
            "/api/v1/reference-workshop/isp-studies",
            json={
                "source_run_id": source["id"],
                "recipe": recipe,
                "name": "fixed-source six",
            },
        )
        assert response.status_code == 202, response.text
        selected = response.json()
        finish(selected["id"])
        response = http.get("/api/v1/reference-workshop/runs/" + selected["id"])
        assert response.status_code == 200, response.text
        summary = response.json()["result"]["summary"]
        assert len(summary["candidates"]) == count
        assert [c[0] for c in calls].count("capture") == 1
        assert [c[0] for c in calls].count("replay") == count
        assert len({digest(c[1]) for c in calls if c[0] == "replay"}) == count
        assert path.read_bytes() == original
        assert summary["source_run_id"] == source["id"]


@pytest.mark.parametrize("kind", ["color", "resolution", "texture"])
def test_selection_rejects_unsupported_metric_roi(kind):
    baseline, altered = candidate("base"), candidate("altered", noise=1)
    altered["fields"][0]["tests"]["isp_lab"]["metric_support"][kind]["eligible"] = False
    result = studies.select([baseline, altered], study())
    assert result["selected_candidate_id"] == "base"
    assert result["rows"][1]["reasons"] == ["missing_or_invalid_field_metrics"]


def test_decreased_overshoot_does_not_hide_increased_undershoot():
    base, changed = candidate("base", halo=0.2), candidate("changed", noise=1, halo=0.19)
    edge = changed["fields"][0]["tests"]["resolution"]["sites"]["center"]["processed_system"]
    edge["undershoot_fraction"] = 0.17
    result = studies.select([base, changed], study())
    assert result["selected_candidate_id"] == "base"
    assert result["rows"][1]["halo_increase"] == pytest.approx(0.07)
    assert result["rows"][1]["reasons"] == ["additional_halo_above_limit"]
