"""Prescription installation admission binds real originals and reviewed contracts."""

import hashlib
import importlib.util
import io
import json
from copy import deepcopy
from pathlib import Path

import pytest

from camera_platform.prescription_psf import request_template
from camera_platform.registry import digest


@pytest.fixture
def transition(monkeypatch):
    tools = Path(__file__).resolve().parents[2] / "tools"
    monkeypatch.syspath_prepend(str(tools))
    spec = importlib.util.spec_from_file_location(
        "prescription_transition", tools / "upgrade_camera_platform_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checksum(label):
    return hashlib.sha256(label.encode()).hexdigest()


def freeze(record):
    record.pop("content_hash", None)
    record["content_hash"] = digest(record)
    return record


@pytest.fixture
def evidence(tmp_path, transition):
    root = tmp_path / "installed"
    root.mkdir()
    modules = {
        "camera_platform": "camera_platform/__init__.py",
        "camera_platform.prescription_psf": "camera_platform/prescription_psf.py",
        "camera_platform.service": "camera_platform/service.py",
        "camera_platform.contracts": "camera_platform/contracts.py",
        "camera_platform.sdk": "camera_platform/sdk.py",
        "camera_platform.registry": "camera_platform/registry.py",
        "pyisetcam": "pyisetcam/__init__.py",
    }
    source = {v: checksum(v) for v in modules.values()}
    environment = {"platform_version": "0.14.0", "source_hashes": source}
    static = {k: checksum(k) for k in transition.PRESCRIPTION_STATIC}
    manifest = {
        "schema": "camera_installed_runtime_v1",
        "wheel_sha256": checksum("wheel"),
        "environment": environment,
        "files": {**source, **{"camera_platform/static/" + k: v for k, v in static.items()}},
    }
    manifest_path = root / "runtime-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    request = request_template()
    summary = {
        "status": "PROPOSED",
        "source_kind": "simulated",
        "approval_status": "unapproved",
        "uncertainty": {"kind": "unknown"},
        "physical_validation": "not_established",
        "issues": [],
    }
    psf = {
        **summary,
        "schema": "prescription_field_psf_characterization_v1",
        "factory_id": "rayoptics_prescription_field_psf_v1",
        "normalized_request": deepcopy(request),
        "request_sha256": digest(request),
        "numerical_agreement": True,
        "approximation_domain_checks": True,
        "propagation_phase": "exp(-2*pi*i*opd_nm/wavelength_nm)",
        "assumptions": ["Pupil transport is not solved"],
        "limitations": ["Jacobian transport is not established"],
        "max_kernel_l1_change_observed": 0.026928909222798926,
        "minimum_retained_energy_observed": 0.9648947932079406,
        "maximum_exit_pupil_mapping_fraction_observed": 0.005070118106886228,
        "maximum_ray_cone_deg_observed": 1.4553716795971077,
        "maximum_neighbor_opd_waves_observed": 0.014,
        "maximum_retained_energy_observed": 0.973,
        "maximum_reference_axis_deg_observed": 1.837,
        "geometry": [{"pupil_boundary_truncated": False}],
        "geometric_reference_f_number": 19.812067271756955,
    }
    ri = {
        **deepcopy(summary),
        "normalized_request": deepcopy(request["prescription"]),
        "request_sha256": digest(request["prescription"]),
        "convergence": {
            "observed_two_level_agreement": True,
            "relative_illumination_absolute_change": [[0.0, 0.004, 0.005]] * 2,
            "angular_cdf_max_absolute_change": [[0.0003, 0.005, 0.0072]] * 2,
        },
    }
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
    records = {k: "saved-" + k for k in kinds}
    conditions = {"f_number": {"value": psf["geometric_reference_f_number"], "unit": "1"}}
    payloads = {
        "radiometry": {"request": deepcopy(request["prescription"])},
        "psf": {"request": deepcopy(request)},
        "parent_lens": {"job_id": records["radiometry"]},
        "lens": {
            "job_id": records["psf"],
            "base_lens_model_id": records["parent_lens"],
            "conditions": deepcopy(conditions),
        },
        "assembly": {
            "conditions": deepcopy(conditions),
            "model_ids": [records["lens"], records["sensor"], "original-isp"],
            "spectral_range": {"min": 500, "max": 600, "unit": "nm"},
        },
        "run": {
            "assembly_id": records["assembly"],
            "test_id": records["test"],
            "scene_id": records["image"],
        },
        "replay": {"source_id": records["run"], "isp_model_id": records["replay_isp"]},
    }
    operations = {
        k: {"status": "confirmed", "intent": {"payload": payloads.get(k, {})}} for k in kinds[1:]
    }
    names = {
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
    originals = {
        ("camera", "psf_intensity.npy"): ("psf", "psf_kernel.npy"),
        ("camera", "psf_field_x_mm.npy"): ("psf", "field_x_mm.npy"),
        ("camera", "psf_field_y_mm.npy"): ("psf", "field_y_mm.npy"),
        ("camera", "spectral-image.npy"): ("image", "spectral-image.npy"),
        ("replay", "raw_frames.npy"): ("camera", "raw_frames.npy"),
    }
    artifacts = {
        k: {
            name + ":" + checksum("/".join(originals.get((k, name), (k, name)))): 512
            for name in group
        }
        for k, group in names.items()
    }
    bundles = {
        k: {
            "download_sha256": checksum(k + "zip"),
            "package_hash": checksum(k + "manifest"),
            "artifacts_verified": len(set().union(*(set(artifacts[g]) for g in groups))),
        }
        for k, groups in (
            ("camera", ("radiometry", "psf", "camera")),
            ("replay", ("radiometry", "psf", "camera", "replay")),
        )
    }
    state = str(tmp_path / "worker-qa")
    base_url = "http://127.0.0.1:8775"
    proof = {
        "schema": "camera_prescription_optics_verification_v1",
        "status": "passed",
        "checks": [{"name": n, "status": "passed"} for n in transition.PRESCRIPTION_CHECKS],
        "environment": environment,
        "static_sha256": static,
        "running_version": "0.14.0",
        "source_kind": "simulated",
        "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
        "state_dir": state,
        "base_url": base_url,
        "inputs": {"state_dir": state, "base_url": base_url},
        "runtime_binding": {
            "mode": "verified_installed_package",
            "runtime_root": str(root),
            "wheel_sha256": manifest["wheel_sha256"],
            "environment_sha256": digest(environment),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "module_paths": {k: str(root / v) for k, v in modules.items()},
        },
        "records": records,
        "operations": operations,
        "published_record_sha256": {
            records[k]: checksum(k)
            for k in ("parent_lens", "lens", "sensor", "assembly", "image", "test", "replay_isp")
        },
        "source_assembly_sha256": checksum("source-assembly"),
        "source_model_sha256": {
            k: checksum(k) for k in ("original-lens", "original-sensor", "original-isp")
        },
        **{k + "_record_sha256": checksum(k) for k in ("radiometry", "psf", "camera", "replay")},
        "prescription_request": request,
        "psf_summary": psf,
        "radiometry_summary": ri,
        "artifact_sha256": artifacts,
        "bundles": bundles,
        "latest_invocation": {
            "read_only": False,
            "post_count": 11,
            "verifier_sha256": checksum("verifier"),
            "counts_before": {"models": 3, "runs": 0},
            "counts_after": {"models": 7, "runs": 4},
        },
    }
    proof["invocations"] = [deepcopy(proof["latest_invocation"])]
    repeat = deepcopy(proof)
    repeat["latest_invocation"].update(
        read_only=True,
        post_count=0,
        counts_before=deepcopy(proof["latest_invocation"]["counts_after"]),
    )
    repeat["checks"].append({"name": "Read-only recheck", "status": "passed"})
    model = freeze(
        {
            "id": "ui-lens",
            "kind": "lens",
            "source_kind": "simulated",
            "uncertainty": {"kind": "unknown"},
            "parents": ["ui-parent"],
            "psf_factory_job_id": "ui-psf-job",
            "factory": {"physical_validation": "not_established"},
            "properties": {"f_number": deepcopy(conditions["f_number"])},
            "factory_application_conditions": deepcopy(conditions),
            "payload": {
                "field_reference_radius_mm": 0.75,
                "psf": {
                    "schema": "field_intensity_psf_v1",
                    "spatial_model": "field_grid",
                    "field_coordinates": "sensor_center_yx",
                    "interpolation": "bilinear_source_position",
                    "field_x_mm": [-0.5, 0.0, 0.5],
                    "field_y_mm": [-0.5, 0.0, 0.5],
                    "wavelength_nm": [500.0, 600.0],
                    "sample_pitch_um": 3.0,
                    "source": {"kind": "simulated"},
                    "values": [[[[[1.0]]]], [[[[1.0]]]]],
                },
            },
        }
    )
    assembly = freeze(
        {
            "id": "ui-assembly",
            "conditions": deepcopy(conditions),
            "model_ids": [model["id"], "ui-sensor", "ui-isp"],
            "model_hashes": {model["id"]: model["content_hash"]},
            "spectral_range": {"min": 500, "max": 600, "unit": "nm"},
            "compatibility": {"status": "PASS", "compatible": True},
        }
    )
    return {
        "package": {
            "status": "passed",
            "wheel_sha256": manifest["wheel_sha256"],
            "environment": environment,
            "response_original_file_common_operator_and_reusable_analysis_verified": True,
            "prescription_optics_api_to_raw_replay_and_parent_bundle_verified": True,
        },
        "installed": {
            "status": "passed",
            "runtime_root": str(root),
            "state_dir": state,
            "environment": environment,
            "response": {"status": "passed"},
            "isolated_server_stopped": True,
            "prescription": proof,
            "prescription_after_development_edit": repeat,
            "prescription_after_restart": deepcopy(repeat),
        },
        "ui": {
            "status": "passed",
            "errors": [],
            "environment": environment,
            "state_dir": str(tmp_path / "other-ui"),
            "base_url": "http://127.0.0.1:8776",
        },
        "prescription_ui": {
            "schema": "camera_prescription_ui_verification_v1",
            "status": "passed",
            "errors": [],
            "checks": ["Observed workflow " + str(i) for i in range(8)],
            "environment": environment,
            "static_sha256": static,
            "state_dir": str(tmp_path / "prescription-ui"),
            "base_url": "http://127.0.0.1:8777",
            "physical_validation": "not_established",
            "publication": model,
            "assembly": assembly,
            "model_id": model["id"],
            "assembly_id": assembly["id"],
            "model_sha256": digest(model),
            "assembly_sha256": digest(assembly),
        },
        "manifest": manifest,
    }


def test_prescription_proof_allows_independent_ui_state_and_changed_zip_timestamp(
    transition, evidence
):
    repeated = evidence["installed"]["prescription_after_restart"]
    repeated["bundles"]["camera"]["download_sha256"] = checksum("later-zip-timestamp")
    transition.verify_prescription_evidence(**evidence)


def test_old_packages_do_not_require_prescription_proof(transition):
    transition.verify_prescription_evidence({}, {}, {}, None, {"files": {}})


def test_timeout_resume_keeps_positive_original_post_history_without_new_posts(
    transition, evidence
):
    proof = evidence["installed"]["prescription"]
    proof["latest_invocation"]["post_count"] = 0
    proof["invocations"].append(deepcopy(proof["latest_invocation"]))
    transition.verify_prescription_evidence(**evidence)
    proof["invocations"] = [deepcopy(proof["latest_invocation"])]
    with pytest.raises(RuntimeError, match="prescription optics"):
        transition.verify_prescription_evidence(**evidence)


@pytest.mark.parametrize(
    "key,value",
    [
        ("maximum_neighbor_opd_waves_observed", 0.251),
        ("maximum_retained_energy_observed", 1.002),
        ("maximum_reference_axis_deg_observed", 2.001),
        ("maximum_neighbor_opd_waves_observed", True),
        ("maximum_neighbor_opd_waves_observed", float("nan")),
        ("geometry", [{"pupil_boundary_truncated": True}]),
    ],
)
def test_true_domain_flag_cannot_hide_failed_numeric_gate(transition, evidence, key, value):
    evidence["installed"]["prescription"]["psf_summary"][key] = value
    with pytest.raises(RuntimeError, match="prescription optics"):
        transition.verify_prescription_evidence(**evidence)


def test_exact_zero_numerical_error_is_valid_but_bad_ri_array_is_not(transition, evidence):
    for name in (
        "prescription",
        "prescription_after_development_edit",
        "prescription_after_restart",
    ):
        evidence["installed"][name]["psf_summary"]["max_kernel_l1_change_observed"] = 0.0
    transition.verify_prescription_evidence(**evidence)
    evidence["installed"]["prescription"]["radiometry_summary"]["convergence"][
        "relative_illumination_absolute_change"
    ][0][0] = 0.031
    with pytest.raises(RuntimeError, match="prescription optics"):
        transition.verify_prescription_evidence(**evidence)


@pytest.mark.parametrize(
    "path,value",
    [
        (("package", "prescription_optics_api_to_raw_replay_and_parent_bundle_verified"), False),
        (("installed", "prescription"), None),
        (("prescription_ui",), None),
        (
            ("installed", "prescription", "checks"),
            [{"name": str(i), "status": "passed"} for i in range(11)],
        ),
        (("installed", "prescription", "running_version"), "0.13.0"),
        (("installed", "prescription", "state_dir"), "/different/state"),
        (("installed", "prescription", "runtime_binding", "mode"), "development"),
        (("installed", "prescription", "runtime_binding", "wheel_sha256"), "0" * 64),
        (("installed", "prescription", "runtime_binding", "environment_sha256"), "0" * 64),
        (("installed", "prescription", "runtime_binding", "manifest_sha256"), "0" * 64),
        (
            ("installed", "prescription", "runtime_binding", "module_paths", "camera_platform.sdk"),
            "/development/camera_platform/sdk.py",
        ),
        (("installed", "prescription", "physical_validation"), "established"),
        (("installed", "prescription", "source_kind"), "measured"),
        (("installed", "prescription", "total_uncertainty"), {"kind": "known"}),
        (("installed", "prescription", "records", "parent_lens"), "wrong-parent"),
        (("installed", "prescription", "published_record_sha256"), {}),
        (("installed", "prescription", "source_model_sha256"), {}),
        (("installed", "prescription", "artifact_sha256"), {}),
        (("installed", "prescription", "psf_summary", "max_kernel_l1_change_observed"), 0.031),
        (("installed", "prescription", "psf_summary", "minimum_retained_energy_observed"), 0.949),
        (("installed", "prescription", "psf_summary", "approximation_domain_checks"), False),
        (("installed", "prescription", "psf_summary", "propagation_phase"), "wrong-sign"),
        (("installed", "prescription", "psf_summary", "limitations"), []),
        (("installed", "prescription", "radiometry_summary", "convergence"), {}),
        (("installed", "prescription", "invocations"), []),
        (("installed", "prescription_after_restart", "camera_record_sha256"), "0" * 64),
        (("installed", "prescription_after_development_edit", "source_assembly_sha256"), "0" * 64),
        (("installed", "prescription_after_restart", "latest_invocation", "post_count"), 1),
        (("installed", "prescription_after_restart", "latest_invocation", "post_count"), False),
        (("installed", "prescription_after_restart", "latest_invocation", "read_only"), False),
        (
            ("installed", "prescription_after_restart", "latest_invocation", "counts_before"),
            {"models": 1},
        ),
        (
            ("installed", "prescription_after_restart", "latest_invocation", "verifier_sha256"),
            "0" * 64,
        ),
        (
            ("installed", "prescription_after_restart", "bundles", "camera", "package_hash"),
            "0" * 64,
        ),
        (("installed", "prescription", "bundles", "camera", "artifacts_verified"), 999),
        (("prescription_ui", "checks"), ["passed"] * 8),
        (("prescription_ui", "errors"), ["Browser failed"]),
        (("prescription_ui", "publication"), None),
        (("prescription_ui", "model_sha256"), "0" * 64),
        (("prescription_ui", "assembly_sha256"), "0" * 64),
        (("prescription_ui", "model_id"), "wrong-lens"),
        (("prescription_ui", "assembly_id"), "wrong-assembly"),
    ],
)
def test_missing_or_changed_bound_evidence_is_refused(transition, evidence, path, value):
    target = evidence
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(RuntimeError, match="prescription optics"):
        transition.verify_prescription_evidence(**evidence)


@pytest.mark.parametrize(
    "side", ["prescription", "prescription_after_development_edit", "prescription_after_restart"]
)
@pytest.mark.parametrize(
    "kind,name",
    [
        ("camera", "raw_frames.npy"),
        ("camera", "rgb_frames.npy"),
        ("psf", "psf_kernel.npy"),
        ("psf", "field_x_mm.npy"),
    ],
)
def test_each_recheck_keeps_exact_original_arrays(transition, evidence, side, kind, name):
    group = evidence["installed"][side]["artifact_sha256"][kind]
    original = next(k for k in group if k.startswith(name + ":"))
    group[name + ":" + checksum("tampered")] = group.pop(original)
    with pytest.raises(RuntimeError, match="prescription optics"):
        transition.verify_prescription_evidence(**evidence)


@pytest.mark.parametrize(
    "kind,key",
    [
        ("prescription", "static_sha256"),
        ("prescription", "environment"),
        ("prescription_ui", "static_sha256"),
        ("prescription_ui", "environment"),
    ],
)
def test_matching_status_cannot_override_source_or_static_binding(transition, evidence, kind, key):
    proof = (
        evidence["prescription_ui"] if kind == "prescription_ui" else evidence["installed"][kind]
    )
    proof[key] = deepcopy(proof[key])
    if key == "static_sha256":
        proof[key]["app.js"] = checksum("changed-ui")
    else:
        proof[key]["source_hashes"]["camera_platform/prescription_psf.py"] = checksum(
            "changed-code"
        )
    with pytest.raises(RuntimeError, match="prescription optics"):
        transition.verify_prescription_evidence(**evidence)


@pytest.mark.parametrize(
    "record,path,value",
    [
        ("publication", ("payload", "field_reference_radius_mm"), 1.0),
        ("publication", ("payload", "psf", "interpolation"), "receiver_blend"),
        ("publication", ("payload", "psf", "field_x_mm"), [-0.75, 0.0, 0.75]),
        ("publication", ("parents",), []),
        ("publication", ("properties", "f_number", "value"), 2.8),
        ("assembly", ("conditions", "f_number", "value"), 2.8),
        ("assembly", ("model_ids",), ["wrong-lens"]),
        ("assembly", ("model_hashes", "ui-lens"), "0" * 64),
        ("assembly", ("spectral_range", "min"), 400),
        ("assembly", ("compatibility", "status"), "FAIL"),
    ],
)
def test_rehashed_ui_record_still_must_preserve_reviewed_contract(
    transition, evidence, record, path, value
):
    ui = evidence["prescription_ui"]
    target = ui[record]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    freeze(ui[record])
    ui["model_sha256" if record == "publication" else "assembly_sha256"] = digest(ui[record])
    if record == "publication":
        ui["assembly"]["model_hashes"]["ui-lens"] = ui[record]["content_hash"]
        freeze(ui["assembly"])
        ui["assembly_sha256"] = digest(ui["assembly"])
    with pytest.raises(RuntimeError, match="prescription optics"):
        transition.verify_prescription_evidence(**evidence)


def ui_records(evidence):
    proof = evidence["prescription_ui"]
    request = evidence["installed"]["prescription"]["prescription_request"]
    return {
        "models/ui-lens": deepcopy(proof["publication"]),
        "assemblies/ui-assembly": deepcopy(proof["assembly"]),
        "models/ui-parent": {
            "id": "ui-parent",
            "factory_job_id": "ui-ri-job",
            "kind": "lens",
            "source_kind": "simulated",
            "payload": {},
        },
        "factory/jobs/ui-psf-job": {
            "status": "succeeded",
            "environment": proof["environment"],
            "request": request,
            "result": {
                "summary": {"status": "PROPOSED"},
                "model_candidate": deepcopy(proof["publication"]["payload"]["psf"]),
            },
        },
        "factory/jobs/ui-ri-job": {
            "status": "succeeded",
            "environment": proof["environment"],
            "request": request["prescription"],
            "result": {"summary": {"status": "PROPOSED"}},
        },
    }


def test_live_ui_crosscheck_uses_versioned_api_and_exact_reviewed_records(
    transition, evidence, monkeypatch
):
    proof = evidence["prescription_ui"]
    request = evidence["installed"]["prescription"]["prescription_request"]
    rows = ui_records(evidence)
    calls = []

    def get(url, timeout):
        calls.append((url, timeout))
        return io.BytesIO(json.dumps(rows[url.split("/api/v1/")[1]]).encode())

    monkeypatch.setattr(transition, "urlopen", get)
    transition.verify_prescription_ui_records(proof, request)
    assert [url for url, _ in calls] == [
        "http://127.0.0.1:8777/api/v1/models/ui-lens",
        "http://127.0.0.1:8777/api/v1/assemblies/ui-assembly",
        "http://127.0.0.1:8777/api/v1/models/ui-parent",
        "http://127.0.0.1:8777/api/v1/factory/jobs/ui-psf-job",
        "http://127.0.0.1:8777/api/v1/factory/jobs/ui-ri-job",
    ]
    monkeypatch.setattr(transition, "urlopen", lambda *a, **k: io.BytesIO(b"{}"))
    with pytest.raises(RuntimeError, match="live reviewed record"):
        transition.verify_prescription_ui_records(proof, request)


@pytest.mark.parametrize(
    "row,key,value",
    [
        ("factory/jobs/ui-ri-job", "request", {"schema": "unrelated_prescription"}),
        ("factory/jobs/ui-psf-job", "request", {"schema": "unrelated_psf"}),
        ("factory/jobs/ui-psf-job", "status", "failed"),
        ("factory/jobs/ui-psf-job", "environment", {"platform_version": "0.13.0"}),
        (
            "factory/jobs/ui-psf-job",
            "result",
            {"summary": {"status": "PROPOSED"}, "model_candidate": {"values": [1]}},
        ),
        ("models/ui-parent", "payload", {"relative_illumination": "other-radiometry"}),
    ],
)
def test_live_ui_parent_and_original_candidate_must_match(
    transition, evidence, monkeypatch, row, key, value
):
    rows = ui_records(evidence)
    rows[row][key] = value
    monkeypatch.setattr(
        transition,
        "urlopen",
        lambda url, **kw: io.BytesIO(json.dumps(rows[url.split("/api/v1/")[1]]).encode()),
    )
    with pytest.raises(RuntimeError, match="exact Factory parent evidence"):
        transition.verify_prescription_ui_records(
            evidence["prescription_ui"],
            evidence["installed"]["prescription"]["prescription_request"],
        )


@pytest.mark.parametrize(
    "base",
    ["https://example.com", "http://127.0.0.1:8777/api/v1", "http://user:secret@127.0.0.1:8777"],
)
def test_ui_crosscheck_is_limited_to_local_api(transition, evidence, base):
    evidence["prescription_ui"]["base_url"] = base
    with pytest.raises(RuntimeError, match="local QA API"):
        transition.verify_prescription_ui_records(
            evidence["prescription_ui"],
            evidence["installed"]["prescription"]["prescription_request"],
        )


def arrange_main(monkeypatch, tmp_path, transition, evidence, *, include_prescription=True):
    old = tmp_path / "old-runtime"
    new = Path(evidence["installed"]["runtime_root"])
    state = tmp_path / "main-state"
    files = {
        str(tmp_path / (name + ".json")): evidence[name]
        for name in ("package", "installed", "ui", "prescription_ui")
    }
    files[str(state / "runtime-process.json")] = {"pid": 42}
    monkeypatch.setattr(transition, "read", lambda p: files[str(p)])
    monkeypatch.setattr(
        transition.launcher,
        "load_installed_runtime",
        lambda p: {
            "schema": transition.launcher.PIN_SCHEMA,
            "runtime_root": str(old if Path(p) == state else new),
        },
    )
    monkeypatch.setattr(
        transition.launcher,
        "verify_manifest",
        lambda p: (
            {"environment": {"platform_version": "0.13.0"}}
            if Path(p) == old
            else evidence["manifest"]
        ),
    )
    monkeypatch.setattr(transition.launcher, "verify_running_package", lambda *a: None)
    monkeypatch.setattr(transition.launcher, "verify_runtime_python", lambda *a: "/verified/python")
    monkeypatch.setattr(
        transition.launcher,
        "probe_package",
        lambda *a: {"environment": evidence["manifest"]["environment"]},
    )
    args = [
        "upgrade",
        "--runtime-root",
        str(new),
        "--state-dir",
        str(state),
        "--port",
        "8770",
        "--expected-old-version",
        "0.13.0",
        "--package-proof",
        str(tmp_path / "package.json"),
        "--runtime-proof",
        str(tmp_path / "installed.json"),
        "--ui-proof",
        str(tmp_path / "ui.json"),
        "--output",
        str(tmp_path / "transition.json"),
    ]
    if include_prescription:
        args.extend(["--prescription-ui-proof", str(tmp_path / "prescription_ui.json")])
    monkeypatch.setattr(transition.sys, "argv", args)
    return state


@pytest.mark.parametrize("include_prescription", [False, True])
def test_cli_refuses_missing_or_invalid_proof_before_any_main_state_work(
    transition, evidence, tmp_path, monkeypatch, include_prescription
):
    arrange_main(
        monkeypatch, tmp_path, transition, evidence, include_prescription=include_prescription
    )
    evidence["prescription_ui"]["errors"] = ["No verified browser publication"]

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid prescription evidence reached main-state work")

    for name in ("snapshot", "stop_verified", "select", "launch_bound"):
        monkeypatch.setattr(transition, name, forbidden)
    monkeypatch.setattr(transition.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="prescription optics"):
        transition.main()


def test_cli_binds_independent_live_ui_before_inspecting_main_state(
    transition, evidence, tmp_path, monkeypatch, capsys
):
    state = arrange_main(monkeypatch, tmp_path, transition, evidence)
    observed = []
    monkeypatch.setattr(
        transition,
        "verify_prescription_ui_records",
        lambda proof, request: observed.append(proof["state_dir"]),
    )

    def snapshot(*args, **kwargs):
        assert observed == [evidence["prescription_ui"]["state_dir"]]
        assert args[0] == state
        return {"records": {}, "jobs": {}, "audit": {}}

    monkeypatch.setattr(transition, "snapshot", snapshot)
    transition.main()
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "prepared"
    assert output["prescription_ui_proof"] == str(tmp_path / "prescription_ui.json")


def test_cli_refuses_prescription_ui_that_targets_main_state(
    transition, evidence, tmp_path, monkeypatch
):
    state = arrange_main(monkeypatch, tmp_path, transition, evidence)
    evidence["prescription_ui"]["state_dir"] = str(state)
    monkeypatch.setattr(transition, "snapshot", lambda *a, **k: pytest.fail("Read main state"))
    with pytest.raises(RuntimeError, match="isolated new package"):
        transition.main()
