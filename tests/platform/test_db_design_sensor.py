"""Frozen sensor DB reuse, unit adaptation and honest evidence classification."""

import copy
from pathlib import Path

import numpy as np
import pytest

from camera_platform import db_design_sensor as sensor
from camera_platform.design_workshop_sensor import build_response
from camera_platform.electrical import capture_electrons

WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cells():
    if not (
        WORKSPACE / "camerae2e_db/image_sensor/simulations/sensor_simulations.sqlite"
    ).is_file():
        pytest.skip("Local pinned sensor DB required")
    return {key: sensor.build_cell(WORKSPACE, key) for key in sensor._PINS}


def test_reference_numeric_contract_unchanged(cells):
    from camera_platform.reference_workshop_sensor import build_cell

    original = build_cell(WORKSPACE)
    actual = cells["reference_imx462"]
    for key in ("physical_model", "geometry_um", "readout", "electrical_model"):
        assert actual[key] == original[key]
    assert actual["provenance"]["db_selection"]["saved_run_id"] is None


@pytest.mark.parametrize("key", ["imx662", "imx307"])
def test_frozen_snapshot_and_readout_preserved(cells, key):
    from pyisetcam.sensor_simulation_db import sensor_simulation_db_get

    cell = cells[key]
    pin = sensor._PINS[key]
    saved = sensor_simulation_db_get(
        pin["report_code"], root=WORKSPACE / "camerae2e_db", run_id=pin["run_id"]
    )
    assert cell["physical_model"] == saved["metadata"]["model_snapshot"]
    assert cell["readout"] == saved["metadata"]["model_snapshot"]["readout"]
    assert cell["provenance"]["db_selection"]["result_sha256"] == pin["result_sha256"]
    assert cell["provenance"]["source_model_sha256"] == sensor._pixel._digest(
        cell["physical_model"]
    )
    assert not cell["product_model"]
    assert not cell["provenance"]["database_modified"]


def test_absolute_response_no_second_qe_or_routing_factor(cells):
    from pyisetcam.physical_estimate import physical_optical_response

    cell = cells["imx662"]
    wave = np.array([450.0, 550.0, 650.0])
    theta = np.array([0.0, 30.0, 60.0])
    expected = physical_optical_response(
        cell["physical_model"], wave[None, :], theta_deg=theta[:, None], phi_deg=0
    )
    actual = build_response(cell, wave, theta)
    np.testing.assert_allclose(actual["eqe"], expected["eqe"], rtol=0, atol=0)
    np.testing.assert_allclose(
        actual["eqe"], np.asarray(actual["layer_absorption"]["silicon"]) * 0.75
    )
    assert cell["physical_model"]["optical_assumptions"]["routing_mode"] == "no_routing_loss"


def test_camera_proxy_is_not_bare_sensor_measurement(cells):
    rows = {
        row["parameter"]: row
        for row in cells["imx662"]["provenance"]["db_selection"]["parameter_sources"]
    }
    assert rows["pixel_pitch_um"]["origin"] == "reported_structure"
    assert rows["pixel_pitch_um"]["source"] == "DEF-2206-805"
    assert rows["full_well_e"]["classification"] == "reported_camera_specification"
    assert rows["full_well_e"]["origin"] == "camera_proxy"
    assert rows["read_noise_e"]["classification"] == "derived_camera_proxy"
    assert rows["dark_current_e_per_s"]["origin"] == "assumption"
    assert rows["red_cfa_thickness_um"]["origin"] == "assumption"
    assert rows["green_cfa_thickness_um"]["origin"] == "reported_structure"
    assert rows["microlens_vertex_radius_um"]["application"] == "geometry_diagnostic_only"
    assert cells["imx662"]["provenance"]["db_selection"]["physical_validation"] == "not_established"


def test_fpn_bias_not_relabeled_as_dark_rate(cells):
    cell = cells["imx662"]
    assert cell["provenance"]["saved_fixed_pattern"]["dsnu_e"] == 1
    assert cell["electrical_model"]["fixed_pattern"]["dsnu_e_per_s"] == 0
    assert cell["electrical_model"]["fixed_pattern"]["prnu_sigma"] == 0
    assert cell["provenance"]["fixed_pattern_application"] == "not_applied_common_bench_policy"


def test_electron_to_integer_raw_preserves_saved_transfer(cells):
    cell = cells["imx662"]
    exposure = 0.004
    readout = cell["readout"]
    result = capture_electrons(
        np.full((8, 8), 1000.0),
        exposure,
        cell["electrical_model"],
        analog_gain=1,
        temperature_c=25,
        noise_enabled=False,
        seed=1,
        geometry={"full_sensor_shape": [8, 8], "crop_origin": [0, 0]},
    )
    scale = (2 ** readout["bit_depth"] - 1 - readout["black_level_dn"]) / readout["full_well_e"]
    expected = np.rint(readout["black_level_dn"] + (1000 + 10 * exposure) * scale)
    assert np.all(result["arrays"]["raw_dn"] == expected)


@pytest.mark.parametrize("mutation", ["pitch", "cfa", "routing", "band", "angle"])
def test_incompatible_native_snapshot_refused(cells, mutation):
    cell = cells["imx662"]
    model = copy.deepcopy(cell["physical_model"])
    metadata = {
        "native_cfa": {"pattern": [[1, 2], [3, 4]], "pattern_index_base": 1, "pixel_pitch_um": 2.9}
    }
    if mutation == "pitch":
        model["pixel_pitch_um"] = 1.4
    elif mutation == "cfa":
        metadata["native_cfa"]["pattern"] = [[1, 2], [2, 3]]
    elif mutation == "routing":
        model["optical_assumptions"]["routing_mode"] = "own_site_lower_bound"
    elif mutation == "band":
        model["wavelength_nm"] = [500, 600]
    else:
        model["angular_domain_deg"] = [0, 30]
    with pytest.raises(ValueError):
        sensor._validate_native(model, metadata)


def test_pin_hash_and_catalog_staleness_refused(cells, monkeypatch):
    pins = copy.deepcopy(sensor._PINS)
    pins["imx662"]["result_sha256"] = "0" * 64
    monkeypatch.setattr(sensor, "_PINS", pins)
    with pytest.raises(ValueError, match="result hash"):
        sensor.build_cell(WORKSPACE, "imx662")
    pins["imx662"]["content_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="catalog revision"):
        sensor.build_cell(WORKSPACE, "imx662")


def test_candidate_inventory_reports_readiness_without_product_validation(cells):
    choices = sensor.candidates(WORKSPACE)
    assert {item["key"] for item in choices} == set(cells)
    assert all(item["execution_ready"] for item in choices)
    assert all(item["physical_validation"] == "not_established" for item in choices)
    assert all(isinstance(item["parameter_sources"], list) for item in choices)
    with pytest.raises(ValueError, match="Unsupported"):
        sensor.build_cell(WORKSPACE, "unreviewed_sensor")
