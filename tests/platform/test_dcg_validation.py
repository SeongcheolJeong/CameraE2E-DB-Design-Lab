"""Paired acquisition/sample identity without API state or environment fingerprints."""

import json
from copy import deepcopy

import pytest
from test_temporal_validation import codes
from test_temporal_validation import fixture as temporal_fixture

from camera_platform.validation import compare


def fixture(*, native=True):
    prediction, measurement, spec = temporal_fixture()
    channels = ["R", "Gr", "Gb", "B"] if native else ["R", "G", "B"]
    raw_format = {
        "cfa": "RGGB",
        "bit_depth": 12,
        "black_level_dn": 64,
        "response_type": "linear",
        "packing": "unpacked",
        "layout": "bayer",
        "hdr_streams": 1,
    }
    contract = {
        "schema": "camera_shared_charge_samples_v1",
        "topology": "ideal_shared_charge_two_conversion_paths",
        "capture_program_id": "capture-program:paired",
        "capture_program_hash": "a" * 64,
        "readout_ids": ["HCG", "LCG"],
        "channels": channels,
        "raw_formats": [deepcopy(raw_format), {**raw_format, "black_level_dn": 32}],
        "sampling": {
            "kind": "native_sensor",
            "full_sensor_shape": [12, 14],
            "crop_origin": [1, 3],
            "pixel_pitch_um": 3.0,
        },
        "raster_shape": [8, 10],
        "observation": "sampled_shared_charge_raw_dark_corrected_v1",
    }
    spec["stimulus"] = {
        "schema": "shared_charge_dcg_test_v1",
        "acquisition_topology": contract["topology"],
        "readout_ids": ["HCG", "LCG"],
        "capture_program_id": contract["capture_program_id"],
        "capture_program_hash": contract["capture_program_hash"],
        "sample_contract": contract,
    }
    for record in (prediction, measurement, spec):
        record["metric_id"] = "raw.channel_mean_dn_shared_charge"
        for frame in record["frame_conditions"]:
            frame["gain"] = 1
    for record in (prediction, measurement):
        record["axes"] = [
            {"name": "acquisition", "unit": "1", "values": ["f0", "f1"]},
            {"name": "readout", "unit": "1", "values": ["HCG", "LCG"]},
            {"name": "channel", "unit": "1", "values": channels.copy()},
        ]
        record["values"] = list(range(100, 100 + 4 * len(channels)))
        record["sample_geometry"] = deepcopy(contract)
        record["raw_formats"] = deepcopy(contract["raw_formats"])
    return prediction, measurement, spec


def assert_invalid(data, code=None):
    report = compare(*data)
    assert report["status"] == "INVALID_COMPARISON", report
    assert not report["physical_validation_pass"]
    if code:
        assert code in codes(report), report["issues"]
    json.dumps(report, allow_nan=False)
    return report


def synchronize_contract(data):
    """Adversarial mutual agreement must not make a malformed contract valid."""
    for record in data[:2]:
        record["sample_geometry"] = deepcopy(data[2]["stimulus"]["sample_contract"])


@pytest.mark.parametrize("native", [True, False])
def test_complete_paired_contract_and_declared_bounds_are_comparable(native):
    data = fixture(native=native)
    frozen = deepcopy(data)
    result = compare(*data)
    assert result["status"] == "PASS", result["issues"]
    assert result["summary"]["sample_count"] == (16 if native else 12)
    assert result["residuals"]["values"] == [0] * result["summary"]["sample_count"]
    assert result["physical_validation_pass"]  # This fixture declares independent measured bounds.
    assert result["temporal_comparison"]["domain_policy"] == "global_evaluated_per_frame"
    assert data == frozen
    json.dumps(result, allow_nan=False)


def test_current_runtime_unknown_uncertainty_cannot_be_promoted_to_physical_pass():
    data = fixture()
    data[0]["uncertainty"] = {"kind": "unknown"}
    result = compare(*data)
    assert result["status"] == "INCONCLUSIVE"
    assert result["independent_validation"]
    assert not result["physical_validation_pass"]
    assert "missing_uncertainty" in codes(result)


@pytest.mark.parametrize("kind", ["implementation_verification", "physical_validation"])
def test_simulated_reference_numerical_agreement_is_never_physical_validation(kind):
    data = fixture()
    data[1]["source_kind"] = "simulated"
    data[2]["validation_kind"] = kind
    result = compare(*data)
    assert result["status"] == ("PASS" if kind == "implementation_verification" else "INCONCLUSIVE")
    assert not result["physical_validation_pass"]
    assert not result["independent_validation"]


@pytest.mark.parametrize("target", [0, 1])
def test_missing_sample_geometry_is_not_inferred_from_the_other_record(target):
    data = fixture()
    data[target].pop("sample_geometry")
    assert_invalid(data, "shared_charge_contract_mismatch")


@pytest.mark.parametrize("field", ["program", "black", "crop", "sites"])
def test_one_different_record_contract_invalidates_identical_metric_samples(field):
    data = fixture()
    changed = data[1]["sample_geometry"]
    if field == "program":
        changed["capture_program_hash"] = "b" * 64
    elif field == "black":
        changed["raw_formats"][0]["black_level_dn"] += 1
    elif field == "crop":
        changed["sampling"]["crop_origin"] = [0, 3]
    else:
        changed["channels"] = ["R", "Gb", "Gr", "B"]
    assert_invalid(data, "shared_charge_contract_mismatch")


@pytest.mark.parametrize(
    "mutation",
    [
        "swap_readouts",
        "flatten",
        "swap_native_greens",
        "collapse_native_green",
        "swap_acquisitions",
    ],
)
def test_mutually_equal_wrong_axes_cannot_relabel_paired_samples(mutation):
    data = fixture()
    for record in data[:2]:
        axes = record["axes"]
        if mutation == "swap_readouts":
            axes[1]["values"].reverse()
        elif mutation == "flatten":
            record["axes"] = [
                {"name": "frame", "unit": "1", "values": ["f0:H", "f0:L", "f1:H", "f1:L"]},
                axes[2],
            ]
        elif mutation == "swap_native_greens":
            axes[2]["values"] = ["R", "Gb", "Gr", "B"]
        elif mutation == "collapse_native_green":
            axes[2]["values"] = ["R", "G", "B"]
            record["values"] = record["values"][:12]
        else:
            axes[0]["values"].reverse()
    assert_invalid(data, "temporal_axis_mismatch")


def test_sample_contract_cannot_erase_program_formats_and_geometry_by_mutual_agreement():
    data = fixture()
    contract = data[2]["stimulus"]["sample_contract"]
    data[2]["stimulus"]["sample_contract"] = {
        key: contract[key] for key in ("schema", "topology", "readout_ids")
    }
    synchronize_contract(data)
    assert_invalid(data, "invalid_shared_charge_contract")


@pytest.mark.parametrize(
    "field",
    [
        "capture_program_id",
        "capture_program_hash",
        "raw_formats",
        "sampling",
        "raster_shape",
        "observation",
        "channels",
    ],
)
def test_every_sample_identity_field_is_required_even_if_both_records_omit_it(field):
    data = fixture()
    data[2]["stimulus"]["sample_contract"].pop(field)
    synchronize_contract(data)
    assert_invalid(data, "invalid_shared_charge_contract")


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "wrong_hash",
        "wrong_topology",
        "observation",
        "boolean_bits",
        "mismatched_bits",
        "black_above_white",
        "packed",
        "multi_stream",
        "cfa",
        "format_missing",
        "boolean_origin",
        "crop_outside",
        "odd_raster",
        "wrong_pitch",
        "native_size",
        "extra_sampling",
    ],
)
def test_mutually_equal_malformed_sample_contract_is_refused(mutation):
    data = fixture()
    contract = data[2]["stimulus"]["sample_contract"]
    if mutation == "extra":
        contract["inferred_readout"] = True
    elif mutation == "wrong_hash":
        contract["capture_program_hash"] = "a" * 63
        data[2]["stimulus"]["capture_program_hash"] = contract["capture_program_hash"]
    elif mutation == "wrong_topology":
        contract["topology"] = "independent_bracket"
    elif mutation == "observation":
        contract["observation"] = "expected_electrons"
    elif mutation == "boolean_bits":
        contract["raw_formats"][0]["bit_depth"] = True
    elif mutation == "mismatched_bits":
        contract["raw_formats"][0]["bit_depth"] = 14
    elif mutation == "black_above_white":
        contract["raw_formats"][0]["black_level_dn"] = 4095
    elif mutation == "packed":
        contract["raw_formats"][0]["packing"] = "mipi"
    elif mutation == "multi_stream":
        contract["raw_formats"][0]["hdr_streams"] = 2
    elif mutation == "cfa":
        contract["raw_formats"][0]["cfa"] = "BGGR"
    elif mutation == "format_missing":
        contract["raw_formats"][0].pop("black_level_dn")
    elif mutation == "boolean_origin":
        contract["sampling"]["crop_origin"][0] = True
    elif mutation == "crop_outside":
        contract["sampling"]["crop_origin"] = [8, 8]
    elif mutation == "odd_raster":
        contract["raster_shape"] = [7, 10]
    elif mutation == "wrong_pitch":
        contract["sampling"]["pixel_pitch_um"] = float("nan")
    elif mutation == "native_size":
        contract["sampling"]["full_sensor_shape"] = [1_000_000, 1_000_000]
    else:
        contract["sampling"]["phase"] = "inferred"
    synchronize_contract(data)
    assert_invalid(data, "invalid_shared_charge_contract")


@pytest.mark.parametrize("field", ["capture_program_id", "capture_program_hash"])
def test_complete_contract_must_match_outer_frozen_test_program_identity(field):
    data = fixture()
    data[2]["stimulus"][field] = "b" * 64 if field.endswith("hash") else "another-program"
    assert_invalid(data, "invalid_shared_charge_contract")


@pytest.mark.parametrize("stimulus", [None, [], "same program"])
def test_malformed_stimulus_returns_invalid_comparison_instead_of_crashing(stimulus):
    data = fixture()
    data[2]["stimulus"] = stimulus
    assert_invalid(data, "invalid_shared_charge_contract")


def test_dcg_metric_alone_forces_timing_and_contract_validation():
    data = fixture()
    for record in data:
        record.pop("clock")
        record.pop("frame_conditions")
        record["conditions"]["exposure_s"] = {"value": 0.004, "unit": "s"}
        record["conditions"]["gain"] = {"value": 1, "unit": "1"}
        record.pop("sample_geometry", None)
    data[2].pop("stimulus")
    report = assert_invalid(data)
    assert {
        "invalid_temporal_clock",
        "invalid_frame_conditions",
        "invalid_shared_charge_contract",
    } <= codes(report)


def test_identically_nonunit_acquisition_gain_does_not_change_fixed_path_model():
    data = fixture()
    for record in data:
        record["frame_conditions"][0]["gain"] = 2
    assert_invalid(data, "invalid_shared_charge_gain")


def test_redundant_raw_format_cannot_contradict_the_frozen_sample_contract():
    data = fixture()
    data[1]["raw_formats"].reverse()
    assert_invalid(data, "shared_charge_contract_mismatch")


def test_covariance_temperature_is_evaluated_for_every_acquisition_domain():
    data = fixture()
    data[0]["frame_valid_domains"] = [
        {"frame_id": frame["frame_id"], "valid_domain": deepcopy(data[0]["valid_domain"])}
        for frame in data[0]["frame_conditions"]
    ]
    data[0]["frame_valid_domains"][1]["valid_domain"]["temperature_c"]["max"] = 24
    report = compare(*data)
    assert report["status"] == "OUT_OF_DOMAIN"
    assert not report["physical_validation_pass"]


def test_program_applicability_intersection_narrows_temperature_and_gain_without_mutation():
    from camera_platform.dcg_runtime import default_capture_program
    from camera_platform.sequences import _dcg_valid_domain
    from camera_platform.temporal import request_template

    program = default_capture_program(request_template()["readout"])
    domain = fixture()[0]["valid_domain"]
    frozen = deepcopy(domain)
    narrowed = _dcg_valid_domain(domain, program)
    assert narrowed["temperature_c"] == {"min": 25, "max": 25, "unit": "degC"}
    assert narrowed["gain"] == {"min": 1, "max": 1, "unit": "1"}
    assert narrowed["exposure_s"] == domain["exposure_s"]
    assert domain == frozen
    for bad in (
        {},
        {**domain, "gain": {"min": 2, "max": 4, "unit": "1"}},
        {**domain, "temperature_c": {"min": 0, "max": 24, "unit": "degC"}},
        {**domain, "temperature_c": {"min": 0, "max": 100, "unit": "K"}},
    ):
        with pytest.raises(ValueError):
            _dcg_valid_domain(bad, program)


@pytest.mark.parametrize("native", [False, True])
def test_template_pins_sites_and_raw_observation_effects_to_program_snapshot(native):
    from types import SimpleNamespace

    from camera_platform.dcg_runtime import default_capture_program
    from camera_platform.sequences import test_template as create_test_template
    from camera_platform.temporal import request_template

    request = request_template()
    program = {
        "id": "program",
        "content_hash": "a" * 64,
        "assembly_id": "assembly",
        "payload": default_capture_program(request["readout"]),
        "sensor_model_snapshot": {"payload": {"native_pixel_response": {}} if native else {}},
    }
    # Pure registry-interface fixture: no API/database/worker execution is claimed.
    records = {
        ("assemblies", "assembly"): {
            "id": "assembly", "conditions": {}, "model_ids": ["sensor"]},
        ("models", "sensor"): {"id": "sensor", "kind": "sensor",
                               "payload": deepcopy(program["sensor_model_snapshot"]["payload"])},
        ("scene_sequences", "scene"): {
            "id": "scene",
            "content_hash": "b" * 64,
            "payload": {"knots": [{"spectral_image_id": "image"}]},
            "image_snapshots": [
                {
                    "id": "image",
                    "sampling": {
                        "kind": "native_sensor",
                        "full_sensor_shape": [8, 10],
                        "crop_origin": [0, 0],
                        "pixel_pitch_um": 4,
                    },
                    "shape": [8, 10, 5],
                }
            ],
        },
        ("spectral_images", "image"): {
            "id": "image",
            "sampling": {
                "kind": "native_sensor",
                "full_sensor_shape": [8, 10],
                "crop_origin": [0, 0],
                "pixel_pitch_um": 4,
            },
            "shape": [8, 10, 5],
        },
        ("control_sequences", "control"): {
            "id": "control",
            "content_hash": "c" * 64,
            "assembly_id": "assembly",
            "scene_sequence_id": "scene",
            "payload": request["control_sequence"],
        },
        ("capture_programs", "program"): program,
    }
    service = SimpleNamespace(
        registry=SimpleNamespace(get=lambda kind, identity: records[kind, identity])
    )
    result = create_test_template(service, "assembly", "scene", "control", "program")
    assert result["stimulus"]["sample_contract"]["channels"] == (
        ["R", "Gr", "Gb", "B"] if native else ["R", "G", "B"]
    )
    assert result["stimulus"]["observation_method"] == "sampled_shared_charge_raw_dark_corrected_v1"
    assert "raw_dcg_covariance_merge" not in result["included_effects"]
    assert "shared_charge_two_path_readout" in result["included_effects"]
