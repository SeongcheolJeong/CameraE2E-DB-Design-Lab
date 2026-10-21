"""Read-only admission for the reviewed v0.14 -> v0.15 case-review change.

This is not a generic gate waiver. Source approval binds exact bytes; unchanged
calculation evidence retains its old environment and is never a new execution.
The caller owns the existing backup/transition/rollback transaction.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

import start_camera_platform as launcher
from install_camera_platform_runtime import verify_wheel

CHANGED = {
    "camera_platform/" + name
    for name in (
        "__init__.py",
        "__main__.py",
        "api.py",
        "registry.py",
        "sdk.py",
        "service.py",
        "workbench.py",
        "static/app.js",
        "static/index.html",
        "static/style.css",
    )
}
ADDED = {
    "camera_platform/case_contracts.py",
    "camera_platform/case_reviews.py",
    "camera_platform/static/cases.js",
}
METADATA = {
    "pyisetcam-0.1.0.dist-info/RECORD",
    "pyisetcam-0.1.0.dist-info/direct_url.json",
}
STATIC = ("index.html", "app.js", "style.css", "workbench.js", "scenes.js", "cases.js")
UI_CHECKS = {
    "installed_case_selection",
    "installed_case_revision_save",
    "installed_case_history_reopen",
    "installed_case_export_matches",
}
MODULES = {
    "camera_platform": "camera_platform/__init__.py",
    "pyisetcam": "pyisetcam/__init__.py",
    **{
        "camera_platform." + name: "camera_platform/" + name + ".py"
        for name in ("case_reviews", "case_contracts", "api", "registry", "service")
    },
}
AUTHORITY = {"engineering_note": True, "physical_validation": False, "release_approval": False}
REUSE = {"basis": "unchanged_payload_and_dependencies", "new_execution": False}


def require(condition, message):
    if not condition:
        raise RuntimeError("Case delta admission: " + message)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checksum(value):
    return isinstance(value, str) and len(value) == 64 and set(value) <= set("0123456789abcdef")


def bound(binding, expected_path=None):
    require(isinstance(binding, dict), "missing file binding")
    path = Path(binding["path"]).resolve()
    require(path.is_file() and checksum(binding.get("sha256")), "invalid file binding")
    require(file_hash(path) == binding["sha256"], "bound evidence changed: " + str(path))
    if expected_path is not None:
        require(path == Path(expected_path).resolve(), "evidence path differs from CLI input")
    return path


def read_bound(binding, expected_path=None):
    return launcher._json_file(bound(binding, expected_path), limit=64 * 1024**2)


def validate_source_delta(old, new, approval):
    """Every runtime file is classified; approved names alone grant no exemption."""
    old_env, new_env = old["environment"], new["environment"]
    require(old_env.get("platform_version") == "0.14.0", "baseline must be v0.14.0")
    require(new_env.get("platform_version") == "0.15.0", "candidate must be v0.15.0")

    def dependencies(env):
        return {
            key: value
            for key, value in env.items()
            if key not in {"platform_version", "source_hashes"}
        }

    require(dependencies(old_env) == dependencies(new_env), "dependency/Python/OS change")
    before, after = old["files"], new["files"]
    require(not (before.keys() - after.keys()), "runtime payload deletion")
    actual = {
        name: {"before": before.get(name), "after": after.get(name)}
        for name in before.keys() | after.keys()
        if before.get(name) != after.get(name)
    }
    source = {name: value for name, value in actual.items() if name not in METADATA}
    require(set(source) == CHANGED | ADDED, "unexpected or incomplete case source delta")
    require(all(name in before for name in CHANGED), "changed baseline source is absent")
    require(all(name not in before for name in ADDED), "new case source already existed")
    require(source == approval.get("allowed_delta"), "source bytes differ from reviewed approval")
    approved = approval.get("source_files", {})
    for name in CHANGED | ADDED:
        require(
            approved.get("src/" + name) == after[name], "candidate differs from approved source"
        )
    for name, value in new_env.get("source_hashes", {}).items():
        require(after.get(name) == value, "candidate environment source inventory mismatch")
    require(bool(new_env.get("source_hashes")), "missing environment source inventory")
    return {
        "source_delta": source,
        "installation_metadata_delta": {
            name: value for name, value in actual.items() if name in METADATA
        },
    }


def verify_installation_metadata(root, wheel):
    """Only generated RECORD/direct_url may differ, and their contents are checked."""
    root, wheel = Path(root), Path(wheel)
    direct = launcher._json_file(root / "pyisetcam-0.1.0.dist-info/direct_url.json")
    require(direct.get("url") == wheel.resolve().as_uri(), "installer wheel path changed")
    require(
        direct.get("archive_info", {}).get("hashes", {}).get("sha256") == file_hash(wheel),
        "installer wheel hash changed",
    )
    files = launcher.package_files(root)
    seen = set()
    rows = csv.reader(io.StringIO((root / "pyisetcam-0.1.0.dist-info/RECORD").read_text()))
    for row in rows:
        require(len(row) == 3, "malformed installation RECORD")
        name, encoded, size = row
        require(name in files and name not in seen, "unknown or duplicate RECORD file")
        seen.add(name)
        if name == "pyisetcam-0.1.0.dist-info/RECORD":
            require(encoded == size == "", "RECORD self entry changed")
            continue
        expected = "sha256=" + base64.urlsafe_b64encode(bytes.fromhex(files[name])).decode().rstrip(
            "="
        )
        require(
            encoded == expected and size == str((root / name).stat().st_size),
            "installation RECORD does not bind actual bytes",
        )
    require(seen == files.keys(), "installation RECORD omits payload files")
    with zipfile.ZipFile(wheel) as archive:
        wheel_payload = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if not name.endswith("/") and name.startswith(("camera_platform/", "pyisetcam/"))
        }
    installed_payload = {
        name: value
        for name, value in files.items()
        if name.startswith(("camera_platform/", "pyisetcam/"))
    }
    require(wheel_payload == installed_payload, "installed payload differs from wheel")


def validate_case(record):
    require(record.get("schema") == "camera_case_review_v1", "not a saved case review")
    require(record.get("authority") == AUTHORITY, "case grants unsupported authority")
    require(
        record.get("content_hash")
        == digest({key: value for key, value in record.items() if key != "content_hash"}),
        "case record content hash mismatch",
    )
    evidence = record["snapshots"]["evidence"]
    refs = [{key: item[key] for key in ("kind", "id", "requirement_keys")} for item in evidence]
    require(refs == record["evidence"], "case evidence references differ from snapshots")
    for item in evidence:
        require(item["snapshot"].get("id") == item["id"], "snapshot identity mismatch")
        require(item["record_sha256"] == digest(item["snapshot"]), "full evidence hash mismatch")
        for linked in item["linked_records"]:
            require(linked["snapshot"].get("id") == linked["id"], "linked snapshot identity")
            require(
                linked["record_sha256"] == digest(linked["snapshot"]),
                "linked full result hash mismatch",
            )


def validate_restart(restart):
    require(restart.get("old_process_group_stopped") is True, "old API/Worker not stopped")
    for kind in ("api", "worker"):
        a, b = (restart.get(prefix + "_" + kind + "_pid") for prefix in ("old", "new"))
        require(
            type(a) is int and type(b) is int and a > 1 and b > 1 and a != b,
            "restart lacks distinct confirmed " + kind + " processes",
        )
    require(
        type(restart.get("post_count")) is int and restart["post_count"] == 0,
        "restart verification submitted new work",
    )
    rows = restart.get("rows_before")
    require(
        isinstance(rows, dict) and set(rows) == {"records", "jobs", "audit"},
        "restart lacks all row snapshots",
    )
    require(
        all(
            isinstance(part, dict) and part and all(checksum(h) for h in part.values())
            for part in rows.values()
        ),
        "restart has counts instead of full row hashes",
    )
    require(rows == restart.get("rows_after"), "restart changed records/jobs/audit")
    cases = restart.get("case_records_before")
    require(
        bool(cases) and cases == restart.get("case_records_after"),
        "restart changed frozen case records",
    )


def historical_solver_evidence(baseline, old_manifest, old_root, actual_backend):
    """Current old/new probes alone cannot establish unchanged historic dependencies."""
    binding = baseline["candidate_audit"]
    audit = read_bound(binding)
    require(
        audit.get("schema") == "camera_release_candidate_audit_v1"
        and audit.get("status") == "passed_for_declared_candidate_scope"
        and audit.get("candidate_version") == "0.14.0"
        and audit.get("runtime_root") == str(old_root)
        and audit.get("wheel_sha256") == old_manifest["wheel_sha256"]
        and audit.get("manifest_sha256") == baseline["manifest"]["sha256"],
        "historical solver audit differs from approved baseline",
    )
    directory = Path(binding["path"]).resolve().parent
    paths = {}
    reports = {}
    for key, name in (
        ("package", "package-verification.json"),
        ("runtime", "prescription-installed-runtime-verification.json"),
    ):
        paths[key] = {
            "path": str(directory / name),
            "sha256": audit["input_sha256"][name],
        }
        reports[key] = read_bound(paths[key])
        require(
            reports[key].get("status") == "passed"
            and reports[key].get("environment") == old_manifest["environment"],
            "historical solver proof differs from baseline environment",
        )
    package, runtime = reports["package"], reports["runtime"]
    require(
        package.get("wheel_sha256") == old_manifest["wheel_sha256"]
        and runtime.get("runtime_root") == str(old_root)
        and runtime.get("scene", {}).get("status") == "passed"
        and package.get("scene_backend") == runtime["scene"].get("backend") == actual_backend,
        "current Mitsuba dependency fingerprint differs from hash-bound v0.14 evidence",
    )
    return {"audit": binding, **paths, "scene_backend": actual_backend}


def local_base(value):
    parsed = urlsplit(value)
    require(
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and parsed.port != 8770
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"},
        "installed evidence must use an isolated local QA URL",
    )
    return value.rstrip("/")


def get(base, path):
    with urlopen(base + "/api/v1/" + path, timeout=20) as response:
        return response.read()


def validate_installed(runtime, ui, manifest, root, primary):
    """Bind real installed execution, frozen records, restart and visible exports."""
    env = manifest["environment"]
    require(
        runtime.get("schema") == "camera_case_installed_verification_v1"
        and runtime.get("status") == "passed_for_declared_scope",
        "missing installed case proof",
    )
    require(
        runtime.get("original_proof_reused_as_execution_evidence") is False
        and runtime.get("execution_mode") == "fresh_installed_http_worker"
        and runtime.get("primary_activated") is False,
        "old/source proof is not fresh execution",
    )
    require(runtime.get("post_intents"), "fresh execution lacks POST intents")
    manifest_sha = file_hash(root / "runtime-manifest.json")
    require(
        runtime.get("runtime_root") == str(root)
        and runtime.get("manifest_sha256") == manifest_sha
        and runtime.get("wheel_sha256") == manifest["wheel_sha256"]
        and runtime.get("environment") == env,
        "installed package/environment mismatch",
    )
    qa = Path(runtime["state_dir"]).resolve()
    require(qa != primary and not qa.is_relative_to(primary), "QA targets primary state")
    base = local_base(runtime["base_url"])
    expected_modules = {name: str(root / path) for name, path in MODULES.items()}
    for key in ("probe_before", "probe_after"):
        probe = runtime.get(key, {})
        require(
            probe.get("environment") == env
            and all(
                probe.get("module_paths", {}).get(name) == path
                for name, path in expected_modules.items()
            ),
            "installed imports differ from immutable runtime",
        )
    for binding in runtime.get("inputs", {}).values():
        bound(binding)
    require(bool(runtime.get("inputs")), "installed proof has no input bindings")
    records = runtime.get("records", {})
    for key in ("case_a", "case_a_revision", "case_b", "run_a", "run_b"):
        require(isinstance(records.get(key), dict), "missing installed full record: " + key)
    for record in records.values():
        require(
            runtime.get("record_sha256", {}).get(record["id"]) == digest(record),
            "installed full-record hash mismatch",
        )
        if record.get("schema") == "camera_case_review_v1":
            validate_case(record)
    first, revised, other = (records[k] for k in ("case_a", "case_a_revision", "case_b"))
    require(
        first["case_id"] == revised["case_id"] != other["case_id"]
        and revised["previous_revision_id"] == first["id"]
        and revised["revision"] == first["revision"] + 1,
        "installed case history mismatch",
    )
    for key in ("run_a", "run_b"):
        run = records[key]
        require(
            run.get("environment") == env and run.get("status") == "succeeded",
            "run is not successful in the candidate environment",
        )
        require(
            any(
                intent.get("path") == "/runs"
                and intent.get("state") == "responded"
                and intent.get("actual_status") == 202
                and intent.get("response_id") == run["id"]
                and intent.get("payload", {}).get("assembly_id") == run["assembly_id"]
                and intent.get("payload", {}).get("test_id") == run["test_id"]
                for intent in runtime["post_intents"]
            ),
            "fresh run is not linked to an actual installed POST response",
        )
    validate_restart(runtime["restart"])
    expected_cases = {
        record["id"]: record
        for record in records.values()
        if record.get("schema") == "camera_case_review_v1"
    }
    require(
        runtime["restart"]["case_records_after"] == expected_cases,
        "restart cases differ from installed execution records",
    )
    require(
        ui.get("schema") == "camera_case_installed_ui_v1"
        and ui.get("status") == "passed_for_declared_scope"
        and ui.get("method") == "CUA"
        and ui.get("errors") == [],
        "missing installed browser observations",
    )
    require(
        ui.get("runtime_root") == str(root)
        and ui.get("manifest_sha256") == manifest_sha
        and ui.get("environment") == env
        and Path(ui["state_dir"]).resolve() == qa
        and local_base(ui["base_url"]) == base,
        "browser targets a different package/state",
    )
    checks = ui.get("checks", [])
    require(
        all(isinstance(item, dict) and item.get("status") == "passed" for item in checks)
        and UI_CHECKS <= {item.get("name") for item in checks},
        "missing case UI observations",
    )
    for proof in (runtime, ui):
        require(
            all(
                proof.get("static_sha256", {}).get(name)
                == manifest["files"].get("camera_platform/static/" + name)
                and proof.get("static_sha256", {}).get(name)
                for name in STATIC
            ),
            "installed UI static payload mismatch",
        )
    pin = launcher.load_installed_runtime(qa)
    require(
        pin["runtime_root"] == str(root) and pin["manifest_sha256"] == manifest_sha,
        "live QA pin differs from candidate",
    )
    launcher.verify_running_package(base + "/", qa, pin)
    witness = launcher._json_file(qa / "runtime-process.json")
    require(runtime.get("runtime_binding") == witness, "installed process witness changed")
    observed_cases = ui.get("case_records", {})
    observed_cases = (
        list(observed_cases.values()) if isinstance(observed_cases, dict) else observed_cases
    )
    require(
        isinstance(observed_cases, list) and len(observed_cases) >= 2,
        "UI lacks full saved case records",
    )
    for record in observed_cases:
        validate_case(record)
        actual = json.loads(get(base, "case_reviews/" + quote(record["id"], safe="")))
        require(actual == record, "live browser case differs from observed record")
    export = ui["export"]
    exported = read_bound(export)
    require(
        exported.get("id") == export.get("case_review_id") and exported in observed_cases,
        "downloaded case export differs from observed record",
    )
    for record in records.values():
        collection = "case_reviews" if record.get("schema") == "camera_case_review_v1" else "runs"
        actual = json.loads(get(base, collection + "/" + quote(record["id"], safe="")))
        require(actual == record, "live installed record differs from execution proof")
    artifacts = {item["name"]: item for item in records["run_a"].get("artifacts", [])}
    rgb = "rgb_frames.npy" if "rgb_frames.npy" in artifacts else "rgb.npy"
    for name in ("raw_frames.npy", rgb):
        artifact = artifacts[name]
        sha = artifact["sha256"]
        require(
            runtime.get("artifact_sha256", {}).get(name) == sha,
            "fresh camera artifact identity mismatch",
        )
        observation = runtime.get("artifact_http_sha256", {}).get(sha, {})
        observed_run = next(
            (
                record
                for record in (records["run_a"], records["run_b"])
                if record["id"] == observation.get("run_id")
            ),
            None,
        )
        require(
            observation.get("sha256") == sha
            and observation.get("bytes") == artifact["bytes"]
            and observed_run is not None
            and any(
                item["sha256"] == sha and item["bytes"] == artifact["bytes"]
                for item in observed_run["artifacts"]
            ),
            "missing original artifact HTTP verification",
        )
        data = get(base, "artifacts/" + sha)
        require(
            hashlib.sha256(data).hexdigest() == sha and len(data) == artifact["bytes"],
            "live RAW/RGB differs from installed execution",
        )


def qualify(args, old_manifest, new_manifest, old_root, new_root):
    """Return an admission record; never write files, select a pin, or stop a process."""
    proof_path = Path(args.case_delta_proof).resolve()
    proof = launcher._json_file(proof_path, limit=64 * 1024**2)
    require(
        proof.get("schema") == "camera_case_feature_delta_v1"
        and proof.get("status") == "ready_for_upgrade",
        "unsupported delta proof",
    )
    require(proof.get("baseline_reuse") == REUSE, "baseline evidence must not be new execution")
    require(
        checksum(args.case_source_approval_sha256)
        and args.case_source_approval_sha256 == proof["source_approval"]["sha256"],
        "explicit reviewed source approval hash is required",
    )
    approval = read_bound(proof["source_approval"])
    require(
        approval.get("schema") == "camera_case_delta_source_approval_v1"
        and approval.get("status") == "approved_for_case_delta"
        and all(
            isinstance(approval.get(k), str) and approval[k].strip()
            for k in ("reviewer", "method", "scope")
        ),
        "missing scoped source review",
    )
    baseline = approval["baseline"]
    delivery = read_bound(baseline["delivery"])
    require(
        read_bound(baseline["manifest"], old_root / "runtime-manifest.json") == old_manifest,
        "baseline manifest differs from running primary",
    )
    require(
        delivery.get("schema") == "camera_platform_delivery_v1"
        and delivery.get("version") == "0.14.0"
        and delivery.get("runtime_root") == str(old_root)
        and delivery.get("environment") == old_manifest["environment"]
        and delivery.get("package", {}).get("sha256") == old_manifest["wheel_sha256"]
        and delivery.get("package", {}).get("manifest_sha256") == baseline["manifest"]["sha256"]
        and baseline.get("wheel_sha256") == old_manifest["wheel_sha256"],
        "baseline delivery differs from running primary",
    )
    old_wheel = Path(baseline["delivery"]["path"]).resolve().parent / delivery["package"]["path"]
    require(file_hash(old_wheel) == old_manifest["wheel_sha256"], "baseline wheel bytes changed")
    changes = validate_source_delta(old_manifest, new_manifest, approval)
    candidate = proof["candidate"]
    require(
        candidate.get("runtime_root") == str(new_root)
        and candidate.get("manifest_sha256") == file_hash(new_root / "runtime-manifest.json"),
        "candidate manifest binding mismatch",
    )
    wheel = bound(candidate["wheel"])
    package = read_bound(proof["package_proof"], args.package_proof)
    require(
        verify_wheel(wheel, package) == new_manifest["wheel_sha256"]
        and package.get("environment") == new_manifest["environment"],
        "new package verification differs from candidate",
    )
    verify_installation_metadata(new_root, wheel)
    runtime = read_bound(proof["runtime_proof"], args.runtime_proof)
    ui = read_bound(proof["ui_proof"], args.ui_proof)
    validate_installed(runtime, ui, new_manifest, new_root, Path(args.state_dir).resolve())
    selected_python = launcher.WORKSPACE / ".venv-platform/bin/python"
    actual_python = str(selected_python) if selected_python.is_file() else sys.executable
    require(
        Path(runtime["python"]).absolute() == Path(actual_python).absolute(),
        "installed qualification used a different launcher interpreter",
    )
    probes = {}
    for executable in dict.fromkeys((sys.executable, actual_python)):
        probes[executable] = {}
        for name, root, manifest in (
            ("old", old_root, old_manifest),
            ("new", new_root, new_manifest),
        ):
            probe = launcher.probe_package(executable, root)
            require(
                probe["environment"] == manifest["environment"],
                "fresh transition/launcher interpreter environment mismatch",
            )
            probes[executable][name] = probe
    require(args.mitsuba_python is not None, "explicit preserved Mitsuba interpreter required")
    scene_python = launcher._scene_interpreter(args.mitsuba_python)
    saved_scene = launcher.load_runtime_config(Path(args.state_dir), "mitsuba_python")
    require(launcher._scene_interpreter(saved_scene) == scene_python, "Mitsuba binding change")
    old_backend = launcher.probe_scene_interpreter(scene_python, runtime_root=old_root)
    new_backend = launcher.probe_scene_interpreter(scene_python, runtime_root=new_root)
    require(old_backend == new_backend, "Mitsuba backend environment change")
    historical_solvers = historical_solver_evidence(baseline, old_manifest, old_root, new_backend)
    saved_meep = launcher.load_runtime_config(Path(args.state_dir))
    selected_meep = launcher.select_meep_binding(None, os.environ, saved_meep)
    require(
        selected_meep == launcher.select_meep_binding(None, {}, saved_meep),
        "environment overrides the preserved Meep interpreter",
    )
    if selected_meep:
        old_meep = launcher.probe_meep_interpreter(selected_meep, runtime_root=old_root)
        new_meep = launcher.probe_meep_interpreter(selected_meep, runtime_root=new_root)
        require(old_meep == new_meep, "Meep backend environment change")
    return scene_python, {
        "schema": "camera_case_delta_admission_v1",
        "status": "passed",
        "proof": {"path": str(proof_path), "sha256": file_hash(proof_path)},
        "source_approval": proof["source_approval"],
        "baseline": baseline,
        "baseline_wheel": {"path": str(old_wheel), "sha256": old_manifest["wheel_sha256"]},
        "candidate": candidate,
        **changes,
        "baseline_reuse": REUSE,
        "new_execution": {"runtime": proof["runtime_proof"], "ui": proof["ui_proof"]},
        "interpreter_probes": probes,
        "scene_backend": new_backend,
        "historical_solver_evidence": historical_solvers,
        "dependency_scope": {
            "core": "Same recorded Python, OS and package version fingerprints",
            "mitsuba": "Matches hash-bound v0.14 interpreter, adapter and version fingerprint",
            "meep": (
                "Current configured fixed probe; no new Meep calculation or historical "
                "transitive binary comparison is claimed"
            ),
            "clean_os_installation": False,
            "transitive_dependency_binary_closure": False,
        },
        "authority": (
            "Engineering verification only; not physical validation or user release approval"
        ),
    }
