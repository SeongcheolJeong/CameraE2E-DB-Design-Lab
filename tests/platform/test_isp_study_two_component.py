"""Stationary charge Studies must inspect both retained optical components."""

import pytest
from test_hdr_workflow import workflow as workflow
from test_nonlinear_workflow import worker_finish

from camera_platform import capture_programs, isp_charge_source, isp_studies_v2, sequences


def _equal_exposure_dual_source(workflow, secondary_pattern):
    service, _, assembly, primary, _, _, _, request = workflow
    secondary = sequences.create_fixture(
        service,
        sequences.fixture_template(service, assembly["id"])
        | {"name": "Independent secondary source", "revision": secondary_pattern,
           "shape": [16, 16], "knot_count": 2, "pattern": secondary_pattern,
           "irradiance_scale": .25},
    )
    controls = sequences.control_template(service, assembly["id"], primary["id"])
    for frame in controls["payload"]["frames"]:
        frame["applied"]["exposure_ns"] = 4_000_000
    control = sequences.create_sequence(service, "control_sequences", controls)
    draft = capture_programs.template(
        service, assembly["id"], mode="independent_bracket_nonlinear"
    )
    draft.update(name="Stationarity probe", revision=secondary_pattern)
    draft["payload"]["hdr_merge"] = None
    draft["payload"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "constant",
        "clock_id": control["payload"]["clock"]["clock_id"], "level": 1,
    }
    program = capture_programs.create(service, draft)
    test = service.create_test(sequences.test_template(
        service, assembly["id"], primary["id"], control["id"],
        program["id"], secondary["id"],
    ))
    run = worker_finish(service, {
        **request, "control_sequence_id": control["id"],
        "capture_program_id": program["id"], "test_id": test["id"],
        "parameters": {"seed": 927, "unmodulated_scene_sequence_id": secondary["id"]},
    })
    source, _, calibrations = isp_charge_source._resolve(
        service, "run", run["id"],
        {"schema": isp_charge_source.SELECTION_SCHEMA, "mode": "sequential_frames"},
    )
    return service, source, calibrations


def test_stationary_study_accepts_both_static_authored_components(workflow):
    service, source, calibrations = _equal_exposure_dual_source(workflow, "static")
    isp_studies_v2._fixed_acquisition(service, source, calibrations)


def test_stationary_study_refuses_changing_unmodulated_component(workflow):
    service, source, calibrations = _equal_exposure_dual_source(workflow, "moving_spot")
    with pytest.raises(ValueError, match="changing unmodulated optical scene"):
        isp_studies_v2._fixed_acquisition(service, source, calibrations)

