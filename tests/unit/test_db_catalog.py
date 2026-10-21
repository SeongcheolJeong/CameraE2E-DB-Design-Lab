from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from pyisetcam import (
    CameraE2EDBEntry,
    camerae2e_db_catalog,
    camerae2e_db_get,
    camerae2e_db_lineage,
    camerae2e_db_manifest,
    camerae2e_db_parameters,
    camerae2e_db_search,
    camerae2e_db_summary,
    camerae2e_db_validate,
    camerae2e_physics_pipeline_plan,
)


def test_camerae2e_db_catalog_has_core_families() -> None:
    entries = camerae2e_db_catalog()

    assert entries
    assert all(isinstance(entry, CameraE2EDBEntry) for entry in entries)
    families = {entry.family for entry in entries}

    assert {"lens", "sensor", "isp", "perception", "assets", "parity"} <= families


def test_camerae2e_db_search_finds_runtime_parameter_sources() -> None:
    lens_rows = camerae2e_db_search("lens", include_missing=False)
    fdtd_rows = camerae2e_db_search(tags=["fdtd"], include_missing=False)
    hwisp_rows = camerae2e_db_search(family="isp", include_missing=False)
    perception_rows = camerae2e_db_search("yolo", include_missing=False)

    assert any(row["name"] == "lens_patents_active" for row in lens_rows)
    assert any(row["name"] == "fdtd_sensor_lut_active" for row in fdtd_rows)
    assert any(row["name"] == "hwisp_parameter_profiles" for row in hwisp_rows)
    assert any(row["name"] == "task_perception_model_profiles" for row in perception_rows)


def test_camerae2e_db_parameters_are_directly_usable_paths() -> None:
    lens_params = camerae2e_db_parameters("lens_patents_active")
    hwisp_params = camerae2e_db_parameters("hwisp_parameter_profiles")
    perception_params = camerae2e_db_parameters("task_perception_model_profiles")

    assert Path(lens_params["db_path"]).exists()
    assert Path(lens_params["psf_dir"]).exists()
    assert isinstance(hwisp_params["profile_names"], list)
    assert hwisp_params["profile_names"]
    assert Path(perception_params["profiles_path"]).exists()
    assert isinstance(perception_params["profile_names"], list)
    assert perception_params["profile_names"]


def test_camerae2e_db_get_and_summary_are_json_friendly() -> None:
    row = camerae2e_db_get("lens_patents_active")
    summary = camerae2e_db_summary()

    assert row["name"] == "lens_patents_active"
    assert isinstance(row["path"], str)
    assert isinstance(row["parameters"]["db_path"], str)
    assert summary["total"] >= 6
    assert "lens_patents_active" in summary["active"]


def test_camerae2e_db_manifest_validation_and_lineage_are_json_friendly() -> None:
    manifest = camerae2e_db_manifest()
    validation = camerae2e_db_validate()
    lineage = camerae2e_db_lineage("tcad_sensor_db_active")

    assert manifest["schema_version"] == "camerae2e_db_manifest_v1"
    assert manifest["entries"]
    assert all("readiness_tier" in entry for entry in manifest["entries"])
    assert validation["schema_version"] == "camerae2e_db_validation_v1"
    assert lineage["root"] == "tcad_sensor_db_active"
    assert any(edge["to"] == "fdtd_sensor_lut_active" for edge in lineage["edges"])


def test_camerae2e_db_validate_promotes_active_fdtd_tcad_run_mismatch_to_stale_dependency(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fdtd_root = tmp_path / "FDTD"
    fdtd_run = fdtd_root / "runs" / "active_fdtd_run"
    tcad_run = fdtd_root / "runs" / "fdtd_to_tcad_generation_2d_cra_smoke"
    fdtd_run.mkdir(parents=True)
    tcad_run.mkdir(parents=True)
    lut_path = _write_minimal_fdtd_lut(fdtd_run)
    generation_path = _write_minimal_tcad_generation_map(tcad_run)
    _write_default_tcad_summaries(fdtd_root, generation_path)
    _write_default_tcad_gate(fdtd_root)

    monkeypatch.setenv("PYISETCAM_FDTD_ROOT", str(fdtd_root))
    monkeypatch.setenv("PYISETCAM_FDTD_LUT_PATH", str(lut_path))

    tcad = camerae2e_db_get("tcad_sensor_db_active")
    validation = camerae2e_db_validate()

    assert tcad["readiness_tier"] == "calibration_required"
    assert "active artifact mismatch" in tcad["stale_reason"]
    assert validation["stale_dependency_count"] >= 1

    plan = camerae2e_physics_pipeline_plan()
    tcad_action = next(
        action for action in plan["actions"] if action["entry"] == "tcad_sensor_db_active"
    )
    assert tcad_action["kind"] == "stale_dependency"
    assert tcad_action["action"] == "refresh_downstream_from_current_dependency"
    assert plan["active_runs"]["lineage_match"] is False


@pytest.fixture
def simulation_catalog(tmp_path: Path, monkeypatch):
    """A saved-summary API double: catalog reads may never request full results."""
    import pyisetcam.db_catalog as catalog

    root = tmp_path / "custom_camera_db"
    db_path = root / "image_sensor/simulations/sensor_simulations.sqlite"
    monkeypatch.setenv("PYISETCAM_CAMERA_DB_ROOT", str(root))
    monkeypatch.delenv("PYISETCAM_SENSOR_SIMULATION_DB_PATH", raising=False)
    for helper in (
        "_lens_entries", "_sensor_entries", "_hwisp_entries", "_perception_entries",
        "_upstream_entries", "_parity_entries",
    ):
        monkeypatch.setattr(catalog, helper, lambda: [])
    module = ModuleType("pyisetcam.sensor_simulation_db")
    calls = []
    rows = [
        {
            "sensor_id": f"sensor_{index}",
            "report_code": f"DEF-TEST-{index}",
            "device_name": "IMX662" if index == 0 else f"Device {index}",
            "manufacturer": "Sony" if index == 0 else "Example",
            "model": "physical_estimate_v1" if index == 0 else None,
            "status": status,
            "reason": None if index == 0 else f"Recorded {status} reason",
            "run_id": "saved-run",
            "result_path": "artifacts/sensor_0/results.npz" if index == 0 else None,
            "config_sha256": "b" * 64 if index == 0 else None,
            "result_sha256": "c" * 64 if index == 0 else None,
            "provenance": {"input_hashes": {"reviewed_evidence": "d" * 64}},
        }
        for index, status in enumerate(("simulated", "excluded", "failed", "quarantined"))
    ]

    def path_api(*, root):
        calls.append(("path", root))
        assert root == db_path.parents[2]
        return db_path

    def summary_api(*, root):
        calls.append(("summary", root))
        return {
            "schema": "sensor_simulation_db_v1",
            "status": "available" if db_path.is_file() else "missing",
            "db_path": str(db_path),
            "counts": {
                "total": 4 if db_path.is_file() else 0,
                "simulated": int(db_path.is_file()),
                "excluded": int(db_path.is_file()),
                "failed": int(db_path.is_file()),
                "quarantined": int(db_path.is_file()),
                "by_model": {"physical_estimate_v1": 1} if db_path.is_file() else {},
            },
            "db_sha256": "a" * 64 if db_path.is_file() else None,
            "provenance": {"build_id": "saved-build"},
        }

    def search_api(*, root):
        calls.append(("search", root))
        return rows

    def forbidden(*args, **kwargs):
        pytest.fail("A catalog lookup must not load full results or run a simulation")

    module.sensor_simulation_db_path = path_api
    module.sensor_simulation_db_summary = summary_api
    module.sensor_simulation_db_search = search_api
    module.sensor_simulation_db_get = forbidden
    module.sensor_simulation_db_build = forbidden
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return db_path, rows, calls, module


def test_simulation_catalog_missing_is_read_only(simulation_catalog) -> None:
    db_path, _, calls, _ = simulation_catalog

    row = camerae2e_db_get("sensor_simulation_db")

    assert row["status"] == row["readiness_tier"] == "missing"
    assert row["path"] == str(db_path)
    assert row["metadata"]["summary"]["counts"]["total"] == 0
    assert not db_path.parent.exists()
    assert not any(name == "search" for name, _ in calls)
    assert camerae2e_db_search(tags=["simulation"], include_missing=False) == []


def test_simulation_catalog_indexes_names_hashes_and_saved_statuses(simulation_catalog) -> None:
    db_path, _, calls, _ = simulation_catalog
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"Saved database represented by the summary API double")

    matches = camerae2e_db_search("sony imx662", tags=["simulation"], include_missing=False)
    row = matches[0]
    parent = camerae2e_db_get("sensor_simulation_db")

    assert len(matches) == 1
    assert row["name"] == "sensor_simulation:DEF-TEST-0"
    assert row["status"] == "available"
    assert row["readiness_tier"] == "proxy"
    assert row["source_hash"] == "sha256:" + "c" * 64
    assert row["parameters"] == {"sensor_id": "sensor_0", "root": str(db_path.parents[2])}
    assert parent["source_hash"] == "sha256:" + "a" * 64
    assert parent["metadata"]["summary"]["counts"]["total"] == 4
    assert parent["provenance"]["build_id"] == "saved-build"
    assert row["metadata"]["config_sha256"] == "b" * 64
    assert row["provenance"]["input_hashes"]["reviewed_evidence"] == "d" * 64
    assert row["provenance"]["estimate_only"] is True
    assert row["provenance"]["measured_accuracy_verified"] is False
    assert {name for name, _ in calls} == {"path", "summary", "search"}

    for index, status in enumerate(("excluded", "failed", "quarantined"), start=1):
        missing = camerae2e_db_get(f"sensor_simulation:DEF-TEST-{index}")
        assert missing["status"] == "missing"
        assert missing["readiness_tier"] == "missing"
        assert missing["metadata"]["status"] == status
        assert missing["metadata"]["reason"] == f"Recorded {status} reason"
    assert len(camerae2e_db_search(tags=["simulation"], include_missing=False)) == 2
    json.dumps(camerae2e_db_manifest())


def test_simulation_catalog_lineage_does_not_claim_measurement_validation(
    simulation_catalog,
) -> None:
    db_path, _, _, _ = simulation_catalog
    db_path.parent.mkdir(parents=True)
    db_path.touch()

    lineage = camerae2e_db_lineage("sensor_simulation:DEF-TEST-0")
    validation = camerae2e_db_validate(include_missing=False)
    strict = camerae2e_db_validate(strict=True, include_missing=False)

    assert lineage["edges"] == [
        {"from": "sensor_simulation:DEF-TEST-0", "to": "sensor_simulation_db"}
    ]
    assert validation["ok"]
    assert not strict["ok"]
    assert all(issue["kind"] == "readiness_tier" for issue in strict["issues"])
    assert all(node["readiness_tier"] != "validated" for node in lineage["nodes"])


def test_simulation_catalog_retains_corrupt_database_error(simulation_catalog) -> None:
    db_path, _, calls, module = simulation_catalog
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"invalid database")

    def corrupt(*, root):
        raise ValueError("Unsupported saved simulation schema")

    module.sensor_simulation_db_summary = corrupt
    row = camerae2e_db_get("sensor_simulation_db")

    assert row["status"] == "missing"
    assert row["metadata"]["load_error"] == "Unsupported saved simulation schema"
    assert "could not be read" in row["stale_reason"]
    assert not any(name == "search" for name, _ in calls)
    assert db_path.read_bytes() == b"invalid database"


def test_simulation_catalog_honors_explicit_database_path(
    simulation_catalog, monkeypatch,
) -> None:
    db_path, _, calls, module = simulation_catalog
    monkeypatch.setenv("PYISETCAM_SENSOR_SIMULATION_DB_PATH", str(db_path))

    def explicit_path(*, root):
        calls.append(("path", root))
        assert root is None  # The storage API owns explicit-path precedence.
        return db_path

    module.sensor_simulation_db_path = explicit_path
    row = camerae2e_db_get("sensor_simulation_db")

    assert row["path"] == str(db_path)
    assert row["parameters"]["root"] is None
    assert "PYISETCAM_SENSOR_SIMULATION_DB_PATH" in row["env_vars"]
    assert all(root is None for _, root in calls)


def _write_minimal_fdtd_lut(run_dir: Path) -> Path:
    summary_csv = run_dir / "camera_lut_summary.csv"
    lut_path = run_dir / "camera_lut.json"
    summary_csv.write_text(
        "\n".join(
            [
                "mode,wavelength_nm,case,field_x_norm,field_z_norm,cra_x_deg,cra_z_deg,total_response,normalized_total_response_to_first",
                "ocl-3x3,450,center,0,0,0,0,0.7,1.0",
                "ocl-3x3,550,center,0,0,0,0,0.8,1.0",
                "ocl-3x3,650,center,0,0,0,0,0.9,1.0",
                "ocl-3x3,550,edge20x,1,0,20,0,0.5,0.625",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    lut_path.write_text(
        json.dumps(
            {
                "schema": "camera_supercell_optical_lut_v2",
                "mode": "ocl-3x3",
                "wavelengths_nm": [450.0, 550.0, 650.0],
                "summary_csv": str(summary_csv),
            }
        ),
        encoding="utf-8",
    )
    return lut_path


def _write_minimal_tcad_generation_map(run_dir: Path) -> Path:
    path = run_dir / "tcad_generation_map_2d.npz"
    generation = np.ones((2, 2, 2), dtype=float) * 1.0e21
    np.savez(
        path,
        schema=np.asarray(["tcad_generation_map_2d_x_depth_v1"]),
        generation_cm3_s=generation,
        x_um=np.asarray([0.0, 1.0]),
        depth_um_from_si_top=np.asarray([0.1, 0.2]),
        case=np.asarray(["center", "edge20x"]),
        wavelength_nm=np.asarray([550.0, 550.0]),
        field_x_norm=np.asarray([0.0, 1.0]),
        field_z_norm=np.asarray([0.0, 0.0]),
        cra_x_deg=np.asarray([0.0, 20.0]),
        cra_z_deg=np.asarray([0.0, 0.0]),
    )
    return path


def _write_default_tcad_summaries(fdtd_root: Path, generation_path: Path) -> None:
    for relative, case in (
        ("runs/devsim_split_pd_2d_fdtd_map_proxy_center_smoke/summary.json", "center"),
        ("runs/devsim_split_pd_2d_fdtd_map_proxy_edge20x_smoke/summary.json", "edge20x"),
    ):
        path = fdtd_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "devsim_split_pd_2d_smoke_v1",
                    "config": {
                        "generation_profile_case": case,
                        "generation_profile_wavelength_nm": 550.0,
                        "generation_map_npz": str(generation_path),
                    },
                    "generation_source": "imported_2d_map",
                    "electrical_model": "proxy-pinned-split-pd",
                    "left_photo_delta_a_per_cm": 1.0e-6,
                    "right_photo_delta_a_per_cm": 1.0e-6,
                    "photo_split_phase_x_proxy": 0.0,
                    "terminal_current_balance_illuminated_a_per_cm": 0.0,
                }
            ),
            encoding="utf-8",
        )


def _write_default_tcad_gate(fdtd_root: Path) -> None:
    path = fdtd_root / "runs" / "tcad_accuracy_gate_reference_profile" / "tcad_accuracy_gate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "tcad_accuracy_gate_v1",
                "profile_name": "unit_test_proxy",
                "framework_ready": True,
                "accuracy_ready": False,
                "accuracy_blocking_failure_count": 1,
                "framework_blocking_failure_count": 0,
                "checks": [],
                "inputs": {},
            }
        ),
        encoding="utf-8",
    )
