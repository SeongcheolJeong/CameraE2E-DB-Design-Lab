from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyisetcam import (
    camerae2e_run_scenario,
    image_sensor_db_config,
    image_sensor_db_get,
    image_sensor_db_optimize_camera_parameters,
    image_sensor_db_parameters,
    image_sensor_db_records,
    image_sensor_db_summary,
)
from pyisetcam.image_sensor_db import image_sensor_db_reconcile_scenario


def test_image_sensor_db_records_are_searchable() -> None:
    records = image_sensor_db_records(limit=5)

    assert records
    assert {
        "sensor_id",
        "code",
        "manufacturer",
        "stack_config_path",
        "tcad_profile_path",
    } <= set(records[0])
    assert image_sensor_db_records("sony", limit=1)


def test_image_sensor_db_get_accepts_sensor_id_and_code() -> None:
    first = image_sensor_db_records(limit=1)[0]

    by_id = image_sensor_db_get(first["sensor_id"])
    by_code = image_sensor_db_get(first["code"])

    assert by_id["sensor_id"] == first["sensor_id"]
    assert by_code["code"] == first["code"]


def test_image_sensor_db_parameters_return_existing_paths() -> None:
    first = image_sensor_db_records(limit=1)[0]
    params = image_sensor_db_parameters(first["sensor_id"])

    assert Path(params["catalog_path"]).exists()
    assert Path(params["stack_config_path"]).exists()
    assert Path(params["tcad_profile_path"]).exists()
    assert params["lut_path"] is None
    assert params["generation_map_path"] is None
    assert params["collection_summary_paths"] == []
    assert params["accuracy_gate_path"] is None
    assert params["physics_binding"]["scope"] == "missing"


def test_image_sensor_db_config_returns_json_safe_hybrid_scenario() -> None:
    first = image_sensor_db_records(limit=1)[0]
    config = image_sensor_db_config(first["sensor_id"], model="legacy")

    assert config["schema_version"] == "camerae2e_image_sensor_db_config_v1"
    assert config["strategy"] == "hybrid"
    assert config["scenario"]["sensor"]
    assert "policy" in config
    assert config["policy"]["component_tiers"]["sensor_db_metadata"] == "proxy"
    assert config["fdtd"] is None
    assert config["tcad"] is None
    assert config["scenario"]["image_sensor_db"]["physics_binding"]["attached"] is False
    json.dumps(config)


def test_image_sensor_db_config_analytic_only_runs_faca_scenario() -> None:
    first = image_sensor_db_records(limit=1)[0]
    config = image_sensor_db_config(first["sensor_id"], model="legacy", strategy="analytic_only")

    assert config["scenario"]["fdtd"] == {"enabled": False}
    assert config["scenario"]["tcad"] == {"enabled": False}
    result = camerae2e_run_scenario(
        {
            "scene": {"type": "uniform ee", "args": [8]},
            **config["scenario"],
            "sensor": {
                **config["scenario"]["sensor"],
                "noise_flag": 0,
            },
        },
        include_arrays=False,
    )

    assert result["schema_version"] == "camerae2e_faca_scenario_v1"
    assert result["parameter_lineage"]


def test_image_sensor_db_config_maps_quad_bayer_to_shared_ocl_proxy() -> None:
    quad_record = next(
        record
        for record in image_sensor_db_records()
        if record.get("cfa_pattern") in {"quad_bayer", "tetracell_bayer"}
    )
    config = image_sensor_db_config(
        quad_record["sensor_id"], model="legacy", strategy="analytic_only",
    )

    assert config["sensor"]["cfa_preset"] == "quad_bayer_rgb"
    assert config["sensor"]["ocl_group_shape"] == "2x2"
    assert config["sensor"]["ocl_group_equalization"] == 1.0
    assert config["policy"]["analytic_role"].startswith("Analytic proxy axes")


def test_image_sensor_db_optimize_camera_parameters_preserves_source_lineage() -> None:
    first = image_sensor_db_records(limit=1)[0]
    result = image_sensor_db_optimize_camera_parameters(
        first["sensor_id"], model="legacy",
        strategy="analytic_only",
        base_scenario={"scene": {"type": "uniform ee", "args": [8]}},
        preset="exposure",
        parameter_space={"sensor.integration_time": [0.001, 0.004]},
        objective={"metric": "metrics.color.rgb_mean", "direction": "maximize"},
        max_cases=2,
        seed=17,
        top_k=1,
    )

    source = result["source_image_sensor_db"]
    assert source["schema_version"] == "camerae2e_optimization_source_image_sensor_db_v1"
    assert source["sensor_id"] == first["sensor_id"]
    assert source["strategy"] == "analytic_only"
    assert result["best_case"]["parameters"]["sensor.integration_time"] == 0.004
    assert result["selected_scenarios"][0]["image_sensor_db"]["sensor_id"] == first["sensor_id"]


def test_image_sensor_db_summary_has_counts() -> None:
    summary = image_sensor_db_summary()

    assert summary["record_count"] >= 100
    assert summary["manufacturer_count"] >= 5
    assert "Sony" in summary["manufacturers"]


def test_shared_reference_requires_opt_in_and_cannot_satisfy_sensor_specific_requirement() -> None:
    first = image_sensor_db_records(limit=1)[0]
    config = image_sensor_db_config(first["sensor_id"], model="legacy", allow_shared_proxy=True)
    assert config["physics_binding"]["scope"] == "shared_proxy"
    assert config["physics_binding"]["sensor_specific"] is False
    assert config["fdtd"] is not None
    assert config["tcad"] is not None
    with pytest.raises(ValueError, match="Sensor-specific"):
        image_sensor_db_config(
            first["sensor_id"], model="legacy", allow_shared_proxy=True,
            require_sensor_specific=True,
        )
    with pytest.raises(ValueError, match="lut_only"):
        image_sensor_db_config(first["sensor_id"], model="legacy", strategy="lut_only")


def _write_catalog(root, bindings):
    records = []
    for sensor_id, binding in bindings.items():
        record = {
            "code": sensor_id.upper(),
            "metadata": {"manufacturer": "Test", "device_name": sensor_id},
            "derived_specs": {"pixel_pitch_um": 1.4, "cfa_pattern": "bayer"},
            "generated_files": {"stack_config": f"{sensor_id}.json"},
        }
        if binding is not None:
            record["physics_artifacts"] = binding
        records.append(record)
    (root / "sensor_catalog.json").write_text(json.dumps({"records": records}))


def test_sensor_artifact_binding_is_specific_and_resolves_relative_to_catalog(tmp_path) -> None:
    for name in ("a", "b"):
        (tmp_path / f"{name}_lut.json").write_text("{}")
    _write_catalog(tmp_path, {
        name: {"sensor_id": name, "lut_path": f"{name}_lut.json"} for name in ("a", "b")
    })
    configs = [image_sensor_db_config(
        name, model="legacy", root=tmp_path, strategy="lut_only", require_sensor_specific=True,
    ) for name in ("a", "b")]
    assert configs[0]["fdtd"]["lut"] != configs[1]["fdtd"]["lut"]
    for name, config in zip(("a", "b"), configs, strict=True):
        assert config["fdtd"]["lut"] == str(tmp_path / f"{name}_lut.json")
        assert config["tcad"] is None
        assert config["physics_binding"]["sensor_specific"] is True
        assert config["physics_binding"]["measured_accuracy_verified"] is False


def test_sensor_binding_mismatch_and_missing_declared_file_do_not_fall_back(tmp_path) -> None:
    _write_catalog(tmp_path, {"a": {"sensor_id": "b", "lut_path": "missing.json"}})
    with pytest.raises(ValueError, match="sensor_id"):
        image_sensor_db_config("a", model="legacy", root=tmp_path, allow_shared_proxy=True)
    _write_catalog(tmp_path, {"a": {"sensor_id": "a", "lut_path": "missing.json"}})
    with pytest.raises(FileNotFoundError, match="Declared sensor physics"):
        image_sensor_db_config("a", model="legacy", root=tmp_path, allow_shared_proxy=True)


def test_missing_sensor_specific_binding_rejects_quantitative_selection(tmp_path) -> None:
    _write_catalog(tmp_path, {"a": None})
    with pytest.raises(ValueError, match="Sensor-specific"):
        image_sensor_db_config("a", model="legacy", root=tmp_path, require_sensor_specific=True)


def _bound_sensor_config(tmp_path, *, strict=False):
    (tmp_path / "a_lut.json").write_text("{}")
    _write_catalog(tmp_path, {"a": {"sensor_id": "a", "lut_path": "a_lut.json"}})
    return image_sensor_db_config(
        "a", model="legacy", root=tmp_path, strategy="lut_only", require_sensor_specific=strict,
    )


def test_missing_binding_disables_physics_in_an_existing_camera(tmp_path) -> None:
    from pyisetcam import camera_create

    _write_catalog(tmp_path, {"a": None})
    scenario = image_sensor_db_config("a", model="legacy", root=tmp_path)["scenario"]
    camera = camera_create()
    camera.fields["sensor"].fields["fdtd_sensor"] = {
        "enabled": True, "lut": "/missing/old-sensor-lut.json",
    }
    camera.fields["sensor"].fields["tcad_sensor"] = {
        "enabled": True, "db": "/missing/old-sensor-db.json",
    }
    result = camerae2e_run_scenario(
        {**scenario, "scene": {"type": "uniform ee", "args": [8]}, "sensor": {"noise_flag": 0}},
        camera=camera, include_arrays=False,
    )
    assert result["scenario"]["image_sensor_db"]["physics_binding"]["attached"] is False


def test_scenario_overrides_invalidate_sensor_specific_association(tmp_path) -> None:
    config = _bound_sensor_config(tmp_path)
    scenario = dict(config["scenario"], fdtd={"enabled": True, "lut": str(tmp_path / "other.json")})
    resolved = image_sensor_db_reconcile_scenario(scenario)
    binding = resolved["image_sensor_db"]["physics_binding"]
    assert binding["scope"] == "overridden"
    assert binding["sensor_specific"] is False
    assert binding["overridden_components"] == ["fdtd"]
    with pytest.raises(ValueError, match="after scenario overrides"):
        image_sensor_db_reconcile_scenario(scenario, require_sensor_specific=True)


def test_resolved_lut_object_is_checked_against_catalog_binding(tmp_path) -> None:
    from pyisetcam.fdtd_sensor import FDTDSensorLUT

    scenario = _bound_sensor_config(tmp_path)["scenario"]
    lut = FDTDSensorLUT(source_path=tmp_path / "a_lut.json", schema="test", mode="test")
    resolved = image_sensor_db_reconcile_scenario(scenario, actual_physics={"fdtd": {"lut": lut}})
    assert resolved["image_sensor_db"]["physics_binding"]["sensor_specific"] is True
    other = FDTDSensorLUT(source_path=tmp_path / "other.json", schema="test", mode="test")
    resolved = image_sensor_db_reconcile_scenario(scenario, actual_physics={"fdtd": {"lut": other}})
    assert resolved["image_sensor_db"]["physics_binding"]["scope"] == "overridden"


def test_resolved_tcad_object_and_flat_config_share_the_declared_identity(tmp_path) -> None:
    from types import SimpleNamespace

    from pyisetcam.tcad_sensor import TCADSensorDB

    for filename in ("generation.npz", "summary.json"):
        (tmp_path / filename).write_text("{}")
    _write_catalog(tmp_path, {"a": {
        "sensor_id": "a", "generation_map_path": "generation.npz",
        "collection_summary_paths": ["summary.json"],
    }})
    scenario = image_sensor_db_config(
        "a", model="legacy", root=tmp_path, strategy="lut_only",
    )["scenario"]
    loaded = TCADSensorDB(
        generation_map=SimpleNamespace(source_path=tmp_path / "generation.npz"),
        collection_summaries=(SimpleNamespace(source_path=tmp_path / "summary.json"),),
    )
    for actual in ({"db": loaded}, scenario["tcad"]["db"]):
        resolved = image_sensor_db_reconcile_scenario(scenario, actual_physics={"tcad": actual})
        assert resolved["image_sensor_db"]["physics_binding"]["sensor_specific"] is True


def test_optimizer_revalidates_base_overrides_before_computing(tmp_path, monkeypatch) -> None:
    _bound_sensor_config(tmp_path)
    called = []
    monkeypatch.setattr(
        "pyisetcam.optimization.camerae2e_optimize_camera_parameters",
        lambda *args, **kwargs: called.append(True),
    )
    with pytest.raises(ValueError, match="after scenario overrides"):
        image_sensor_db_optimize_camera_parameters(
            "a", model="legacy", root=tmp_path, strategy="lut_only", require_sensor_specific=True,
            base_scenario={"fdtd": {"lut": str(tmp_path / "other.json")}},
        )
    assert not called


@pytest.mark.parametrize("override_in_base", [True, False])
def test_optimizer_provenance_describes_effective_selected_bindings(
    tmp_path, monkeypatch, override_in_base,
) -> None:
    _bound_sensor_config(tmp_path)
    other_path = str(tmp_path / "other.json")

    def optimize(scenario, **kwargs):
        selected = dict(scenario)
        if not override_in_base:
            selected["fdtd"] = {"enabled": True, "lut": other_path}
        return {"selected_scenarios": [selected]}

    monkeypatch.setattr("pyisetcam.optimization.camerae2e_optimize_camera_parameters", optimize)
    result = image_sensor_db_optimize_camera_parameters(
        "a", model="legacy", root=tmp_path, strategy="lut_only",
        base_scenario={
            **({"fdtd": {"lut": other_path}} if override_in_base else {}),
            "image_sensor_db": {"sensor_id": "stale-other-sensor"},
        },
    )
    source = result["source_image_sensor_db"]
    assert source["physics_binding"]["scope"] == "overridden"
    assert source["policy"]["physics_binding"]["scope"] == "overridden"
    selected_metadata = result["selected_scenarios"][0]["image_sensor_db"]
    assert selected_metadata["sensor_id"] == "a"
    assert selected_metadata["physics_binding"]["scope"] == "overridden"


def test_optimizer_source_tiers_follow_selected_components_not_base(tmp_path, monkeypatch) -> None:
    _bound_sensor_config(tmp_path)

    def optimize(scenario, **kwargs):
        # The selected cases disable the base FDTD and one enables TCAD.
        plain = {**scenario, "fdtd": {"enabled": False}, "tcad": {"enabled": False}}
        electrical = {**plain, "tcad": {"enabled": True, "db": {
            "generation_map_path": str(tmp_path / "generation.npz"),
            "collection_summary_paths": [str(tmp_path / "summary.json")],
            "accuracy_gate_path": None,
        }}}
        return {"selected_scenarios": [plain, electrical]}

    monkeypatch.setattr("pyisetcam.optimization.camerae2e_optimize_camera_parameters", optimize)
    result = image_sensor_db_optimize_camera_parameters(
        "a", model="legacy", root=tmp_path, strategy="lut_only",
    )
    source = result["source_image_sensor_db"]
    assert source["readiness_tier"] == "calibration_required"
    assert source["policy"]["component_tiers"]["fdtd_lut"] == "missing"
    assert source["policy"]["component_tiers"]["tcad_collection"] == "calibration_required"
    assert source["policy"]["component_variation"] == {"fdtd": False, "tcad": True}
    assert source["physics_binding"]["scope"] == "mixed"
