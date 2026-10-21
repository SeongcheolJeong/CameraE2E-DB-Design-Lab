#!/usr/bin/env python3
"""Bind an R1 engineering RC to an exact wheel, scope and installed-flow proof.

This is a separate product-release record. It does not call or relax the
physical-camera validation gate in ``PlatformService.create_release``. A
structurally complete, self-consistent proof can make an RC candidate ready for
review; it never approves a product or establishes measured prediction accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

SCHEMA = "camera_platform_r1_product_release_manifest_v1"
EVIDENCE_SCHEMA = "camera_platform_r1_install_evidence_v1"
MODE_SCHEMA = "camera_platform_project_mode_v1"
MODE = "r1_engineering"
PACKAGE_SCHEMA = "camera_platform_installed_package_v1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = (ROOT / "outputs/camera-platform").resolve()
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_CHECKS = (
    "isolated_install",
    "installed_imports",
    "required_assets",
    "two_model_scene",
    "numerical_contract",
    "deterministic_reproduction",
    "complete_user_journey",
    "failure_paths",
    "restart_restore",
    "performance",
    "support_display_consistency",
    "release_file_identity",
)
RC2_REQUIRED_CHECKS = ("design_isp_replay", "selected_temporal_hdr_dcg")
JOURNEY_STEPS = (
    "model_scene_conditions", "raw_rgb", "comparison", "selection_save", "reopen"
)
FAILURE_CASES = (
    "invalid_input", "no_candidate", "evaluation_failure", "cancelled",
    "interrupted", "save_failure",
)


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _file_record(path: Path, *, suffix: str | None = None) -> dict:
    path = path.absolute()
    record = {"path": str(path), "sha256": None, "bytes": None}
    if (path.is_symlink() or not path.is_file()
            or suffix is not None and path.suffix != suffix):
        return record
    record.update(sha256=_sha(path), bytes=path.stat().st_size)
    return record


def _json(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, UnicodeError, ValueError):
        return None


def _bound_path(value, base: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).absolute()


def _wheel_valid(path: Path) -> bool:
    if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            names = set()
            total = 0
            for member in members:
                name = member.filename
                posix = PurePosixPath(name)
                if (posix.is_absolute() or ".." in posix.parts or "\\" in name
                        or name in names or member.flag_bits & 1
                        or stat.S_ISLNK(member.external_attr >> 16)):
                    return False
                names.add(name)
                total += member.file_size
            if not {
                "camera_platform/__init__.py",
                "camera_platform/static/index.html",
                "pyisetcam/__init__.py",
            } <= names:
                return False
            if len(members) > 20_000 or total > 512 * 1024**2 or archive.testzip() is not None:
                return False
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return False
    return True


def _wheel_version(path: Path) -> str | None:
    """Use the wheel's own metadata so a renamed RC cannot relax release gates."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist()
                     if name.endswith(".dist-info/METADATA")]
            if len(names) != 1 or archive.getinfo(names[0]).file_size > 1_048_576:
                return None
            metadata = archive.read(names[0]).decode("utf-8")
        name = re.search(r"^Name:\s*(.+)$", metadata, re.MULTILINE)
        version = re.search(r"^Version:\s*(.+)$", metadata, re.MULTILINE)
        if name is None or name.group(1).strip().lower() != "pyisetcam" or version is None:
            return None
        return version.group(1).strip()
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile):
        return None


def _wheel_package_hashes(path: Path) -> dict[str, str] | None:
    try:
        with zipfile.ZipFile(path) as archive:
            return {
                member.filename: hashlib.sha256(archive.read(member)).hexdigest()
                for member in archive.infolist()
                if not member.is_dir()
                and member.filename.startswith(("camera_platform/", "pyisetcam/"))
            }
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return None


def _required_checks(version: str | None) -> tuple[str, ...]:
    # Unknown or newer candidates cannot silently inherit the old static-only proof.
    return REQUIRED_CHECKS if version == "0.16.1rc1" else REQUIRED_CHECKS + RC2_REQUIRED_CHECKS


def _passed_fields(name: str, check: dict) -> bool:
    """Require observable details for gates where `passed` alone is too weak."""
    if name == "isolated_install":
        return all(check.get(key) is True for key in (
            "fresh_environment", "no_system_site_packages", "no_checkout_imports"
        ))
    if name == "required_assets":
        return all(check.get(key) is True for key in (
            "no_qa_dependency", "no_checkout_dependency"
        ))
    if name == "two_model_scene":
        return all(isinstance(check.get(key), list)
                   and len(set(check[key])) >= 2
                   and all(isinstance(item, str) and item for item in check[key])
                   for key in ("model_ids", "scene_ids"))
    if name == "complete_user_journey":
        steps = check.get("steps")
        return isinstance(steps, dict) and all(steps.get(key) == "passed"
                                               for key in JOURNEY_STEPS)
    if name == "failure_paths":
        cases = check.get("cases")
        return isinstance(cases, dict) and all(cases.get(key) == "passed"
                                               for key in FAILURE_CASES)
    if name == "performance":
        numbers = ("runtime_seconds", "peak_memory_bytes", "saved_bytes",
                   "list_read_seconds")
        return (isinstance(check.get("published_max_input"), str)
                and bool(check["published_max_input"])
                and all(type(check.get(key)) in (int, float)
                        and 0 <= check[key] < float("inf") for key in numbers)
                and check.get("list_avoids_raw_recalculation") is True)
    if name == "design_isp_replay":
        return all(check.get(key) is True for key in (
            "input_unchanged", "no_new_captures", "rgb_delta_observed",
            "browser_replay", "saved_reopen",
        ))
    if name == "selected_temporal_hdr_dcg":
        modes = check.get("modes")
        return (check.get("registered_source") is True
                and check.get("numerical_bridge") is True
                and check.get("browser_hdr") is True
                and isinstance(modes, dict)
                and all(modes.get(key) == "passed" for key in ("temporal", "hdr", "dcg")))
    return True


def build_manifest(
    *, project_id: str, state_dir: Path, wheel: Path, install_evidence: Path,
    support_doc: Path,
) -> dict:
    if not isinstance(project_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}",
                                                          project_id):
        raise ValueError("project_id requires a 1..64 character lowercase slug")
    state = state_dir.expanduser().resolve()
    wheel, install_evidence, support_doc = (
        Path(item).expanduser().absolute() for item in (wheel, install_evidence, support_doc)
    )
    mode_file = state / "project-mode.json"
    files = {
        "wheel": _file_record(wheel, suffix=".whl"),
        "install_evidence": _file_record(install_evidence, suffix=".json"),
        "support_doc": _file_record(support_doc, suffix=".md"),
        "project_mode": _file_record(mode_file, suffix=".json"),
    }
    blockers: list[dict[str, str]] = []

    def block(code: str, message: str):
        blockers.append({"code": code, "message": message})

    if state == DEFAULT_STATE or state.is_relative_to(DEFAULT_STATE):
        block("r1_state_not_separate", "R1 state must be separate from the v0.15 default state")
    mode = _json(mode_file) if files["project_mode"]["sha256"] else None
    if (not isinstance(mode, dict) or mode.get("schema") != MODE_SCHEMA
            or mode.get("mode") != MODE or mode.get("project_id") != project_id):
        block("project_mode_unbound", "R1 project marker schema, mode and project ID must match")
    if files["wheel"]["sha256"] is None or not _wheel_valid(wheel):
        block("wheel_missing_or_invalid",
              "A readable R1 wheel with camera_platform and UI files is required")
    wheel_version = _wheel_version(wheel)
    if wheel_version is None:
        block("wheel_version_missing", "Wheel must contain pyisetcam distribution metadata")
    if files["support_doc"]["sha256"] is None:
        block("support_scope_missing", "The frozen support-scope Markdown file is required")
    evidence = _json(install_evidence) if files["install_evidence"]["sha256"] else None
    if not isinstance(evidence, dict) or evidence.get("schema") != EVIDENCE_SCHEMA:
        block("install_evidence_missing", "R1 installed-flow evidence schema is required")
        evidence = {}
    checks = evidence.get("checks") if isinstance(evidence.get("checks"), dict) else {}
    expected = {
        "project_id": project_id,
        "mode": MODE,
        "state_dir": str(state),
        "project_mode_sha256": files["project_mode"]["sha256"],
        "wheel_sha256": files["wheel"]["sha256"],
        "support_doc_sha256": files["support_doc"]["sha256"],
    }
    for key, value in expected.items():
        if value is None or evidence.get(key) != value:
            block("install_binding_mismatch",
                  f"Installed evidence {key} differs from this candidate")
    if evidence.get("status") != "passed":
        block("install_evidence_incomplete",
              "Installed-flow evidence must finish with status passed")
    if evidence.get("physical_validation") != "not_established":
        block("physical_claim_mismatch",
              "R1 evidence must preserve physical_validation=not_established")

    payload_path = _bound_path(evidence.get("installed_payload_manifest_path"),
                               install_evidence.parent)
    payload_file = (_file_record(payload_path, suffix=".json") if payload_path is not None
                    else {"path": None, "sha256": None, "bytes": None})
    files["installed_payload_manifest"] = payload_file
    payload = _json(payload_path) if payload_file["sha256"] else None
    if (not HEX64.fullmatch(str(evidence.get("installed_payload_manifest_sha256")))
            or evidence["installed_payload_manifest_sha256"] != payload_file["sha256"]
            or not isinstance(payload, dict) or payload.get("schema") != PACKAGE_SCHEMA
            or payload.get("wheel_sha256") != files["wheel"]["sha256"]
            or not isinstance(payload.get("files"), dict) or not payload["files"]
            or payload["files"] != _wheel_package_hashes(wheel)
            or not all(isinstance(name, str) and name
                       and isinstance(sha, str) and HEX64.fullmatch(sha)
                       for name, sha in payload["files"].items())):
        block("installed_payload_unbound",
              "Installed payload manifest must hash and bind the exact wheel")

    bound_checks = {}
    for name in _required_checks(wheel_version):
        check = checks.get(name)
        if not isinstance(check, dict) or check.get("status") != "passed":
            block("required_check_missing", f"R1 installed check {name} has no passed result")
            bound_checks[name] = {"status": "blocked", "evidence_sha256": None}
            continue
        path = _bound_path(check.get("evidence_path"), install_evidence.parent)
        artifact = _file_record(path) if path is not None else {
            "path": None, "sha256": None, "bytes": None,
        }
        valid = (artifact["sha256"] is not None
                 and check.get("evidence_sha256") == artifact["sha256"]
                 and _passed_fields(name, check))
        if name == "release_file_identity":
            valid = (valid and artifact["sha256"] == payload_file["sha256"]
                     and check.get("wheel_sha256") == files["wheel"]["sha256"]
                     and check.get("installed_payload_manifest_sha256")
                     == payload_file["sha256"])
        if not valid:
            block("required_check_unbound",
                  f"R1 installed check {name} lacks bound evidence or details")
        bound_checks[name] = {
            "status": "passed" if valid else "blocked",
            "evidence_path": artifact["path"],
            "evidence_sha256": artifact["sha256"],
        }

    return {
        "schema": SCHEMA,
        "release_track": MODE,
        "status": "rc_ready" if not blockers else "blocked",
        "approval_status": "unapproved",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "project": {"project_id": project_id, "state_dir": str(state), "mode": MODE},
        "files": files,
        "checks": bound_checks,
        "blockers": blockers,
        "meaning": ("RC evidence is structurally complete and bound to these local bytes; "
                    "this is not product approval or physical-camera accuracy validation"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--install-evidence", required=True, type=Path)
    parser.add_argument("--support-doc", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    output = args.output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        parser.error("Choose a new output path; an existing R1 release record is immutable")
    report = build_manifest(project_id=args.project_id, state_dir=args.state_dir,
                            wheel=args.wheel, install_evidence=args.install_evidence,
                            support_doc=args.support_doc)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"status": report["status"], "output": str(output),
                      "blockers": report["blockers"]}, ensure_ascii=False))
    return 0 if report["status"] == "rc_ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
