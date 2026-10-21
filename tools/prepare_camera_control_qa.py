#!/usr/bin/env python3
"""Author an isolated synthetic camera/scene for real UI-to-worker control checks.

Never modifies an existing state directory. Model applicability is explicitly
fixture-authored, with new revisions; no physical validity is implied.
"""

import argparse
import json
from copy import deepcopy
from pathlib import Path

from camera_platform import factory, sequences
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import seed
from camera_platform.service import PlatformService


def prepare(state_dir, output):
    state_dir, output = Path(state_dir).resolve(), Path(output).resolve()
    if state_dir.exists():
        raise ValueError("Use a new isolated QA state directory")
    service = PlatformService(state_dir)
    seed(service)
    base = service.registry.list("assemblies")[0]
    models = {}
    for identity in base["model_ids"]:
        model = service.registry.get("models", identity)
        fields = {k: deepcopy(v) for k, v in model.items() if k in ModelCreate.model_fields}
        fields.update(
            name="Authored control QA " + model["kind"],
            revision="control-ui-1",
            parents=[identity],
            source_uri="fixture://camera-control-ui-declared-model",
            method_and_version="authored_behavioral_gain_domain:1",
        )
        fields["valid_domain"]["gain"] = {"min": 1.0, "max": 8.0, "unit": "1"}
        models[model["kind"]] = service.create_model(fields)
    queued = factory.submit(service, factory.template(service, "prysm_psf_v1"))
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    psf = service.registry.finish(claimed["id"], result=service.execute_run(claimed))
    models["lens"] = factory.publish(
        service,
        {
            "job_id": psf["id"],
            "base_lens_model_id": models["lens"]["id"],
            "name": "Authored control QA PSF",
            "revision": "control-ui-1",
            "conditions": base["conditions"],
            "condition_scope": "optical_response",
        },
    )
    fields = {k: deepcopy(v) for k, v in base.items() if k in AssemblyCreate.model_fields}
    fields.update(
        name="AE AWB control QA camera",
        revision="control-ui-1",
        model_ids=[models[k]["id"] for k in ("lens", "sensor", "isp")],
    )
    fields["conditions"]["exposure_s"] = {"value": 0.001, "unit": "s"}
    assembly = service.create_assembly(fields)
    scene = sequences.create_fixture(
        service,
        {
            **sequences.fixture_template(service, assembly["id"]),
            "shape": [16, 24],
            "knot_count": 3,
            "pattern": "static",
            "duration_ns": 320_000_000,
            "irradiance_scale": 2e-5,
        },
    )
    report = {
        "schema": "camera_control_ui_inputs_v1",
        "state_dir": str(state_dir),
        "assembly_id": assembly["id"],
        "scene_sequence_id": scene["id"],
        "models": {k: v["id"] for k, v in models.items()},
        "psf_job_id": psf["id"],
        "physical_validation": "not_established",
        "input_kind": "authored_synthetic_fixture",
        "initial_exposure_ns": 1_000_000,
        "frame_count": 32,
        "shape": [16, 24],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.state_dir, args.output), ensure_ascii=False))
