"""Both PWM and background components come from the same moving 3D Scene."""

from __future__ import annotations

import hashlib
import os
import time
from copy import deepcopy

import numpy as np
import pytest
import test_scene_render
from fastapi.testclient import TestClient
from test_registered_3d_pwm_trapped import (
    _array,
    _pwm_integral,
    _small_native_camera,
)
from test_scene_light_components import _setup
from test_scene_render import finish

from camera_platform import capture_programs, scene_motion_sampling, scene_render, sequences
from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService, PreflightError

TIMES_NS = [0, 20_000_000, 40_000_000, 60_000_000, 80_000_000]
CLOCK = {
    "clock_id": "dual-3d-light-clock", "unit": "ns", "reference": "relative",
    "origin": "Authored zero of a synthetic moving occluder, not a camera clock",
}


@pytest.fixture
def scene_workflow(tmp_path, monkeypatch):
    return test_scene_render.scene_workflow.__wrapped__(tmp_path, monkeypatch)


def _ambient_integral(rate_knots, start_ns, end_ns):
    """Scalar linear-trapezoid integral, with no waveform or runtime helper."""
    result = 0.0
    for index, (left, right) in enumerate(zip(TIMES_NS, TIMES_NS[1:], strict=False)):
        a, b = max(start_ns, left), min(end_ns, right)
        if b <= a:
            continue
        first = rate_knots[index] + (
            rate_knots[index + 1] - rate_knots[index]
        ) * (a - left) / (right - left)
        last = rate_knots[index] + (
            rate_knots[index + 1] - rate_knots[index]
        ) * (b - left) / (right - left)
        result += (first + last) * 0.5 * (b - a) * 1e-9
    return result


def _program(service, assembly_id, control, phase_ns):
    data = capture_programs.template(
        service, assembly_id, mode="trapped_gain_merge",
        control_sequence_id=control["id"],
    )
    data.update(name=f"Two 3D lights trapped PWM phase {phase_ns} ns",
                revision=f"phase-{phase_ns}")
    payload = data["payload"]
    payload["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": CLOCK["clock_id"], "period_ns": 1_000_000,
        "on_time_ns": 450_000, "phase_origin_ns": phase_ns,
        "low": 0, "high": 1,
    }
    payload["retained_charge_model"].update(
        transfer_efficiency=[0.5] * 4,
        trap_capacity_e=[1000] * 4,
        capture_rate_s=[5.] * 4,
        release_time_s=[0.01] * 4,
    )
    return capture_programs.create(service, data)


def test_registered_two_3d_lights_feed_pwm_trap_dual_gain_raw(scene_workflow):
    service, original_assembly, initial_request = scene_workflow
    scene, lights, base = _setup(service, original_assembly, initial_request)
    assembly = _small_native_camera(service, service.registry.get(
        "assemblies", base["assembly_id"]
    ))
    receiver = {"crop_origin": [10, 10], "shape": [12, 12]}
    base.update(
        assembly_id=assembly["id"], shape=receiver["shape"],
        crop_origin=receiver["crop_origin"], render_psf_halo=True,
        render_clock=CLOCK, samples_per_pixel=2,
    )
    series = []
    jobs_by_light = []
    for light in lights:
        request = {**base, "light_component_id": light["id"]}
        jobs = [finish(service, queued) for queued in
                scene_motion_sampling.submit_renders(service, request, TIMES_NS)]
        assert all(job["status"] == "succeeded" for job in jobs)
        assert all(job["request"]["light_component_id"] == light["id"] for job in jobs)
        jobs_by_light.append(jobs)
        series.append(scene_motion_sampling.publish_sequence(
            service, [job["id"] for job in jobs],
            name=f"Moving 3D {light['id']} component",
        ))
    pair = scene_render.verify_light_component_sequences(
        service, series[0]["id"], series[1]["id"]
    )
    assert pair["scene_asset_id"] == scene["id"]
    assert pair["light_asset_ids"] == [light["id"] for light in lights]
    assert len(pair["pair_sha256"]) == len(TIMES_NS)
    for first, second in zip(*jobs_by_light, strict=True):
        instant = scene_render.verify_light_component_pair(
            service, first["id"], second["id"]
        )
        assert instant["pair_sha256"] in pair["pair_sha256"]

    control_data = sequences.control_template(service, assembly["id"], series[0]["id"])
    control_data["payload"]["mode"].update(
        shutter="rolling", line_time_ns=137_000, hidden_top_rows=1,
    )
    for frame, exposure in zip(
        control_data["payload"]["frames"],
        (700_000, 1_000_000, 1_000_000, 1_300_000), strict=True,
    ):
        frame["applied"].update(exposure_ns=exposure, gain=1)
    control = sequences.create_sequence(service, "control_sequences", control_data)
    sdk = PlatformClient.__new__(PlatformClient)

    def local_post(path, payload):
        assert path == "runs"
        try:
            return service.submit_run(payload)
        except PreflightError as error:
            raise AssertionError(error.report) from error

    sdk.post = local_post
    outcomes = []
    programs = []
    tests = []
    for phase_ns in (0, 350_000):
        program = _program(service, assembly["id"], control, phase_ns)
        programs.append(program)
        if phase_ns == 0:
            duplicate = scene_motion_sampling.publish_sequence(
                service, [job["id"] for job in jobs_by_light[0]],
                name="Invalid duplicate 3D PWM Light component",
            )
            duplicate_test = service.create_test(sequences.test_template(
                service, assembly["id"], series[0]["id"], control["id"],
                program["id"], unmodulated_scene_sequence_id=duplicate["id"],
                capture_crop=receiver,
            ))
            with pytest.raises(ValueError, match="distinct render jobs"):
                sdk.submit_retained_charge(
                    assembly["id"], duplicate_test["id"], series[0]["id"],
                    control["id"], program["id"], seed=917,
                    noise_enabled=False,
                    unmodulated_scene_sequence_id=duplicate["id"],
                    capture_crop=receiver,
                )
        test = service.create_test(sequences.test_template(
            service, assembly["id"], series[0]["id"], control["id"],
            program["id"], unmodulated_scene_sequence_id=series[1]["id"],
            capture_crop=receiver,
        ))
        tests.append(test)
        queued = sdk.submit_retained_charge(
            assembly["id"], test["id"], series[0]["id"], control["id"],
            program["id"], seed=917, noise_enabled=False,
            unmodulated_scene_sequence_id=series[1]["id"], capture_crop=receiver,
        )
        assert queued["dual_3d_light_evidence"] == pair
        run = finish(service, queued)
        assert run["status"] == "succeeded"
        assert run["result"]["summary"]["dual_3d_light_evidence"] == pair
        assert run["result"]["summary"]["physical_validation"] == "not_established"
        assert run["result"]["summary"]["waveform"]["scope"] == (
            "modulated_component_only"
        )
        outcomes.append(run)

    source_led = _array(service, outcomes[0], "channel_photoelectron_rate_scenes")
    source_ambient = _array(
        service, outcomes[0], "unmodulated_channel_photoelectron_rate_scenes"
    )
    assert source_led.shape[0] == source_ambient.shape[0] == len(TIMES_NS)
    assert not np.array_equal(source_led[0], source_led[-1])
    assert not np.array_equal(source_led, source_ambient)
    sites = _array(service, outcomes[0], "cfa_channel_index")
    starts = _array(service, outcomes[0], "row_start_ns")
    ends = _array(service, outcomes[0], "row_end_ns")
    for phase_ns, run in zip((0, 350_000), outcomes, strict=True):
        led_e = _array(service, run, "modulated_channel_electrons")
        ambient_e = _array(service, run, "unmodulated_channel_electrons")
        total_e = _array(service, run, "mean_electrons")
        raw = _array(service, run, "raw_groups")
        assert raw.dtype.kind in "iu" and raw.shape == (4, 2, 12, 12)
        for row, col in ((2, 2), (2, 3), (6, 6), (9, 9)):
            site = int(sites[row, col])
            led_knots = source_led[:, row, col, site]
            ambient_knots = source_ambient[:, row, col, site]
            for frame in range(4):
                opening, closing = int(starts[frame, row]), int(ends[frame, row])
                expected_led = _pwm_integral(
                    led_knots, TIMES_NS, opening, closing, phase_ns
                )
                expected_ambient = _ambient_integral(
                    ambient_knots, opening, closing
                )
                np.testing.assert_allclose(
                    led_e[frame, row, col, site], expected_led, rtol=2e-11, atol=1e-9
                )
                np.testing.assert_allclose(
                    ambient_e[frame, row, col, site], expected_ambient,
                    rtol=2e-11, atol=1e-9,
                )
                np.testing.assert_allclose(
                    total_e[frame, row, col], expected_led + expected_ambient,
                    rtol=2e-11, atol=1e-9,
                )
    np.testing.assert_array_equal(
        _array(service, outcomes[0], "unmodulated_channel_electrons"),
        _array(service, outcomes[1], "unmodulated_channel_electrons"),
    )
    assert not np.array_equal(
        _array(service, outcomes[0], "modulated_channel_electrons"),
        _array(service, outcomes[1], "modulated_channel_electrons"),
    )
    assert not np.array_equal(
        _array(service, outcomes[0], "raw_groups"),
        _array(service, outcomes[1], "raw_groups"),
    )
    assert series[0]["payload"]["clock"] == CLOCK

    # Reuse the exact same two rendered 3D Light components and phase-zero
    # electrical program. The only optical change is the frozen source RI mode.
    original_blobs = {
        artifact["sha256"]: service.registry.blob_path(artifact["sha256"]).read_bytes()
        for component in series for image in component["image_snapshots"]
        for artifact in image["artifacts"]
    }
    selected = sdk.submit_retained_charge(
        assembly["id"], tests[0]["id"], series[0]["id"], control["id"],
        programs[0]["id"], seed=917, noise_enabled=False,
        unmodulated_scene_sequence_id=series[1]["id"], capture_crop=receiver,
        field_radiometry_mode="source_position_ri_v1",
    )
    assert selected["dual_3d_light_evidence"] == pair
    assert selected["request"]["field_radiometry_mode"] == "source_position_ri_v1"
    assert set(selected["resources"]["source_position_ri"]["component_map_bytes"]) == {
        "modulated", "unmodulated"}
    cold = PlatformService(service.registry.directory)
    for changed in ("field_mode", "ambient_knot"):
        forged = deepcopy(selected)
        if changed == "field_mode":
            forged["request"]["field_radiometry_mode"] = "receiver_position_ri_v1"
        else:
            forged["request"]["unmodulated_scene_sequence"]["knots"][0]["time_ns"] += 1
        with pytest.raises(ValueError, match="queued request differs"):
            cold.execute_run(forged)
    app = create_app(service.registry.directory, start_worker=True, seed_demo=False)
    with TestClient(app) as http:
        assert app.state.worker.pid != os.getpid()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            response = http.get(f"/api/v1/runs/{selected['id']}")
            assert response.status_code == 200, response.text
            source_run = response.json()
            if source_run["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(.1)
        else:
            pytest.fail("Separate Worker did not finish dual-3D source RI within 60 seconds")
        assert source_run["status"] == "succeeded", source_run.get("error")
    source_summary = source_run["result"]["summary"]
    assert source_summary["field_radiometry_mode"] == "source_position_ri_v1"
    assert source_summary["dual_3d_light_evidence"] == pair
    assert source_summary["physical_validation"] == "not_established"
    assert source_summary["readout_ids"] == ["HCG", "LCG"]
    assert source_summary["gain_path_merge"]["temporal_hdr_fusion"] is False
    assert source_summary["source_position_ri_components"]["modulated"][
        "source_support"]["crop_origin"] == series[0]["image_snapshots"][0][
            "sampling"]["crop_origin"]
    assert source_summary["source_position_ri_components"]["unmodulated"][
        "receiver_crop"]["crop_origin"] == receiver["crop_origin"]
    for component, map_name, ledger_name, rate_name in (
        (series[0], "source_relative_illumination_map", "flux_diagnostics_per_scene",
         "channel_photoelectron_rate_scenes"),
        (series[1], "unmodulated_source_relative_illumination_map",
         "unmodulated_flux_diagnostics_per_scene",
         "unmodulated_channel_photoelectron_rate_scenes"),
    ):
        field_map = _array(service, source_run, map_name)
        rates = _array(service, source_run, rate_name)
        assert rates.shape[0] == len(TIMES_NS)
        assert field_map.shape == tuple(component["image_snapshots"][0]["shape"])
        assert np.ptp(field_map) > 0
        for knot_index, image_record in enumerate(component["image_snapshots"]):
            image = _array(service, image_record, "spectral-image")
            ledger = source_summary[ledger_name][knot_index]
            area = (source_summary["pixel_pitch_um"] * 1e-6) ** 2
            np.testing.assert_allclose(ledger["pre_ri_source_spectral_flux"],
                                       image.sum(axis=(0, 1)) * area, rtol=1e-12)
            np.testing.assert_allclose(ledger["post_ri_source_spectral_flux"],
                                       (image * field_map).sum(axis=(0, 1)) * area,
                                       rtol=1e-12)
            np.testing.assert_allclose(ledger["input_spectral_flux"],
                                       ledger["post_ri_source_spectral_flux"], rtol=1e-12)
    mod_rates = _array(service, source_run, "channel_photoelectron_rate_scenes")
    ambient_rates = _array(service, source_run, "unmodulated_channel_photoelectron_rate_scenes")
    starts = _array(service, source_run, "row_start_ns")
    ends = _array(service, source_run, "row_end_ns")
    sites = _array(service, source_run, "cfa_channel_index")
    led_e = _array(service, source_run, "modulated_channel_electrons")
    ambient_e = _array(service, source_run, "unmodulated_channel_electrons")
    total_e = _array(service, source_run, "mean_electrons")
    for row, col in ((2, 2), (2, 3), (6, 6), (9, 9)):
        site = int(sites[row, col])
        for frame in range(4):
            opening, closing = int(starts[frame, row]), int(ends[frame, row])
            expected_led = _pwm_integral(
                mod_rates[:, row, col, site], TIMES_NS, opening, closing, 0)
            expected_ambient = _ambient_integral(
                ambient_rates[:, row, col, site], opening, closing)
            np.testing.assert_allclose(led_e[frame, row, col, site], expected_led,
                                       rtol=2e-11, atol=1e-9)
            np.testing.assert_allclose(ambient_e[frame, row, col, site], expected_ambient,
                                       rtol=2e-11, atol=1e-9)
            np.testing.assert_allclose(total_e[frame, row, col],
                                       expected_led + expected_ambient,
                                       rtol=2e-11, atol=1e-9)
    assert np.any(_array(service, source_run, "retained_trapped_e") > 0)
    assert _array(service, source_run, "raw_groups").shape == (4, 2, 12, 12)
    assert _array(service, source_run, "rgb_frames").shape == (4, 12, 12, 3)
    assert not np.allclose(mod_rates, source_led, rtol=1e-3)
    assert not np.allclose(ambient_rates, source_ambient, rtol=1e-3)
    for identity, content in original_blobs.items():
        assert hashlib.sha256(content).hexdigest() == identity
        assert service.registry.blob_path(identity).read_bytes() == content
    assert service.registry.audit()["integrity"] == "verified"
