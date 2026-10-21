"""Independent PWM quadrature oracle for nested registered dual-Light 3D grids."""

from __future__ import annotations

import copy
import io
import json

import numpy as np
import pytest
import test_scene_render
from test_scene_light_components import CLOCK, _setup
from test_scene_render import scene_workflow as scene_workflow  # noqa: F401

from camera_platform import scene_motion_sampling, scene_render
from camera_platform.demo import PROVENANCE
from camera_platform.service import PlatformService

TIMES = [0, 20_000_000, 40_000_000]
PWM = {
    "schema": "camera_light_waveform_v1", "kind": "pwm",
    "clock_id": CLOCK["clock_id"], "period_ns": 11_000_000,
    "on_time_ns": 4_000_000, "phase_origin_ns": 1_500_000,
    "low": 0.1, "high": 1.25,
}


def _irradiance(service, job):
    record = next(a for a in job["artifacts"] if a["name"] == "spectral-image.npy")
    return np.load(io.BytesIO(service.registry.blob_path(
        record["sha256"]).read_bytes()), allow_pickle=False)


def _manual_integral(times, values, row, start, end, waveform=None):
    """Enumerate every PWM edge, then independently sum linear trapezoids."""
    result = np.zeros_like(values[0][row], dtype=np.float64)
    for index, (left, right) in enumerate(zip(times, times[1:], strict=False)):
        lower, upper = max(start, left), min(end, right)
        if upper <= lower:
            continue
        edges = {lower, upper}
        if waveform is not None:
            phase = waveform["phase_origin_ns"]
            period = waveform["period_ns"]
            on = waveform["on_time_ns"]
            for cycle in range((lower - phase) // period - 1,
                               (upper - phase) // period + 3):
                for edge in (phase + cycle * period, phase + cycle * period + on):
                    if lower < edge < upper:
                        edges.add(edge)
        for first, last in zip(sorted(edges), sorted(edges)[1:], strict=False):
            fraction_first = (first - left) / (right - left)
            fraction_last = (last - left) / (right - left)
            value_first = (values[index][row] * (1 - fraction_first)
                           + values[index + 1][row] * fraction_first)
            value_last = (values[index][row] * (1 - fraction_last)
                          + values[index + 1][row] * fraction_last)
            level = 1.0
            if waveform is not None:
                midpoint = (first + last) / 2
                high = (midpoint - waveform["phase_origin_ns"]) % waveform[
                    "period_ns"] < waveform["on_time_ns"]
                level = waveform["high"] if high else waveform["low"]
            result += (value_first + value_last) * (last - first) * 0.5e-9 * level
    return result


def _assert_metrics(record, coarse, fine):
    delta = np.abs(fine - coarse)
    reference = float(np.sum(np.abs(fine)))
    assert record["coarse_sample_sum"] == pytest.approx(float(np.sum(coarse)), rel=2e-12)
    assert record["fine_sample_sum"] == pytest.approx(float(np.sum(fine)), rel=2e-12)
    assert record["absolute_l1_change"] == pytest.approx(float(np.sum(delta)), rel=2e-12)
    assert record["maximum_absolute_change"] == pytest.approx(float(np.max(delta)), rel=2e-12)
    if reference:
        assert record["relative_l1_change"] == pytest.approx(
            float(np.sum(delta) / reference), rel=2e-12,
        )
    else:
        assert record["relative_l1_change"] is None


def test_registered_dual_3d_pwm_row_refinement_and_independent_scalar_oracle(
    scene_workflow,
):
    service, assembly, request = scene_workflow
    scene, lights, base = _setup(service, assembly, request)
    ids = [light["id"] for light in lights]
    jobs = {identity: [] for identity in ids}
    for time_ns in TIMES:
        for identity in ids:
            queued = scene_render.submit(service, {
                **base, "light_component_id": identity,
                "render_time_ns": time_ns, "name": f"refine {identity} at {time_ns}",
            })
            jobs[identity].append(test_scene_render.finish(service, queued))
    seq = {}
    for level, indices in (("coarse", (0, 2)), ("fine", (0, 1, 2))):
        for identity in ids:
            seq[level, identity] = scene_motion_sampling.publish_sequence(
                service, [jobs[identity][index]["id"] for index in indices],
                name=f"{level} real 3D {identity}",
            )
    ordered = [seq[level, identity]["id"]
               for level in ("coarse", "fine") for identity in ids]
    first_request = jobs[ids[0]][0]["request"]
    origin = first_request["sampling"]["crop_origin"]
    windows = [
        {"row": origin[0] + 2, "start_ns": 0, "end_ns": 40_000_000},
        {"row": origin[0] + 5, "start_ns": 4_000_000, "end_ns": 32_000_000},
    ]
    diagnostic = scene_motion_sampling.compare_dual_light_refinement(
        service, *ordered, waveform=PWM, row_windows_ns=windows,
    )
    assert diagnostic["schema"] == "registered_3d_dual_light_pwm_refinement_v1"
    assert diagnostic["scene_asset_id"] == scene["id"]
    assert diagnostic["light_asset_ids"] == ids
    assert diagnostic["coarse_times_ns"] == [TIMES[0], TIMES[-1]]
    assert diagnostic["fine_times_ns"] == TIMES
    assert "not a continuous-motion" in diagnostic["interpretation"]
    assert diagnostic["physical_validation"] == "not_established"
    assert len(diagnostic["component_full_frame_checks"]) == 2
    data = {identity: [_irradiance(service, job) for job in jobs[identity]]
            for identity in ids}
    for spec, measured in zip(windows, diagnostic["row_windows"], strict=True):
        row = spec["row"] - origin[0]
        start, end = spec["start_ns"], spec["end_ns"]
        assert measured["row_native"] == spec["row"]
        computed = {}
        for level, indices in (("coarse", (0, 2)), ("fine", (0, 1, 2))):
            times = [TIMES[index] for index in indices]
            mod = [data[ids[0]][index] for index in indices]
            amb = [data[ids[1]][index] for index in indices]
            computed[level] = {
                "modulated_component_ungated": _manual_integral(
                    times, mod, row, start, end,
                ),
                "ambient_component": _manual_integral(
                    times, amb, row, start, end,
                ),
                "modulated_component_pwm_gated": _manual_integral(
                    times, mod, row, start, end, PWM,
                ),
            }
            computed[level]["combined_pwm_plus_ambient"] = (
                computed[level]["modulated_component_pwm_gated"]
                + computed[level]["ambient_component"]
            )
        for key in computed["coarse"]:
            _assert_metrics(measured[key], computed["coarse"][key], computed["fine"][key])
    assert any(item["combined_pwm_plus_ambient"]["absolute_l1_change"] > 0
               for item in diagnostic["row_windows"])
    cold = PlatformService(service.registry.directory)
    assert scene_motion_sampling.compare_dual_light_refinement(
        cold, *ordered, waveform=PWM, row_windows_ns=windows,
    ) == diagnostic
    shifted = scene_motion_sampling.compare_dual_light_refinement(
        cold, *ordered,
        waveform={**PWM, "phase_origin_ns": PWM["phase_origin_ns"] + 2_000_000},
        row_windows_ns=windows,
    )
    assert shifted["diagnostic_sha256"] != diagnostic["diagnostic_sha256"]
    assert shifted["row_windows"][0]["combined_pwm_plus_ambient"] != (
        diagnostic["row_windows"][0]["combined_pwm_plus_ambient"]
    )
    with pytest.raises(ValueError, match="matching authored PWM clock"):
        scene_motion_sampling.compare_dual_light_refinement(
            cold, *ordered, waveform={**PWM, "clock_id": "other-clock"},
            row_windows_ns=windows,
        )
    with pytest.raises(ValueError, match="Native row window"):
        scene_motion_sampling.compare_dual_light_refinement(
            cold, *ordered, waveform=PWM,
            row_windows_ns=[{**windows[0], "row": True}],
        )
    with pytest.raises(ValueError, match="same two frozen Light assets"):
        scene_motion_sampling.compare_dual_light_refinement(
            cold, ordered[0], ordered[1], ordered[3], ordered[2],
            waveform=PWM, row_windows_ns=windows,
        )
    with pytest.raises(ValueError, match="strictly refine"):
        scene_motion_sampling.compare_dual_light_refinement(
            cold, ordered[2], ordered[3], ordered[0], ordered[1],
            waveform=PWM, row_windows_ns=windows,
        )
    # A newly rendered endpoint is not the same immutable coarse sample, even
    # when the geometry, time and numerical output happen to match.
    new_endpoint = test_scene_render.finish(service, scene_render.submit(service, {
        **base, "light_component_id": ids[0], "render_time_ns": TIMES[0],
        "name": "same time but different provenance", "force": True,
    }))
    replacement = scene_motion_sampling.publish_sequence(
        service, [new_endpoint["id"], jobs[ids[0]][1]["id"], jobs[ids[0]][2]["id"]],
        name="Disjoint endpoint provenance",
    )
    with pytest.raises(ValueError, match="exact coarse render jobs"):
        scene_motion_sampling.compare_dual_light_refinement(
            service, ordered[0], ordered[1], replacement["id"], ordered[3],
            waveform=PWM, row_windows_ns=windows,
        )
    # A different 3D Scene may carry the same two Light assets and clock but
    # has different geometry; never mix it into the paired component evidence.
    other_payload = copy.deepcopy(scene["payload"])
    other_payload["instances"][2]["motion"]["velocity_m_s"] = [0.3, 0., 0.]
    other_scene = scene_render.create_asset(service, {
        **PROVENANCE, "name": "Different moving geometry with same Light assets",
        "payload": other_payload,
    })
    assert other_scene["illumination_id"] == scene["illumination_id"]
    other_jobs = [test_scene_render.finish(service, scene_render.submit(service, {
        **base, "scene_asset_id": other_scene["id"],
        "light_component_id": ids[0], "render_time_ns": time_ns,
        "name": f"disjoint scene {time_ns}",
    })) for time_ns in (TIMES[0], TIMES[-1])]
    other_sequence = scene_motion_sampling.publish_sequence(
        service, [job["id"] for job in other_jobs], name="Different 3D scene",
    )
    with pytest.raises(ValueError, match="one frozen scene, camera and time"):
        scene_motion_sampling.compare_dual_light_refinement(
            service, other_sequence["id"], ordered[1], ordered[2], ordered[3],
            waveform=PWM, row_windows_ns=windows,
        )
    # A poisoned content-addressed source is refused on a cold read.
    source = jobs[ids[0]][1]
    artifact = next(a for a in source["artifacts"] if a["name"] == "spectral-image.npy")
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"tampered source")
    with pytest.raises(ValueError):
        scene_motion_sampling.compare_dual_light_refinement(
            PlatformService(service.registry.directory), *ordered,
            waveform=PWM, row_windows_ns=windows,
        )
    json.dumps(diagnostic, allow_nan=False)
