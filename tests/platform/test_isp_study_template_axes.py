"""Templates offer only active stages while keeping the fixed ISP and RAW intact."""

from copy import deepcopy

import pytest
from test_isp_calibration import source as source
from test_isp_studies import prepared as prepared
from test_isp_studies import rows

from camera_platform import isp_studies as studies
from camera_platform.contracts import ModelCreate
from camera_platform.registry import digest


def baseline_with_modes(service, baseline, denoise, sharpen, strength=0.4):
    document = {
        key: deepcopy(value) for key, value in baseline.items() if key in ModelCreate.model_fields
    }
    document["payload"]["raw_denoise"].update(
        mode="calibrated_same_site_filter_v1" if denoise else "bypass",
        strength=strength,
    )
    document["payload"]["linear_sharpen"].update(
        mode="bounded_detail_v1" if sharpen else "bypass", amount=strength
    )
    return service.create_model(document)


@pytest.mark.parametrize(
    "denoise,sharpen", [(False, False), (True, False), (False, True), (True, True)]
)
def test_template_axes_follow_actual_enabled_stages(prepared, denoise, sharpen):
    service, raw, _, calibration, baseline, _ = prepared
    baseline = baseline_with_modes(service, baseline, denoise, sharpen)
    before = rows(service)
    if not denoise and not sharpen:
        with pytest.raises(ValueError, match="Both raw denoise and linear sharpen are bypassed"):
            studies.template(service, "run", raw["source_id"], calibration["id"], baseline["id"])
    else:
        spec = studies.template(service, "run", raw["source_id"], calibration["id"], baseline["id"])
        expected = []
        if denoise:
            expected.append("raw_denoise.strength")
        if sharpen:
            expected.append("linear_sharpen.amount")
        assert [axis["parameter"] for axis in spec["axes"]] == expected
        assert spec["calibration_id"] == calibration["id"]
        assert spec["baseline_isp_model_id"] == baseline["id"]
        assert spec["datasets"][0]["source_id"] == raw["source_id"]
        plan = studies.plan(service, spec)
        candidates = plan["candidates"]
        assert len(candidates) == (3 if denoise else 1) * (2 if sharpen else 1)
        assert sum(candidate["is_baseline"] for candidate in candidates) == 1
        assert (
            sum(
                candidate["profile_sha256"] == digest(baseline["payload"])
                for candidate in candidates
            )
            == 1
        )
    assert rows(service) == before
    assert service.registry.get("models", baseline["id"]) == baseline


def test_baseline_outside_template_grid_is_retained_once(prepared):
    service, raw, _, calibration, baseline, _ = prepared
    baseline = baseline_with_modes(service, baseline, True, False, strength=0.53)
    spec = studies.template(service, "run", raw["source_id"], calibration["id"], baseline["id"])
    plan = studies.plan(service, spec)
    assert len(plan["candidates"]) == 4
    assert sum(c["profile_sha256"] == digest(baseline["payload"]) for c in plan["candidates"]) == 1
    assert service.registry.get("models", baseline["id"]) == baseline


@pytest.mark.parametrize(
    "denoise,sharpen,parameter",
    [
        (False, True, "raw_denoise.strength"),
        (True, False, "linear_sharpen.amount"),
    ],
)
def test_manual_bypassed_axis_is_still_refused_without_writes(
    prepared, denoise, sharpen, parameter
):
    service, raw, _, calibration, baseline, _ = prepared
    baseline = baseline_with_modes(service, baseline, denoise, sharpen)
    spec = studies.template(service, "run", raw["source_id"], calibration["id"], baseline["id"])
    spec["axes"] = [{"parameter": parameter, "values": [0.0, 0.4]}]
    before = rows(service)
    with pytest.raises(ValueError, match="bypassed stage"):
        studies.plan(service, spec)
    assert rows(service) == before


def test_template_rejects_wrong_calibration_without_rebinding(prepared):
    service, raw, _, _, baseline, _ = prepared
    before = rows(service)
    with pytest.raises(ValueError, match="exact calibration"):
        studies.template(service, "run", raw["source_id"], "different-calibration", baseline["id"])
    assert rows(service) == before
