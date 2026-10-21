"""Real 3D motion, authored PWM/ambient, trap history, paired RAW and ISP.

The scalar oracle starts at the saved optical electron-rate knots. It does not
call the product waveform integrator, trap solver or paired RAW encoder.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import zipfile

import numpy as np
import pytest
import test_scene_render

from camera_platform import (
    capture_programs,
    factory,
    imaging,
    retained_isp_replay,
    retained_isp_source,
    scene_motion_sampling,
    scene_render,
    sequences,
)
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import PROVENANCE
from camera_platform.evidence import build_bundle
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService

CLOCK = {
    "clock_id": "moving-3d-pwm-trap-clock", "unit": "ns", "reference": "relative",
    "origin": "Authored simulation zero; not a measured LED or camera clock",
}
TIMES_NS = [0, 20_000_000, 40_000_000, 60_000_000, 80_000_000]
PERIOD_NS = 1_000_000
ON_NS = 450_000


def _array(service, run, name):
    artifact = next(item for item in run["artifacts"] if item["name"] == name + ".npy")
    return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)


def _pwm_integral(
    rate_knots, times_ns, start_ns, end_ns, phase_ns,
    *, period_ns=PERIOD_NS, on_ns=ON_NS,
):
    """Enumerate ON intervals and integrate each linear rate trapezoid directly."""
    result = 0.0
    for index, (left, right) in enumerate(zip(times_ns, times_ns[1:], strict=False)):
        low, high = max(start_ns, left), min(end_ns, right)
        if high <= low:
            continue
        for cycle in range((low - phase_ns) // period_ns - 1,
                           (high - phase_ns) // period_ns + 2):
            a = max(low, phase_ns + cycle * period_ns)
            b = min(high, phase_ns + cycle * period_ns + on_ns)
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


def _pwm_on_ns(start_ns, end_ns, phase_ns):
    return sum(
        max(0, min(end_ns, phase_ns + cycle * PERIOD_NS + ON_NS)
            - max(start_ns, phase_ns + cycle * PERIOD_NS))
        for cycle in range((start_ns - phase_ns) // PERIOD_NS - 1,
                           (end_ns - phase_ns) // PERIOD_NS + 2)
    )


def test_scalar_pwm_integral_preserves_scene_time_correlation():
    # Linear 10->30 e/s on [0,2 s]; equal ON duration at different phases
    # produces different charge. Mean brightness times ON duration would fail.
    times = [0, 2_000_000_000]
    assert _pwm_integral(
        [10., 30.], times, 0, 2_000_000_000, 0,
        period_ns=2_000_000_000, on_ns=1_000_000_000,
    ) == pytest.approx(15.)
    assert _pwm_integral(
        [10., 30.], times, 0, 2_000_000_000, 1_000_000_000,
        period_ns=2_000_000_000, on_ns=1_000_000_000,
    ) == (
        pytest.approx(25.)
    )


def _small_native_camera(service, assembly):
    original_lens = next(
        service.registry.get("models", identity) for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "lens"
    )
    # The seed factory Lens is pinned to its 4 ms draft exposure. Re-publish
    # the same PSF evidence at optical-response scope for the variable frames.
    lens = factory.publish(service, {
        "job_id": original_lens["psf_factory_job_id"],
        "base_lens_model_id": original_lens["parents"][0],
        "name": "Moving 3D PWM exposure-independent PSF",
        "revision": "3d-pwm-1", "conditions": assembly["conditions"],
        "condition_scope": "optical_response",
    })
    sensor = next(
        service.registry.get("models", identity) for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    data = {
        key: copy.deepcopy(value) for key, value in sensor.items()
        if key in ModelCreate.model_fields
    }
    data.update(name="32 x 32 authored native trap sensor", revision="3d-pwm-1",
                parents=[sensor["id"]])
    pitch = data["payload"]["pixel_pitch_um"]
    for key in ("active_width", "active_height"):
        data["properties"][key] = {"value": pitch * 32 / 1000, "unit": "mm"}
    small = service.create_model(data)
    fields = {
        key: copy.deepcopy(value) for key, value in assembly.items()
        if key in AssemblyCreate.model_fields
    }
    fields.update(name="Moving 3D PWM native trapped camera", revision="3d-pwm-1",
                  model_ids=[small["id"] if identity == sensor["id"] else
                             lens["id"] if identity == original_lens["id"] else identity
                             for identity in assembly["model_ids"]])
    result = service.create_assembly(fields)
    assert result["compatibility"]["status"] == "PASS"
    return result


def _moving_scene(service, scene_id):
    blocker = scene_render.create_asset(service, {
        **PROVENANCE, "name": "Moving opaque PWM occluder",
        "payload": {
            "kind": "geometry", "unit": "m", "primitive": "triangle_mesh",
            "vertices": [
                [-0.01, -0.1, 1.0], [0.0, -0.1, 1.0],
                [0.0, 0.1, 1.0], [-0.01, 0.1, 1.0],
            ],
            "faces": [[0, 1, 2], [0, 2, 3]],
        },
    })
    matte = scene_render.create_asset(service, {
        **PROVENANCE, "name": "Zero-radiance opaque foreground",
        "payload": {"kind": "material", "bsdf": "diffuse",
                    "spectrum": test_scene_render.spec([0., 0., 0.]),
                    "surface_state": "authored synthetic opaque surface"},
    })
    payload = copy.deepcopy(service.registry.get("scene_assets", scene_id)["payload"])
    payload["clock"] = CLOCK
    payload["instances"].append({
        "geometry_id": blocker["id"], "material_id": matte["id"],
        "to_world": [
            [1., 0., 0., 0.], [0., 1., 0., 0.],
            [0., 0., 1., 0.], [0., 0., 0., 1.],
        ],
        "motion": {"model": "linear_translation_v1", "velocity_m_s": [0.08, 0., 0.]},
    })
    return scene_render.create_asset(service, {
        **PROVENANCE, "name": "Registered moving PWM optical component",
        "payload": payload,
    })


def _ambient(service, assembly_id, source_image):
    # This is an independent authored image-plane component. It is NOT a
    # separately rendered 3D ambient lighting path or inferred from LED RAW.
    metadata = imaging.spectral_template(service, assembly_id)
    metadata.update(
        name="Independent authored sensor-plane ambient",
        revision="ambient-1", source_uri="fixture://independent-ambient-plane",
        method_and_version="authored_static_ideal_image_ambient_v1",
        license="MIT; authored numerical light component",
        sampling=copy.deepcopy(source_image["sampling"]),
    )
    pixels = np.full(source_image["shape"], 1e-5, dtype="<f8")
    stream = io.BytesIO()
    np.save(stream, pixels, allow_pickle=False)
    image = imaging.import_spectral(service, metadata, stream.getvalue())
    assert image["shape"] == source_image["shape"]
    return sequences.create_sequence(service, "scene_sequences", {
        **PROVENANCE, "name": "Independent static ambient optical component",
        "revision": "ambient-1",
        "payload": {
            "schema": "timed_spectral_scene_v1", "clock": CLOCK,
            "interpolation": "linear_irradiance",
            "knots": [{"time_ns": t, "spectral_image_id": image["id"]}
                      for t in (TIMES_NS[0], TIMES_NS[-1])],
        },
    })


def _program(service, assembly_id, control, phase_ns):
    data = capture_programs.template(
        service, assembly_id, mode="trapped_gain_merge",
        control_sequence_id=control["id"],
    )
    data.update(name=f"Shared-charge trapped PWM phase {phase_ns} ns",
                revision=f"phase-{phase_ns}")
    program = data["payload"]
    program["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": CLOCK["clock_id"], "period_ns": PERIOD_NS,
        "on_time_ns": ON_NS, "phase_origin_ns": phase_ns,
        "low": 0, "high": 1,
    }
    program["retained_charge_model"].update(
        transfer_efficiency=[0.5] * 4,
        trap_capacity_e=[1000] * 4,
        capture_rate_s=[5.] * 4,
        release_time_s=[0.01] * 4,
    )
    return capture_programs.create(service, data)


def _check_scalar_chain(service, run, phase_ns):
    arrays = {name: _array(service, run, name) for name in (
        "channel_photoelectron_rate_scenes", "unmodulated_channel_photoelectron_rate_scenes",
        "modulated_channel_electrons", "unmodulated_channel_electrons", "mean_electrons",
        "retained_prnu", "retained_dark_rate_e_s", "retained_delivered_e",
        "retained_residual_e", "retained_trapped_e", "retained_initial_residual_e",
        "retained_initial_trapped_e", "row_start_ns", "row_end_ns",
        "waveform_row_effective_s", "cfa_channel_index", "trap_segment_start_ns",
        "trap_segment_end_ns", "trap_segment_phase", "trap_segment_count", "raw_groups",
    )}
    led = arrays["channel_photoelectron_rate_scenes"]
    ambient = arrays["unmodulated_channel_photoelectron_rate_scenes"]
    assert led.shape[0] == len(TIMES_NS) and ambient.shape[0] == 1
    sites = arrays["cfa_channel_index"]
    selected = [(5, 5), (5, 6), (6, 5), (6, 6)]
    model = run["capture_program_snapshot"]["payload"]
    trap = model["retained_charge_model"]
    readout = model["electrical_model"]
    maximum_step = run["result"]["summary"]["retained_charge"]["trap_integration"][
        "effective_max_step_ns"
    ]
    for row, col in selected:
        site = int(sites[row, col])
        rate_knots = led[:, row, col, site]
        ambient_rate = float(ambient[0, row, col, site])
        prnu = float(arrays["retained_prnu"][row, col])
        dark_rate = float(arrays["retained_dark_rate_e_s"][row, col])
        free = trapped = 0.0
        previous_end = None
        for frame in range(arrays["raw_groups"].shape[0]):
            opening = int(arrays["row_start_ns"][frame, row])
            closing = int(arrays["row_end_ns"][frame, row])
            on_ns = _pwm_on_ns(opening, closing, phase_ns)
            assert arrays["waveform_row_effective_s"][frame, row] == pytest.approx(
                on_ns * 1e-9, abs=1e-18
            )
            led_photo = _pwm_integral(rate_knots, TIMES_NS, opening, closing, phase_ns)
            ambient_photo = ambient_rate * (closing - opening) * 1e-9
            np.testing.assert_allclose(
                arrays["modulated_channel_electrons"][frame, row, col, site],
                led_photo, rtol=2e-11, atol=1e-9,
            )
            np.testing.assert_allclose(
                arrays["unmodulated_channel_electrons"][frame, row, col, site],
                ambient_photo, rtol=2e-11, atol=1e-9,
            )
            np.testing.assert_allclose(
                arrays["mean_electrons"][frame, row, col],
                led_photo + ambient_photo, rtol=2e-11, atol=1e-9,
            )
            cursor = opening if previous_end is None else previous_end
            clear = model["pd_clear_before_exposure"][frame]["clear"]
            exposure_seen = False
            for index in range(int(arrays["trap_segment_count"][frame])):
                start = int(arrays["trap_segment_start_ns"][frame, index, row])
                end = int(arrays["trap_segment_end_ns"][frame, index, row])
                phase = int(arrays["trap_segment_phase"][frame, index])
                if end == start:
                    continue  # Zero-duration row padding in a batched schedule.
                assert start == cursor and end > start
                if phase == 0:
                    assert end <= opening and not exposure_seen
                    photo = 0.0
                else:
                    assert phase == 1 and opening <= start < end <= closing
                    if not exposure_seen:
                        if clear:
                            free = 0.0  # PD clear does not erase trapped charge.
                        exposure_seen = True
                    photo = (_pwm_integral(rate_knots, TIMES_NS, start, end, phase_ns)
                             + ambient_rate * (end - start) * 1e-9)
                    assert not any(start < t < end for t in TIMES_NS)
                    assert not any(
                        start < phase_ns + cycle * PERIOD_NS + edge < end
                        for cycle in range((start - phase_ns) // PERIOD_NS - 1,
                                           (end - phase_ns) // PERIOD_NS + 2)
                        for edge in (0, ON_NS)
                    )
                # The saved interval is a light/scene boundary, not necessarily
                # one reaction step. Recreate the bounded split independently.
                duration_ns = end - start
                remaining_ns = duration_ns
                while remaining_ns:
                    step_ns = min(remaining_ns, maximum_step)
                    dt = step_ns * 1e-9
                    generated = ((photo * prnu) * step_ns / duration_ns
                                 + dark_rate * dt)
                    free = min(float(trap["capacity_e"]), free + generated)
                    released = trapped * -math.expm1(-dt / trap["release_time_s"][site])
                    trapped -= released
                    free = min(float(trap["capacity_e"]), free + released)
                    vacancy = trap["trap_capacity_e"][site] - trapped
                    captured = min(
                        vacancy,
                        free * -math.expm1(-trap["capture_rate_s"][site]
                                            * vacancy / trap["trap_capacity_e"][site] * dt),
                    )
                    free -= captured
                    trapped += captured
                    remaining_ns -= step_ns
                cursor = end
            assert exposure_seen and cursor == closing
            delivered = free * trap["transfer_efficiency"][site]
            free -= delivered
            np.testing.assert_allclose(
                [arrays["retained_delivered_e"][frame, row, col],
                 arrays["retained_residual_e"][frame, row, col],
                 arrays["retained_trapped_e"][frame, row, col]],
                [delivered, free, trapped], rtol=2e-10, atol=2e-8,
            )
            for path_index, path in enumerate(readout["paths"]):
                adc = path["adc"]
                white = 2**adc["bit_depth"] - 1
                slope = (path["conversion_gain_v_per_e"] * path["analog_gain"]
                         * white / adc["full_scale_v"])
                analog = adc["black_level_dn"] + min(
                    delivered, path["input_linear_ceiling_e"]
                ) * slope
                adc_code = min(white, max(0, round(analog)))
                code = min(white, max(0, round(
                    adc["black_level_dn"] + path["digital_gain"]
                    * (adc_code - adc["black_level_dn"])
                )))
                assert int(arrays["raw_groups"][frame, path_index, row, col]) == code
            previous_end = closing
    assert np.any(arrays["retained_initial_residual_e"][1:] > 0)
    assert np.any(arrays["retained_initial_trapped_e"][1:] > 0)
    return arrays


def test_registered_moving_3d_pwm_ambient_trap_paired_raw_isp_and_bundle(tmp_path, monkeypatch):
    if not test_scene_render.PYTHON.exists():
        pytest.skip("Isolated Mitsuba environment is not installed")
    service, original_assembly, request = test_scene_render.scene_workflow.__wrapped__(
        tmp_path, monkeypatch,
    )
    assembly = _small_native_camera(service, original_assembly)
    moving = _moving_scene(service, request["scene_asset_id"])
    receiver = {"crop_origin": [10, 10], "shape": [12, 12]}
    request.update(
        assembly_id=assembly["id"], scene_asset_id=moving["id"],
        shape=receiver["shape"], crop_origin=receiver["crop_origin"],
        render_psf_halo=True, render_clock=CLOCK, samples_per_pixel=2,
    )
    render_jobs = [
        test_scene_render.finish(service, queued)
        for queued in scene_motion_sampling.submit_renders(service, request, TIMES_NS)
    ]
    assert all(job["status"] == "succeeded" for job in render_jobs)
    first_hits = [_array(service, job, "first-hit-object-index") for job in render_jobs]
    assert any(not np.array_equal(first_hits[0], other) for other in first_hits[1:])
    led = scene_motion_sampling.publish_sequence(
        service, [job["id"] for job in render_jobs], name="Moving 3D PWM source knots",
    )
    ambient = _ambient(service, assembly["id"], led["image_snapshots"][0])
    control_data = sequences.control_template(service, assembly["id"], led["id"])
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
        return service.submit_run(payload)

    sdk.post = local_post
    runs = []
    for phase_ns in (0, 350_000):
        program = _program(service, assembly["id"], control, phase_ns)
        test = service.create_test(sequences.test_template(
            service, assembly["id"], led["id"], control["id"], program["id"],
            unmodulated_scene_sequence_id=ambient["id"], capture_crop=receiver,
        ))
        queued = sdk.submit_retained_charge(
            assembly["id"], test["id"], led["id"], control["id"], program["id"],
            seed=917, noise_enabled=False,
            unmodulated_scene_sequence_id=ambient["id"], capture_crop=receiver,
        )
        assert queued["request"]["retained_charge_events"]["capture_crop"] == receiver
        run = test_scene_render.finish(service, queued)
        assert run["status"] == "succeeded"
        summary = run["result"]["summary"]
        assert summary["physical_validation"] == "not_established"
        assert summary["waveform"]["scope"] == "modulated_component_only"
        assert summary["retained_charge"]["trap_integration"]["exact_ctmc"] is False
        assert summary["hdr_merge_applied"] is False
        assert summary["gain_path_merge"]["temporal_hdr_fusion"] is False
        assert summary["readout_ids"] == ["HCG", "LCG"]
        assert summary["geometry"]["crop_origin"] == receiver["crop_origin"]
        assert summary["prediction"]["included_effects"] == test["included_effects"]
        runs.append(run)
    a = _check_scalar_chain(service, runs[0], 0)
    b = _check_scalar_chain(service, runs[1], 350_000)
    assert a["raw_groups"].dtype.kind in "iu" and a["raw_groups"].shape == (4, 2, 12, 12)
    assert not np.array_equal(a["modulated_channel_electrons"],
                              b["modulated_channel_electrons"])
    np.testing.assert_array_equal(a["unmodulated_channel_electrons"],
                                  b["unmodulated_channel_electrons"])
    assert not np.array_equal(a["raw_groups"], b["raw_groups"])
    rgb = _array(service, runs[0], "rgb_groups")
    assert rgb.shape == (4, 2, 12, 12, 3) and np.isfinite(rgb).all()
    assert not np.array_equal(rgb, _array(service, runs[1], "rgb_groups"))
    cold = PlatformService(service.registry.directory)
    for readout_id, index in (("HCG", 0), ("LCG", 1)):
        source = retained_isp_source.resolve(cold, runs[0]["id"], {
            "schema": retained_isp_source.SELECTION_SCHEMA, "readout_id": readout_id,
        })
        assert source["geometry"]["crop_origin"] == receiver["crop_origin"]
        np.testing.assert_array_equal(
            retained_isp_source.load_inputs(cold, source), a["raw_groups"][:, index],
        )
    profile = service.create_model(retained_isp_replay.template(
        service, source_id=runs[0]["id"], readout_id="HCG",
    ))
    replay = test_scene_render.finish(service, retained_isp_replay.submit(service, {
        "schema": retained_isp_replay.SCHEMA, "name": "Moving PWM trapped HCG code ISP",
        "source_kind": "run", "source_id": runs[0]["id"],
        "selector": {"schema": retained_isp_source.SELECTION_SCHEMA, "readout_id": "HCG"},
        "isp_model_id": profile["id"],
    }))
    assert replay["status"] == "succeeded"
    np.testing.assert_array_equal(_array(service, replay, "raw_frames"), a["raw_groups"][:, 0])
    assert _array(service, replay, "rgb_frames").shape == (4, 12, 12, 3)
    for run in runs:
        with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["run"]["id"] == run["id"]
            for name in ("raw_groups.npy", "rgb_groups.npy"):
                artifact = next(a for a in run["artifacts"] if a["name"] == name)
                content = archive.read(f"artifacts/{artifact['sha256']}/{name}")
                assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
    with zipfile.ZipFile(build_bundle(service, "replays", replay["id"])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["replay"]["id"] == replay["id"]
        assert manifest["source"]["id"] == runs[0]["id"]
        for name in ("raw_frames.npy", "rgb_frames.npy"):
            artifact = next(a for a in replay["artifacts"] if a["name"] == name)
            content = archive.read(f"artifacts/{artifact['sha256']}/{name}")
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
