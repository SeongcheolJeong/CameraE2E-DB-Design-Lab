"""Inline artifact bytes are charged per cell even when graph inputs deduplicate."""

import hashlib
import json
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest
from test_isp_calibration import source as source
from test_isp_studies import acquire, complete, rows
from test_isp_studies import prepared as prepared

from camera_platform import isp_calibration, isp_replay_v2
from camera_platform import isp_studies as studies
from camera_platform.registry import IntegrityError, canonical, digest


def replay_for(prepared):
    service, raw, _, _, baseline, _ = prepared
    return isp_replay_v2.prepare(
        service,
        {
            "name": "summary byte budget",
            "source_kind": "run",
            "source_id": raw["source_id"],
            "isp_model_id": baseline["id"],
        },
    )


@pytest.mark.parametrize(
    "value",
    [
        {"unicode": "교정🔬", "escapes": '"\\\n\t\x00', "float": -1.25e-100},
        {"huge_integer": 2**256, "nested": [False, None, {"x": 1.0}]},
    ],
)
def test_streamed_size_is_independent_canonical_utf8_byte_count(value):
    expected = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    assert studies._json_bytes(value) == len(expected)


def test_large_escaped_calibration_is_counted_exactly_once_per_cell(prepared):
    replay = replay_for(prepared)
    before = studies._summary_forecast(replay)
    old_bytes = len(canonical(replay["calibration_snapshot"]).encode())
    replay["calibration_snapshot"] = deepcopy(replay["calibration_snapshot"])
    # Adversarial provenance, including multibyte UTF-8 and JSON escape expansion.
    replay["calibration_snapshot"]["retained_model_notes"] = ['校正"\\\n' * 250_000] * 2
    new_bytes = len(canonical(replay["calibration_snapshot"]).encode())
    after = studies._summary_forecast(replay)
    assert new_bytes > 5 * 1024 * 1024
    assert after["summary_artifact_bytes_limit"] - before["summary_artifact_bytes_limit"] == (
        new_bytes - old_bytes
    )
    assert after["generated_summary_bytes_limit"] == before["generated_summary_bytes_limit"]


def test_sequence_metadata_and_conditions_are_not_hidden_in_fixed_allowance(prepared):
    replay = replay_for(prepared)
    source = replay["source"]
    before = studies._summary_forecast(replay)
    old_conditions_bytes = len(canonical(source["conditions"]).encode())
    source["conditions"] = {"context": '反復"\\\n' * 100_000}
    after = studies._summary_forecast(replay)
    assert after["copied_summary_bytes"] - before["copied_summary_bytes"] == 2 * (
        len(canonical(source["conditions"]).encode()) - old_conditions_bytes
    )
    source.update(
        frame_records=[{"frame_id": "frame-" + str(i)} for i in range(16)],
        frame_conditions=[{"context": "long" * 10_000}] * 16,
        clock={"description": "clock" * 10_000},
        control_trace={"trace": "trace" * 100_000},
        response_acquisition={"notes": "response" * 100_000},
    )
    before_snapshot = deepcopy(replay)
    with_sequence = studies._summary_forecast(replay)
    minimum_extra = sum(
        len(canonical(source[k]).encode())
        for k in ("frame_conditions", "clock", "control_trace", "response_acquisition")
    )
    assert with_sequence["copied_summary_bytes"] - after["copied_summary_bytes"] > minimum_extra
    assert replay == before_snapshot


@pytest.mark.parametrize("frames", [4, 16])
def test_actual_replay_summary_fits_declared_generated_metadata_contract(prepared, frames):
    service, raw, _, _, _, _ = prepared
    if frames == 4:
        replay = replay_for(prepared)
    else:
        raw, _ = acquire(service, raw["source_snapshot"]["assembly_id"], seed=78, frames=frames)
        calibration = isp_calibration.import_calibration(
            service, isp_calibration.template(service, "run", raw["source_id"])
        )
        model = service.create_model(
            isp_replay_v2.template(
                service,
                source_kind="run",
                source_id=raw["source_id"],
                calibration_id=calibration["id"],
            )
        )
        replay = isp_replay_v2.prepare(
            service,
            {
                "name": "maximum frame summary",
                "source_kind": "run",
                "source_id": raw["source_id"],
                "isp_model_id": model["id"],
            },
        )
    forecast = studies._summary_forecast(replay)
    result = isp_replay_v2.execute(service, replay)
    artifact = next(a for a in result["artifacts"] if a["name"] == "summary.json")
    actual = service.registry.blob_path(artifact["sha256"]).read_bytes()
    assert len(actual) == artifact["bytes"] == len(canonical(result["summary"]).encode())
    assert (
        0
        < len(actual) - forecast["copied_summary_bytes"]
        < (forecast["generated_summary_bytes_limit"])
    )
    studies._verify_summary_forecast({"evidence_forecast": forecast}, result)


def test_four_actual_cells_bundle_fits_plan_and_planning_writes_nothing(prepared):
    service, *_, spec = prepared
    spec["axes"][0]["values"] = [0.2, 0.4, 0.8]
    before = rows(service)
    blobs = sorted(service.registry.blobs.rglob("*"))
    plan, _, requests = studies._prepare(service, spec)
    assert rows(service) == before
    assert sorted(service.registry.blobs.rglob("*")) == blobs
    assert plan["job_count"] == len(requests) == 4
    assert plan["plan_hash_basis"]["evidence_forecast_version"] == studies.SUMMARY_FORECAST_VERSION
    assert plan["resources"]["estimated_summary_artifact_bytes"] == sum(
        request["evidence_forecast"]["summary_artifact_bytes_limit"] for request in requests
    )
    assert all(
        digest(request) == cell["request_sha256"]
        for request, cell in zip(requests, plan["cells"], strict=True)
    )
    study = studies.submit(service, {"specification": spec, "plan_hash": plan["plan_hash"]})
    assert complete(service, study)["coverage"]["succeeded"] == 4
    with zipfile.ZipFile(studies.export_bundle(service, study["id"])) as archive:
        physical_bytes = sum(info.file_size for info in archive.infolist())
    assert physical_bytes <= plan["resources"]["estimated_bundle_bytes"] < 256 * 1024 * 1024


def test_wider_grid_over_budget_is_refused_before_model_job_or_audit_writes(prepared, monkeypatch):
    service, *_, spec = prepared
    narrow = studies.plan(service, spec)
    spec["axes"][0]["values"] = [0.2, 0.4, 0.8]
    wide = studies.plan(service, spec)
    # This threshold would admit the old formula, which omitted all inline summaries.
    cap = wide["resources"]["estimated_bundle_bytes"] - (
        wide["resources"]["estimated_summary_artifact_bytes"] // 2
    )
    assert narrow["resources"]["estimated_bundle_bytes"] < cap
    before, blobs = rows(service), sorted(service.registry.blobs.rglob("*"))
    monkeypatch.setattr(studies, "MAX_ARCHIVE_BYTES", cap)
    with pytest.raises(ValueError, match="uncompressed evidence exceeds"):
        studies.plan(service, spec)
    assert rows(service) == before
    assert sorted(service.registry.blobs.rglob("*")) == blobs
    assert spec["axes"][0]["values"] == [0.2, 0.4, 0.8]


def test_large_shared_snapshot_exceeds_real_256mib_cap_without_graph_or_publication(
    prepared, monkeypatch
):
    service, *_, spec = prepared
    spec["axes"] = [
        {"parameter": "raw_denoise.strength", "values": [0, 0.2, 0.4, 0.6, 0.8, 1]},
        {"parameter": "linear_sharpen.amount", "values": [0, 0.3, 0.6, 1]},
    ]
    # Inject a shared provenance payload at the prepared-replay boundary. This
    # isolates the size admission contract from calibration semantic validation;
    # no rendering or fake output artifacts are involved. JSON repeats it 24 times.
    large_notes = {"retained_model_notes": "較" * (4 * 1024 * 1024)}
    original = isp_replay_v2.prepare

    def large_replay(*args, **kwargs):
        result = original(*args, **kwargs)
        result["calibration_snapshot"] = {**result["calibration_snapshot"], **large_notes}
        return result

    monkeypatch.setattr(isp_replay_v2, "prepare", large_replay)
    from camera_platform import evidence_package

    def unexpected_graph(*args, **kwargs):
        pytest.fail("Oversize inline artifacts must fail before constructing the full graph")

    monkeypatch.setattr(evidence_package, "graph", unexpected_graph)
    assert studies.MAX_ARCHIVE_BYTES == 268_435_456
    before, blobs = rows(service), sorted(service.registry.blobs.rglob("*"))
    with pytest.raises(ValueError, match="uncompressed evidence exceeds"):
        studies.plan(service, spec)
    assert rows(service) == before
    assert sorted(service.registry.blobs.rglob("*")) == blobs
    assert [len(axis["values"]) for axis in spec["axes"]] == [6, 4]


def test_runtime_guard_exact_boundary_unknown_version_and_legacy_are_explicit():
    job = {
        "evidence_forecast": {
            "version": studies.SUMMARY_FORECAST_VERSION,
            "summary_artifact_bytes_limit": 100,
        }
    }
    result = {"artifacts": [{"name": "summary.json", "bytes": 100}]}
    studies._verify_summary_forecast(job, result)
    result["artifacts"][0]["bytes"] += 1
    with pytest.raises(IntegrityError, match="declared preflight limit"):
        studies._verify_summary_forecast(job, result)
    job["evidence_forecast"]["version"] = "unsupported"
    with pytest.raises(IntegrityError, match="forecast version"):
        studies._verify_summary_forecast(job, result)
    # No new forecast field is injected into old frozen requests or output.
    legacy = {"implementation": "preserved-dev9", "request_sha256": "0" * 64}
    legacy_digest = digest(legacy)
    studies._verify_summary_forecast(legacy, result)
    assert digest(legacy) == legacy_digest and "evidence_forecast" not in legacy


def test_preserved_actual_large_calibration_summaries_fit_new_forecast():
    folder = (
        Path(__file__).resolve().parents[2]
        / "reports/camera_platform/evidence-scaling-v016/http-final"
    )
    study_file, bundle_file = folder / "study.json", folder / "study-bundle.zip"
    if not study_file.exists() or not bundle_file.exists():
        pytest.skip("Supplemental preserved local dev9 qualification fixture is unavailable")
    study_bytes = study_file.read_bytes()
    frozen_sha = hashlib.sha256(study_bytes).hexdigest()
    study = json.loads(study_bytes)
    audit = json.loads((folder / "bundle-estimate-audit.json").read_bytes())
    source = study["datasets"][0]["source"]
    bounds = {}
    for candidate in study["candidates"]:
        replay = {
            "source": source,
            "profile": candidate["profile"],
            "source_id": source["source_id"],
            "isp_model_snapshot": candidate["model_snapshot"],
            "calibration_snapshot": study["calibration_snapshot"],
        }
        bounds[candidate["isp_model_id"]] = studies._summary_forecast(replay)
    with zipfile.ZipFile(bundle_file) as archive:
        for artifact in audit["per_cell_summary_json_artifacts"]:
            payload = archive.read("artifacts/" + artifact["sha256"])
            assert hashlib.sha256(payload).hexdigest() == artifact["sha256"]
            assert len(payload) == artifact["bytes"] > 7_600_000
            summary = json.loads(payload)
            bound = bounds[summary["replay_isp_model_id"]]
            assert len(payload) <= bound["summary_artifact_bytes_limit"]
    repaired_estimate_lower_bound = audit["estimated_bundle_bytes"] + sum(
        item["summary_artifact_bytes_limit"] for item in bounds.values()
    )
    assert audit["actual_uncompressed_bundle_bytes"] <= repaired_estimate_lower_bound
    assert repaired_estimate_lower_bound < studies.MAX_ARCHIVE_BYTES
    assert hashlib.sha256(study_file.read_bytes()).hexdigest() == frozen_sha
    assert "evidence_forecast_version" not in study["plan_hash_basis"]
