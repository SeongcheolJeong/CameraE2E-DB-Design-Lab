"""The browser form must submit the live frequency-study contract faithfully."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_sensor_frequency_target_job import (
    default_spec,
    specification,
)

STATIC = Path(__file__).resolve().parents[2] / "src" / "camera_platform" / "static"
SCRIPT = STATIC / "frequency-target.js"


def _input(*, mode: str = "adaptive", refinements: str = "2") -> dict[str, object]:
    return {
        "name": "격자 검증", "lensId": "12345678-1234-1234-1234-123456789abc",
        "sensorId": "DEP-001", "sensorRun": "a" * 64, "modeId": "declared-mode-1",
        "roiR0": "64", "roiR1": "96", "roiC0": "128", "roiC1": "160",
        "sceneFov": "2", "edgeOffset": "0", "sceneLuminance": "1000",
        "exposureMs": "4", "samplingMode": mode, "refinements": refinements,
        "sceneRows": "2048", "sceneCols": "2048", "targetFraction": "0.9",
        "samplesPerPeriod": "4", "evaluationFractions": "0.25, 0.5, 0.75, 0.9",
        "meanTolerance": "0.01", "shiftTolerance": "0.02", "mtfTolerance": "0.02",
        "shiftFraction": "0.25", "contrastFloor": "0.05", "tileBudgetMiB": "512",
        "psfMode": "stored_geometric_only", "psfPitch": "", "aliasGridSeed": "",
        "sensorTileRows": "16", "sensorTileCols": "16", "sourceTileRows": "64",
        "sourceTileCols": "64", "readoutChunk": "16", "ack": True,
    }


def _node(script: str, payload: object) -> object:
    if shutil.which("node") is None:
        pytest.skip("Node.js is needed for browser-form contract checks.")
    result = subprocess.run(
        ["node", "-e", script, str(SCRIPT)],
        input=json.dumps(payload), text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def test_form_builds_exact_live_adaptive_and_explicit_contracts() -> None:
    node_script = """
const fs = require('node:fs');
const ui = require(process.argv[1]);
const {template, inputs} = JSON.parse(fs.readFileSync(0, 'utf8'));
const made = inputs.map(input => ui.frequencyTargetBuildSpecification(template, input));
console.log(JSON.stringify(made));
"""
    larger_budget = _input(mode="explicit", refinements="0")
    larger_budget["tileBudgetMiB"] = "6144"
    made = _node(node_script, {
        "template": default_spec(),
        "inputs": [_input(), larger_budget],
    })
    assert len(made) == 2
    for item in made:
        accepted = specification(item)
        assert accepted["registered_roi"]["capture"]["noise_flag"] == 0
        assert accepted["sampling_goal"]["scene_rasterization"] == "cell_area_slanted_edge"
        assert (
            accepted["sampling_goal"]["mean_electron_full_scale_normalized_max_delta_tolerance"]
            == 0.01
        )
    assert made[0]["sampling_goal"]["mode"] == "adaptive"
    assert made[1]["sampling_goal"]["max_refinements"] == 0
    assert made[1]["sampling_goal"]["optical_tile_estimated_working_budget_bytes"] == (
        6 * 1024**3
    )


def test_ui_rejects_out_of_goal_frequency_and_physical_claim() -> None:
    node_script = """
const fs = require('node:fs');
const ui = require(process.argv[1]);
const {template, input} = JSON.parse(fs.readFileSync(0, 'utf8'));
const outcomes = [];
input.evaluationFractions = '0.25, 1';
try {
  ui.frequencyTargetBuildSpecification(template, input);
  outcomes.push('accepted bad frequency');
}
catch { outcomes.push('rejected bad frequency'); }
const job = {job_kind:'lens_sensor_frequency_target',status:'succeeded',result:{summary:{
  schema:'camerae2e_lens_sensor_frequency_target_study_v1',
  status:'inconclusive_independent_psf_source_resolution',
  execution:{status:'numeric_joint_scene_oi_grid_discrete_frequency_targets_converged_model_only'},
  physical_accuracy_qualified:false,physical_mtf_comparison_allowed:false,product_ranking_allowed:false
}}};
const facts = ui.frequencyTargetResultFacts(job);
outcomes.push(
  facts.numeric === false && facts.executionNumericallyConverged === true
    ? 'separate numeric and source status' : 'mixed numeric status'
);
job.result.summary.physical_accuracy_qualified = true;
try { ui.frequencyTargetResultFacts(job); outcomes.push('accepted physical claim'); }
catch { outcomes.push('rejected physical claim'); }
console.log(JSON.stringify(outcomes));
"""
    assert _node(node_script, {"template": default_spec(), "input": _input()}) == [
        "rejected bad frequency",
        "separate numeric and source status",
        "rejected physical claim",
    ]


def test_form_post_uses_current_session_token_and_server_enforces_it() -> None:
    node_script = """
const fs = require('node:fs');
const ui = require(process.argv[1]);
const {payload} = JSON.parse(fs.readFileSync(0, 'utf8'));
const init = ui.frequencyTargetRequestInit(payload, 'session-token');
let missingTokenRejected = false;
try { ui.frequencyTargetRequestInit(payload, ''); }
catch { missingTokenRejected = true; }
console.log(JSON.stringify({init, missingTokenRejected}));
"""
    result = _node(node_script, {"payload": {"schema": "invalid"}})
    assert result["init"]["method"] == "POST"
    assert result["init"]["headers"]["X-Platform-Token"] == "session-token"
    assert json.loads(result["init"]["body"]) == {"schema": "invalid"}
    assert result["missingTokenRejected"] is True

    with TemporaryDirectory() as state_dir:
        app = create_app(
            state_dir=Path(state_dir), workspace=Path(__file__).resolve().parents[2],
            start_worker=False, seed_demo=False,
        )
        client = TestClient(app)
        session = client.get("/api/v1/session")
        assert session.status_code == 200
        assert session.json()["schema"] == "camera_platform_v1"
        token = session.json()["token"]
        route = "/api/v1/lens-sensor-frequency-target"
        denied = client.post(route, json={"schema": "invalid"})
        assert denied.status_code == 403
        admitted = client.post(
            route, json={"schema": "invalid"},
            headers={"X-Platform-Token": token},
        )
        assert admitted.status_code != 403
        assert admitted.status_code >= 400


def test_frequency_page_has_every_form_control_used_by_the_script() -> None:
    html = (STATIC / "frequency-target.html").read_text()
    script = SCRIPT.read_text()
    ids = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', html))
    referenced = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", script))
    assert referenced <= ids
    assert "frequency-target.js" in html
    assert "물리 정확도·제품 순위 미인증" in script
