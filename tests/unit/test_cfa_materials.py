from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from pyisetcam.cfa_materials import (
    absorption_coefficient_per_um,
    absorption_only_transmission,
    cfa_material_stack_config,
    complex_refractive_index,
    image_sensor_cfa_materials,
    interpolate_nk,
    planar_cfa_reference,
    planar_stack_response,
)


@pytest.fixture(scope="module")
def bundles():
    return {
        code: image_sensor_cfa_materials(code)
        for code in ("DEF-1807-801", "DEF-2011-804", "DEF-2206-805")
    }


@pytest.mark.parametrize(
    "code, thickness, limits, passivation, height",
    [
        ("DEF-1807-801", 0.65, [0.52, 0.78], 0.934, 2.4),
        ("DEF-2011-804", 0.5, [0.45, 0.55], 1.15, 2.5),
        ("DEF-2206-805", 0.475, [0.42, 0.53], 1.241, 2.6),
    ],
)
def test_canonical_geometry_preserves_reported_optical_height(
    bundles, code, thickness, limits, passivation, height
):
    bundle = bundles[code]
    geometry = bundle["geometry_um"]
    assert geometry["cfa_thickness"] == thickness
    assert geometry["passivation_thickness"] == passivation
    assert geometry["lens_height"] + thickness + passivation == pytest.approx(height)
    assert geometry["si_thickness"] == bundle["stack_config"]["geometry_um"]["si_thickness"]
    assert bundle["pixel_pitch_um"] == 2.9
    for material in bundle["channels"].values():
        np.testing.assert_allclose(material["thickness_range_um"], limits)
        assert material["thickness_um"] == thickness
        assert material["product_measured"] is False
    provenance = bundle["provenance"]
    assert provenance["optical_stack_height_um"] == height
    assert provenance["passivation_thickness_source"] == "inferred_lumped_remainder"
    assert provenance["legacy_fdtd_cfa_csv_used"] is False
    assert provenance["measured_accuracy_verified"] is False
    json.dumps(bundle, allow_nan=False)


def test_per_report_cfa_nk_and_source_hash_reproduce_native_absorption(bundles):
    for bundle in bundles.values():
        source = Path(bundle["provenance"]["optical_model_path"])
        optical = json.loads(source.read_text())
        for channel, material in bundle["channels"].items():
            assert material["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
            rows = optical["cfa_proxy_nk"]["channels"][channel]["data"]
            np.testing.assert_array_equal(material["n"], [row["n"] for row in rows])
            np.testing.assert_array_equal(material["k"], [row["k"] for row in rows])
            np.testing.assert_allclose(
                absorption_only_transmission(
                    material, material["wavelength_nm"], material["thickness_um"]
                ),
                [row["transmission_absorption_only"] for row in rows],
                atol=5.01e-7,
                rtol=0,
            )
        # The legacy FDTD CSV used n=1.6, k=.008 for green at 550 nm.
        assert interpolate_nk(bundle["channels"]["green"], 550) == (1.7, 0.01094)


def test_shared_material_hash_and_distinct_geometry_hashes(bundles):
    assert len({b["material_bundle_sha256"] for b in bundles.values()}) == 1
    assert len({b["geometry_sha256"] for b in bundles.values()}) == 3
    bundle = bundles["DEF-1807-801"]
    assert bundle["provenance"]["original_generated_cfa_thickness_um"] == 1.008
    assert bundle["provenance"]["original_generated_passivation_thickness_um"] == 0.576
    assert interpolate_nk(bundle["materials"]["silicon"], 550) == (4.077, 0.027968)
    assert interpolate_nk(bundle["materials"]["lens"], 550) == (1.61, 0.0)
    assert interpolate_nk(bundle["materials"]["passivation"], 550) == (1.46, 0.0)


def test_export_uses_canonical_scalar_materials_without_mutation(bundles):
    bundle = bundles["DEF-1807-801"]
    before = copy.deepcopy(bundle)
    stack = cfa_material_stack_config(bundle, 550)
    assert stack["geometry_um"] == bundle["geometry_um"]
    for role in ("cfa_red", "cfa_green", "cfa_blue", "silicon", "lens", "passivation"):
        assert "nk_table" not in stack["materials"][role]
        assert stack["materials"][role]["measured"] is False
    assert stack["materials"]["cfa_green"]["n"] == 1.7
    assert stack["materials"]["cfa_green"]["k"] == 0.01094
    assert stack["canonical_geometry_sha256"] == bundle["geometry_sha256"]
    assert bundle == before


def test_nonpositive_remaining_optical_height_fails(monkeypatch, bundles):
    from pyisetcam import sensor_estimate

    bundle = bundles["DEF-1807-801"]
    source = Path(bundle["provenance"]["optical_model_path"])
    optical = json.loads(source.read_text())
    optical["optical"]["geometry"]["optical_stack_height_um"]["value"] = 1.0
    monkeypatch.setattr(sensor_estimate, "_optical_model", lambda *_: (source, optical))
    with pytest.raises(ValueError, match="nonpositive passivation remainder"):
        image_sensor_cfa_materials("DEF-1807-801")


@pytest.fixture
def material():
    return {"wavelength_nm": [400, 600], "n": [1.5, 1.7], "k": [0.01, 0.03]}


def test_strict_linear_nk_alpha_and_beer_lambert(material):
    assert interpolate_nk(material, 500) == pytest.approx((1.6, 0.02))
    assert complex_refractive_index(material, 500) == pytest.approx(1.6 + 0.02j)
    alpha = 4 * np.pi * 0.02 / 0.5
    assert absorption_coefficient_per_um(material, 500) == pytest.approx(alpha)
    assert absorption_only_transmission(material, 500, 0.7) == pytest.approx(np.exp(-alpha * 0.7))
    np.testing.assert_array_equal(absorption_only_transmission(material, [400, 600], 0), [1, 1])


@pytest.mark.parametrize("wave", [399, 601, [400, 601], np.nan, np.inf, []])
def test_material_interpolation_rejects_extrapolation_or_nonfinite(material, wave):
    with pytest.raises(ValueError, match="material range"):
        interpolate_nk(material, wave)


@pytest.mark.parametrize(
    "key, value",
    [
        ("wavelength_nm", [400, 400]),
        ("wavelength_nm", [600, 400]),
        ("wavelength_nm", [0, 600]),
        ("n", [1.5]),
        ("n", [-1.5, 1.6]),
        ("k", [0.01, -0.03]),
        ("k", [np.nan, 0.03]),
    ],
)
def test_invalid_material_tables_fail(material, key, value):
    material[key] = value
    with pytest.raises(ValueError, match="Material table"):
        interpolate_nk(material, 500)


@pytest.mark.parametrize("polarization", ["s", "p"])
def test_fresnel_interface_and_quarter_wave_antireflection(polarization):
    interface = planar_stack_response(550, [], exit_n=1.5, polarization=polarization)
    assert interface["R"] == pytest.approx(0.04)
    assert interface["T"] == pytest.approx(0.96)
    assert interface["A"] == 0
    index = np.sqrt(1.5)
    coating = planar_stack_response(
        550,
        [{"n": index, "thickness_um": 0.55 / (4 * index)}],
        exit_n=1.5,
        polarization=polarization,
    )
    assert coating["R"] < 1e-28
    assert coating["T"] == pytest.approx(1, abs=1e-14)
    assert coating["A"] == pytest.approx(0, abs=1e-14)


def test_brewster_angle_and_total_internal_reflection():
    brewster = np.degrees(np.arctan(1.5))
    result = planar_stack_response(550, [], exit_n=1.5, angle_deg=brewster, polarization="p")
    assert result["R"] < 1e-28
    assert result["T"] == pytest.approx(1)
    for polarization in ("s", "p"):
        result = planar_stack_response(
            550, [], incident_n=1.5, angle_deg=60, polarization=polarization
        )
        assert result["R"] == pytest.approx(1)
        assert result["T"] == pytest.approx(0, abs=1e-14)


def test_absorbing_film_agrees_with_closed_form_amplitudes():
    index, thickness, wavelength = 1.5 + 0.1j, 0.7, 0.55
    r01 = (1 - index) / (1 + index)
    r12 = -r01
    phase = np.exp(2j * np.pi * index * thickness / wavelength)
    denominator = 1 + r01 * r12 * phase**2
    amplitude_r = (r01 + r12 * phase**2) / denominator
    amplitude_t = (2 / (1 + index)) * (2 * index / (index + 1)) * phase / denominator
    for polarization in ("s", "p"):
        result = planar_stack_response(
            wavelength * 1000,
            [{"n": index, "thickness_um": thickness}],
            polarization=polarization,
        )
        assert result["R"] == pytest.approx(abs(amplitude_r) ** 2, abs=1e-14)
        assert result["T"] == pytest.approx(abs(amplitude_t) ** 2, abs=1e-14)
        assert result["A"] == pytest.approx(1 - abs(amplitude_r) ** 2 - abs(amplitude_t) ** 2)


@pytest.mark.parametrize("polarization", ["s", "p"])
def test_optically_opaque_film_is_stable(polarization):
    index = 2 + 0.5j
    result = planar_stack_response(
        550, [{"n": index, "thickness_um": 10000}], polarization=polarization
    )
    assert result["R"] == pytest.approx(abs((1 - index) / (1 + index)) ** 2)
    assert result["T"] == 0
    assert result["A"] == pytest.approx(1 - result["R"])


@pytest.mark.parametrize("polarization", ["s", "p"])
def test_splitting_same_material_preserves_response_and_layer_absorption(polarization):
    def solve(thicknesses):
        return planar_stack_response(
            650,
            [{"n": 2 + 0.1j, "thickness_um": value} for value in thicknesses],
            angle_deg=40,
            exit_n=1.4,
            polarization=polarization,
        )

    full, split = solve([1.2]), solve([0.3, 0.9])
    for quantity in ("R", "T", "A"):
        assert full[quantity] == pytest.approx(split[quantity], abs=1e-14)
    assert split["layer_boundary_flux"][0]["back"] == pytest.approx(
        split["layer_boundary_flux"][1]["front"], abs=1e-14
    )
    assert sum(split["layer_absorption"]) == pytest.approx(full["layer_absorption"][0])


def test_random_passive_multilayers_conserve_normal_power():
    rng = np.random.default_rng(321)
    for _ in range(40):
        layers = [
            {"n": rng.uniform(1, 4) + 1j * rng.uniform(0, 0.7), "thickness_um": rng.uniform(0, 8)}
            for _ in range(5)
        ]
        for polarization in ("s", "p"):
            result = planar_stack_response(
                rng.uniform(400, 700),
                layers,
                angle_deg=rng.uniform(0, 80),
                exit_n=1.3,
                polarization=polarization,
            )
            powers = [result["R"], result["T"], *result["layer_absorption"]]
            assert np.all(np.isfinite(powers))
            assert min(powers) >= -1e-14
            assert sum(powers) == pytest.approx(1, abs=1e-13)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"incident_n": 1 + 0.1j},
        {"exit_n": 1 + 0.1j},
        {"angle_deg": 90},
        {"angle_deg": -1},
        {"angle_deg": np.nan},
        {"polarization": "unpolarized"},
        {"layers": [{"n": 1.5 - 0.1j, "thickness_um": 1}]},
        {"layers": [{"n": 1.5, "thickness_um": -1}]},
        {"layers": [{"n": np.nan, "thickness_um": 1}]},
    ],
)
def test_planar_invalid_inputs_fail(kwargs):
    parameters = {"wavelength_nm": 550, "layers": []}
    parameters.update(kwargs)
    with pytest.raises(ValueError):
        planar_stack_response(**parameters)


def test_canonical_planar_pilot_has_explicit_layers_and_unmeasured_boundary(bundles):
    bundle = bundles["DEF-2011-804"]
    result = planar_cfa_reference(bundle, 550)
    assert result["layer_names"] == ["cfa", "passivation", "silicon"]
    assert result["layers"][0]["n"] == 1.7
    assert result["layers"][0]["thickness_um"] == 0.5
    assert result["layers"][2]["thickness_um"] == 6.2
    assert result["layer_absorption"][1] == pytest.approx(0, abs=1e-14)
    assert result["R"] + result["T"] + sum(result["layer_absorption"]) == pytest.approx(1)
    assert result["geometry_sha256"] == bundle["geometry_sha256"]
    assert result["curved_microlens_included"] is False
    assert result["measured_validation"] is False
    json.dumps(result, allow_nan=False)
