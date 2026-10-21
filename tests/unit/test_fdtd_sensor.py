from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from pyisetcam import (
    DEFAULT_WAVE,
    OpticalImage,
    fdtd_sensor_apply_optical_response,
    fdtd_sensor_config,
    fdtd_sensor_cos4_relative_illumination,
    fdtd_sensor_default_lut_path,
    fdtd_sensor_field_response_map,
    fdtd_sensor_lut_crosstalk_kernel,
    fdtd_sensor_lut_load,
    fdtd_sensor_lut_response,
    fdtd_sensor_lut_validate,
    fdtd_sensor_physics_validate,
    sensor_attach_fdtd_lut,
    sensor_compute,
    sensor_create,
    sensor_get,
    sensor_set,
)
from pyisetcam.fdtd_sensor import FDTDSensorLUT, fdtd_sensor_qe_scale


def _write_synthetic_lut(tmp_path: Path) -> Path:
    long_csv = tmp_path / "camera_lut_long.csv"
    summary_csv = tmp_path / "camera_lut_summary.csv"
    json_path = tmp_path / "camera_lut.json"

    summary_csv.write_text(
        "\n".join(
            [
                "mode,wavelength_nm,case,field_x_norm,field_z_norm,cra_x_deg,cra_z_deg,total_response,normalized_total_response_to_first",
                "ocl-3x3,550,center,0,0,0,0,1.0,1.0",
                "ocl-3x3,550,edge20,1,0,20,0,0.50,0.50",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows = [
        ("center", 0, 0, 0.02),
        ("center", 1, 0, 0.03),
        ("center", 2, 0, 0.02),
        ("center", 0, 1, 0.03),
        ("center", 1, 1, 0.80),
        ("center", 2, 1, 0.03),
        ("center", 0, 2, 0.02),
        ("center", 1, 2, 0.03),
        ("center", 2, 2, 0.02),
        ("edge20", 0, 0, 0.06),
        ("edge20", 1, 0, 0.08),
        ("edge20", 2, 0, 0.06),
        ("edge20", 0, 1, 0.08),
        ("edge20", 1, 1, 0.28),
        ("edge20", 2, 1, 0.08),
        ("edge20", 0, 2, 0.06),
        ("edge20", 1, 2, 0.08),
        ("edge20", 2, 2, 0.06),
    ]
    long_lines = [
        "mode,wavelength_nm,case,field_x_norm,field_z_norm,cra_x_deg,cra_z_deg,region_id,region_kind,region_ix,region_iz,response"
    ]
    for case, ix, iz, response in rows:
        field = 0 if case == "center" else 1
        cra = 0 if case == "center" else 20
        long_lines.append(f"ocl-3x3,550,{case},{field},0,{cra},0,pix_x{ix}_z{iz},pixel,{ix},{iz},{response}")
    long_csv.write_text("\n".join(long_lines) + "\n", encoding="utf-8")

    json_path.write_text(
        json.dumps(
            {
                "schema": "camera_supercell_optical_lut_v1",
                "mode": "ocl-3x3",
                "cell_pixels": {"x": 3, "z": 3},
                "wavelengths_nm": [550.0],
                "cases": [
                    {"name": "center", "field_x_norm": 0.0, "field_z_norm": 0.0, "cra_x_deg": 0.0, "cra_z_deg": 0.0},
                    {"name": "edge20", "field_x_norm": 1.0, "field_z_norm": 0.0, "cra_x_deg": 20.0, "cra_z_deg": 0.0},
                ],
                "regions": [
                    {"region_id": f"pix_x{ix}_z{iz}", "kind": "pixel", "ix": ix, "iz": iz}
                    for iz in range(3)
                    for ix in range(3)
                ],
                "long_csv": str(long_csv),
                "summary_csv": str(summary_csv),
                "notes": ["unit-test optical absorption proxy"],
            }
        ),
        encoding="utf-8",
    )
    return json_path


def _write_synthetic_split_pd_lut(tmp_path: Path) -> Path:
    long_csv = tmp_path / "camera_lut_long.csv"
    summary_csv = tmp_path / "camera_lut_summary.csv"
    json_path = tmp_path / "camera_lut.json"
    summary_csv.write_text(
        "\n".join(
            [
                "schema,mode,wavelength_nm,case,field_x_norm,field_z_norm,cra_x_deg,cra_z_deg,total_response,normalized_total_response_to_first",
                "camera_supercell_optical_lut_v2,split-pd-1x1,550,center,0,0,0,0,1.0,1.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    long_csv.write_text(
        "\n".join(
            [
                "schema,mode,wavelength_nm,case,region_id,region_kind,region_ix,region_iz,response",
                "camera_supercell_optical_lut_v2,split-pd-1x1,550,center,pd_left,subpd,-1,0,0.5",
                "camera_supercell_optical_lut_v2,split-pd-1x1,550,center,pd_right,subpd,1,0,0.5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(
            {
                "schema": "camera_supercell_optical_lut_v2",
                "response_model": "si_volume_absorption_flux_calibrated_v1",
                "mode": "split-pd-1x1",
                "wavelengths_nm": [550.0],
                "long_csv": str(long_csv),
                "summary_csv": str(summary_csv),
            }
        ),
        encoding="utf-8",
    )
    return json_path


def _write_root_relative_lut(tmp_path: Path) -> Path:
    root = tmp_path / "FDTD"
    run_dir = root / "runs" / "relative_case"
    run_dir.mkdir(parents=True)
    summary_csv = run_dir / "camera_lut_summary.csv"
    long_csv = run_dir / "camera_lut_long.csv"
    json_path = run_dir / "camera_lut.json"
    summary_csv.write_text(
        "\n".join(
            [
                "mode,wavelength_nm,case,field_x_norm,field_z_norm,cra_x_deg,cra_z_deg,total_response,normalized_total_response_to_first",
                "split-pd-1x1,550,center,0,0,0,0,1.0,1.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    long_csv.write_text(
        "\n".join(
            [
                "mode,wavelength_nm,case,region_id,region_kind,region_ix,region_iz,response",
                "split-pd-1x1,550,center,pd_left,subpd,-1,0,0.5",
                "split-pd-1x1,550,center,pd_right,subpd,1,0,0.5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps(
            {
                "schema": "camera_supercell_optical_lut_v2",
                "mode": "split-pd-1x1",
                "wavelengths_nm": [550.0],
                "long_csv": "runs/relative_case/camera_lut_long.csv",
                "summary_csv": "runs/relative_case/camera_lut_summary.csv",
            }
        ),
        encoding="utf-8",
    )
    return json_path


def _synthetic_oi(rows: int = 24, cols: int = 24) -> OpticalImage:
    wave = np.asarray(DEFAULT_WAVE, dtype=float)
    edge = np.zeros((rows, cols), dtype=float)
    edge[:, cols // 2 :] = 2.0e12
    edge[:, : cols // 2] = 0.5e12
    cube = edge[:, :, None] * np.ones((1, 1, wave.size), dtype=float)

    oi = OpticalImage(name="synthetic fdtd sensor edge")
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
    oi.data["photons"] = cube
    return oi


def test_fdtd_sensor_lut_load_validate_and_response(tmp_path: Path) -> None:
    lut = fdtd_sensor_lut_load(_write_synthetic_lut(tmp_path))
    validation = fdtd_sensor_lut_validate(lut)

    assert validation["ok"] is True
    assert validation["kernel_shape"] == [3, 3]
    assert validation["n_cases"] == 2
    assert fdtd_sensor_lut_response(lut, case="center", wavelength_nm=550) == pytest.approx(1.0)
    assert fdtd_sensor_lut_response(lut, case="edge20", wavelength_nm=550) == pytest.approx(0.5)


def test_fdtd_sensor_lut_load_resolves_fdtd_root_relative_sidecars(tmp_path: Path) -> None:
    lut = fdtd_sensor_lut_load(_write_root_relative_lut(tmp_path))

    assert len(lut.summary_rows) == 1
    assert len(lut.long_rows) == 2
    assert fdtd_sensor_lut_validate(lut)["ok"] is True


def test_split_pd_lut_is_not_treated_as_pixel_crosstalk(tmp_path: Path) -> None:
    lut = fdtd_sensor_lut_load(_write_synthetic_split_pd_lut(tmp_path))

    kernel = fdtd_sensor_lut_crosstalk_kernel(lut, case="center", wavelength_nm=550)

    assert kernel.shape == (1, 1)
    assert kernel[0, 0] == pytest.approx(1.0)


def test_fdtd_sensor_physics_validation_flags_bad_ocl_and_sparse_sweep(tmp_path: Path) -> None:
    lut = fdtd_sensor_lut_load(_write_synthetic_lut(tmp_path))
    physics = fdtd_sensor_physics_validate(lut)

    assert fdtd_sensor_cos4_relative_illumination(20.0) == pytest.approx(np.cos(np.deg2rad(20.0)) ** 4)
    assert physics["status"] in {"warn", "fail"}
    assert physics["checks"]["wavelength"]["status"] == "warn"
    assert physics["checks"]["symmetry"]["status"] == "warn"
    assert physics["checks"]["relative_illumination"]["comparisons"][0]["cos4_response"] == pytest.approx(
        np.cos(np.deg2rad(20.0)) ** 4
    )


def test_fdtd_sensor_physics_validation_detects_ocl_regression(tmp_path: Path) -> None:
    path = _write_synthetic_lut(tmp_path)
    summary_path = path.with_name("camera_lut_summary.csv")
    summary_path.write_text(
        "\n".join(
            [
                "mode,wavelength_nm,case,field_x_norm,field_z_norm,cra_x_deg,cra_z_deg,total_response,normalized_total_response_to_first",
                "ocl-3x3,550,center,0,0,0,0,1.0,1.0",
                "ocl-3x3,550,edge20_uncomp,1,0,20,0,0.70,0.70",
                "ocl-3x3,550,edge20_ocl,1,0,20,0,0.55,0.55",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    lut = fdtd_sensor_lut_load(path)
    physics = fdtd_sensor_physics_validate(lut)

    assert physics["checks"]["ocl_shift"]["status"] == "fail"
    assert physics["checks"]["ocl_shift"]["pairs"][0]["improvement"] < 0.0


def test_fdtd_sensor_field_response_map_uses_center_to_edge_rolloff(tmp_path: Path) -> None:
    lut = fdtd_sensor_lut_load(_write_synthetic_lut(tmp_path))

    response_map = fdtd_sensor_field_response_map(lut, (9, 9), wavelength_nm=550)

    assert response_map.shape == (9, 9)
    assert response_map[4, 4] == pytest.approx(1.0)
    assert response_map[0, 0] == pytest.approx(0.5)


def test_fdtd_sensor_crosstalk_kernel_spreads_impulse(tmp_path: Path) -> None:
    lut = fdtd_sensor_lut_load(_write_synthetic_lut(tmp_path))
    kernel = fdtd_sensor_lut_crosstalk_kernel(lut, case="center", wavelength_nm=550)
    impulse = np.zeros((7, 7), dtype=float)
    impulse[3, 3] = 1.0

    response = fdtd_sensor_apply_optical_response(
        impulse,
        fdtd_sensor_config(lut, mode="crosstalk", crosstalk_strength=1.0),
    )

    assert kernel.shape == (3, 3)
    assert np.isclose(float(np.sum(kernel)), 1.0)
    assert response[3, 3] < 1.0
    assert response[3, 2] > 0.0
    assert np.isclose(float(np.sum(response)), 1.0)


def test_sensor_compute_without_fdtd_config_is_unchanged(asset_store) -> None:
    oi = _synthetic_oi()
    base = sensor_set(sensor_create("monochrome", asset_store=asset_store), "size", oi.data["photons"].shape[:2])
    base = sensor_set(base, "noise flag", 0)
    base = sensor_set(base, "integration time", 0.01)
    disabled = sensor_set(base.clone(), "fdtd sensor", {"enabled": False})

    base_result = sensor_compute(base, oi, seed=0)
    disabled_result = sensor_compute(disabled, oi, seed=0)

    assert np.allclose(sensor_get(base_result, "volts"), sensor_get(disabled_result, "volts"))


def test_sensor_compute_with_fdtd_lut_changes_sensor_volts(asset_store, tmp_path: Path) -> None:
    lut = fdtd_sensor_lut_load(_write_synthetic_lut(tmp_path))
    oi = _synthetic_oi()
    sensor = sensor_set(sensor_create("monochrome", asset_store=asset_store), "size", oi.data["photons"].shape[:2])
    sensor = sensor_set(sensor, "noise flag", 0)
    sensor = sensor_set(sensor, "integration time", 0.01)
    fdtd_sensor = sensor_attach_fdtd_lut(sensor, lut, mode="field+crosstalk", crosstalk_strength=1.0)

    base_result = sensor_compute(sensor, oi, seed=0)
    fdtd_result = sensor_compute(fdtd_sensor, oi, seed=0)

    base_volts = np.asarray(sensor_get(base_result, "volts"), dtype=float)
    fdtd_volts = np.asarray(sensor_get(fdtd_result, "volts"), dtype=float)
    assert fdtd_volts.shape == base_volts.shape
    assert not np.allclose(fdtd_volts, base_volts)
    assert float(np.mean(fdtd_volts[:2, :2])) < float(np.mean(base_volts[:2, :2]))


def test_real_fdtd_default_lut_smoke_if_available(asset_store) -> None:
    path = fdtd_sensor_default_lut_path()
    if path is None:
        pytest.skip("external FDTD LUT repo is not available")
    lut = fdtd_sensor_lut_load(path)
    validation = fdtd_sensor_lut_validate(lut)

    assert validation["ok"] is True
    assert validation["n_wavelengths"] >= 1
    assert validation["n_summary_rows"] >= 1

    oi = _synthetic_oi(rows=12, cols=12)
    sensor = sensor_set(sensor_create("monochrome", asset_store=asset_store), "size", oi.data["photons"].shape[:2])
    sensor = sensor_set(sensor, "noise flag", 0)
    sensor = sensor_set(sensor, "integration time", 0.01)
    fdtd_sensor = sensor_attach_fdtd_lut(sensor, lut, mode="qe+field")

    base_result = sensor_compute(sensor, oi, seed=0)
    fdtd_result = sensor_compute(fdtd_sensor, oi, seed=0)

    base_volts = np.asarray(sensor_get(base_result, "volts"), dtype=float)
    fdtd_volts = np.asarray(sensor_get(fdtd_result, "volts"), dtype=float)
    assert fdtd_volts.shape == base_volts.shape
    assert np.all(np.isfinite(fdtd_volts))


def _multispectral_lut(tmp_path: Path) -> FDTDSensorLUT:
    rows = []
    for wavelength, center, edge in [(450.0, 0.2, 0.1), (550.0, 0.8, 0.2), (650.0, 0.4, 0.1)]:
        for case, field, angle, response in [
            ("center", 0.0, 0.0, center),
            ("edge", 1.0, 20.0, edge),
        ]:
            rows.append(
                {
                    "wavelength_nm": wavelength,
                    "case": case,
                    "field_x_norm": field,
                    "field_z_norm": 0.0,
                    "cra_x_deg": angle,
                    "cra_z_deg": 0.0,
                    "total_response": response,
                    "normalized_total_response_to_first": response / 0.2,
                }
            )
    return FDTDSensorLUT(
        source_path=tmp_path / "camera_lut.json",
        schema="camera_supercell_optical_lut_v2",
        mode="split-pd-1x1",
        wavelengths_nm=np.array([450.0, 550.0, 650.0]),
        summary_rows=rows,
        metadata={"resolution_px_per_um": 84},
    )


def test_center_ratios_use_the_same_wavelength_not_sweep_first(tmp_path: Path) -> None:
    lut = _multispectral_lut(tmp_path)
    for wavelength in lut.wavelengths_nm:
        assert fdtd_sensor_lut_response(
            lut, case="center", wavelength_nm=wavelength
        ) == pytest.approx(1.0)
    assert fdtd_sensor_lut_response(lut, case="edge", wavelength_nm=550) == pytest.approx(0.25)
    assert fdtd_sensor_lut_response(
        lut, case="edge", wavelength_nm=550, normalize_to_center=False
    ) == pytest.approx(0.2)
    config = fdtd_sensor_config(lut, mode="qe", case="edge")
    np.testing.assert_allclose(fdtd_sensor_qe_scale(config, lut.wavelengths_nm), [0.5, 0.25, 0.25])
    comparison = fdtd_sensor_physics_validate(lut)["checks"]["relative_illumination"][
        "comparisons"
    ][1]
    assert comparison["wavelength_nm"] == 550.0
    assert comparison["fdtd_response_norm"] == pytest.approx(0.25)


@pytest.mark.parametrize("normalize", [True, False])
def test_qe_plus_field_applies_the_optical_response_once(tmp_path: Path, normalize: bool) -> None:
    lut = _multispectral_lut(tmp_path)
    config = fdtd_sensor_config(lut, mode="qe+field", case="edge", normalize_to_center=normalize)
    qe = fdtd_sensor_qe_scale(config, [550.0])[0]
    field = fdtd_sensor_apply_optical_response(np.ones((5, 5)), config)
    assert qe == pytest.approx(1.0 if normalize else 0.8)
    assert field[2, 2] == pytest.approx(1.0)
    assert field[0, 0] == pytest.approx(0.25)
    assert qe * field[0, 0] == pytest.approx(0.25 if normalize else 0.2)
    field_only = fdtd_sensor_config(lut, mode="field", normalize_to_center=normalize)
    np.testing.assert_allclose(
        qe * field,
        fdtd_sensor_apply_optical_response(np.ones((5, 5)), field_only),
    )


def test_field_map_uses_explicit_reference_and_is_permutation_invariant(tmp_path: Path) -> None:
    lut = _multispectral_lut(tmp_path)
    mirrored = dict(
        lut.summary_rows[3],
        case="edge_negative",
        field_x_norm=-1.0,
        cra_x_deg=-20.0,
        total_response=0.6,
    )
    lut = replace(lut, summary_rows=[*lut.summary_rows, mirrored])
    reference = fdtd_sensor_field_response_map(lut, (5, 5))
    assert reference[2, 2] == pytest.approx(1.0)
    assert reference[0, 0] == pytest.approx(0.5)  # Equal-weight radial directional average.
    for permutation in [
        list(reversed(lut.summary_rows)),
        lut.summary_rows[2:] + lut.summary_rows[:2],
    ]:
        np.testing.assert_array_equal(
            reference,
            fdtd_sensor_field_response_map(replace(lut, summary_rows=permutation), (5, 5)),
        )
    red = fdtd_sensor_field_response_map(lut, (5, 5), wavelength_nm=650)
    assert red[0, 0] == pytest.approx(0.25)
    config = fdtd_sensor_config(lut, mode="field", wavelength_nm=650)
    np.testing.assert_array_equal(red, fdtd_sensor_apply_optical_response(np.ones((5, 5)), config))
    assert fdtd_sensor_field_response_map(lut, (1, 1))[0, 0] == pytest.approx(1.0)


def test_field_map_rejects_distinct_shift_branches_at_same_angle(tmp_path: Path) -> None:
    lut = _multispectral_lut(tmp_path)
    variant = dict(lut.summary_rows[3], case="edge_ocl", lens_shift_x_um=-0.18)
    lut = replace(lut, summary_rows=[*lut.summary_rows, variant])
    with pytest.raises(ValueError, match="distinct optical shift variants"):
        fdtd_sensor_field_response_map(lut, (5, 5))


def test_long_rows_keep_wavelength_normalization_and_optical_branches(tmp_path: Path) -> None:
    lut = _multispectral_lut(tmp_path)
    long_rows = []
    for row in lut.summary_rows:
        for region in ["pd_left", "pd_right"]:
            item = dict(
                row, region_id=region, region_kind="subpd", response=row["total_response"] / 2.0
            )
            item.pop("total_response")
            long_rows.append(item)
    lut = replace(lut, summary_rows=[], long_rows=long_rows)
    assert fdtd_sensor_lut_response(lut, case="center", wavelength_nm=550) == pytest.approx(1.0)
    assert fdtd_sensor_lut_response(lut, case="edge", wavelength_nm=550) == pytest.approx(0.25)
    original = fdtd_sensor_field_response_map(lut, (5, 5))
    np.testing.assert_array_equal(
        original,
        fdtd_sensor_field_response_map(replace(lut, long_rows=list(reversed(long_rows))), (5, 5)),
    )
    variant = dict(long_rows[6], case="edge_ocl", lens_shift_x_um=-0.18)
    with pytest.raises(ValueError, match="distinct optical shift variants"):
        fdtd_sensor_field_response_map(replace(lut, long_rows=[*long_rows, variant]), (5, 5))


@pytest.mark.parametrize("center_response", [None, 0.0, float("nan")])
def test_center_normalization_requires_valid_same_wavelength_center(
    tmp_path: Path, center_response: float | None
) -> None:
    lut = _multispectral_lut(tmp_path)
    rows = [dict(row) for row in lut.summary_rows]
    if center_response is None:
        rows.pop(2)
    else:
        rows[2]["total_response"] = center_response
    lut = replace(lut, summary_rows=rows)
    with pytest.raises(ValueError, match="center|finite"):
        fdtd_sensor_lut_response(lut, wavelength_nm=550, case="edge")
    with pytest.raises(ValueError, match="center|finite"):
        fdtd_sensor_field_response_map(lut, (5, 5))


def test_resolution_without_convergence_report_cannot_pass(tmp_path: Path) -> None:
    lut = _multispectral_lut(tmp_path)
    report = tmp_path / "convergence_report.json"
    report.write_text(
        json.dumps({"passed": True, "full_numerical_convergence_pass": False}), encoding="utf-8"
    )
    assert fdtd_sensor_physics_validate(lut)["checks"]["convergence"]["status"] == "warn"
    report.unlink()
    missing = fdtd_sensor_physics_validate(lut)["checks"]["convergence"]
    assert missing["status"] == "warn"
    assert missing["convergence_report"] is None
    assert missing["warnings"]
    report.write_text(
        json.dumps({"passed": True, "full_numerical_convergence_pass": True}), encoding="utf-8"
    )
    assert fdtd_sensor_physics_validate(lut)["checks"]["convergence"]["status"] == "pass"


def test_packaged_default_lut_obeys_center_normalization_contract() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "camerae2e_db/fdtd_tcad/runs/convergence_cra3_rgb_r84_gridsnap_quant/camera_lut.json"
    )
    if not path.exists():
        pytest.skip("packaged FDTD LUT is unavailable")
    lut = fdtd_sensor_lut_load(path)
    for wavelength in lut.wavelengths_nm:
        assert fdtd_sensor_lut_response(
            lut, case="center", wavelength_nm=wavelength
        ) == pytest.approx(1.0)
    np.testing.assert_array_equal(
        fdtd_sensor_qe_scale(fdtd_sensor_config(lut), lut.wavelengths_nm), np.ones(3)
    )
    np.testing.assert_array_equal(
        fdtd_sensor_field_response_map(lut, (5, 5)),
        fdtd_sensor_field_response_map(
            replace(lut, summary_rows=list(reversed(lut.summary_rows))), (5, 5)
        ),
    )


@pytest.mark.parametrize("coordinate", ["field_x_norm", "cra_x_deg", "lens_shift_x_um"])
def test_nonfinite_optical_coordinates_are_invalid_and_cannot_poison_output(tmp_path, coordinate):
    lut = _multispectral_lut(tmp_path)
    rows = [dict(row) for row in lut.summary_rows]
    rows[-1][coordinate] = float("nan")
    invalid = replace(lut, summary_rows=rows)
    assert not fdtd_sensor_lut_validate(invalid)["ok"]
    with pytest.raises(ValueError, match="coordinates must be finite"):
        fdtd_sensor_apply_optical_response(np.ones((3, 3)), fdtd_sensor_config(invalid))
