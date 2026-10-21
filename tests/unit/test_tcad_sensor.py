from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from pyisetcam import (
    DEFAULT_WAVE,
    OpticalImage,
    fdtd_sensor_lut_load,
    sensor_attach_physics_lut,
    sensor_attach_tcad_lut,
    sensor_compute,
    sensor_create,
    sensor_get,
    sensor_set,
    tcad_sensor_apply_collection_response,
    tcad_sensor_collection_efficiency,
    tcad_sensor_config,
    tcad_sensor_db_load,
    tcad_sensor_generation_map_slice,
    tcad_sensor_split_phase,
    tcad_sensor_summary,
    tcad_sensor_validate,
)


def _write_fdtd_lut(tmp_path: Path) -> Path:
    summary_csv = tmp_path / "camera_lut_summary.csv"
    json_path = tmp_path / "camera_lut.json"
    summary_csv.write_text(
        "\n".join(
            [
                "mode,wavelength_nm,case,field_x_norm,field_z_norm,cra_x_deg,cra_z_deg,total_response,normalized_total_response_to_first",
                "ocl-3x3,550,center,0,0,0,0,1.0,1.0",
                "ocl-3x3,550,edge20x,1,0,20,0,0.5,0.5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(
            {
                "schema": "camera_supercell_optical_lut_v2",
                "mode": "ocl-3x3",
                "wavelengths_nm": [550.0],
                "cases": [
                    {
                        "name": "center",
                        "field_x_norm": 0.0,
                        "field_z_norm": 0.0,
                        "cra_x_deg": 0.0,
                        "cra_z_deg": 0.0,
                    },
                    {
                        "name": "edge20x",
                        "field_x_norm": 1.0,
                        "field_z_norm": 0.0,
                        "cra_x_deg": 20.0,
                        "cra_z_deg": 0.0,
                    },
                ],
                "summary_csv": str(summary_csv),
            }
        ),
        encoding="utf-8",
    )
    return json_path


def _write_tcad_db(tmp_path: Path) -> tuple[Path, list[Path], Path]:
    generation_path = tmp_path / "tcad_generation_map_2d.npz"
    generation = np.zeros((2, 3, 4), dtype=float)
    generation[0] = np.array(
        [
            [1.0, 0.6, 0.25, 0.1],
            [1.2, 0.7, 0.3, 0.1],
            [1.0, 0.6, 0.25, 0.1],
        ],
        dtype=float,
    )
    # Redistribute generation laterally while preserving its total, so the
    # 2x photocurrent difference below represents a 2x electrical efficiency.
    generation[1] = generation[0] * np.array([[0.6], [1.0], [1.4]], dtype=float)
    np.savez(
        generation_path,
        schema=np.asarray(["tcad_generation_map_2d_x_depth_v1"]),
        generation_cm3_s=generation * 1.0e21,
        absorption_fraction_per_um2=generation * 1.0e-3,
        x_um=np.array([-0.5, 0.0, 0.5], dtype=float),
        depth_um_from_si_top=np.array([0.15, 0.45, 0.75, 1.05], dtype=float),
        y_um=np.array([0.0, -0.3, -0.7, -1.1], dtype=float),
        case=np.asarray(["center", "edge20x"]),
        wavelength_nm=np.asarray([550.0, 550.0]),
        field_x_norm=np.asarray([0.0, 1.0]),
        field_z_norm=np.asarray([0.0, 0.0]),
        cra_x_deg=np.asarray([0.0, 20.0]),
        cra_z_deg=np.asarray([0.0, 0.0]),
        color_channel=np.asarray(["green"]),
        incident_photon_flux_cm2_s=np.asarray([1.0e20]),
        method=np.asarray(["synthetic unit-test map"]),
    )

    summary_paths: list[Path] = []
    for case, left, right, phase in (
        ("center", 1.0e-6, 2.0e-6, -0.01),
        ("edge20x", 2.5e-6, 3.5e-6, 0.04),
    ):
        path = tmp_path / f"{case}_summary.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "devsim_split_pd_2d_smoke_v1",
                    "devsim_version": "2.10.0",
                    "config": {
                        "generation_profile_case": case,
                        "generation_profile_wavelength_nm": 550.0,
                        "generation_map_npz": str(generation_path),
                    },
                    "generation_source": "imported_2d_map",
                    "electrical_model": "proxy-pinned-split-pd",
                    "left_photo_delta_a_per_cm": left,
                    "right_photo_delta_a_per_cm": right,
                    "photo_split_phase_x_proxy": phase,
                    "terminal_current_balance_illuminated_a_per_cm": 1.0e-15,
                    "dark": {},
                    "illuminated": {},
                    "notes": ["synthetic TCAD smoke result"],
                }
            ),
            encoding="utf-8",
        )
        summary_paths.append(path)

    gate_path = tmp_path / "tcad_accuracy_gate.json"
    gate_path.write_text(
        json.dumps(
            {
                "schema": "tcad_accuracy_gate_v1",
                "profile_name": "synthetic_reference_not_measured",
                "framework_ready": True,
                "accuracy_ready": False,
                "accuracy_blocking_failure_count": 3,
                "framework_blocking_failure_count": 0,
                "inputs": {"split_summary": [str(path) for path in summary_paths]},
                "checks": [
                    {
                        "name": "profile_calibration_status",
                        "status": "FAIL",
                        "accuracy_blocking": True,
                    },
                    {
                        "name": "terminal_current_balance",
                        "status": "PASS",
                        "accuracy_blocking": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return generation_path, summary_paths, gate_path


def _synthetic_oi(rows: int = 12, cols: int = 12) -> OpticalImage:
    wave = np.asarray(DEFAULT_WAVE, dtype=float)
    photons = np.ones((rows, cols, wave.size), dtype=float) * 1.0e12
    oi = OpticalImage(name="synthetic tcad oi")
    spacing = 2.8e-6
    oi.fields.update(
        {
            "wave": wave,
            "sample_spacing_m": spacing,
            "width_m": cols * spacing,
            "height_m": rows * spacing,
            "fov_deg": 1.0,
            "vfov_deg": 1.0,
            "optics": {"model": "skip", "focal_length_m": 0.004},
        }
    )
    oi.data["photons"] = photons
    return oi


def test_tcad_sensor_db_load_validate_and_summary(tmp_path: Path) -> None:
    generation_path, summary_paths, gate_path = _write_tcad_db(tmp_path)
    db = tcad_sensor_db_load(
        generation_map_path=generation_path,
        collection_summary_paths=summary_paths,
        accuracy_gate_path=gate_path,
    )

    validation = tcad_sensor_validate(db)
    summary = tcad_sensor_summary(db)

    assert validation["ok"] is True
    assert validation["status"] == "proxy-framework"
    assert validation["framework_ready"] is True
    assert validation["accuracy_ready"] is False
    assert summary["generation_map"]["shape"] == [2, 3, 4]
    assert summary["accuracy_gate"]["accuracy_blocking_failure_count"] == 3


def test_tcad_collection_efficiency_split_phase_and_generation_slice(tmp_path: Path) -> None:
    generation_path, summary_paths, gate_path = _write_tcad_db(tmp_path)
    db = tcad_sensor_db_load(
        generation_map_path=generation_path,
        collection_summary_paths=summary_paths,
        accuracy_gate_path=gate_path,
    )

    x_um, depth_um, generation = tcad_sensor_generation_map_slice(db, case="edge20x")

    assert tcad_sensor_collection_efficiency(db, case="center") == pytest.approx(1.0)
    assert tcad_sensor_collection_efficiency(db, case="edge20x") == pytest.approx(2.0)
    assert tcad_sensor_split_phase(db, case="edge20x") == pytest.approx(0.04)
    assert x_um.shape == (3,)
    assert depth_um.shape == (4,)
    assert generation.shape == (3, 4)


def test_tcad_collection_response_requires_explicit_config(tmp_path: Path) -> None:
    generation_path, summary_paths, gate_path = _write_tcad_db(tmp_path)
    db = tcad_sensor_db_load(
        generation_map_path=generation_path,
        collection_summary_paths=summary_paths,
        accuracy_gate_path=gate_path,
    )
    values = np.ones((3, 3), dtype=float)

    unchanged = tcad_sensor_apply_collection_response(values, None)
    changed = tcad_sensor_apply_collection_response(values, tcad_sensor_config(db, case="edge20x"))

    assert np.allclose(unchanged, values)
    assert np.allclose(changed, values * 2.0)
    with pytest.raises(ValueError):
        tcad_sensor_apply_collection_response(
            values,
            tcad_sensor_config(db, case="edge20x", allow_proxy_accuracy=False),
        )


def test_tcad_config_accepts_top_level_path_mapping(tmp_path: Path) -> None:
    generation_path, summary_paths, gate_path = _write_tcad_db(tmp_path)
    config = tcad_sensor_config(
        {
            "generation_map_path": str(generation_path),
            "collection_summary_paths": [str(path) for path in summary_paths],
            "accuracy_gate_path": str(gate_path),
        },
        case="edge20x",
    )

    response = tcad_sensor_apply_collection_response(np.ones((2, 2), dtype=float), config)

    assert np.allclose(response, 2.0)


def test_sensor_compute_with_tcad_collection_hook_changes_only_when_attached(
    asset_store, tmp_path: Path
) -> None:
    generation_path, summary_paths, gate_path = _write_tcad_db(tmp_path)
    db = tcad_sensor_db_load(
        generation_map_path=generation_path,
        collection_summary_paths=summary_paths,
        accuracy_gate_path=gate_path,
    )
    oi = _synthetic_oi()
    base = sensor_set(
        sensor_create("monochrome", asset_store=asset_store), "size", oi.data["photons"].shape[:2]
    )
    base = sensor_set(base, "noise flag", 0)
    base = sensor_set(base, "integration time", 0.01)
    tcad = sensor_attach_tcad_lut(base, db, case="edge20x")

    base_result = sensor_compute(base, oi, seed=0)
    tcad_result = sensor_compute(tcad, oi, seed=0)

    base_volts = np.asarray(sensor_get(base_result, "volts"), dtype=float)
    tcad_volts = np.asarray(sensor_get(tcad_result, "volts"), dtype=float)
    assert np.allclose(tcad_volts, base_volts * 2.0)
    assert sensor_get(base_result, "tcad sensor") is None
    assert sensor_get(tcad, "tcad sensor") is not None


def test_sensor_attach_physics_lut_can_attach_tcad_without_fdtd(
    asset_store, tmp_path: Path
) -> None:
    generation_path, summary_paths, gate_path = _write_tcad_db(tmp_path)
    db = tcad_sensor_db_load(
        generation_map_path=generation_path,
        collection_summary_paths=summary_paths,
        accuracy_gate_path=gate_path,
    )
    sensor = sensor_create("monochrome", asset_store=asset_store)

    updated = sensor_attach_physics_lut(sensor, tcad_db=db, tcad_kwargs={"case": "edge20x"})

    assert sensor_get(sensor, "tcad sensor") is None
    assert sensor_get(updated, "tcad sensor") is not None


def test_sensor_attach_physics_lut_combines_fdtd_and_tcad_layers(
    asset_store, tmp_path: Path
) -> None:
    generation_path, summary_paths, gate_path = _write_tcad_db(tmp_path)
    db = tcad_sensor_db_load(
        generation_map_path=generation_path,
        collection_summary_paths=summary_paths,
        accuracy_gate_path=gate_path,
    )
    fdtd_lut = fdtd_sensor_lut_load(_write_fdtd_lut(tmp_path))
    oi = _synthetic_oi(rows=16, cols=16)
    base = sensor_set(
        sensor_create("monochrome", asset_store=asset_store), "size", oi.data["photons"].shape[:2]
    )
    base = sensor_set(base, "noise flag", 0)
    base = sensor_set(base, "integration time", 0.01)
    combined = sensor_attach_physics_lut(
        base,
        fdtd_lut=fdtd_lut,
        tcad_db=db,
        fdtd_kwargs={"mode": "field"},
        tcad_kwargs={"case": "edge20x"},
    )

    base_result = sensor_compute(base, oi, seed=0)
    combined_result = sensor_compute(combined, oi, seed=0)

    assert sensor_get(combined, "fdtd sensor") is not None
    assert sensor_get(combined, "tcad sensor") is not None
    assert not np.allclose(sensor_get(base_result, "volts"), sensor_get(combined_result, "volts"))


def _load_test_db(tmp_path: Path):
    generation_path, summary_paths, gate_path = _write_tcad_db(tmp_path)
    return tcad_sensor_db_load(
        generation_map_path=generation_path,
        collection_summary_paths=summary_paths,
        accuracy_gate_path=gate_path,
        root=tmp_path,
    )


def test_collection_efficiency_removes_optical_absorption_and_common_scale(tmp_path: Path) -> None:
    db = _load_test_db(tmp_path)
    center = db.collection_summaries[0]
    edge = replace(
        db.collection_summaries[1],
        left_photo_delta_a_per_cm=center.left_photo_delta_a_per_cm * 0.5,
        right_photo_delta_a_per_cm=center.right_photo_delta_a_per_cm * 0.5,
    )
    generation = db.generation_map.generation_cm3_s.copy()
    generation[1] = generation[0] * 0.5
    db = replace(
        db,
        generation_map=replace(db.generation_map, generation_cm3_s=generation),
        collection_summaries=(center, edge),
    )
    assert tcad_sensor_collection_efficiency(db, case="edge20x") == pytest.approx(1.0)
    # FDTD has already applied the 50% optical loss. Electrical efficiency
    # is unchanged, so it must not apply the optical loss a second time.
    output = tcad_sensor_apply_collection_response([0.5], tcad_sensor_config(db, case="edge20x"))
    assert output == pytest.approx([0.5])
    absolute = tcad_sensor_collection_efficiency(db, case="edge20x", normalize_to_center=False)
    scaled = replace(
        db,
        generation_map=replace(db.generation_map, generation_cm3_s=generation * 7.0),
        collection_summaries=tuple(
            replace(
                summary,
                left_photo_delta_a_per_cm=summary.left_photo_delta_a_per_cm * 7.0,
                right_photo_delta_a_per_cm=summary.right_photo_delta_a_per_cm * 7.0,
            )
            for summary in db.collection_summaries
        ),
    )
    assert tcad_sensor_collection_efficiency(
        scaled, case="edge20x", normalize_to_center=False
    ) == pytest.approx(absolute)
    assert tcad_sensor_collection_efficiency(scaled, case="edge20x") == pytest.approx(1.0)


def test_absolute_collection_efficiency_uses_cell_area_and_generation_scale(tmp_path: Path) -> None:
    db = _load_test_db(tmp_path)
    # Three 0.5 um cells by four 0.3 um cells: full area is 1.8 um^2.
    # Include the boundary half cells; trapezoid over centers loses their area.
    generation = replace(db.generation_map, generation_cm3_s=np.full((2, 3, 4), 1e21))
    generated_current = 1.602176634e-19 * 1e21 * 1.8e-8 * 2.0
    summaries = tuple(
        replace(
            summary,
            config={**summary.config, "generation_map_scale": 2.0},
            left_photo_delta_a_per_cm=generated_current * 0.3,
            right_photo_delta_a_per_cm=generated_current * 0.3,
        )
        for summary in db.collection_summaries
    )
    db = replace(db, generation_map=generation, collection_summaries=summaries)
    assert tcad_sensor_collection_efficiency(db, normalize_to_center=False) == pytest.approx(0.6)
    assert tcad_sensor_apply_collection_response(
        [10.0], tcad_sensor_config(db, normalize_to_center=False)
    ) == pytest.approx([6.0])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_tcad_currents_cannot_pass_proxy_validation(tmp_path: Path, value: float) -> None:
    db = _load_test_db(tmp_path)
    invalid = replace(
        db,
        collection_summaries=(
            replace(db.collection_summaries[0], left_photo_delta_a_per_cm=value),
            *db.collection_summaries[1:],
        ),
    )
    assert tcad_sensor_validate(invalid)["status"] == "invalid"
    with pytest.raises(ValueError, match="Invalid TCAD"):
        tcad_sensor_apply_collection_response(
            [1.0], tcad_sensor_config(invalid, allow_proxy_accuracy=True)
        )
    with pytest.raises(ValueError, match="Invalid TCAD"):
        tcad_sensor_collection_efficiency(invalid)


@pytest.mark.parametrize("defect", ["missing", "empty", "nan", "zero", "axis", "wavelength"])
def test_invalid_generation_is_rejected_even_in_proxy_mode(tmp_path: Path, defect: str) -> None:
    db = _load_test_db(tmp_path)
    generation = db.generation_map
    if defect == "missing":
        generation = None
    elif defect == "empty":
        generation = replace(generation, generation_cm3_s=np.empty((0, 3, 4)))
    elif defect in {"nan", "zero"}:
        generation = replace(
            generation, generation_cm3_s=np.full((2, 3, 4), np.nan if defect == "nan" else 0.0)
        )
    elif defect == "axis":
        generation = replace(generation, x_um=np.array([-0.5, np.nan, 0.5]))
    else:
        generation = replace(generation, wavelength_nm=np.array([550.0]))
    invalid = replace(db, generation_map=generation)
    assert tcad_sensor_validate(invalid)["status"] == "invalid"
    with pytest.raises(ValueError, match="Invalid TCAD"):
        tcad_sensor_apply_collection_response([1.0], tcad_sensor_config(invalid))


def _accuracy_ready_db(db):
    return replace(
        db,
        accuracy_gate=replace(
            db.accuracy_gate,
            framework_ready=True,
            accuracy_ready=True,
            framework_blocking_failure_count=0,
            accuracy_blocking_failure_count=0,
            checks=[],
        ),
    )


def test_accuracy_gate_requires_applicable_summary_and_generation_lineage(tmp_path: Path) -> None:
    db = _accuracy_ready_db(_load_test_db(tmp_path))
    assert tcad_sensor_validate(db)["accuracy_ready"] is True
    tcad_sensor_apply_collection_response([1.0], tcad_sensor_config(db, allow_proxy_accuracy=False))
    unrelated_summary_gate = replace(
        db,
        accuracy_gate=replace(
            db.accuracy_gate, inputs={"split_summary": [str(tmp_path / "unrelated.json")]}
        ),
    )
    unrelated_map_gate = replace(
        db,
        accuracy_gate=replace(
            db.accuracy_gate,
            inputs={
                **db.accuracy_gate.inputs,
                "generation_map_path": str(tmp_path / "unrelated.npz"),
            },
        ),
    )
    for mismatched in (unrelated_summary_gate, unrelated_map_gate):
        validation = tcad_sensor_validate(mismatched)
        assert validation["status"] == "proxy-framework"
        assert validation["accuracy_ready"] is False
        assert validation["checks"]["accuracy_gate"]["applicable"] is False
        assert np.isfinite(
            tcad_sensor_apply_collection_response([1.0], tcad_sensor_config(mismatched))
        ).all()
        with pytest.raises(ValueError, match="not accuracy-ready"):
            tcad_sensor_apply_collection_response(
                [1.0], tcad_sensor_config(mismatched, allow_proxy_accuracy=False)
            )

    # An unrelated G cannot be used as the denominator of the loaded current.
    unrelated_generation = replace(
        db, generation_map=replace(db.generation_map, source_path=tmp_path / "unrelated.npz")
    )
    assert tcad_sensor_validate(unrelated_generation)["status"] == "invalid"
    with pytest.raises(ValueError, match="not the selected generation map"):
        tcad_sensor_apply_collection_response([1.0], tcad_sensor_config(unrelated_generation))

    missing_reference = replace(
        db,
        collection_summaries=tuple(
            replace(
                summary,
                config={
                    key: value
                    for key, value in summary.config.items()
                    if key != "generation_map_npz"
                },
            )
            for summary in db.collection_summaries
        ),
    )
    assert tcad_sensor_validate(missing_reference)["status"] == "proxy-framework"
    assert tcad_sensor_validate(missing_reference)["accuracy_ready"] is False


def test_framework_gate_failure_cannot_be_overridden_by_proxy_flag(tmp_path: Path) -> None:
    db = _load_test_db(tmp_path)
    invalid = replace(db, accuracy_gate=replace(db.accuracy_gate, framework_ready=False))
    with pytest.raises(ValueError, match="Invalid TCAD"):
        tcad_sensor_apply_collection_response(
            [1.0], tcad_sensor_config(invalid, allow_proxy_accuracy=True)
        )


def test_collection_configuration_rejects_nonfinite_values_and_unknown_case(tmp_path: Path) -> None:
    db = _load_test_db(tmp_path)
    with pytest.raises(ValueError, match="finite"):
        tcad_sensor_apply_collection_response([np.nan], tcad_sensor_config(db))
    with pytest.raises(ValueError, match="collection_strength"):
        tcad_sensor_apply_collection_response(
            [1.0], tcad_sensor_config(db, collection_strength=np.nan)
        )
    with pytest.raises(ValueError, match="matches case"):
        tcad_sensor_collection_efficiency(db, case="unknown")


def test_explicit_collection_paths_do_not_inherit_unrelated_default_gate(tmp_path: Path) -> None:
    generation_path, summary_paths, gate_path = _write_tcad_db(tmp_path)
    default_gate = tmp_path / "runs/tcad_accuracy_gate_reference_profile/tcad_accuracy_gate.json"
    default_gate.parent.mkdir(parents=True)
    default_gate.write_text(gate_path.read_text(encoding="utf-8"), encoding="utf-8")
    db = tcad_sensor_db_load(
        generation_map_path=generation_path,
        collection_summary_paths=summary_paths,
        root=tmp_path,
    )
    assert db.accuracy_gate is None
    assert tcad_sensor_validate(db)["status"] == "proxy-framework"
    assert np.isfinite(tcad_sensor_apply_collection_response([1.0], tcad_sensor_config(db))).all()


@pytest.mark.parametrize(
    "missing", ["generation_map_path", "collection_summary_paths", "accuracy_gate_path"]
)
def test_explicit_missing_artifact_cannot_be_silently_omitted(tmp_path: Path, missing: str) -> None:
    generation_path, summary_paths, gate_path = _write_tcad_db(tmp_path)
    paths = {
        "generation_map_path": generation_path,
        "collection_summary_paths": summary_paths,
        "accuracy_gate_path": gate_path,
    }
    absent = tmp_path / "absent"
    paths[missing] = [*summary_paths, absent] if missing == "collection_summary_paths" else absent
    with pytest.raises(FileNotFoundError):
        tcad_sensor_db_load(**paths, root=tmp_path)


def test_collection_selection_prefers_center_and_rejects_duplicate_pairs(tmp_path: Path) -> None:
    db = _load_test_db(tmp_path)
    reordered = replace(db, collection_summaries=tuple(reversed(db.collection_summaries)))
    assert tcad_sensor_collection_efficiency(reordered) == pytest.approx(1.0)
    duplicate = replace(
        db, collection_summaries=(*db.collection_summaries, db.collection_summaries[0])
    )
    with pytest.raises(ValueError, match="duplicate TCAD"):
        tcad_sensor_collection_efficiency(duplicate)


def test_missing_summary_wavelength_is_inferred_from_generation(tmp_path: Path) -> None:
    db = _load_test_db(tmp_path)
    db = replace(
        db,
        collection_summaries=tuple(
            replace(summary, wavelength_nm=None) for summary in db.collection_summaries
        ),
    )
    assert tcad_sensor_validate(db)["framework_ready"] is True
    assert tcad_sensor_collection_efficiency(
        db, case="edge20x", wavelength_nm=550.0
    ) == pytest.approx(2.0)
    assert tcad_sensor_collection_efficiency(db, case="edge20x") == pytest.approx(2.0)
    with pytest.raises(ValueError, match="matches case"):
        tcad_sensor_collection_efficiency(db, case="edge20x", wavelength_nm=650.0)


def test_missing_summary_wavelength_cannot_normalize_across_wavelengths(tmp_path: Path) -> None:
    db = _load_test_db(tmp_path)
    db = replace(
        db,
        generation_map=replace(db.generation_map, wavelength_nm=np.array([550.0, 650.0])),
        collection_summaries=tuple(
            replace(summary, wavelength_nm=None) for summary in db.collection_summaries
        ),
    )
    assert tcad_sensor_validate(db)["framework_ready"] is True
    # Each current still has a unique valid generation denominator, but a
    # 550 nm center cannot normalize the 650 nm off-axis collection efficiency.
    absolute = tcad_sensor_collection_efficiency(
        db, case="edge20x", wavelength_nm=650.0, normalize_to_center=False
    )
    assert np.isfinite(absolute)
    for wavelength in (None, 650.0):
        with pytest.raises(ValueError, match="center.*650"):
            tcad_sensor_collection_efficiency(db, case="edge20x", wavelength_nm=wavelength)


def test_multiwavelength_collection_requires_selection_and_matching_center(tmp_path: Path) -> None:
    db = _load_test_db(tmp_path)
    generation = db.generation_map
    generation = replace(
        generation,
        generation_cm3_s=np.concatenate(
            [generation.generation_cm3_s, generation.generation_cm3_s * 0.5]
        ),
        cases=np.tile(generation.cases, 2),
        wavelength_nm=np.array([550.0, 550.0, 650.0, 650.0]),
        field_x_norm=np.tile(generation.field_x_norm, 2),
        field_z_norm=np.tile(generation.field_z_norm, 2),
        cra_x_deg=np.tile(generation.cra_x_deg, 2),
        cra_z_deg=np.tile(generation.cra_z_deg, 2),
    )
    extra_summaries = tuple(
        replace(
            summary,
            wavelength_nm=650.0,
            left_photo_delta_a_per_cm=summary.left_photo_delta_a_per_cm * scale,
            right_photo_delta_a_per_cm=summary.right_photo_delta_a_per_cm * scale,
        )
        for summary, scale in zip(db.collection_summaries, [0.25, 0.75], strict=True)
    )
    db = replace(
        db,
        generation_map=generation,
        collection_summaries=(*db.collection_summaries, *extra_summaries),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        tcad_sensor_collection_efficiency(db, case="edge20x")
    assert tcad_sensor_collection_efficiency(
        db, case="edge20x", wavelength_nm=550.0
    ) == pytest.approx(2.0)
    assert tcad_sensor_collection_efficiency(
        db, case="edge20x", wavelength_nm=650.0
    ) == pytest.approx(6.0)
    config = tcad_sensor_config(db, case="edge20x", wavelength_nm=650.0)
    assert tcad_sensor_apply_collection_response([1.0], config) == pytest.approx([6.0])


def test_collection_efficiency_above_one_is_reported_without_clamping(tmp_path: Path) -> None:
    db = _load_test_db(tmp_path)
    high_gain = replace(
        db,
        collection_summaries=tuple(
            replace(
                summary,
                left_photo_delta_a_per_cm=1.0,
                right_photo_delta_a_per_cm=1.0,
            )
            for summary in db.collection_summaries
        ),
    )
    validation = tcad_sensor_validate(high_gain)
    assert validation["status"] == "proxy-framework"
    assert any("efficiency exceeds 1" in warning for warning in validation["warnings"])
    assert tcad_sensor_collection_efficiency(high_gain, normalize_to_center=False) > 1.0
