"""Independent source-boundary review cases; completed-registry reads are isolated.

The monkeypatched read models internally self-consistent result metadata. It is
not a bypass of the registry's completed-result audit hash: these cases check
whether the new source resolver separately binds metadata to original inputs.
"""

from copy import deepcopy

import pytest
from test_hdr_workflow import workflow as workflow
from test_isp_charge_source import acquisition, selector

from camera_platform import isp_charge_source as sources
from camera_platform.registry import IntegrityError, canonical, digest


@pytest.mark.parametrize("mode", ["independent_bracket_nonlinear", "shared_charge_nonlinear"])
@pytest.mark.parametrize(
    "field", ["pitch", "crop_origin", "clock", "frame_timing", "record_timing"]
)
def test_self_consistent_summary_metadata_is_bound_to_original_inputs(
    workflow, monkeypatch, field, mode
):
    service, _, run = acquisition(workflow, mode)
    changed = deepcopy(run)
    summary = changed["result"]["summary"]
    if field == "pitch":
        summary["pixel_pitch_um"] *= 2
    elif field == "crop_origin":
        summary["geometry"]["crop_origin"] = [1, 1]
        for cal in summary["calibrations"]:
            cal["acquisition"]["geometry"]["crop_origin"] = [1, 1]
            cal["acquisition_sha256"] = digest(cal["acquisition"])
            for path in cal.get("paths", []):
                path["acquisition"]["geometry"]["crop_origin"] = [1, 1]
                path["acquisition_sha256"] = digest(path["acquisition"])
    elif field == "clock":
        summary["clock"]["clock_id"] = "unrecorded-clock"
    elif field == "frame_timing":
        summary["timing"]["frames"][0]["frame_id"] = "unrecorded-frame"
    else:
        key = "acquisitions" if mode == "shared_charge_nonlinear" else "frames"
        summary[key][0]["timing"]["frame_id"] = "unrecorded-frame"
    replacement = service.registry.store_blob(
        canonical(summary).encode(), "summary.json", "application/json"
    )
    changed["artifacts"] = [
        replacement if item["name"] == "summary.json" else item for item in changed["artifacts"]
    ]
    changed["result"].update(deepcopy(summary))
    changed["result"]["artifacts"] = changed["artifacts"]
    original = service.registry.get_run
    monkeypatch.setattr(
        service.registry,
        "get_run",
        lambda identity: deepcopy(changed) if identity == run["id"] else original(identity),
    )
    with pytest.raises((IntegrityError, ValueError)):
        sources.resolve(
            service,
            "run",
            run["id"],
            selector("hcg") if mode == "shared_charge_nonlinear" else selector(),
        )
