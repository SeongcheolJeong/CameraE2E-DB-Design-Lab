from __future__ import annotations

import json
from importlib.resources import files

import numpy as np
import pytest

from pyisetcam.image_sensor_db import (
    image_sensor_db_config,
    image_sensor_db_optimize_camera_parameters,
)
from pyisetcam.sensor import sensor_create, sensor_get, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor, resample_sensor_capture
from pyisetcam.sensor_estimate import (
    _recover_reviewed_missing_cfa,
    apply_sensor_estimate,
    image_sensor_db_model,
    image_sensor_db_model_inventory,
)


def test_reviewed_gmax_mono_recovery_preserves_assumptions_and_source_files():
    from pathlib import Path

    code = "DEP-1902-801"
    model = image_sensor_db_model(code)
    path = Path(model["provenance"]["optical_model_path"])
    original = path.read_bytes()
    source = json.loads(original)
    assert source["optical"]["cfa"]["pattern"]["value"] is None
    assert source["cfa_proxy_nk"]["enabled"] is False
    assert source["cfa_proxy_nk"]["channels"] == {}
    for selection in ("auto", "db_estimate_v1"):
        assert image_sensor_db_config(code, model=selection)["model"] == "db_estimate_v1"
    assert model["filter_names"] == ["clear"]
    assert model["cfa_pattern"] == "monochrome"
    assert model["pixel_pitch_um"] == 2.5
    assert model["active_si_thickness_um"] == 5.0
    assert model["provenance"]["active_si_thickness_source"] == "generated_stack_fallback"
    assert model["assumptions"]["effective_efficiency"] == 0.75
    np.testing.assert_array_equal(model["cfa_transmission"], np.ones((31, 1)))
    np.testing.assert_allclose(
        model["eqe"], 0.75 * np.asarray(model["silicon_absorption"])[:, None]
    )
    assert 0 < np.max(model["eqe"]) <= 0.75
    assert model["provenance"]["source_scope_recovery"]["measured_qe"] is False
    assert model["provenance"]["cfa_thickness"]["value"] is None
    sensor = apply_sensor_estimate(sensor_create(), model)
    np.testing.assert_array_equal(sensor.fields["pattern"], [[1]])
    assert path.read_bytes() == original


def test_missing_cfa_recovery_does_not_overwrite_existing_or_unreviewed_curves():
    missing = {
        "optical": {"cfa": {"pattern": {"value": None}}},
        "cfa_proxy_nk": {"enabled": False, "channels": {}},
    }
    untouched, provenance = _recover_reviewed_missing_cfa(missing, "DEF-2011-801")
    assert untouched is missing and provenance is None
    recovered, provenance = _recover_reviewed_missing_cfa(missing, "DEP-1902-801")
    assert recovered is not missing and provenance["original_cfa_pattern"] is None
    assert missing["optical"]["cfa"]["pattern"]["value"] is None
    missing["cfa_proxy_nk"]["channels"] = {"red": {"data": []}}
    with pytest.raises(ValueError, match="conflicts with existing optical curves"):
        _recover_reviewed_missing_cfa(missing, "DEP-1902-801")
    missing["optical"]["cfa"]["pattern"]["value"] = "bayer"
    untouched, provenance = _recover_reviewed_missing_cfa(missing, "DEP-1902-801")
    assert untouched is missing and provenance is None


@pytest.fixture
def estimate_db(tmp_path):
    """Pair an old catalog with enriched data using report code, not filename."""
    selected = tmp_path / "sensor_db"
    enriched = tmp_path / "image_sensor_db"
    selected.mkdir()
    (enriched / "optical_qe_db/models").mkdir(parents=True)
    (enriched / "generated_stack_configs").mkdir()
    raw = {
        "code": "TEST-001",
        "metadata": {"manufacturer": "Test", "device_name": "Sensor"},
        "derived_specs": {
            "pixel_pitch_um": 2.0,
            "active_si_thickness_um": 4.0,
            "cfa_pattern": "bayer",
            "sensor_modality": "cmos_image_sensor",
            "pixel_rows": 16,
            "pixel_cols": 16,
            "global_shutter": True,
        },
        "generated_files": {"stack_config": "/old/machine/enriched_sensor.json"},
    }
    old = json.loads(json.dumps(raw))
    old["generated_files"]["stack_config"] = "/old/machine/selected_sensor.json"
    old["derived_specs"]["pixel_pitch_um"] = 9.0
    old["derived_specs"]["active_si_thickness_um"] = 1.0
    (selected / "sensor_catalog.json").write_text(json.dumps({"records": [old]}))
    (enriched / "sensor_catalog.json").write_text(json.dumps({"records": [raw]}))
    model = {
        "code": "TEST-001",
        "optical": {"cfa": {"pattern": {"value": "bayer"}}, "geometry": {}},
        "cfa_proxy_nk": {
            "enabled": True,
            "thickness_um": {"value": 0.65, "source_kind": "inferred_proxy_default"},
            "channels": {
                name: {
                    "data": [
                        {"wavelength_nm": wavelength, "transmission_absorption_only": level}
                        for wavelength in [400, 500, 600, 700]
                    ]
                }
                for name, level in [("red", 0.9), ("green", 0.6), ("blue", 0.3)]
            },
        },
    }
    (enriched / "optical_qe_db/models/enriched_sensor.json").write_text(json.dumps(model))
    (enriched / "generated_stack_configs/enriched_sensor.json").write_text(
        json.dumps(
            {
                "calibration_status": {"source_report_code": "TEST-001"},
                "geometry_um": {"si_thickness": 2.5},
            }
        )
    )
    return selected, enriched


def _rewrite(path, edit):
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))


def test_model_uses_paired_report_geometry_and_green_absorption(estimate_db):
    selected, _ = estimate_db
    model = image_sensor_db_model("selected_sensor", root=selected)
    assert model["pixel_pitch_um"] == 2.0
    assert model["active_si_thickness_um"] == 4.0
    assert model["sensor_id"] == "selected_sensor"
    assert model["provenance"]["matched_by"] == "report_code"
    material = json.loads(
        files("pyisetcam").joinpath("data/sensor_estimate/si_green_2008_visible.json").read_text()
    )
    alpha = 4 * np.pi * np.asarray(material["k"]) / (np.asarray(material["wavelength_nm"]) / 1000)
    expected = -np.expm1(-alpha * 4.0)
    np.testing.assert_allclose(model["silicon_absorption"], expected)
    np.testing.assert_allclose(model["eqe"], expected[:, None] * np.array([[0.9, 0.6, 0.3]]) * 0.75)
    assert model["provenance"]["measured_accuracy_verified"] is False
    assert model["readout"]["black_level_dn"] == 64
    assert model["provenance"]["assumption_sources"]["full_well_e"] == "db_estimate_v1_default"
    json.dumps(model, allow_nan=False)


def test_thickness_increases_absorption_and_efficiency_scales_once(estimate_db):
    selected, _ = estimate_db
    thin = image_sensor_db_model(
        "TEST-001", root=selected, assumptions={"active_si_thickness_um": 1}
    )
    thick = image_sensor_db_model(
        "TEST-001", root=selected, assumptions={"active_si_thickness_um": 5}
    )
    half = image_sensor_db_model(
        "TEST-001",
        root=selected,
        assumptions={"active_si_thickness_um": 5, "effective_efficiency": 0.375},
    )
    assert np.all(np.asarray(thick["eqe"]) >= np.asarray(thin["eqe"]))
    assert np.asarray(thick["eqe"])[-1, 0] > np.asarray(thin["eqe"])[-1, 0]
    np.testing.assert_allclose(half["eqe"], np.asarray(thick["eqe"]) / 2)
    assert thick["provenance"]["active_si_thickness_source"] == "user_assumption"
    assert half["provenance"]["assumption_sources"]["effective_efficiency"] == "user_assumption"


def test_apply_replaces_optical_factors_once_preserves_array_and_readout(estimate_db):
    selected, _ = estimate_db
    model = image_sensor_db_model("TEST-001", root=selected)
    original = sensor_create()
    original = sensor_set(original, "pixel fill factor", 0.25)
    original = sensor_set(original, "pixel spectral qe", 0.2)
    original = sensor_set(original, "ir filter", np.full(31, 0.1))
    original = sensor_set(original, "ocl group shape", "2x2")
    original.fields["pixel"]["width_gap_m"] = 8e-6
    original.fields["size"] = (24, 32)
    applied = apply_sensor_estimate(original, model)
    np.testing.assert_allclose(sensor_get(applied, "qe"), model["eqe"])
    np.testing.assert_allclose(sensor_get(applied, "pixel size"), [2e-6, 2e-6])
    assert sensor_get(applied, "deltax") == 2e-6
    assert sensor_get(applied, "pixel fill factor") == 1
    assert sensor_get(applied, "size") == (24, 32)
    assert applied.fields["ocl_group_proxy"] is None
    assert applied.fields["pixel"]["read_noise_v"] == original.fields["pixel"]["read_noise_v"]
    assert applied.fields["e2e_model_readout"]["read_noise_e"] == 10
    assert applied.metadata["sensor_estimate"]["estimate_only"] is True
    np.testing.assert_allclose(
        sensor_get(apply_sensor_estimate(applied, model), "qe"), model["eqe"]
    )
    assert sensor_get(original, "pixel fill factor") == 0.25
    assert "global_shutter" not in applied.fields


@pytest.mark.parametrize("layer", ["fdtd_sensor", "tcad_sensor"])
def test_apply_rejects_active_physics_overlap(estimate_db, layer):
    model = image_sensor_db_model("TEST-001", root=estimate_db[0])
    sensor = sensor_create()
    sensor.fields[layer] = {"enabled": True}
    with pytest.raises(ValueError, match="cannot overlap"):
        apply_sensor_estimate(sensor, model)
    sensor.fields[layer] = {"enabled": False}
    apply_sensor_estimate(sensor, model)


def test_generated_si_fallback_labeled_and_missing_si_actionable(estimate_db):
    selected, enriched = estimate_db
    _rewrite(
        enriched / "sensor_catalog.json",
        lambda x: x["records"][0]["derived_specs"].pop("active_si_thickness_um"),
    )
    model = image_sensor_db_model("TEST-001", root=selected)
    assert model["active_si_thickness_um"] == 2.5
    assert model["provenance"]["active_si_thickness_source"] == "generated_stack_fallback"
    (enriched / "generated_stack_configs/enriched_sensor.json").unlink()
    with pytest.raises(ValueError, match="assumptions.active_si_thickness_um"):
        image_sensor_db_model("TEST-001", root=selected)
    assert (
        image_sensor_db_model("TEST-001", root=selected, assumptions={"active_si_thickness_um": 3})[
            "active_si_thickness_um"
        ]
        == 3
    )


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("pixel_pitch_um", None, "pixel_pitch_um"),
        ("sensor_modality", "event_image_sensor", "modality"),
        ("sensor_modality", "swir_image_sensor", "modality"),
    ],
)
def test_inventory_excludes_invalid_geometry_or_modality(estimate_db, field, value, match):
    selected, enriched = estimate_db
    _rewrite(
        enriched / "sensor_catalog.json",
        lambda x: x["records"][0]["derived_specs"].update({field: value}),
    )
    with pytest.raises(ValueError, match=match):
        image_sensor_db_model("TEST-001", root=selected)
    inventory = image_sensor_db_model_inventory(root=selected)
    assert inventory["available_count"] == 0
    assert inventory["excluded_count"] == 1
    assert match in inventory["records"][0]["reason"]


@pytest.mark.parametrize("pattern", [None, "ryyb", "unknown"])
def test_unknown_cfa_never_defaults_to_bayer(estimate_db, pattern):
    selected, enriched = estimate_db
    _rewrite(
        enriched / "optical_qe_db/models/enriched_sensor.json",
        lambda x: x["optical"]["cfa"]["pattern"].update(value=pattern),
    )
    with pytest.raises(ValueError, match="CFA pattern"):
        image_sensor_db_model("TEST-001", root=selected)


def test_clear_and_quad_cfa_pattern_mapping(estimate_db):
    selected, enriched = estimate_db
    path = enriched / "optical_qe_db/models/enriched_sensor.json"
    _rewrite(path, lambda x: x["optical"]["cfa"]["pattern"].update(value="quad_bayer"))
    quad = apply_sensor_estimate(sensor_create(), image_sensor_db_model("TEST-001", root=selected))
    np.testing.assert_array_equal(
        quad.fields["pattern"], [[1, 1, 2, 2], [1, 1, 2, 2], [2, 2, 3, 3], [2, 2, 3, 3]]
    )

    def clear(data):
        data["optical"]["cfa"]["pattern"]["value"] = "monochrome"
        data["cfa_proxy_nk"]["channels"]["clear"] = data["cfa_proxy_nk"]["channels"]["red"]

    _rewrite(path, clear)
    mono = apply_sensor_estimate(sensor_create(), image_sensor_db_model("TEST-001", root=selected))
    assert mono.fields["filter_spectra"].shape == (31, 1)
    np.testing.assert_array_equal(mono.fields["pattern"], [[1]])


@pytest.mark.parametrize(
    "assumptions",
    [
        {"effective_efficiency": 1.01},
        {"effective_efficiency": -1},
        {"full_well_e": 0},
        {"read_noise_e": -1},
        {"bit_depth": 10.5},
        {"bit_depth": 25},
        {"dark_current_e_per_s": float("nan")},
        {"typo": 1},
    ],
)
def test_invalid_assumptions_rejected(estimate_db, assumptions):
    with pytest.raises(ValueError):
        image_sensor_db_model("TEST-001", root=estimate_db[0], assumptions=assumptions)


def test_config_default_descriptor_and_legacy_escape_hatch(estimate_db):
    selected, _ = estimate_db
    config = image_sensor_db_config(
        "TEST-001", root=selected, assumptions={"effective_efficiency": 0.5}
    )
    assert config["model"] == "db_estimate_v1"
    descriptor = config["scenario"]["sensor_model"]
    assert descriptor["assumptions"] == {"effective_efficiency": 0.5}
    assert "eqe" not in descriptor
    assert config["scenario"]["sensor"] == {}
    assert config["scenario"]["fdtd"] == {"enabled": False}
    assert config["scenario"]["tcad"] == {"enabled": False}
    json.dumps(config)
    for kwargs in [
        {"allow_shared_proxy": True},
        {"require_sensor_specific": True},
        {"strategy": "lut_only"},
    ]:
        with pytest.raises(ValueError, match="model='legacy'"):
            image_sensor_db_config("TEST-001", root=selected, **kwargs)
    with pytest.raises(ValueError, match="model must"):
        image_sensor_db_config("TEST-001", root=selected, model="unknown")
    assert (
        "sensor_model"
        not in image_sensor_db_config("TEST-001", root=selected, model="legacy")["scenario"]
    )


def test_default_catalog_inventory_and_cross_sensor_optical_differences():
    inventory = image_sensor_db_model_inventory()
    assert inventory["record_count"] >= 200
    assert inventory["available_count"] > 100
    assert inventory["excluded_count"] > 0
    assert inventory["available_count"] + inventory["excluded_count"] == inventory["record_count"]
    assert all(item["reason"] for item in inventory["records"] if not item["available"])
    available = [
        item
        for item in inventory["records"]
        if item["available"] and item["cfa_pattern"] == "bayer"
    ]
    first = image_sensor_db_model(available[0]["sensor_id"])
    models = [image_sensor_db_model(item["sensor_id"]) for item in available[1:8]]
    assert any(not np.allclose(first["eqe"], model["eqe"]) for model in models)
    assert any(first["pixel_pitch_um"] != model["pixel_pitch_um"] for model in models)


@pytest.mark.parametrize("error", ["missing_channel", "duplicate_wave", "short_wave", "nonfinite"])
def test_cfa_curve_integrity_is_required(estimate_db, error):
    selected, enriched = estimate_db

    def corrupt(data):
        channels = data["cfa_proxy_nk"]["channels"]
        if error == "missing_channel":
            del channels["red"]
        elif error == "duplicate_wave":
            channels["red"]["data"][1]["wavelength_nm"] = 400
        elif error == "short_wave":
            channels["red"]["data"] = channels["red"]["data"][:-1]
        else:
            channels["red"]["data"][0]["transmission_absorption_only"] = float("nan")

    _rewrite(enriched / "optical_qe_db/models/enriched_sensor.json", corrupt)
    with pytest.raises(ValueError, match="CFA"):
        image_sensor_db_model("TEST-001", root=selected)


def test_enriched_root_loads_without_original_external_authoring_paths(estimate_db):
    selected, enriched = estimate_db
    paired = image_sensor_db_model("TEST-001", root=selected)
    direct = image_sensor_db_model("enriched_sensor", root=enriched)
    np.testing.assert_array_equal(paired["eqe"], direct["eqe"])
    assert paired["sensor_id"] != direct["sensor_id"]
    assert paired["report_code"] == direct["report_code"]


def test_optimizer_estimate_source_marks_descriptor_overrides(estimate_db, monkeypatch):
    selected, _ = estimate_db

    def optimize(scenario, **kwargs):
        return {"selected_scenarios": [scenario]}

    monkeypatch.setattr("pyisetcam.optimization.camerae2e_optimize_camera_parameters", optimize)
    result = image_sensor_db_optimize_camera_parameters(
        "TEST-001",
        root=selected,
        base_scenario={"sensor_model": {"assumptions": {"effective_efficiency": 0.5}}},
    )
    source = result["source_image_sensor_db"]
    assert source["policy"]["model_binding"]["scope"] == "overridden"
    assert "model_provenance" not in source["policy"]
    assert source["selected_sensor_models"][0]["assumptions"]["effective_efficiency"] == 0.5


def test_applying_new_optical_model_invalidates_cached_capture(estimate_db):
    model = image_sensor_db_model("TEST-001", root=estimate_db[0])
    sensor = configure_e2e_sensor(apply_sensor_estimate(sensor_create(), model))
    sensor.fields.update(size=(8, 8), integration_time=0.01, auto_exposure=False)
    sensor.data["mean_electrons"] = np.full((8, 8), 100.0)
    captured = resample_sensor_capture(sensor, seed=1)
    assert "raw_dn" in captured.data
    half = image_sensor_db_model(
        "TEST-001",
        root=estimate_db[0],
        assumptions={"effective_efficiency": 0.375},
    )
    changed = apply_sensor_estimate(captured, half)
    assert not changed.data
    assert "sensor_capture" not in changed.metadata
    with pytest.raises(ValueError, match="mean_electrons"):
        resample_sensor_capture(changed, seed=2)
    assert "raw_dn" in captured.data
