"""Mixed legacy/nonlinear discovery and documented public request dispatch."""

from test_hdr_workflow import workflow as workflow
from test_nonlinear_workflow import nonlinear_workflow, worker_finish


def test_mixed_raw_listing_retains_legacy_and_labels_each_nonlinear_selection(workflow):
    service, sdk, *_, request = workflow
    linear = worker_finish(service, request)
    runs = {}
    for mode in (
        "independent_bracket_nonlinear",
        "reference_guarded_nonlinear",
        "shared_charge_nonlinear",
    ):
        setup = nonlinear_workflow(workflow, mode)
        runs[mode] = worker_finish(service, setup[-1])
    response = sdk.http.get("/api/v1/replay-sources")
    assert response.status_code == 200, response.text
    rows = response.json()
    legacy = next(r for r in rows if r["source_id"] == linear["id"])
    assert "selector" not in legacy
    assert legacy["raw_format"]["response_type"] == "linear"
    for mode, run in runs.items():
        choices = [r for r in rows if r["source_id"] == run["id"]]
        assert len(choices) == (2 if mode == "shared_charge_nonlinear" else 1)
        for choice in choices:
            assert choice["replay_profile_version"] == 3
            assert choice["raw_shape"] == [4, 16, 16]
            assert choice["raw_format"]["response_type"] == "nonlinear_charge_voltage"
        if mode == "shared_charge_nonlinear":
            assert {r["selector"]["readout_id"] for r in choices} == {"hcg", "lcg"}
            assert len({r["raw_sha256"] for r in choices}) == 1
            for choice in choices:
                index = 0 if choice["selector"]["readout_id"] == "hcg" else 1
                assert choice["raw_format"] == run["result"]["summary"]["raw_formats"][index]
                assert choice["name"].endswith("DCG · " + choice["selector"]["readout_id"].upper())
        else:
            suffix = "순차 HDR" if mode == "independent_bracket_nonlinear" else "기준 노출 HDR"
            assert choices[0]["name"].endswith(suffix)


def test_openapi_documents_versioned_replay_without_changing_legacy_request(workflow):
    _, sdk, *_ = workflow
    schema = sdk.http.get("/openapi.json").json()
    body = schema["paths"]["/api/v1/replays"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    # OpenAPI qualifies duplicate Python class names once a separately versioned
    # retained-code ReplayCreate joins the union. Test the wire contracts rather
    # than generated component names, while preserving both previous variants.
    variants = [
        schema["components"]["schemas"][row["$ref"].rsplit("/", 1)[1]]
        for row in body["anyOf"]
    ]
    assert len(variants) == 3
    legacy_variants = [v for v in variants if "schema" not in v["properties"]]
    assert len(legacy_variants) == 1
    legacy = legacy_variants[0]["properties"]
    assert set(legacy) == {"name", "source_kind", "source_id", "isp_model_id", "force"}
    versioned = {
        v["properties"]["schema"]["const"]: v for v in variants if "schema" in v["properties"]
    }
    assert set(versioned) == {"camera_isp_replay_v3", "camera_retained_code_isp_replay_v1"}
    new = versioned["camera_isp_replay_v3"]["properties"]
    assert new["schema"]["const"] == "camera_isp_replay_v3"
    assert "selector" in new
    retained = versioned["camera_retained_code_isp_replay_v1"]
    assert set(retained["properties"]) == set(legacy) | {"schema", "selector"}
    assert retained["properties"]["source_kind"]["const"] == "run"
    assert {"schema", "selector", "source_id", "isp_model_id"} <= set(retained["required"])
