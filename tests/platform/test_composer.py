"""Composer prevents plausible-looking but semantically incompatible cameras."""

from copy import deepcopy

import pytest

from camera_platform.composer import check_assembly


def assembly_fixture():
    domain = {"temperature_c": {"min": 0, "max": 60, "unit": "degC"}}
    spectrum = {"min": 400, "max": 700, "unit": "nm"}
    raw = {
        "cfa": "RGGB",
        "bit_depth": 12,
        "layout": "mosaic",
        "packing": "unpacked_uint16",
        "black_level": 64,
        "hdr_streams": ["long"],
    }
    models = [
        {
            "id": "lens:1",
            "kind": "lens",
            "included_effects": ["lens_shading", "lens_transmission"],
            "valid_domain": deepcopy(domain),
            "spectral_range": deepcopy(spectrum),
            "reference_plane": "sensor.entry",
            "angular_representation": "distribution",
            "properties": {
                "image_circle_diameter": {"value": 10, "unit": "mm"},
                "incident_angle_range": {"min": -20, "max": 20, "unit": "deg"},
            },
        },
        {
            "id": "sensor:1",
            "kind": "sensor",
            "included_effects": ["cfa", "microlens"],
            "valid_domain": deepcopy(domain),
            "spectral_range": deepcopy(spectrum),
            "reference_plane": "sensor.entry",
            "raw_format": deepcopy(raw),
            "properties": {
                "active_width": {"value": 8, "unit": "mm"},
                "active_height": {"value": 6, "unit": "mm"},
                "accepted_angle_range": {"min": -30, "max": 30, "unit": "deg"},
            },
        },
        {
            "id": "isp:1",
            "kind": "isp",
            "included_effects": ["demosaic"],
            "valid_domain": deepcopy(domain),
            "input_raw_format": deepcopy(raw),
        },
    ]
    assembly = {
        "id": "camera:1",
        "model_ids": [model["id"] for model in models],
        "conditions": {"temperature_c": {"value": 25, "unit": "degC"}},
        "spectral_range": deepcopy(spectrum),
        "raw_format": deepcopy(raw),
    }
    return assembly, models


def codes(report):
    return {issue["code"] for issue in report["issues"]}


def test_fully_declared_compatible_assembly_passes_without_accuracy_claim():
    report = check_assembly(*assembly_fixture())
    assert report["status"] == "PASS", report["issues"]
    assert report["compatible"]
    assert "independent measurement" in report["claim"]


def test_dictionary_and_list_model_inputs_are_equivalent():
    assembly, models = assembly_fixture()
    assert check_assembly(assembly, models) == check_assembly(
        assembly, {m["id"]: m for m in models}
    )


def test_unclaimed_quantitative_condition_cannot_make_assembly_compatible():
    assembly, models = assembly_fixture()
    assembly["conditions"]["f_number"] = {"value": 2.8, "unit": "1"}
    report = check_assembly(assembly, models)
    assert report["status"] == "INCONCLUSIVE"
    assert "unclaimed_condition_axis" in codes(report)
    assert not report["compatible"]

    # A model-specific axis may have one declared owner.  The Composer still
    # checks that owner's bound without claiming the other models depend on it.
    models[0]["valid_domain"]["f_number"] = {"min": 2.0, "max": 4.0, "unit": "1"}
    assert check_assembly(assembly, models)["status"] == "PASS"
    assembly["conditions"]["f_number"]["value"] = 5.6
    assert check_assembly(assembly, models)["status"] == "OUT_OF_DOMAIN"


@pytest.mark.parametrize("point", [True, float("inf"), None])
def test_invalid_unclaimed_quantitative_condition_is_not_silent(point):
    assembly, models = assembly_fixture()
    assembly["conditions"]["f_number"] = {"value": point, "unit": "1"}
    assert "unclaimed_condition_axis" in codes(check_assembly(assembly, models))


def test_fixed_lens_aperture_is_exact_condition_owner():
    assembly, models = assembly_fixture()
    stop = {"value": 8.0, "unit": "1"}
    models[0]["properties"]["f_number"] = deepcopy(stop)
    assembly["conditions"]["f_number"] = deepcopy(stop)
    assert check_assembly(assembly, models)["status"] == "PASS"
    assembly["conditions"]["f_number"]["value"] = 5.6
    report = check_assembly(assembly, models)
    assert report["status"] == "FAIL"
    assert "lens_f_number_mismatch" in codes(report)


def test_image_circle_must_cover_sensor_diagonal_not_just_width():
    assembly, models = assembly_fixture()
    models[0]["properties"]["image_circle_diameter"]["value"] = 9
    report = check_assembly(assembly, models)
    assert report["status"] == "FAIL"
    assert "image_circle_too_small" in codes(report)


def test_incompatible_geometry_units_are_not_silently_converted():
    assembly, models = assembly_fixture()
    models[1]["properties"]["active_width"]["unit"] = "um"
    assert "geometry_unit_mismatch" in codes(check_assembly(assembly, models))


def test_matching_nonphysical_units_do_not_make_geometry_valid():
    assembly, models = assembly_fixture()
    models[0]["properties"]["image_circle_diameter"]["unit"] = "banana"
    models[1]["properties"]["active_width"]["unit"] = "banana"
    models[1]["properties"]["active_height"]["unit"] = "banana"
    assert "invalid_geometry" in codes(check_assembly(assembly, models))


def test_unknown_geometry_cannot_be_compatible():
    assembly, models = assembly_fixture()
    del models[1]["properties"]["active_height"]
    assert check_assembly(assembly, models)["status"] == "INCONCLUSIVE"


def test_sensor_spectrum_must_cover_requested_light_range():
    assembly, models = assembly_fixture()
    models[1]["spectral_range"]["max"] = 650
    report = check_assembly(assembly, models)
    assert report["status"] == "OUT_OF_DOMAIN"
    assert "spectral_coverage" in codes(report)


def test_missing_spectrum_is_unknown():
    assembly, models = assembly_fixture()
    del models[0]["spectral_range"]
    assert check_assembly(assembly, models)["status"] == "INCONCLUSIVE"


def test_sensor_angular_response_must_cover_incident_lens_cone():
    assembly, models = assembly_fixture()
    models[1]["properties"]["accepted_angle_range"]["max"] = 10
    report = check_assembly(assembly, models)
    assert report["status"] == "OUT_OF_DOMAIN"
    assert "angular_coverage" in codes(report)


def test_chief_ray_alone_does_not_establish_distribution_matching():
    assembly, models = assembly_fixture()
    assembly["evaluation_intent"] = "lens_sensor_angular_matching"
    models[0]["angular_representation"] = "chief_ray_only"
    assert "missing_angular_distribution" in codes(check_assembly(assembly, models))
    assert check_assembly(assembly, models)["status"] == "INCONCLUSIVE"


def test_sensor_eqe_and_separate_cfa_cannot_both_apply_cfa():
    assembly, models = assembly_fixture()
    models[0]["included_effects"].append("cfa")
    report = check_assembly(assembly, models)
    assert report["status"] == "FAIL"
    assert "duplicate_effect" in codes(report)


def test_repeated_effect_inside_one_model_is_also_rejected():
    assembly, models = assembly_fixture()
    models[1]["included_effects"].append("cfa")
    assert "duplicate_effect" in codes(check_assembly(assembly, models))


def test_absent_effect_declaration_does_not_mean_no_effects():
    assembly, models = assembly_fixture()
    del models[0]["included_effects"]
    assert check_assembly(assembly, models)["status"] == "INCONCLUSIVE"


def test_optical_reference_planes_must_match():
    assembly, models = assembly_fixture()
    models[0]["reference_plane"] = "photodiode.absorption"
    assert "reference_plane_mismatch" in codes(check_assembly(assembly, models))


@pytest.mark.parametrize(
    "field,value",
    [
        ("cfa", "BGGR"),
        ("bit_depth", 10),
        ("layout", "planar"),
        ("packing", "mipi_raw12"),
        ("black_level", 0),
        ("hdr_streams", ["long", "short"]),
    ],
)
def test_raw_stream_contract_mismatches_block_execution(field, value):
    assembly, models = assembly_fixture()
    models[2]["input_raw_format"][field] = value
    report = check_assembly(assembly, models)
    assert report["status"] == "FAIL"
    assert "raw_format_mismatch" in codes(report)


def test_absent_raw_bit_depth_is_unknown_even_when_both_sides_omit_it():
    assembly, models = assembly_fixture()
    for record, key in [
        (models[1], "raw_format"),
        (models[2], "input_raw_format"),
        (assembly, "raw_format"),
    ]:
        del record[key]["bit_depth"]
    report = check_assembly(assembly, models)
    assert report["status"] == "INCONCLUSIVE"
    assert "missing_raw_field" in codes(report)


def test_operating_temperature_outside_any_model_blocks_execution():
    assembly, models = assembly_fixture()
    assembly["conditions"]["temperature_c"]["value"] = 80
    assert check_assembly(assembly, models)["status"] == "OUT_OF_DOMAIN"


def test_missing_validity_cannot_be_assumed_unrestricted():
    assembly, models = assembly_fixture()
    models[0]["valid_domain"] = {}
    assert check_assembly(assembly, models)["status"] == "INCONCLUSIVE"


def test_missing_pinned_model_is_an_error():
    assembly, models = assembly_fixture()
    assembly["model_ids"].append("sensor:does-not-exist")
    assert check_assembly(assembly, models)["status"] == "FAIL"


def test_composer_does_not_mutate_registered_models():
    inputs = assembly_fixture()
    before = deepcopy(inputs)
    check_assembly(*inputs)
    assert inputs == before
