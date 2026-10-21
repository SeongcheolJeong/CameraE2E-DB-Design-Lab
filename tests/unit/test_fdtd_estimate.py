"""Synthetic numerical contracts; these fixtures are not measured FDTD validation."""

import copy
import json

import numpy as np
import pytest

from pyisetcam.fdtd_estimate import (
    _illumination_config,
    _incident_directions,
    apply_fdtd_estimate,
    fdtd_estimate_current_density,
    fdtd_response,
    image_sensor_fdtd_model,
)
from pyisetcam.ip import ip_compute, ip_create
from pyisetcam.sensor import sensor_compute, sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor
from pyisetcam.types import OpticalImage


@pytest.fixture
def response_file(tmp_path, monkeypatch):
    import pyisetcam.cfa_materials as materials
    import pyisetcam.image_sensor_db as database

    bundle = {
        "sensor_id": "synthetic",
        "report_code": "SYN-001",
        "pixel_pitch_um": 2.0,
        "material_bundle_sha256": "a" * 64,
        "geometry_sha256": "b" * 64,
    }
    monkeypatch.setattr(materials, "image_sensor_cfa_materials", lambda *a, **kw: bundle)
    monkeypatch.setattr(
        database,
        "image_sensor_db_get",
        lambda *a, **kw: {
            "sensor_id": "synthetic",
            "code": "SYN-001",
        },
    )
    samples = []
    for wave in (400, 500, 600, 700):
        for theta in (0, 20):
            for phi in (0,) if theta == 0 else (0, 90, 180, 270):
                for pol, pol_factor in (("s", 0.9), ("p", 1.1)):
                    base = np.array(
                        [0.08 + (wave - 400) / 10000, 0.12, 0.10, 0.16 - (wave - 400) / 10000]
                    )
                    angular = 1 + theta / 100 * np.array([1, 0.5, -0.5, -1])
                    angular += theta / 100 * np.cos(np.deg2rad(phi)) * 0.1
                    absorption = base * angular * pol_factor
                    samples.append(
                        {
                            "wavelength_nm": wave,
                            "theta_deg": theta,
                            "phi_deg": phi,
                            "polarization": pol,
                            "absorption_cell_fraction": absorption.tolist(),
                            "reflection": 0.1,
                            "transmission": 0.77 - float(np.sum(absorption)),
                            "layer_absorption": {
                                "cfa": 0.1,
                                "lens": 0.01,
                                "passivation": 0.02,
                                "silicon": float(np.sum(absorption)),
                            },
                        }
                    )
    validation = {
        "status": "passed",
        "gates": dict.fromkeys(("energy_balance", "spatial", "time", "pml", "interpolation"), True),
        "evidence": {
            "energy_balance": {"max_abs_error": 1e-12, "sample_count": len(samples)},
            **{
                key: {
                    "max_relative_error": 0.01,
                    "max_angular_ratio_relative_error": 0.01,
                    "sample_count": 3,
                    "comparisons": [
                        {"wavelength_nm": 400, "theta_deg": 0},
                        {"wavelength_nm": 550, "theta_deg": 20},
                        {"wavelength_nm": 700, "theta_deg": 20},
                    ],
                }
                for key in ("spatial", "time", "pml", "interpolation")
            },
        },
    }
    payload = {
        "schema": "camerae2e_fdtd_response_v1",
        **bundle,
        "cell_pixels": [2, 2],
        "site_names": ["R", "Gr", "Gb", "B"],
        "estimate_only": True,
        "solver_code_sha256": "c" * 64,
        "provenance": {"synthetic_unit_fixture": True, "measured_accuracy_verified": False},
        "validation": validation,
        "samples": samples,
    }
    path = tmp_path / "synthetic_response.json"
    path.write_text(json.dumps(payload))
    return path, payload, bundle


def _rewrite(response_file, mutate):
    path, payload, _ = response_file
    changed = copy.deepcopy(payload)
    mutate(changed)
    path.write_text(json.dumps(changed))
    return path


def _sensor_oi(model, illumination, asset_store):
    sensor = sensor_create("default", {"size": (8, 8)}, asset_store=asset_store)
    sensor = sensor_set(sensor, "integration time", 0.01)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", 0)
    sensor = apply_fdtd_estimate(sensor, model, illumination)
    sensor = configure_e2e_sensor(sensor, readout={"dark_current_e_per_s": 0, "bit_depth": 24})
    wave = np.arange(400.0, 701.0, 10.0)
    oi = OpticalImage(
        fields={"wave": wave, "width_m": 16e-6}, data={"photons": np.full((8, 8, wave.size), 1e14)}
    )
    return sensor, oi


def test_validated_model_has_fixed_native_reference_and_provenance(response_file):
    model = image_sensor_fdtd_model("synthetic", response_file[0])
    expected = np.array([0.08, 0.12, 0.10, 0.16]) * 4 * 0.75
    np.testing.assert_allclose(model["eqe"][0], expected)
    assert model["filter_names"] == ["R", "Gr", "Gb", "B"]
    assert model["readout"]["black_level_dn"] == 64
    assert model["provenance"]["measured_accuracy_verified"] is False
    json.dumps(model, allow_nan=False)


def test_only_machine_roundoff_negative_loss_is_accepted(response_file):
    def roundoff(payload):
        sample = payload["samples"][0]
        sample["layer_absorption"]["lens"] = -2e-16
        sample["transmission"] += 0.01

    path = _rewrite(response_file, roundoff)
    assert (
        image_sensor_fdtd_model("synthetic", path)["provenance"]["passivity_roundoff_epsilon"]
        == 1e-10
    )
    changed = json.loads(path.read_text())
    changed["samples"][0]["layer_absorption"]["lens"] = -1e-6
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="fractions"):
        image_sensor_fdtd_model("synthetic", path)


def test_mixed_native_error_policy_requires_weak_absolute_evidence(response_file):
    path = _rewrite(
        response_file, lambda p: p["validation"].update(policy="native_response_mixed_v1")
    )
    with pytest.raises(ValueError, match="max_weak_response_abs_error"):
        image_sensor_fdtd_model("synthetic", path)
    payload = json.loads(path.read_text())
    for name in ("spatial", "time", "pml", "interpolation"):
        payload["validation"]["evidence"][name].update(
            max_weak_response_abs_error=0.0005, max_angular_ratio_weak_response_abs_error=0.0005
        )
    path.write_text(json.dumps(payload))
    image_sensor_fdtd_model("synthetic", path)
    payload["validation"]["evidence"]["time"]["max_weak_response_abs_error"] = 0.002
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="weak-response"):
        image_sensor_fdtd_model("synthetic", path)


@pytest.mark.parametrize("gate", ["energy_balance", "spatial", "time", "pml", "interpolation"])
def test_gate_labels_without_passing_numerical_evidence_are_rejected(response_file, gate):
    path = _rewrite(response_file, lambda p: p["validation"]["evidence"][gate].clear())
    with pytest.raises(ValueError, match="finite"):
        image_sensor_fdtd_model("synthetic", path)


@pytest.mark.parametrize("gate", ["energy_balance", "spatial", "time", "pml", "interpolation"])
def test_failed_or_unverified_gate_is_rejected(response_file, gate):
    path = _rewrite(response_file, lambda p: p["validation"]["gates"].update({gate: False}))
    with pytest.raises(ValueError, match="gate"):
        image_sensor_fdtd_model("synthetic", path)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda p: p.update(sensor_id="wrong"), "identity"),
        (lambda p: p.update(report_code="wrong"), "identity"),
        (lambda p: p.update(material_bundle_sha256="d" * 64), "canonical"),
        (lambda p: p.update(geometry_sha256="d" * 64), "canonical"),
        (lambda p: p.update(solver_code_sha256="unrecorded"), "SHA-256"),
        (lambda p: p.update(pixel_pitch_um=3), "pitch"),
        (lambda p: p["samples"][0].update(reflection=0.9), "energy conservation"),
        (lambda p: p["samples"].pop(), "complete"),
        (lambda p: p["samples"].append(p["samples"][0]), "duplicate"),
        (lambda p: p["validation"].update(status="unverified"), "unverified"),
        (
            lambda p: p["validation"]["evidence"]["spatial"].update(max_relative_error=0.06),
            "tolerance",
        ),
    ],
)
def test_identity_hash_energy_and_grid_rejections(response_file, mutate, match):
    path = _rewrite(response_file, mutate)
    with pytest.raises(ValueError, match=match):
        image_sensor_fdtd_model("synthetic", path)


def test_spectral_angular_response_precedes_integration_and_retains_native_sites(
    response_file, asset_store
):
    model = image_sensor_fdtd_model("synthetic", response_file[0])
    illumination = {"directions": [{"theta_deg": 20, "phi_deg": 0, "weight": 1}]}
    sensor, oi = _sensor_oi(model, illumination, asset_store)
    actual = sensor_compute(sensor, oi)
    angular = 1 + 0.2 * np.array([1, 0.5, -0.5, -1]) + 0.02
    expected_by_site = (
        np.sum(np.asarray(model["eqe"]), axis=0) * angular * 10 * 1e14 * 0.01 * (2e-6) ** 2
    )
    for row, col, site in ((0, 0, 0), (0, 1, 1), (1, 0, 2), (1, 1, 3)):
        np.testing.assert_allclose(
            actual.data["mean_electrons"][row::2, col::2], expected_by_site[site], rtol=1e-12
        )
    assert not np.isclose(expected_by_site[1], expected_by_site[2])
    assert "response_grid" not in actual.metadata["sensor_estimate"]
    assert actual.metadata["sensor_estimate"]["illumination"]["mode"] == "weighted_directions"
    # A scene FOV is not a chief-ray direction.
    oi.fields["fov_deg"] = 120
    np.testing.assert_array_equal(
        sensor_compute(sensor, oi).data["mean_electrons"], actual.data["mean_electrons"]
    )


def test_weight_normalization_and_collection_efficiency_each_apply_once(response_file, asset_store):
    model = image_sensor_fdtd_model("synthetic", response_file[0])
    rays = [
        {"theta_deg": 0, "phi_deg": 0, "weight": 2},
        {"theta_deg": 20, "phi_deg": 0, "weight": 6},
    ]
    sensor, oi = _sensor_oi(model, {"directions": rays}, asset_store)
    a = sensor_compute(sensor, oi)
    half = image_sensor_fdtd_model("synthetic", response_file[0], collection_efficiency=0.375)
    second, _ = _sensor_oi(half, {"directions": rays}, asset_store)
    b = sensor_compute(second, oi)
    np.testing.assert_allclose(b.data["mean_electrons"], a.data["mean_electrons"] / 2)
    multiplied = [{**ray, "weight": 10 * ray["weight"]} for ray in rays]
    second, _ = _sensor_oi(model, {"directions": multiplied}, asset_store)
    np.testing.assert_allclose(
        sensor_compute(second, oi).data["mean_electrons"], a.data["mean_electrons"]
    )


def test_response_helper_matches_spectral_capture_before_spatial_integration(
    response_file, asset_store
):
    model = image_sensor_fdtd_model("synthetic", response_file[0])
    illumination = {"directions": [{"theta_deg": 10, "phi_deg": 45, "weight": 1}]}
    sensor, oi = _sensor_oi(model, illumination, asset_store)
    from pyisetcam.sensor import _ELEMENTARY_CHARGE_C

    response = fdtd_response(model, oi.fields["wave"], illumination)
    expected = np.sum(response, axis=0) * 10 * 1e14 * _ELEMENTARY_CHARGE_C
    np.testing.assert_allclose(
        fdtd_estimate_current_density(oi, sensor), np.broadcast_to(expected, (8, 8, 4)), rtol=1e-14
    )


def test_pupil_weights_are_projected_solid_angle_and_not_throughput():
    configuration = _illumination_config(
        {
            "exit_pupil_distance_m": 0.01,
            "image_side_f_number": 2,
            "radial_samples": 2,
            "azimuth_samples": 4,
        }
    )
    x, y = np.meshgrid(np.array([-0.001, 0.0, 0.001]), np.array([0.0]))
    rays = _incident_directions(configuration, x, y)
    np.testing.assert_allclose(sum(row[2] for row in rays), 1)
    assert np.ptp([row[2][0, 0] for row in rays]) > 0
    assert np.mean([row[0][0, 1] for row in rays]) > 0
    assert configuration["f_number_definition"] == "exit-pupil distance / exit-pupil diameter"


def test_constant_angular_response_is_invariant_to_f_number_and_quadrature(response_file):
    path, payload, _ = response_file
    for sample in payload["samples"]:
        sample["absorption_cell_fraction"] = [0.1] * 4
        sample["layer_absorption"]["silicon"] = 0.4
        sample["transmission"] = 0.37
    path.write_text(json.dumps(payload))
    model = image_sensor_fdtd_model("synthetic", path)
    reference = fdtd_response(
        model,
        [400, 550, 700],
        {
            "directions": [{"theta_deg": 0, "phi_deg": 0, "weight": 1}],
        },
    )
    for f_number, radial_samples in ((2, 3), (4, 6), (8, 12)):
        response = fdtd_response(
            model,
            [400, 550, 700],
            {
                "exit_pupil_distance_m": 0.01,
                "image_side_f_number": f_number,
                "radial_samples": radial_samples,
                "azimuth_samples": 16,
            },
        )
        np.testing.assert_allclose(response, reference, atol=1e-14)


def test_periodic_azimuth_closes_without_extrapolating(response_file):
    model = image_sensor_fdtd_model("synthetic", response_file[0])
    values = [
        fdtd_response(
            model,
            [450, 650],
            {
                "directions": [
                    {"theta_deg": 10, "phi_deg": phi, "weight": 1},
                ]
            },
        )
        for phi in (1, 359)
    ]
    np.testing.assert_allclose(values[0], values[1], atol=1e-14)


def test_normal_only_lut_allows_normal_rays_but_rejects_finite_pupil(response_file):
    path, payload, _ = response_file
    payload["samples"] = [row for row in payload["samples"] if row["theta_deg"] == 0]
    path.write_text(json.dumps(payload))
    model = image_sensor_fdtd_model("synthetic", path)
    response = fdtd_response(
        model,
        [550],
        {
            "directions": [
                {"theta_deg": 0, "phi_deg": 270, "weight": 1},
            ]
        },
    )
    assert response.shape == (1, 4)
    assert np.all(np.isfinite(response))
    with pytest.raises(ValueError, match="no extrapolation"):
        fdtd_response(model, [550], {"exit_pupil_distance_m": 0.01, "image_side_f_number": 8})


def test_replacing_fdtd_with_db_estimate_clears_spectral_hook(response_file, asset_store):
    from pyisetcam.sensor_estimate import apply_sensor_estimate

    model = image_sensor_fdtd_model("synthetic", response_file[0])
    sensor, _ = _sensor_oi(
        model,
        {
            "directions": [
                {"theta_deg": 0, "phi_deg": 0, "weight": 1},
            ]
        },
        asset_store,
    )
    estimate = {
        "kind": "db_estimate_v1",
        "wavelength_nm": np.arange(400, 701, 10).tolist(),
        "eqe": np.full((31, 3), 0.3).tolist(),
        "filter_names": ["red", "green", "blue"],
        "cfa_pattern": "bayer",
        "pixel_pitch_um": 2.0,
        "readout": model["readout"],
    }
    replaced = apply_sensor_estimate(sensor, estimate)
    assert "fdtd_estimate" not in replaced.fields
    assert "fdtd_illumination" not in replaced.fields
    assert replaced.fields["filter_spectra"].shape == (31, 3)


@pytest.mark.parametrize(
    "illumination",
    [
        None,
        {"fov_deg": 30},
        {"directions": [{"theta_deg": 0, "phi_deg": 0, "weight": 0}]},
        {"directions": [{"theta_deg": 0, "phi_deg": 0, "weight": 1, "polarization": "s"}]},
    ],
)
def test_missing_or_unsupported_illumination_is_explicit_error(
    response_file, asset_store, illumination
):
    model = image_sensor_fdtd_model("synthetic", response_file[0])
    with pytest.raises(ValueError):
        _sensor_oi(model, illumination, asset_store)


@pytest.mark.parametrize("theta", [20.01, 40])
def test_angular_extrapolation_is_rejected(response_file, asset_store, theta):
    model = image_sensor_fdtd_model("synthetic", response_file[0])
    sensor, oi = _sensor_oi(
        model, {"directions": [{"theta_deg": theta, "phi_deg": 0, "weight": 1}]}, asset_store
    )
    with pytest.raises(ValueError, match="no extrapolation"):
        sensor_compute(sensor, oi)


def test_spectral_extrapolation_is_rejected(response_file, asset_store):
    model = image_sensor_fdtd_model("synthetic", response_file[0])
    sensor, oi = _sensor_oi(
        model, {"directions": [{"theta_deg": 0, "phi_deg": 0, "weight": 1}]}, asset_store
    )
    oi.fields["wave"][0] = 390
    with pytest.raises(ValueError, match="no extrapolation"):
        fdtd_estimate_current_density(oi, sensor)


def test_fixed_central_color_matrix_does_not_cancel_oblique_color_change(
    response_file, asset_store
):
    model = image_sensor_fdtd_model("synthetic", response_file[0])
    results = []
    for theta in (0, 20):
        sensor, oi = _sensor_oi(
            model, {"directions": [{"theta_deg": theta, "phi_deg": 0, "weight": 1}]}, asset_store
        )
        captured = sensor_compute(sensor, oi)
        results.append(
            ip_compute(ip_create(asset_store=asset_store), captured, asset_store=asset_store)
        )
    np.testing.assert_array_equal(
        results[0].data["transforms"][0], results[1].data["transforms"][0]
    )
    np.testing.assert_array_equal(
        results[0].data["transforms"][2], results[1].data["transforms"][2]
    )
    assert results[0].data["sensorspace"].shape == (8, 8, 4)
    assert results[0].data["result"].shape == (8, 8, 3)
    assert not np.allclose(results[0].data["result"], results[1].data["result"], rtol=0.01)


def test_optical_application_invalidates_cache_and_rejects_active_legacy_layers(
    response_file, asset_store
):
    model = image_sensor_fdtd_model("synthetic", response_file[0])
    illumination = {"directions": [{"theta_deg": 0, "phi_deg": 0, "weight": 1}]}
    sensor, oi = _sensor_oi(model, illumination, asset_store)
    captured = sensor_compute(sensor, oi)
    updated = apply_fdtd_estimate(captured, model, illumination)
    assert not updated.data
    assert "sensor_capture" not in updated.metadata
    sensor.fields["tcad_sensor"] = {"enabled": True}
    with pytest.raises(ValueError, match="overlap"):
        apply_fdtd_estimate(sensor, model, illumination)


@pytest.mark.parametrize(
    "override",
    [
        lambda s: s.fields["pixel"].update(size_m=np.full(2, 3e-6)),
        lambda s: s.fields["pixel"].update(fill_factor=0.5),
        lambda s: s.fields.update(pattern=np.array([[1, 2], [2, 3]])),
        lambda s: s.fields.update(pixel_qe=np.full(31, 0.5)),
        lambda s: s.fields.update(ir_filter=np.full(31, 0.5)),
        lambda s: s.fields.update(filter_spectra=np.full((31, 4), 0.1)),
        lambda s: s.fields.update(tcad_sensor={"enabled": True}),
        lambda s: s.fields.update(fdtd_sensor={"enabled": True}),
        lambda s: s.fields.update(ocl_group_proxy={"enabled": True}),
        lambda s: s.fields.update(vignetting=1),
    ],
)
def test_post_configuration_optical_changes_cannot_silently_invalidate_lut(
    response_file, asset_store, override
):
    model = image_sensor_fdtd_model("synthetic", response_file[0])
    sensor, oi = _sensor_oi(
        model,
        {
            "directions": [
                {"theta_deg": 0, "phi_deg": 0, "weight": 1},
            ]
        },
        asset_store,
    )
    override(sensor)
    with pytest.raises(ValueError, match="FDTD"):
        sensor_compute(sensor, oi)
