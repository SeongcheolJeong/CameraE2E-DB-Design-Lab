"""Two authored ideal-image components: synthetic arithmetic and frozen-job binding."""

from copy import deepcopy

import numpy as np
import pytest
import test_hdr_workflow
import test_native_temporal
import test_nonlinear_validation

from camera_platform import capture_programs, hdr_runtime, sequences
from camera_platform.hdr_runtime import HDRBracketRuntime, default_capture_program
from camera_platform.validation import compare


@pytest.fixture
def native_request():
    return test_native_temporal.native_request.__wrapped__()


@pytest.fixture
def workflow(tmp_path):
    yield from test_hdr_workflow.workflow.__wrapped__(tmp_path)


def _pwm(data, *, phase_origin_ns=0):
    return {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": data["control_sequence"]["clock"]["clock_id"],
        "period_ns": 1_000_000, "on_time_ns": 500_000,
        "phase_origin_ns": phase_origin_ns, "low": 0, "high": 1,
    }


def _two_component_native(data):
    data["capture_program"] = default_capture_program(data["readout"])
    data["capture_program"]["waveform"] = _pwm(data)
    data["unmodulated_scene_sequence"] = deepcopy(data["scene_sequence"])
    for knot in data["scene_sequence"]["knots"]:
        image = knot["scene"]["spectral_irradiance"].copy()
        image[:, :5] = 0
        knot["scene"]["spectral_irradiance"] = image
    for knot in data["unmodulated_scene_sequence"]["knots"]:
        image = knot["scene"]["spectral_irradiance"].copy() * 2
        image[:, 5:] = 0
        knot["scene"]["spectral_irradiance"] = image
    return data


def test_authored_components_integrate_independently_into_native_charge(native_request):
    data = _two_component_native(native_request)
    output = HDRBracketRuntime().execute(data)
    arrays, summary = output["arrays"], output["summary"]
    site_rate = test_native_temporal.expected_rate(data)
    for frame_index, duration_s in enumerate((.002, .004)):
        np.testing.assert_allclose(
            arrays["unmodulated_channel_electrons"][frame_index, :, :5],
            np.broadcast_to(2 * site_rate * duration_s, (8, 5, 4)), rtol=3e-14,
        )
        np.testing.assert_allclose(
            arrays["modulated_channel_electrons"][frame_index, :, 5:],
            np.broadcast_to(.5 * site_rate * duration_s, (8, 5, 4)), rtol=3e-14,
        )
        np.testing.assert_array_equal(
            arrays["modulated_channel_electrons"][frame_index, :, :5], 0)
        np.testing.assert_array_equal(
            arrays["unmodulated_channel_electrons"][frame_index, :, 5:], 0)
    np.testing.assert_allclose(
        arrays["channel_electrons"],
        arrays["unmodulated_channel_electrons"] + arrays["modulated_channel_electrons"],
        rtol=0, atol=0,
    )
    np.testing.assert_allclose(arrays["waveform_row_exposure_ratio"], .5)
    assert summary["waveform"]["scope"] == "modulated_component_only"
    assert "cycle-average" in summary["hdr_rate_interpretation"]
    assert summary["physical_validation"] == "not_established"
    assert set(arrays) == set(summary["array_units"])


def test_unmodulated_timeline_varies_without_waveform_in_lfm_only(native_request):
    data = _two_component_native(native_request)
    data["capture_program"]["hdr_merge"] = None
    for frame, exposure in zip(data["control_sequence"]["frames"], (200_000, 400_000), strict=True):
        frame["applied"]["exposure_ns"] = exposure
    data["unmodulated_scene_sequence"]["knots"][1]["scene"]["spectral_irradiance"] *= 3
    first = HDRBracketRuntime().execute(data)
    data["capture_program"]["waveform"]["phase_origin_ns"] = 500_000
    shifted = HDRBracketRuntime().execute(data)
    a, b = first["arrays"], shifted["arrays"]
    np.testing.assert_array_equal(a["unmodulated_channel_electrons"],
                                  b["unmodulated_channel_electrons"])
    assert not np.array_equal(a["modulated_channel_electrons"],
                              b["modulated_channel_electrons"])
    start = a["row_start_ns"][0, 0]
    exposure_s = .0002
    duration_s = (data["scene_sequence"]["knots"][-1]["time_ns"]
                  - data["scene_sequence"]["knots"][0]["time_ns"]) * 1e-9
    midpoint_s = (start + 100_000) * 1e-9
    expected = (2 * test_native_temporal.expected_rate(data) * exposure_s
                * (1 + 2 * midpoint_s / duration_s))
    np.testing.assert_allclose(a["unmodulated_channel_electrons"][0, 0, 0],
                               expected, rtol=3e-14)


@pytest.mark.parametrize("problem", ["clock", "geometry", "waveform"])
def test_dual_input_rejects_mismatched_contract(native_request, problem):
    data = _two_component_native(native_request)
    if problem == "clock":
        data["unmodulated_scene_sequence"]["clock"]["clock_id"] = "other-clock"
    elif problem == "geometry":
        for knot in data["unmodulated_scene_sequence"]["knots"]:
            knot["scene"]["sampling"]["crop_origin"] = [2, 1]
    else:
        data["capture_program"]["waveform"] = None
    issues = HDRBracketRuntime().validate_request(data)
    assert issues, problem
    assert any("clock" in issue["message"].lower() if problem == "clock"
               else ("geometry" in issue["message"].lower() if problem == "geometry"
                     else "waveform" in issue["message"].lower()) for issue in issues)


@pytest.mark.parametrize("component", ["modulated", "unmodulated"])
def test_hdr_rejects_motion_in_either_optical_component(native_request, component):
    data = _two_component_native(native_request)
    key = ("scene_sequence" if component == "modulated"
           else "unmodulated_scene_sequence")
    data[key]["knots"][1]["scene"]["spectral_irradiance"] *= 2
    assert any("static" in issue["message"]
               for issue in HDRBracketRuntime().validate_request(data))


def test_combined_spectral_budget_is_enforced(native_request, monkeypatch):
    data = _two_component_native(native_request)
    # Each 8 x 10 x 2 input is below 200 samples; the two together exceed it.
    monkeypatch.setattr(hdr_runtime, "MAX_UNIQUE_SPECTRAL_SAMPLES", 200)
    assert any("Combined illumination components" in issue["message"]
               for issue in HDRBracketRuntime().validate_request(data))


def test_combined_resource_budget_rejects_before_seeded_native_map(native_request, monkeypatch):
    data = _two_component_native(native_request)
    for component in ("scene_sequence", "unmodulated_scene_sequence"):
        for knot in data[component]["knots"]:
            knot["scene"]["sampling"]["full_sensor_shape"] = [4096, 4096]
    data["lens"]["field_reference_radius_mm"] = 20
    data["lens"]["directional_response"]["field_reference_radius_mm"] = 20
    data["lens"]["directional_response"]["image_height_mm"] = [0, 20]
    data["capture_program"]["electrical_model"]["fixed_pattern"]["dsnu_e_per_s"] = 1

    def forbidden_pattern(*args, **kwargs):
        pytest.fail("full native pattern must not be allocated after a combined budget failure")

    monkeypatch.setattr(hdr_runtime.electrical, "_patterns", forbidden_pattern)
    issues = HDRBracketRuntime().validate_request(data)
    assert len(issues) == 1
    assert "Combined illumination components exceed working-array budget" in issues[0]["message"]


@pytest.mark.parametrize("mode,schema", [
    ("independent_bracket", "electrical_hdr_components_test_v1"),
    ("independent_bracket_nonlinear", "electrical_hdr_components_test_v2"),
])
def test_registered_components_bind_test_artifacts_and_worker(workflow, mode, schema):
    service, sdk, assembly, scene, control, _, legacy_test, request = workflow
    second = sequences.create_fixture(
        service, sequences.fixture_template(service, assembly["id"])
        | {"name": "Independent unmodulated image", "revision": "ambient-1",
           "shape": [16, 16], "knot_count": 2, "pattern": "static",
           "irradiance_scale": .25},
    )
    draft = capture_programs.template(service, assembly["id"], mode=mode)
    draft.update(name="Two authored light components", revision="components-1")
    draft["payload"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": control["payload"]["clock"]["clock_id"],
        "period_ns": 1_000_000, "on_time_ns": 500_000,
        "phase_origin_ns": 0, "low": 0, "high": 1,
    }
    program = capture_programs.create(service, draft)
    spec = sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
        unmodulated_scene_sequence_id=second["id"],
    )
    test = service.create_test(spec)
    assert test["stimulus"]["schema"] == schema
    assert test["stimulus"]["unmodulated_scene_sequence_hash"] == second["content_hash"]
    two = request | {
        "test_id": test["id"], "capture_program_id": program["id"],
        "parameters": {"seed": 927, "unmodulated_scene_sequence_id": second["id"]},
    }
    with pytest.raises(ValueError, match="exact sequence"):
        service.submit_run(two | {"test_id": legacy_test["id"]})
    queued = sdk.submit_hdr(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        seed=927, unmodulated_scene_sequence_id=second["id"],
    )
    assert queued["unmodulated_scene_sequence_snapshot"] == second
    assert {a["sha256"] for a in second["artifacts"]} <= {
        a["sha256"] for a in queued["input_artifacts"]}
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    altered = deepcopy(job)
    altered["unmodulated_scene_sequence_snapshot"]["content_hash"] = "0" * 64
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(altered)
    altered = deepcopy(job)
    altered["request"]["unmodulated_scene_sequence"]["knots"][0]["time_ns"] += 1
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(altered)
    run = service.registry.finish(job["id"], result=service.execute_run(job))
    summary = run["result"]["summary"]
    assert summary["unmodulated_scene_sequence_id"] == second["id"]
    assert summary["unmodulated_scene_sequence_hash"] == second["content_hash"]
    assert summary["prediction"]["included_effects"] == test["included_effects"]
    assert summary["physical_validation"] == "not_established"
    assert test_hdr_workflow.load_array(
        service, run, "unmodulated_channel_electrons").shape == (4, 16, 16, 3)


def test_linear_hdr_submit_checks_program_source_artifact(workflow, monkeypatch, tmp_path):
    service, _, _, _, _, program, _, request = workflow
    source_sha = next(a["sha256"] for a in program["artifacts"]
                      if a["name"] == "capture-program-source.json")
    damaged = tmp_path / "damaged-program-source.json"
    damaged.write_text("{}")
    original = service.registry.blob_path
    with monkeypatch.context() as patch:
        patch.setattr(service.registry, "blob_path",
                      lambda sha: damaged if sha == source_sha else original(sha))
        with pytest.raises(ValueError, match="artifact descriptor|source/normalized"):
            service.submit_run(request)


def test_nonlinear_component_test_retains_raw_comparison_contract():
    prediction, reference, spec = test_nonlinear_validation.samples(False)
    spec["stimulus"].update(
        schema="electrical_hdr_components_test_v2",
        unmodulated_scene_sequence_id="scene:unmodulated",
        unmodulated_scene_sequence_hash="d" * 64,
        illumination_component_schema=hdr_runtime.ILLUMINATION_COMPONENT_SCHEMA,
    )
    result = compare(prediction, reference, spec)
    assert result["status"] == "INCONCLUSIVE"
    assert not result["physical_validation_pass"]
    reference["method_and_version"] = "sampled_raw_dark_corrected_v1"
    assert compare(prediction, reference, spec)["status"] == "INVALID_COMPARISON"
