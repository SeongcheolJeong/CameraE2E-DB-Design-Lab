"""Admission contract tests, not simulated claims of installed execution or UI."""

import base64
import csv
import importlib
import io
import json
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "tools"))
    return importlib.import_module("camera_case_delta")


@pytest.fixture
def source_delta(gate):
    before = {name: "1" * 64 for name in gate.CHANGED}
    before.update(
        {
            "camera_platform/scene_render.py": "2" * 64,
            "pyisetcam/sensor.py": "3" * 64,
            "pyisetcam-0.1.0.dist-info/METADATA": "4" * 64,
        }
    )
    after = {**before, **{name: "5" * 64 for name in gate.CHANGED | gate.ADDED}}
    deps = {"python": "3.12.13", "system": "test", "packages": {"numpy": "2.4.2"}}
    old = {"files": before, "environment": {**deps, "platform_version": "0.14.0"}}
    new = {
        "files": after,
        "environment": {
            **deps,
            "platform_version": "0.15.0",
            "source_hashes": {name: after[name] for name in gate.CHANGED | gate.ADDED},
        },
    }
    approval = {
        "allowed_delta": {
            name: {"before": before.get(name), "after": after[name]}
            for name in gate.CHANGED | gate.ADDED
        },
        "source_files": {"src/" + name: after[name] for name in gate.CHANGED | gate.ADDED},
    }
    return old, new, approval


def test_exact_reviewed_case_delta_retains_unchanged_solver_and_dependencies(gate, source_delta):
    old, new, approval = source_delta
    report = gate.validate_source_delta(old, new, approval)
    assert report["source_delta"] == approval["allowed_delta"]
    assert report["installation_metadata_delta"] == {}


@pytest.mark.parametrize(
    "path",
    [
        "camera_platform/scene_render.py",
        "pyisetcam/sensor.py",
        "camera_platform/unreviewed.py",
        "pyisetcam-0.1.0.dist-info/METADATA",
    ],
)
def test_unchanged_physics_extra_payload_and_dependency_metadata_cannot_be_exempted(
    gate, source_delta, path
):
    old, new, approval = source_delta
    new["files"][path] = "9" * 64
    with pytest.raises(RuntimeError, match="source delta"):
        gate.validate_source_delta(old, new, approval)


def test_allowed_registry_filename_does_not_authorize_different_bytes(gate, source_delta):
    old, new, approval = source_delta
    new["files"]["camera_platform/registry.py"] = "9" * 64
    with pytest.raises(RuntimeError, match="reviewed approval"):
        gate.validate_source_delta(old, new, approval)


def test_self_consistent_delta_map_still_must_match_reviewed_source_inventory(gate, source_delta):
    old, new, approval = source_delta
    path = "camera_platform/api.py"
    new["files"][path] = "9" * 64
    approval["allowed_delta"][path]["after"] = "9" * 64
    with pytest.raises(RuntimeError, match="approved source"):
        gate.validate_source_delta(old, new, approval)


@pytest.mark.parametrize("change", ["deletion", "dependencies", "old_version", "new_version"])
def test_schema_version_dependency_or_payload_deletion_requires_another_qualification(
    gate, source_delta, change
):
    old, new, approval = source_delta
    if change == "deletion":
        del new["files"]["pyisetcam/sensor.py"]
    elif change == "dependencies":
        new["environment"]["packages"] = {"numpy": "other"}
    elif change == "old_version":
        old["environment"]["platform_version"] = "0.13.0"
    else:
        new["environment"]["platform_version"] = "0.16.0"
    with pytest.raises(RuntimeError):
        gate.validate_source_delta(old, new, approval)


def test_bound_approval_detects_document_replacement_and_cli_path_substitution(gate, tmp_path):
    path = tmp_path / "approval.json"
    path.write_text('{"review":"first"}')
    binding = {"path": str(path), "sha256": gate.file_hash(path)}
    assert gate.read_bound(binding) == {"review": "first"}
    with pytest.raises(RuntimeError, match="CLI input"):
        gate.read_bound(binding, tmp_path / "another.json")
    path.write_text('{"review":"replacement"}')
    with pytest.raises(RuntimeError, match="changed"):
        gate.read_bound(binding)


@pytest.fixture
def installation(gate, tmp_path):
    root = tmp_path / "runtime"
    wheel = tmp_path / "fixture.whl"
    payload = {
        "camera_platform/__init__.py": b"__version__='0.15.0'\n",
        "pyisetcam/__init__.py": b"# unchanged fixture\n",
    }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    for name, data in payload.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    directory = root / "pyisetcam-0.1.0.dist-info"
    directory.mkdir()
    (directory / "direct_url.json").write_text(
        json.dumps(
            {
                "url": wheel.as_uri(),
                "archive_info": {"hashes": {"sha256": gate.file_hash(wheel)}},
            }
        )
    )

    def record():
        stream = io.StringIO()
        writer = csv.writer(stream)
        for name, checksum in gate.launcher.package_files(root).items():
            if name.endswith("/RECORD"):
                continue
            writer.writerow(
                [
                    name,
                    "sha256="
                    + base64.urlsafe_b64encode(bytes.fromhex(checksum)).decode().rstrip("="),
                    (root / name).stat().st_size,
                ]
            )
        writer.writerow(["pyisetcam-0.1.0.dist-info/RECORD", "", ""])
        (directory / "RECORD").write_text(stream.getvalue())

    record()
    return root, wheel, record


def test_generated_record_and_direct_url_bind_real_installed_bytes(gate, installation):
    root, wheel, _ = installation
    gate.verify_installation_metadata(root, wheel)


@pytest.mark.parametrize("change", ["record_omission", "altered_file", "different_wheel_payload"])
def test_metadata_exemption_cannot_hide_payload_changes(gate, installation, change):
    root, wheel, regenerate = installation
    if change == "record_omission":
        record = root / "pyisetcam-0.1.0.dist-info/RECORD"
        record.write_text("\n".join(record.read_text().splitlines()[1:]) + "\n")
    else:
        (root / "camera_platform/__init__.py").write_text("unapproved code")
        if change == "different_wheel_payload":
            regenerate()
    with pytest.raises(RuntimeError, match="RECORD|payload differs"):
        gate.verify_installation_metadata(root, wheel)


def frozen_case(gate):
    run = {"id": "run-a", "content_hash": "request-only", "result": {"mean": 12}}
    evidence = {
        "kind": "runs",
        "id": "run-a",
        "requirement_keys": ["r1"],
        "record_sha256": gate.digest(run),
        "snapshot": run,
        "linked_records": [],
    }
    case = {
        "id": "case-a",
        "schema": "camera_case_review_v1",
        "authority": gate.AUTHORITY,
        "evidence": [{k: evidence[k] for k in ("kind", "id", "requirement_keys")}],
        "snapshots": {"evidence": [evidence]},
    }
    case["content_hash"] = gate.digest(case)
    return case


def test_case_freezes_full_results_instead_of_only_request_hash(gate):
    case = frozen_case(gate)
    gate.validate_case(case)
    case["snapshots"]["evidence"][0]["snapshot"]["result"]["mean"] = 999
    case["content_hash"] = gate.digest({k: v for k, v in case.items() if k != "content_hash"})
    with pytest.raises(RuntimeError, match="full evidence"):
        gate.validate_case(case)


def test_case_review_cannot_grant_physical_or_release_authority(gate):
    case = deepcopy(frozen_case(gate))
    case["authority"]["physical_validation"] = True
    with pytest.raises(RuntimeError, match="authority"):
        gate.validate_case(case)


@pytest.fixture
def restart():
    rows = {name: {"original": "a" * 64} for name in ("records", "jobs", "audit")}
    return {
        "old_api_pid": 100,
        "new_api_pid": 200,
        "old_worker_pid": 101,
        "new_worker_pid": 201,
        "old_process_group_stopped": True,
        "post_count": 0,
        "rows_before": deepcopy(rows),
        "rows_after": deepcopy(rows),
        "case_records_before": {"case-a": {"value": "fixed"}},
        "case_records_after": {"case-a": {"value": "fixed"}},
    }


def test_restart_exact_rows_are_not_just_counts(gate, restart):
    gate.validate_restart(restart)
    restart["rows_after"]["audit"]["original"] = "b" * 64
    with pytest.raises(RuntimeError, match="records/jobs/audit"):
        gate.validate_restart(restart)


@pytest.mark.parametrize("change", ["still_running", "same_pid", "new_post", "counts_only"])
def test_unconfirmed_restart_or_new_execution_cannot_claim_read_only_reopen(gate, restart, change):
    if change == "still_running":
        restart["old_process_group_stopped"] = False
    elif change == "same_pid":
        restart["new_worker_pid"] = restart["old_worker_pid"]
    elif change == "new_post":
        restart["post_count"] = 1
    else:
        restart["rows_before"] = restart["rows_after"] = {"records": 1, "jobs": 1, "audit": 1}
    with pytest.raises(RuntimeError):
        gate.validate_restart(restart)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8770",
        "https://example.com",
        "http://user@127.0.0.1:8775",
        "http://127.0.0.1:8775/x",
    ],
)
def test_live_evidence_cannot_target_primary_or_nonlocal_api(gate, url):
    with pytest.raises(RuntimeError, match="isolated local QA"):
        gate.local_base(url)


def test_current_reviewed_source_approval_is_in_delta_contract(gate):
    """Read the actual signed-by-hash review; do not run or admit an upgrade."""
    workspace = Path(__file__).resolve().parents[2]
    approval_path = workspace / "reports/camera_platform/cases-v015/install-candidate-source.json"
    if not approval_path.exists():
        pytest.skip("local release evidence is not part of portable source tests")
    approval = json.loads(approval_path.read_text())
    assert set(approval["allowed_delta"]) == gate.CHANGED | gate.ADDED
    for name, item in approval["allowed_delta"].items():
        assert item["after"] == approval["source_files"]["src/" + name]


@pytest.fixture
def historic_solver(gate, tmp_path):
    backend = {
        "executable": "/solver/python",
        "executable_sha256": "a" * 64,
        "adapter_sha256": "b" * 64,
        "versions": {"mitsuba": "3.9.1"},
    }
    old_root = tmp_path / "old-runtime"
    manifest = {"wheel_sha256": "c" * 64, "environment": {"platform_version": "0.14.0"}}
    package = {
        "status": "passed",
        "environment": manifest["environment"],
        "wheel_sha256": manifest["wheel_sha256"],
        "scene_backend": backend,
    }
    runtime = {
        "status": "passed",
        "environment": manifest["environment"],
        "runtime_root": str(old_root),
        "scene": {"status": "passed", "backend": backend},
    }
    hashes = {}
    for name, value in (
        ("package-verification.json", package),
        ("prescription-installed-runtime-verification.json", runtime),
    ):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        hashes[name] = gate.file_hash(path)
    audit_path = tmp_path / "historical-audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "schema": "camera_release_candidate_audit_v1",
                "candidate_version": "0.14.0",
                "status": "passed_for_declared_candidate_scope",
                "runtime_root": str(old_root),
                "wheel_sha256": manifest["wheel_sha256"],
                "manifest_sha256": "d" * 64,
                "input_sha256": hashes,
            }
        )
    )
    baseline = {
        "manifest": {"sha256": "d" * 64},
        "candidate_audit": {"path": str(audit_path), "sha256": gate.file_hash(audit_path)},
    }
    return baseline, manifest, old_root, backend


def test_solver_matches_hash_bound_historical_backend_not_only_current_probes(
    gate, historic_solver
):
    gate.historical_solver_evidence(*historic_solver)
    baseline, manifest, root, current = deepcopy(historic_solver)
    current["executable_sha256"] = "e" * 64
    with pytest.raises(RuntimeError, match="hash-bound v0.14"):
        gate.historical_solver_evidence(baseline, manifest, root, current)


@pytest.mark.parametrize(
    "target",
    [
        "historical-audit.json",
        "package-verification.json",
        "prescription-installed-runtime-verification.json",
    ],
)
def test_rewritten_historical_solver_proof_cannot_redefine_baseline(
    gate, historic_solver, tmp_path, target
):
    path = tmp_path / target
    value = json.loads(path.read_text())
    value["changed"] = "same filename, different baseline"
    path.write_text(json.dumps(value))
    with pytest.raises(RuntimeError, match="bound evidence changed"):
        gate.historical_solver_evidence(*historic_solver)
