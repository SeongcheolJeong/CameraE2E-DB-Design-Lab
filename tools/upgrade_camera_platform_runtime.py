#!/usr/bin/env python3
"""Verified local runtime transition with backup, immutable-state check and code rollback.

Default is inspection. --apply is appropriate after package, isolated Worker and
installed browser evidence pass. A newly arriving job aborts the transition after
shutdown and resumes the old code; registry data is never overwritten by rollback.
"""

import argparse
import hashlib
import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

import camera_runtime_rollback as recovery
import start_camera_platform as launcher
from install_camera_platform_runtime import select


def read(path):
    return json.loads(Path(path).read_text())


def snapshot(state, *, require_idle=True):
    with sqlite3.connect((state / "registry.sqlite").as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        if (
            require_idle
            and conn.execute("SELECT id FROM jobs WHERE status IN ('queued','running')").fetchone()
        ):
            raise RuntimeError("Active jobs must finish before a runtime transition")
        return {
            table: {
                str(row[key]): hashlib.sha256(
                    json.dumps(dict(row), sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                for row in conn.execute("SELECT * FROM " + table + " ORDER BY " + key)
            }
            for table, key in (("records", "id"), ("jobs", "id"), ("audit", "seq"))
        }


def verify_preserved(before, after):
    """Newly admitted work may append rows after the new API starts serving."""
    for table, rows in before.items():
        if any(
            after.get(table, {}).get(identity) != checksum for identity, checksum in rows.items()
        ):
            raise RuntimeError("Runtime startup changed original records, jobs or audit events")


def launch_bound(state, port, scene_python=None):
    """Bind only the new launch; rollback must retain the prior solver environment."""
    previous = os.environ.get(launcher.MITSUBA_ENV)
    try:
        if scene_python is not None:
            os.environ[launcher.MITSUBA_ENV] = scene_python
        launcher.main(["--state-dir", str(state), "--port", str(port), "--no-open"])
    finally:
        if previous is None:
            os.environ.pop(launcher.MITSUBA_ENV, None)
        else:
            os.environ[launcher.MITSUBA_ENV] = previous


def verify_preparation_evidence(package, installed, ui, preparation_ui, manifest):
    """Require the new feature's actual installed proofs before touching old state."""
    if "camera_platform/scene_camera.py" not in manifest["files"]:
        return
    message = "Installed camera preparation, restart, package and UI evidence must agree"

    def require(value):
        if not value:
            raise RuntimeError(message)

    def passed_checks(proof, minimum):
        checks = proof.get("checks")
        return (
            isinstance(checks, list)
            and len(checks) >= minimum
            and all(isinstance(c, dict) and c.get("status") == "passed" for c in checks)
        )

    require(isinstance(preparation_ui, dict))
    proof = installed.get("preparation")
    require(isinstance(proof, dict))
    require(
        package.get("scene_camera_preparation_api_idempotent_save_to_raw_verified") is True
        and proof.get("schema") == "camera_scene_preparation_verification_v1"
        and proof.get("status") == "passed"
        and proof.get("environment") == installed.get("environment") == manifest["environment"]
        and proof.get("backend") == installed.get("scene", {}).get("backend")
        and proof.get("state_dir") == installed.get("state_dir")
        and proof.get("preview_sha256")
        and proof.get("assembly_sha256")
    )
    artifacts = proof.get("artifact_sha256", {})
    require(
        isinstance(artifacts, dict)
        and all(
            isinstance(artifacts.get(kind), dict)
            and all(artifacts[kind].get(name) for name in names)
            for kind, names in (
                (
                    "render",
                    ("spectral-radiance.npy", "spectral-image.npy", "first-hit-depth-m.npy"),
                ),
                ("camera", ("raw_frames.npy", "rgb_frames.npy")),
            )
        )
    )
    for key in ("preparation_after_development_edit", "preparation_after_restart"):
        repeat = installed.get(key)
        require(isinstance(repeat, dict))
        require(
            repeat.get("status") == "passed"
            and repeat.get("preview_sha256") == proof["preview_sha256"]
            and repeat.get("assembly_sha256") == proof["assembly_sha256"]
            and repeat.get("artifact_sha256") == artifacts
        )
    require(
        ui.get("status") == "passed"
        and ui.get("schema") == "camera_scene_authoring_ui_v1"
        and passed_checks(ui, 36)
        and preparation_ui.get("status") == "passed"
        and preparation_ui.get("schema") == "camera_scene_preparation_ui_v1"
        and passed_checks(preparation_ui, 20)
        and preparation_ui.get("errors") == []
        and preparation_ui.get("environment") == ui.get("environment") == manifest["environment"]
        and preparation_ui.get("backend") == proof["backend"]
        and isinstance(preparation_ui.get("state_dir"), str)
        and isinstance(preparation_ui.get("base_url"), str)
        and preparation_ui["base_url"].rstrip("/") == ui["base_url"].rstrip("/")
        and Path(preparation_ui["state_dir"]).resolve() == Path(ui["state_dir"]).resolve()
    )
    static = preparation_ui.get("static_sha256")
    require(
        isinstance(static, dict)
        and all(
            static.get(name) == manifest["files"].get("camera_platform/static/" + name)
            and static.get(name)
            for name in ("scenes.js", "style.css")
        )
    )


def verify_projection_evidence(package, installed, ui, projection_ui, manifest):
    """Admit stored-radiance packages only with matching installed and UI proofs."""
    if "camera_platform/scene_projection.py" not in manifest["files"]:
        return
    message = "Installed radiance projection, restart, package and UI evidence must agree"

    def require(value):
        if not value:
            raise RuntimeError(message)

    def checksum(value):
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(c in "0123456789abcdef" for c in value)
        )

    def linked_records(value):
        return isinstance(value, dict) and all(
            isinstance(value.get(kind), str) and bool(value[kind].strip())
            for kind in ("source_render", "assembly", "projection", "image", "test", "run")
        )

    proof = installed.get("projection")
    require(isinstance(proof, dict) and isinstance(projection_ui, dict))
    require(
        package.get("scene_projection_api_to_raw_without_renderer_verified") is True
        and proof.get("schema") == "camera_scene_projection_verification_v1"
        and proof.get("status") == "passed"
        and proof.get("environment") == installed.get("environment") == manifest["environment"]
        and isinstance(proof.get("state_dir"), str)
        and bool(proof["state_dir"])
        and proof["state_dir"] == installed.get("state_dir")
    )
    records = proof.get("records")
    require(linked_records(records))
    artifacts = proof.get("artifact_sha256")
    require(
        isinstance(artifacts, dict)
        and all(
            checksum(artifacts.get(name))
            for name in (
                "original_radiance",
                "original_irradiance",
                "original_mc_standard_error",
                "new_irradiance",
                "new_mc_standard_error",
                "raw",
                "rgb",
            )
        )
    )
    for key in ("projection_after_development_edit", "projection_after_restart"):
        repeat = installed.get(key)
        require(isinstance(repeat, dict))
        require(
            repeat.get("status") == "passed"
            and repeat.get("environment") == proof["environment"]
            and repeat.get("state_dir") == proof["state_dir"]
            and linked_records(repeat.get("records"))
            and repeat["records"]["source_render"] == records["source_render"]
            and repeat["records"]["assembly"] == records["assembly"]
            and repeat.get("artifact_sha256") == artifacts
        )
    checks = projection_ui.get("checks")
    require(
        projection_ui.get("schema") == "camera_scene_projection_ui_v1"
        and projection_ui.get("status") == "passed"
        and projection_ui.get("errors") == []
        and isinstance(checks, list)
        and len(checks) >= 22
        and all(isinstance(c, dict) and c.get("status") == "passed" for c in checks)
        and projection_ui.get("environment") == ui.get("environment") == manifest["environment"]
        and isinstance(projection_ui.get("state_dir"), str)
        and bool(projection_ui["state_dir"])
        and isinstance(ui.get("state_dir"), str)
        and isinstance(projection_ui.get("base_url"), str)
        and bool(projection_ui["base_url"])
        and isinstance(ui.get("base_url"), str)
        and projection_ui["base_url"].rstrip("/") == ui["base_url"].rstrip("/")
        and Path(projection_ui["state_dir"]).resolve() == Path(ui["state_dir"]).resolve()
    )
    static = projection_ui.get("static_sha256")
    require(
        isinstance(static, dict)
        and all(
            checksum(static.get(name))
            and static[name] == manifest["files"].get("camera_platform/static/" + name)
            for name in ("scenes.js", "style.css")
        )
    )


PRESCRIPTION_CHECKS = (
    "Exact QA state, main-state exclusion, version and source binding verified",
    "Actual Worker radiometry and signed prescription field PSF share unchanged input",
    "Factory original artifacts, numeric gates and physical approximation limits verified",
    "Published optics retain the exact radiometry parent and physical field scale",
    "Declared 128 by 128 sensor at 3 micrometres and fixed 500/600 nm assembly verified",
    "HTTP/Worker field optics generate actual nondegenerate 64 by 64 RAW and RGB",
    "Changed white balance modifies RGB while preserving every original RAW value",
    "Camera and replay ZIPs retain both Factory originals and complete model lineage",
    "Every downloaded artifact and ZIP entry matches its declared SHA-256 and byte count",
    "Synthetic status, unknown uncertainty and physical-validation limits preserved",
    "Original source records and bound executable/UI sources remain unchanged",
)
PRESCRIPTION_STATIC = ("index.html", "app.js", "scenes.js", "workbench.js", "style.css")


def proof_digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def proof_checksum(value):
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def verify_prescription_evidence(package, installed, ui, prescription_ui, manifest):
    """Bind successful prescription work, its originals, and UI publication to the wheel.

    Counts alone cannot admit the feature. The initial and read-only rechecks must
    retain exact requests, parents, outputs, numeric gates and installed imports.
    UI work may use a different isolated state; main() also checks its live records.
    """
    if "camera_platform/prescription_psf.py" not in manifest["files"]:
        return
    message = (
        "Installed prescription optics, originals, restart, package and UI evidence must agree"
    )

    def require(value):
        if not value:
            raise RuntimeError(message)

    def finite(value):
        return type(value) in (int, float) and math.isfinite(value)

    def checks(proof):
        rows = proof.get("checks")
        return (
            isinstance(rows, list)
            and all(isinstance(row, dict) and row.get("status") == "passed" for row in rows)
            and set(PRESCRIPTION_CHECKS) <= {row.get("name") for row in rows}
        )

    def static(proof):
        values = proof.get("static_sha256")
        return isinstance(values, dict) and all(
            proof_checksum(values.get(name))
            and values[name] == manifest["files"].get("camera_platform/static/" + name)
            for name in PRESCRIPTION_STATIC
        )

    def record_hash(record):
        require(isinstance(record, dict) and proof_checksum(record.get("content_hash")))
        require(
            proof_digest({k: v for k, v in record.items() if k != "content_hash"})
            == record["content_hash"]
        )
        return proof_digest(record)

    proof = installed.get("prescription")
    require(isinstance(proof, dict) and isinstance(prescription_ui, dict))
    require(
        package.get("prescription_optics_api_to_raw_replay_and_parent_bundle_verified") is True
        and proof.get("schema") == "camera_prescription_optics_verification_v1"
        and proof.get("status") == "passed"
        and checks(proof)
        and proof.get("environment") == installed.get("environment") == manifest["environment"]
        and proof.get("running_version") == manifest["environment"].get("platform_version")
        and isinstance(proof.get("state_dir"), str)
        and bool(proof["state_dir"])
        and proof["state_dir"] == installed.get("state_dir")
        and isinstance(proof.get("base_url"), str)
        and bool(proof["base_url"])
        and proof.get("inputs") == {"state_dir": proof["state_dir"], "base_url": proof["base_url"]}
        and proof.get("source_kind") == "simulated"
        and proof.get("physical_validation") == "not_established"
        and proof.get("total_uncertainty") == {"kind": "unknown"}
        and static(proof)
    )
    source = manifest["environment"].get("source_hashes")
    require(isinstance(source, dict) and source.get("camera_platform/prescription_psf.py"))
    require(
        all(
            proof_checksum(value) and manifest["files"].get(name) == value
            for name, value in source.items()
        )
    )
    binding = proof.get("runtime_binding")
    require(isinstance(binding, dict) and isinstance(installed.get("runtime_root"), str))
    root = Path(installed["runtime_root"]).resolve()
    require(
        binding.get("mode") == "verified_installed_package"
        and binding.get("runtime_root") == str(root)
        and binding.get("wheel_sha256") == manifest.get("wheel_sha256")
        and proof_checksum(binding.get("wheel_sha256"))
        and binding.get("environment_sha256") == proof_digest(manifest["environment"])
        and binding.get("manifest_sha256")
        == hashlib.sha256((root / "runtime-manifest.json").read_bytes()).hexdigest()
    )
    modules = {
        "camera_platform": "camera_platform/__init__.py",
        "camera_platform.prescription_psf": "camera_platform/prescription_psf.py",
        "camera_platform.service": "camera_platform/service.py",
        "camera_platform.contracts": "camera_platform/contracts.py",
        "camera_platform.sdk": "camera_platform/sdk.py",
        "camera_platform.registry": "camera_platform/registry.py",
        "pyisetcam": "pyisetcam/__init__.py",
    }
    require(
        binding.get("module_paths") == {name: str(root / path) for name, path in modules.items()}
    )
    records = proof.get("records")
    kinds = (
        "source_assembly",
        "radiometry",
        "psf",
        "parent_lens",
        "lens",
        "sensor",
        "assembly",
        "image",
        "test",
        "run",
        "replay_isp",
        "replay",
    )
    require(
        isinstance(records, dict)
        and all(isinstance(records.get(k), str) and bool(records[k].strip()) for k in kinds)
    )
    require(len({records[k] for k in kinds}) == len(kinds))
    published = proof.get("published_record_sha256")
    require(
        isinstance(published, dict)
        and set(published)
        == {
            records[k]
            for k in ("parent_lens", "lens", "sensor", "assembly", "image", "test", "replay_isp")
        }
        and all(proof_checksum(v) for v in published.values())
    )
    require(
        all(
            proof_checksum(proof.get(k + "_record_sha256"))
            for k in ("radiometry", "psf", "camera", "replay")
        )
    )
    require(proof_checksum(proof.get("source_assembly_sha256")))
    originals = proof.get("source_model_sha256")
    require(
        isinstance(originals, dict)
        and len(originals) >= 3
        and all(proof_checksum(v) for v in originals.values())
    )
    request, psf, ri = (
        proof.get(k) for k in ("prescription_request", "psf_summary", "radiometry_summary")
    )
    require(all(isinstance(v, dict) for v in (request, psf, ri)))
    require(
        request.get("schema") == "prescription_field_psf_v1"
        and request.get("field_x_mm") == request.get("field_y_mm") == [-0.5, 0.0, 0.5]
        and request.get("pixel_pitch_um") == 3.0
        and request.get("kernel_size") == 41
        and request.get("pupil_samples") == 32
        and request.get("subpixel_samples") == 3
        and request.get("max_kernel_l1_change") == 0.03
        and request.get("minimum_retained_energy") == 0.95
        and request.get("maximum_exit_pupil_mapping_fraction") == 0.03
        and request.get("maximum_ray_cone_deg") == 8.0
        and psf.get("normalized_request") == request
        and isinstance(request.get("prescription"), dict)
        and ri.get("normalized_request") == request["prescription"]
        and request["prescription"].get("image_height_mm") == [0.0, 0.375, 0.75]
        and request["prescription"].get("wavelength_nm") == [500.0, 600.0]
        and all(
            s.get("status") == "PROPOSED"
            and s.get("source_kind") == "simulated"
            and s.get("approval_status") == "unapproved"
            and s.get("uncertainty") == {"kind": "unknown"}
            and s.get("physical_validation") == "not_established"
            and s.get("issues") == []
            and s.get("request_sha256") == proof_digest(s["normalized_request"])
            for s in (psf, ri)
        )
        and psf.get("numerical_agreement") is True
        and psf.get("schema") == "prescription_field_psf_characterization_v1"
        and psf.get("factory_id") == "rayoptics_prescription_field_psf_v1"
        and psf.get("approximation_domain_checks") is True
        and psf.get("propagation_phase") == "exp(-2*pi*i*opd_nm/wavelength_nm)"
        and any("transport is not solved" in v for v in psf.get("assumptions", []))
        and any("Jacobian" in v for v in psf.get("limitations", []))
        and ri.get("convergence", {}).get("observed_two_level_agreement") is True
    )
    for observed, threshold, lower in (
        ("max_kernel_l1_change_observed", "max_kernel_l1_change", False),
        ("minimum_retained_energy_observed", "minimum_retained_energy", True),
        (
            "maximum_exit_pupil_mapping_fraction_observed",
            "maximum_exit_pupil_mapping_fraction",
            False,
        ),
        ("maximum_ray_cone_deg_observed", "maximum_ray_cone_deg", False),
    ):
        value, limit = psf.get(observed), request.get(threshold)
        require(
            finite(value)
            and finite(limit)
            and (value > 0 if lower else value >= 0)
            and limit > 0
            and (value >= limit if lower else value <= limit)
        )
    f_number = psf.get("geometric_reference_f_number")
    require(finite(f_number) and f_number > 0)
    for observed, limit in (
        ("maximum_neighbor_opd_waves_observed", 0.25),
        ("maximum_retained_energy_observed", 1.001),
        ("maximum_reference_axis_deg_observed", 2.0),
    ):
        value = psf.get(observed)
        require(finite(value) and 0 <= value <= limit)
    require(psf["maximum_retained_energy_observed"] >= psf["minimum_retained_energy_observed"])
    geometry = psf.get("geometry")
    require(
        isinstance(geometry, list)
        and bool(geometry)
        and all(
            isinstance(g, dict) and g.get("pupil_boundary_truncated") is False for g in geometry
        )
    )
    for observed, threshold in (
        ("relative_illumination_absolute_change", "ri_tolerance_absolute"),
        ("angular_cdf_max_absolute_change", "angular_cdf_tolerance_absolute"),
    ):
        values = ri["convergence"].get(observed)
        limit = request["prescription"].get("sampling", {}).get(threshold)
        require(
            finite(limit)
            and limit > 0
            and isinstance(values, list)
            and len(values) == 2
            and all(
                isinstance(row, list)
                and len(row) == 3
                and all(finite(v) and 0 <= v <= limit for v in row)
                for row in values
            )
        )
    operations = proof.get("operations")
    require(isinstance(operations, dict))
    payloads = {}
    for kind in kinds[1:]:
        operation = operations.get(kind)
        require(
            isinstance(operation, dict) and operation.get("status") in {"confirmed", "reconciled"}
        )
        intent = operation.get("intent", {})
        require(isinstance(intent.get("payload"), dict))
        payloads[kind] = intent["payload"]
    conditions = payloads["lens"].get("conditions")
    require(
        payloads["radiometry"].get("request") == request["prescription"]
        and payloads["psf"].get("request") == request
        and payloads["parent_lens"].get("job_id") == records["radiometry"]
        and payloads["lens"].get("job_id") == records["psf"]
        and payloads["lens"].get("base_lens_model_id") == records["parent_lens"]
        and isinstance(conditions, dict)
        and conditions.get("f_number") == {"value": f_number, "unit": "1"}
        and payloads["assembly"].get("conditions") == conditions
        and records["lens"] in payloads["assembly"].get("model_ids", [])
        and records["sensor"] in payloads["assembly"].get("model_ids", [])
        and payloads["assembly"].get("spectral_range") == {"min": 500, "max": 600, "unit": "nm"}
        and payloads["run"].get("assembly_id") == records["assembly"]
        and payloads["run"].get("test_id") == records["test"]
        and payloads["run"].get("scene_id") == records["image"]
        and payloads["replay"].get("source_id") == records["run"]
        and payloads["replay"].get("isp_model_id") == records["replay_isp"]
    )
    artifacts = proof.get("artifact_sha256")
    require(isinstance(artifacts, dict))
    by_name = {}
    required = {
        "radiometry": (
            "relative_illumination.npy",
            "angular_weights.npy",
            "irradiance_per_radiance_sr.npy",
        ),
        "psf": (
            "psf_kernel.npy",
            "fine_opd_nm.npy",
            "fine_admitted.npy",
            "field_x_mm.npy",
            "field_y_mm.npy",
        ),
        "image": ("spectral-image.npy",),
        "camera": (
            "raw_frames.npy",
            "rgb_frames.npy",
            "psf_intensity.npy",
            "psf_field_x_mm.npy",
            "psf_field_y_mm.npy",
            "sensor_radius_mm.npy",
            "response_radius_map.npy",
            "spectral-image.npy",
        ),
        "replay": ("raw_frames.npy", "rgb_frames.npy"),
    }
    for kind, names in required.items():
        group = artifacts.get(kind)
        require(isinstance(group, dict) and bool(group))
        values = {}
        for key, size in group.items():
            require(isinstance(key, str) and ":" in key)
            name, checksum = key.rsplit(":", 1)
            require(
                name not in values and proof_checksum(checksum) and type(size) is int and size > 0
            )
            values[name] = (checksum, size)
        require(set(names) <= values.keys())
        by_name[kind] = values
    require(
        by_name["camera"]["raw_frames.npy"] == by_name["replay"]["raw_frames.npy"]
        and by_name["camera"]["rgb_frames.npy"][0] != by_name["replay"]["rgb_frames.npy"][0]
        and by_name["camera"]["psf_intensity.npy"] == by_name["psf"]["psf_kernel.npy"]
        and by_name["camera"]["spectral-image.npy"] == by_name["image"]["spectral-image.npy"]
        and all(
            by_name["camera"]["psf_field_" + axis + "_mm.npy"]
            == by_name["psf"]["field_" + axis + "_mm.npy"]
            for axis in ("x", "y")
        )
    )
    immutable = (
        "schema",
        "inputs",
        "state_dir",
        "base_url",
        "records",
        "operations",
        "environment",
        "runtime_binding",
        "running_version",
        "static_sha256",
        "prescription_request",
        "radiometry_summary",
        "psf_summary",
        "artifact_sha256",
        "published_record_sha256",
        "source_assembly_sha256",
        "source_model_sha256",
        "radiometry_record_sha256",
        "psf_record_sha256",
        "camera_record_sha256",
        "replay_record_sha256",
        "source_kind",
        "physical_validation",
        "total_uncertainty",
    )
    for name in (
        "prescription",
        "prescription_after_development_edit",
        "prescription_after_restart",
    ):
        repeated = installed.get(name)
        require(
            isinstance(repeated, dict) and repeated.get("status") == "passed" and checks(repeated)
        )
        require(all(repeated.get(k) == proof[k] for k in immutable))
        invocation = repeated.get("latest_invocation", {})
        require(
            proof_checksum(invocation.get("verifier_sha256"))
            and invocation["verifier_sha256"]
            == proof.get("latest_invocation", {}).get("verifier_sha256")
        )
        for key in ("counts_before", "counts_after"):
            counts = invocation.get(key)
            require(
                isinstance(counts, dict)
                and bool(counts)
                and all(type(v) is int and v >= 0 for v in counts.values())
            )
        if name != "prescription":
            require(
                invocation.get("read_only") is True
                and type(invocation.get("post_count")) is int
                and invocation["post_count"] == 0
                and invocation["counts_before"] == invocation["counts_after"]
            )
        else:
            invocations = repeated.get("invocations")
            require(
                invocation.get("read_only") is False
                and type(invocation.get("post_count")) is int
                and invocation["post_count"] >= 0
                and isinstance(invocations, list)
                and any(
                    isinstance(i, dict)
                    and i.get("read_only") is False
                    and type(i.get("post_count")) is int
                    and i["post_count"] > 0
                    for i in invocations
                )
            )
        bundles = repeated.get("bundles")
        require(isinstance(bundles, dict))
        for kind in ("camera", "replay"):
            bundle = bundles.get(kind, {})
            originals = {"camera", "radiometry", "psf"} | (
                {"replay"} if kind == "replay" else set()
            )
            original_entries = set().union(*(set(artifacts[k]) for k in originals))
            require(
                proof_checksum(bundle.get("download_sha256"))
                and proof_checksum(bundle.get("package_hash"))
                and type(bundle.get("artifacts_verified")) is int
                and bundle["artifacts_verified"] == len(original_entries)
                and all(
                    bundle.get(k) == proof.get("bundles", {}).get(kind, {}).get(k)
                    for k in ("package_hash", "artifacts_verified")
                )
            )
    ui_checks = prescription_ui.get("checks")
    require(
        prescription_ui.get("schema") == "camera_prescription_ui_verification_v1"
        and prescription_ui.get("status") == "passed"
        and prescription_ui.get("errors") == []
        and isinstance(ui_checks, list)
        and len(ui_checks) >= 8
        and all(isinstance(c, str) and bool(c.strip()) for c in ui_checks)
        and len(set(ui_checks)) == len(ui_checks)
        and prescription_ui.get("environment") == ui.get("environment") == manifest["environment"]
        and prescription_ui.get("physical_validation") == "not_established"
        and isinstance(prescription_ui.get("state_dir"), str)
        and bool(prescription_ui["state_dir"])
        and isinstance(prescription_ui.get("base_url"), str)
        and bool(prescription_ui["base_url"])
        and static(prescription_ui)
    )
    model, assembly = (prescription_ui.get(k) for k in ("publication", "assembly"))
    require(record_hash(model) == prescription_ui.get("model_sha256"))
    require(record_hash(assembly) == prescription_ui.get("assembly_sha256"))
    require(
        model.get("id") == prescription_ui.get("model_id")
        and bool(model.get("id"))
        and assembly.get("id") == prescription_ui.get("assembly_id")
        and bool(assembly.get("id"))
        and model.get("kind") == "lens"
        and model.get("source_kind") == "simulated"
        and model.get("uncertainty") == {"kind": "unknown"}
        and isinstance(model.get("parents"), list)
        and len(model["parents"]) == 1
        and bool(model["parents"][0])
        and bool(model.get("psf_factory_job_id"))
        and model.get("factory", {}).get("physical_validation") == "not_established"
        and model.get("properties", {}).get("f_number") == {"value": f_number, "unit": "1"}
        and model.get("factory_application_conditions") == assembly.get("conditions")
        and assembly.get("conditions", {}).get("f_number") == {"value": f_number, "unit": "1"}
        and model["id"] in assembly.get("model_ids", [])
        and assembly.get("model_hashes", {}).get(model["id"]) == model["content_hash"]
        and assembly.get("spectral_range") == {"min": 500, "max": 600, "unit": "nm"}
        and assembly.get("compatibility", {}).get("status") == "PASS"
        and assembly.get("compatibility", {}).get("compatible") is True
    )
    payload = model.get("payload", {})
    kernel = payload.get("psf", {})
    require(
        payload.get("field_reference_radius_mm") == 0.75
        and kernel.get("schema") == "field_intensity_psf_v1"
        and kernel.get("spatial_model") == "field_grid"
        and kernel.get("field_coordinates") == "sensor_center_yx"
        and kernel.get("interpolation") == "bilinear_source_position"
        and kernel.get("field_x_mm") == kernel.get("field_y_mm") == request["field_x_mm"]
        and kernel.get("wavelength_nm") == [500.0, 600.0]
        and kernel.get("sample_pitch_um") == 3.0
        and kernel.get("source", {}).get("kind") == "simulated"
        and isinstance(kernel.get("values"), list)
        and len(kernel["values"]) == 2
    )


def verify_prescription_ui_records(proof, prescription_request):
    """The separately bound live UI state must still serve the exact reviewed records."""
    base = proof["base_url"].rstrip("/")
    parsed = urlsplit(base)
    if not (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path in ("", "/")
    ):
        raise RuntimeError("Prescription browser evidence must use a local QA API")

    def get(collection, identity):
        with urlopen(
            base + "/api/v1/" + collection + "/" + quote(identity, safe=""), timeout=20
        ) as response:
            return json.load(response)

    for collection, key, identity in (
        ("models", "publication", "model_id"),
        ("assemblies", "assembly", "assembly_id"),
    ):
        actual = get(collection, proof[identity])
        if actual != proof[key]:
            raise RuntimeError(
                "Prescription browser evidence differs from the live reviewed record"
            )
    lens = proof["publication"]
    parent = get("models", lens["parents"][0])
    psf = get("factory/jobs", lens["psf_factory_job_id"])
    if not isinstance(parent.get("factory_job_id"), str) or not parent["factory_job_id"]:
        raise RuntimeError("Prescription browser publication has no radiometry parent job")
    radiometry = get("factory/jobs", parent["factory_job_id"])
    if not (
        all(
            job.get("status") == "succeeded"
            and job.get("environment") == proof["environment"]
            and job.get("result", {}).get("summary", {}).get("status") == "PROPOSED"
            for job in (psf, radiometry)
        )
        and psf.get("request") == prescription_request
        and radiometry.get("request") == prescription_request["prescription"]
        and psf["result"].get("model_candidate") == lens["payload"]["psf"]
        and parent.get("kind") == "lens"
        and parent.get("source_kind") == "simulated"
        and parent.get("payload")
        == {
            k: v
            for k, v in lens["payload"].items()
            if k not in {"psf", "field_reference_radius_mm"}
        }
    ):
        raise RuntimeError(
            "Prescription browser publication lost its exact Factory parent evidence"
        )


def stop_verified(state, root, port):
    witness = read(state / "runtime-process.json")
    pid = witness["pid"]
    if witness["runtime_root"] != str(root):
        raise RuntimeError("Refusing to stop a different installed runtime")
    binding = launcher.load_installed_runtime(state)
    if binding["runtime_root"] != str(root):
        raise RuntimeError("Process and selected runtime differ before shutdown")
    python = launcher.verify_runtime_python(binding)
    recovery.stop_runtime(state, binding, python, expected_pid=pid)
    with socket.socket() as listener:
        if listener.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError("A listener remains after managed processes stopped")
    return pid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--expected-old-version", required=True)
    parser.add_argument("--package-proof", type=Path, required=True)
    parser.add_argument("--runtime-proof", type=Path, required=True)
    parser.add_argument("--ui-proof", type=Path, required=True)
    parser.add_argument("--preparation-ui-proof", type=Path)
    parser.add_argument("--projection-ui-proof", type=Path)
    parser.add_argument("--prescription-ui-proof", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--mitsuba-python", type=Path)
    parser.add_argument(
        "--python",
        type=Path,
        help="Verified candidate runtime Python; omission reuses the old selection",
    )
    parser.add_argument(
        "--assets-manifest",
        type=Path,
        help="Verified candidate asset manifest; omission preserves the previous asset selection",
    )
    parser.add_argument("--case-delta-proof", type=Path)
    parser.add_argument("--case-source-approval-sha256")
    args = parser.parse_args(sys.argv[1:])
    if args.case_source_approval_sha256 and not args.case_delta_proof:
        parser.error("--case-source-approval-sha256 requires --case-delta-proof")
    with recovery.transition_lock(args.state_dir.resolve()):
        return transition(args)


def transition(args):
    state, new = args.state_dir.resolve(), args.runtime_root.resolve()
    old_binding = launcher.load_installed_runtime(state)
    if old_binding is None:
        raise RuntimeError("A verified installed runtime must be selected before upgrade")
    old = Path(old_binding["runtime_root"])
    base = f"http://127.0.0.1:{args.port}/"
    launcher.verify_running_package(base, state, old_binding)
    old_manifest, new_manifest = launcher.verify_manifest(old), launcher.verify_manifest(new)
    if old_manifest["environment"]["platform_version"] != args.expected_old_version or old == new:
        raise RuntimeError("Current runtime differs from the explicitly expected source version")
    package, installed, ui = read(args.package_proof), read(args.runtime_proof), read(args.ui_proof)
    case_delta = None
    if args.case_delta_proof:
        from camera_case_delta import qualify

        scene_python, case_delta = qualify(args, old_manifest, new_manifest, old, new)
    else:
        if not (
            package["status"] == installed["status"] == ui["status"] == "passed"
            and package["wheel_sha256"] == new_manifest["wheel_sha256"]
            and package["environment"] == installed["environment"] == new_manifest["environment"]
            and installed["runtime_root"] == str(new)
            and installed["isolated_server_stopped"]
            and package["response_original_file_common_operator_and_reusable_analysis_verified"]
            and installed["response"]["status"] == "passed"
            and not ui["errors"]
        ):
            raise RuntimeError("Package, installed Worker and browser evidence must agree")
        scene_python = None
        if "camera_platform/scene_render.py" in new_manifest["files"]:
            if args.mitsuba_python is None:
                raise RuntimeError(
                    "Scene runtime transition requires an explicit Mitsuba interpreter"
                )
            scene_python = launcher._scene_interpreter(args.mitsuba_python)
            backend = launcher.probe_scene_interpreter(scene_python, runtime_root=new)
            if not (
                package.get("scene_authoring_api_and_renderer_to_raw_verified")
                and installed.get("scene", {}).get("status") == "passed"
                and installed.get("scene_after_restart")
                and installed.get("scene_after_development_edit")
                and ui.get("environment") == new_manifest["environment"]
                and ui.get("schema") == "camera_scene_authoring_ui_v1"
                and package.get("scene_backend") == installed["scene"]["backend"] == backend
            ):
                raise RuntimeError(
                    "Installed scene renderer, restart, package and UI evidence must agree"
                )
        preparation_ui = read(args.preparation_ui_proof) if args.preparation_ui_proof else None
        verify_preparation_evidence(package, installed, ui, preparation_ui, new_manifest)
        projection_ui = read(args.projection_ui_proof) if args.projection_ui_proof else None
        verify_projection_evidence(package, installed, ui, projection_ui, new_manifest)
        prescription_ui = read(args.prescription_ui_proof) if args.prescription_ui_proof else None
        verify_prescription_evidence(package, installed, ui, prescription_ui, new_manifest)
        ui_state = Path(ui["state_dir"])
        ui_binding = launcher.load_installed_runtime(ui_state)
        if ui_binding["runtime_root"] != str(new) or ui_state.resolve() == state:
            raise RuntimeError("Browser evidence must target the new package in an isolated state")
        launcher.verify_running_package(ui["base_url"] + "/", ui_state, ui_binding)
        if "camera_platform/prescription_psf.py" in new_manifest["files"]:
            prescription_state = Path(prescription_ui["state_dir"]).resolve()
            prescription_binding = launcher.load_installed_runtime(prescription_state)
            if prescription_binding["runtime_root"] != str(new) or prescription_state == state:
                raise RuntimeError(
                    "Prescription browser evidence must target an isolated new package"
                )
            launcher.verify_running_package(
                prescription_ui["base_url"].rstrip("/") + "/",
                prescription_state,
                prescription_binding,
            )
            verify_prescription_ui_records(
                prescription_ui, installed["prescription"]["prescription_request"]
            )
    # Resolve the previous selection independently from the Python running this
    # transition script. Reuse it by default, but never substitute it for an
    # explicitly requested candidate dependency environment.
    old_python = launcher.verify_runtime_python(old_binding)
    candidate_python = (
        launcher.runtime_python_path(args.python) if args.python is not None else old_python
    )
    candidate_assets = (
        launcher.asset_binding_from_manifest(args.assets_manifest)
        if args.assets_manifest is not None
        else old_binding.get("assets")
    )
    bind_candidate_python = (
        args.python is not None
        or args.assets_manifest is not None
        or old_binding["schema"] == launcher.PIN_SCHEMA_V2
    )
    candidate_environment = launcher.runtime_asset_environment(
        {"schema": launcher.PIN_SCHEMA_V2, "assets": candidate_assets}
        if bind_candidate_python
        else old_binding
    )
    if (
        launcher.probe_package(candidate_python, new, candidate_environment)["environment"]
        != new_manifest["environment"]
    ):
        raise RuntimeError("Candidate Python differs from the verified package environment")
    original_pin = {k: v for k, v in old_binding.items() if k != "manifest"}
    before = snapshot(state)
    report = {
        "schema": "camera_runtime_transition_v1",
        "status": "prepared",
        "state_dir": str(state),
        "from": args.expected_old_version,
        "to": new_manifest["environment"]["platform_version"],
        "old_runtime_root": str(old),
        "new_runtime_root": str(new),
        "before": before,
        "package_proof": str(args.package_proof),
        "runtime_proof": str(args.runtime_proof),
        "ui_proof": str(args.ui_proof),
        "preparation_ui_proof": str(args.preparation_ui_proof)
        if args.preparation_ui_proof
        else None,
        "projection_ui_proof": str(args.projection_ui_proof) if args.projection_ui_proof else None,
        "prescription_ui_proof": str(args.prescription_ui_proof)
        if args.prescription_ui_proof
        else None,
        "old_pid": read(state / "runtime-process.json")["pid"],
        "old_python": old_python,
        "candidate_python": candidate_python,
        "candidate_assets": candidate_assets,
        "original_runtime_pin": original_pin,
    }
    if case_delta is not None:
        report["case_delta"] = case_delta
    if not args.apply:
        print(json.dumps({k: v for k, v in report.items() if k != "before"}))
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    recovery_directory = args.output.parent / (args.output.stem + "-recovery-" + stamp)
    baseline = recovery.capture_state(state, recovery_directory, "before")
    report["recovery_baseline"] = baseline
    backup = (
        launcher.WORKSPACE / "outputs/camera-platform-backups" / f"pre-v{report['to']}-{stamp}.zip"
    )
    script = (
        "import sys,json;sys.path.insert(0,sys.argv[1]);"
        "from camera_platform.backup import backup_state;"
        "r=backup_state(sys.argv[2],sys.argv[3]);"
        "print(json.dumps({'manifest_hash':r['manifest_hash']}))"
    )
    saved = subprocess.run(
        [old_python, "-I", "-B", "-c", script, str(old), str(state), str(backup)],
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
        env=launcher.runtime_asset_environment(old_binding),
    )
    report.update(
        backup=str(backup), backup_manifest_hash=json.loads(saved.stdout)["manifest_hash"]
    )
    if snapshot(state) != before:
        raise RuntimeError("State changed during backup; leave the current runtime running")
    stop_attempted = False
    candidate_pin = None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    try:
        stop_attempted = True
        stop_verified(state, old, args.port)
        # Catch jobs/records admitted between the idle check and actual shutdown.
        if snapshot(state) != before:
            raise RuntimeError("State changed before shutdown; resume the previous runtime")
        # The old process may have completed a content-addressed blob write before
        # shutdown. Bind the exact stopped baseline before the candidate can migrate.
        with recovery.quiescent_state(state, old_python):
            stopped_baseline = recovery.capture_state(state, recovery_directory, "stopped-old")
            if not recovery.original_prefix_preserved(baseline, stopped_baseline):
                raise RuntimeError("Original state changed before candidate selection")
            baseline = stopped_baseline
            report["selection_baseline"] = baseline
        candidate_pin = select(
            new, state, candidate_python, bind_python=bind_candidate_python, assets=candidate_assets
        )
        launch_bound(state, args.port, scene_python)
        launcher.verify_running_package(base, state, launcher.load_installed_runtime(state))
        # The verified new API may already have accepted legitimate new requests.
        # Preserve the old prefix; never roll back its code merely because new work arrived.
        after = snapshot(state, require_idle=False)
        verify_preserved(before, after)
        report.update(
            status="passed",
            after=after,
            new_pid=read(state / "runtime-process.json")["pid"],
            preserved_counts={table: len(rows) for table, rows in before.items()},
            appended_counts={
                table: len(after[table]) - len(rows) for table, rows in before.items()
            },
        )
    except BaseException as error:
        report.update(status="failed", error=str(error))
        if stop_attempted:
            active_binding = candidate_pin or old_binding
            active_python = candidate_python if candidate_pin else old_python
            active_environment = (
                candidate_environment
                if candidate_pin
                else launcher.runtime_asset_environment(old_binding)
            )
            try:
                # Never discard a failed stop's captured groups and retry with a
                # weaker observation after their leaders may have exited.
                if getattr(error, "evidence", {}).get("process_closure"):
                    raise recovery.RecoveryRefusedError(
                        "Process closure could not be proven; retain current state and binding",
                        error.evidence,
                    )
                report["recovery_processes"] = recovery.stop_runtime(
                    state, active_binding, active_python
                )
                with socket.socket() as listener:
                    if listener.connect_ex(("127.0.0.1", args.port)) == 0:
                        raise recovery.RecoveryRefusedError("State port is not quiescent")
                with recovery.quiescent_state(
                    state,
                    active_python,
                    private_groups=report["recovery_processes"]["private_groups"],
                ) as lease:
                    report["recovery"] = recovery.recover_schema(
                        state,
                        baseline,
                        active_binding,
                        active_python,
                        active_environment,
                        recovery_directory,
                        lease,
                    )
                    launcher.restore_runtime_pin(state, original_pin, executable=old_python)
                launcher.main(
                    [
                        "--state-dir",
                        str(state),
                        "--port",
                        str(args.port),
                        "--no-open",
                        "--python",
                        old_python,
                    ]
                )
                launcher.verify_running_package(base, state, launcher.load_installed_runtime(state))
                report["old_runtime_restored"] = True
            except BaseException as recovery_error:
                report.update(
                    status="forward_recovery_required",
                    old_runtime_restored=False,
                    recovery_error=str(recovery_error),
                    recovery_evidence=getattr(recovery_error, "evidence", {}),
                )
        raise
    finally:
        report["verified_at"] = datetime.now(UTC).isoformat()
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps({k: report[k] for k in ("status", "from", "to", "preserved_counts", "backup")})
    )


if __name__ == "__main__":
    main()
