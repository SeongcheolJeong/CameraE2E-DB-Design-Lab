"""Convenience methods preserve explicit field radiometry without a network call."""

from camera_platform.sdk import PlatformClient


def _capturing_client():
    client = object.__new__(PlatformClient)
    calls = []

    def submit(assembly_id, test_id, **kwargs):
        calls.append((assembly_id, test_id, kwargs))
        return kwargs

    client.submit = submit
    return client, calls


def test_temporal_convenience_mode_is_explicit_and_default_request_is_unchanged():
    client, calls = _capturing_client()
    default = client.submit_temporal("assembly", "test", "scene", "control")
    assert default["parameters"] == {"seed": 42}
    selected = client.submit_temporal(
        "assembly", "test", "scene", "control",
        seed=19, field_radiometry_mode="source_position_ri_v1",
    )
    assert selected["plugin_id"] == "temporal_scene_v1"
    assert selected["parameters"] == {
        "seed": 19, "field_radiometry_mode": "source_position_ri_v1",
    }
    assert len(calls) == 2


def test_retained_convenience_forwards_mode_with_dual_light_and_receiver_crop():
    client, calls = _capturing_client()
    default = client.submit_retained_charge(
        "assembly", "test", "scene", "control", "program",
    )
    assert default["parameters"] == {"seed": 42, "noise_enabled": True}
    selected = client.submit_retained_charge(
        "assembly", "test", "scene", "control", "program",
        seed=23, noise_enabled=False, unmodulated_scene_sequence_id="ambient",
        capture_crop={"crop_origin": [4, 4], "shape": [8, 8]},
        field_radiometry_mode="source_position_ri_v1",
    )
    assert selected["plugin_id"] == "retained_charge_v1"
    assert selected["parameters"] == {
        "seed": 23, "noise_enabled": False,
        "unmodulated_scene_sequence_id": "ambient",
        "capture_crop": {"crop_origin": [4, 4], "shape": [8, 8]},
        "field_radiometry_mode": "source_position_ri_v1",
    }
    assert len(calls) == 2


def test_hdr_and_dcg_convenience_forward_only_explicit_mode():
    client, calls = _capturing_client()
    hdr_default = client.submit_hdr("assembly", "test", "scene", "control", "program")
    assert hdr_default["parameters"] == {"seed": 42}
    hdr_selected = client.submit_hdr(
        "assembly", "test", "scene", "control", "program",
        seed=17, unmodulated_scene_sequence_id="ambient",
        field_radiometry_mode="source_position_ri_v1",
    )
    assert hdr_selected["plugin_id"] == "hdr_bracket_v1"
    assert hdr_selected["parameters"] == {
        "seed": 17, "unmodulated_scene_sequence_id": "ambient",
        "field_radiometry_mode": "source_position_ri_v1",
    }
    dcg_default = client.submit_dcg("assembly", "test", "scene", "control", "program")
    assert dcg_default["parameters"] == {"seed": 42}
    dcg_selected = client.submit_dcg(
        "assembly", "test", "scene", "control", "program",
        field_radiometry_mode="source_position_ri_v1",
    )
    assert dcg_selected["plugin_id"] == "shared_charge_dcg_v1"
    assert dcg_selected["parameters"] == {
        "seed": 42, "field_radiometry_mode": "source_position_ri_v1",
    }
    assert len(calls) == 4
