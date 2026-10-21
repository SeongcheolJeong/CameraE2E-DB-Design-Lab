#!/usr/bin/env python3
"""Read-only supplement to a successful charge ISP HTTP verification journal.

Compare the original saved bundles against fresh GET exports, reconstruct both
Study object graphs, and compare saved analyses with fresh GETs. Never submit
work, regenerate bundles, retry mutations, or manage the server lifecycle.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from verify_camera_dcg import process_witness

from camera_platform.evidence_package import read_bundle
from camera_platform.registry import canonical, digest
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def sha(data):
    return hashlib.sha256(data).hexdigest()


def legacy_replay_artifacts(manifest):
    """Exact legacy replay archive inventory, preserving every semantic alias."""
    entries = [a for key in ("replay", "source") for a in manifest[key]["artifacts"]]
    calibration = manifest.get("isp_calibration", {})
    entries += calibration.get("artifacts", []) + calibration.get("evidence_artifacts", [])
    for item in manifest.get("isp_calibration_lineage", []):
        entries += item.get("artifacts", []) + item.get("evidence_artifacts", [])
    for item in manifest.get("isp_calibration_sources", []):
        entries += item.get("artifacts", [])
    for item in manifest["factory_jobs"]:
        entries += item.get("input_artifacts", []) + item["artifacts"]
    result = {}
    for entry in entries:
        checksum, name, size = entry["sha256"], entry["name"], entry["bytes"]
        if (
            not isinstance(checksum, str)
            or not re.fullmatch(r"[a-f0-9]{64}", checksum)
            or not isinstance(name, str)
            or Path(name).name != name
            or name in ("", ".", "..")
            or type(size) is not int
            or size < 0
        ):
            raise ValueError("Invalid legacy artifact descriptor")
        path = f"artifacts/{checksum}/{name}"
        if path in result and result[path] != size:
            raise ValueError("Conflicting legacy artifact sizes")
        result[path] = size
    return result


def verify_bundle(content, exported):
    """Return verified inventory statistics without extracting any member."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate archive members")
        if sum(i.file_size for i in infos) > 256 * 1024**2 + 4096:
            raise ValueError("Expanded archive exceeds verification budget")
        if archive.getinfo("manifest.json").file_size > 16 * 1024**2:
            raise ValueError("Archive manifest exceeds verification budget")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema") == "camera_evidence_bundle_v2":
            restored = read_bundle(io.BytesIO(content), expected_manifest_sha256=digest(exported))
            if canonical(restored) != canonical(exported):
                raise AssertionError("Reconstructed Study graph differs from GET export")
            return {
                "format": "camera_evidence_bundle_v2",
                "object_count": len(manifest["objects"]),
                "artifact_member_count": len(manifest["artifact_blobs"]),
                "artifact_bytes": sum(a["bytes"] for a in manifest["artifact_blobs"]),
                "logical_manifest_sha256": digest(exported),
                "archive_manifest_sha256": digest(manifest),
                "verification": (
                    "exact externally pinned graph, reachable objects and artifact closure"
                ),
            }
        if manifest.get("type") != "isp_replay_evidence_package":
            raise ValueError("Expected legacy replay or versioned Study bundle")
        if archive.read("manifest.json") != canonical(exported).encode():
            raise AssertionError("Legacy replay manifest bytes differ from canonical GET export")
        content_fields = {
            k: v for k, v in manifest.items() if k not in {"schema", "type", "package_hash"}
        }
        if digest(content_fields) != manifest["package_hash"]:
            raise AssertionError("Legacy replay logical package checksum differs")
        inventory = legacy_replay_artifacts(manifest)
        if set(names) != {"manifest.json", "README.txt", *inventory}:
            raise AssertionError("Legacy replay member closure differs from manifest")
        for name, size in inventory.items():
            if (
                archive.getinfo(name).file_size != size
                or sha(archive.read(name)) != name.split("/")[1]
            ):
                raise AssertionError("Legacy replay artifact bytes differ: " + name)
        return {
            "format": "legacy_full_manifest",
            "object_count": 0,
            "artifact_member_count": len(inventory),
            "artifact_bytes": sum(inventory.values()),
            "logical_manifest_sha256": digest(exported),
            "archive_manifest_sha256": digest(manifest),
            "verification": "exact canonical manifest bytes and artifact member closure",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--api-pid", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    journal_path, output = args.journal.resolve(), args.output.resolve()
    journal_bytes = journal_path.read_bytes()
    journal = json.loads(journal_bytes)
    if (
        journal.get("schema") != "camera_charge_isp_http_verification_v1"
        or journal.get("status") != "passed"
    ):
        parser.error("Require an already successful charge ISP HTTP journal")
    address = urlsplit(journal["base_url"])
    if (
        address.scheme != "http"
        or address.hostname != "127.0.0.1"
        or address.port in (None, 8770)
        or address.path not in ("", "/")
        or address.query
        or address.fragment
        or address.username
        or address.password
    ):
        parser.error("Require a separate explicit loopback QA port")
    state = Path(journal["state_dir"]).resolve()
    if (
        "qa" not in state.name.lower()
        or output == state
        or state in output.parents
        or output == journal_path
        or output.parent == journal_path.parent
    ):
        parser.error("Use a separate report path outside QA state and the primary report directory")
    environment = environment_fingerprint()
    if environment != journal["environment"]:
        raise ValueError("Source or runtime environment differs from the successful journal")
    expected_labels = {
        "hdr-replay",
        "reference-replay",
        "dcg-hcg-replay",
        "dcg-lcg-replay",
        "study",
        "validation-study",
    }
    if set(journal["bundles"]) != expected_labels:
        raise ValueError("Require the exact six-bundle qualification journal")
    report = {
        "schema": "camera_charge_isp_readback_supplement_v1",
        "status": "in_progress",
        "started_at": datetime.now(UTC).isoformat(),
        "journal_path": str(journal_path),
        "journal_sha256": sha(journal_bytes),
        "journal_status": journal["status"],
        "tool_sha256": sha(Path(__file__).read_bytes()),
        "process_witness_tool_sha256": sha(
            Path(__file__).with_name("verify_camera_dcg.py").read_bytes()
        ),
        "environment_sha256": digest(environment),
        "base_url": journal["base_url"],
        "state_dir": str(state),
        "requests": [],
        "bundles": {},
        "analyses": {},
        "checks": [],
        "physical_validation": "not_established",
        "scope": "GET-only saved evidence readback; no numerical rerun or new candidate acceptance",
        "limitations": [
            "Journal and saved files are local evidence pins, "
            "not independent source authentication.",
            "Study decoding uses the product reader; this checks serialization correspondence.",
            "Authored synthetic validation data do not establish physical-camera accuracy.",
            "Array dtype strictness and independent numerical correctness are separate tests.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)

    def save():
        report["updated_at"] = datetime.now(UTC).isoformat()
        temporary = output.with_suffix(".tmp")
        with temporary.open("x") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        temporary.replace(output)

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        report["checks"].append(label)

    try:
        with PlatformClient(journal["base_url"], timeout=120) as client:

            def get(route):
                report["requests"].append({"method": "GET", "route": route})
                return client.get(route)

            health = get("health")
            check(
                "isolated QA state and supervised Worker",
                health["worker"] == "running" and Path(health["state_dir"]).resolve() == state,
            )
            report["process_witness"] = process_witness(address.port, args.api_pid, state)
            for label in sorted(expected_labels):
                row, bundle = journal["records"][label], journal["bundles"][label]
                kind = "isp-studies" if label in ("study", "validation-study") else "replays"
                if row["route"] != kind + "/" + row["id"] or not re.fullmatch(
                    r"[a-zA-Z0-9_-]+", row["id"]
                ):
                    raise ValueError("Invalid journal record route")
                saved_record = (journal_path.parent / (label + ".json")).read_bytes()
                check(label + " saved record checksum", sha(saved_record) == row["sha256"])
                check(
                    label + " exact record GET",
                    canonical(get(row["route"])).encode() == saved_record,
                )
                exported = get(row["route"] + "/export")
                content = (journal_path.parent / (label + ".zip")).read_bytes()
                check(
                    label + " saved ZIP checksum",
                    sha(content) == bundle["sha256"] and len(content) == bundle["bytes"],
                )
                verified = verify_bundle(content, exported)
                check(
                    label + " pinned archive manifest",
                    verified["archive_manifest_sha256"] == bundle["manifest_sha256"],
                )
                report["bundles"][label] = {
                    "route": row["route"],
                    "zip_sha256": sha(content),
                    "zip_bytes": len(content),
                    **verified,
                }
                if kind == "isp-studies":
                    filename = (
                        "study-analysis.json" if label == "study" else "validation-analysis.json"
                    )
                    saved = (journal_path.parent / filename).read_bytes()
                    analysis = get(row["route"] + "/analysis")
                    check(
                        label + " exact saved analysis GET", canonical(analysis).encode() == saved
                    )
                    check(
                        label + " analysis identity",
                        analysis["study_id"] == row["id"]
                        and analysis["schema"] == "camera_isp_study_analysis_v2",
                    )
                    report["analyses"][label] = {
                        "path": str(journal_path.parent / filename),
                        "sha256": sha(saved),
                        "bytes": len(saved),
                        "study_id": analysis["study_id"],
                        "phase": analysis["phase"],
                        "status": analysis["status"],
                    }
                save()
            check(
                "primary journal unchanged during readback",
                journal_path.read_bytes() == journal_bytes,
            )
            check(
                "source environment unchanged during readback",
                environment_fingerprint() == environment,
            )
            report["status"] = "passed"
            report["counts"] = {
                "bundles": len(report["bundles"]),
                "analyses": len(report["analyses"]),
                "checks": len(report["checks"]),
                "explicit_get_requests": len(report["requests"]),
                "session_gets": (
                    "PlatformClient performs one session GET; bootstrap fallback only on 404"
                ),
            }
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        save()
    print(json.dumps({"status": report["status"], "output": str(output), **report["counts"]}))


if __name__ == "__main__":
    main()
