"""Verify explicit installed wheel/runtime/Python with real case HTTP/Worker/restart.

Always use a new --output-directory. New QA state and poisoned working directory
are outside the checkout. Successful QA stays alive for a separate browser check.
This qualifies the selected local dependencies, not clean-OS or physical accuracy.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import socket
import stat
import subprocess
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from uuid import uuid4

import httpx
import start_camera_platform as launcher
from verify_camera_independent_worker import OwnedAPI, processes, rows

ROOT = Path(__file__).resolve().parents[1]


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def source_files():
    # Orchestration helpers only; never load or depend on checkout product modules.
    files = [
        Path(__file__).resolve(),
        *[
            ROOT / "tools" / name
            for name in (
                "verify_camera_independent_worker.py",
                "prepare_camera_platform_environment.py",
                "install_camera_platform_runtime.py",
                "start_camera_platform.py",
                "verify_camera_environment_execution.py",
                "verify_camera_installed_runtime.py",
            )
        ],
    ]
    return {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)
    }


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_inputs(wheel, runtime):
    """Bind every wheel member, except pip's rewritten RECORD, to exact installed bytes."""
    require(
        wheel.is_file() and not wheel.is_symlink() and wheel.suffix == ".whl",
        "Select a regular local wheel",
    )
    require(
        runtime.is_absolute() and runtime.resolve() == runtime and not runtime.is_symlink(),
        "Runtime must be a canonical absolute directory",
    )
    manifest = launcher.verify_manifest(runtime)
    require(manifest["wheel_sha256"] == sha(wheel), "Wheel differs from runtime manifest")
    require(
        manifest["environment"].get("platform_version") == "0.15.0",
        "Case qualification requires installed version 0.15.0",
    )
    members, total = {}, 0
    with zipfile.ZipFile(wheel) as archive:
        seen = set()
        for member in archive.infolist():
            name, path = member.filename, PurePosixPath(member.filename)
            require(
                not path.is_absolute()
                and ".." not in path.parts
                and "\\" not in name
                and name not in seen
                and not member.flag_bits & 1
                and not stat.S_ISLNK(member.external_attr >> 16),
                "Unsafe wheel member",
            )
            seen.add(name)
            total += member.file_size
            require(total <= 512 * 1024**2 and len(seen) <= 20000, "Wheel payload is too large")
            if not member.is_dir():
                members[name] = hashlib.sha256(archive.read(member)).hexdigest()
    required = {
        "camera_platform/__init__.py",
        "pyisetcam/__init__.py",
        "camera_platform/case_contracts.py",
        "camera_platform/case_reviews.py",
        "camera_platform/static/cases.js",
    }
    require(required <= members.keys(), "Wheel lacks the case platform payload")
    payload = {k: v for k, v in members.items() if k.startswith(("camera_platform/", "pyisetcam/"))}
    installed = {
        k: v
        for k, v in manifest["files"].items()
        if k.startswith(("camera_platform/", "pyisetcam/"))
    }
    require(payload == installed, "Installed code/static/data differs from the full wheel payload")
    compared = {k: v for k, v in members.items() if not k.endswith(".dist-info/RECORD")}
    require(
        all(manifest["files"].get(k) == v for k, v in compared.items()),
        "Installed wheel metadata or payload differs",
    )
    expected_source = manifest["environment"].get("source_hashes", {})
    require(
        expected_source and all(payload.get(k) == v for k, v in expected_source.items()),
        "Runtime source fingerprint is not bound to wheel bytes",
    )
    return manifest, {
        "wheel_sha256": sha(wheel),
        "runtime_manifest_sha256": sha(runtime / "runtime-manifest.json"),
        "wheel_members_compared": len(compared),
        "package_payload_sha256": payload,
        "source_hashes": expected_source,
        "record_scope": "Wheel RECORD is rewritten by pip; complete installed inventory "
        "is manifest-bound",
    }


PROBE = r"""
import importlib, json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
names = ("camera_platform.service", "camera_platform.case_reviews",
         "camera_platform.api", "pyisetcam")
for name in names:
    importlib.import_module(name)
from camera_platform.service import environment_fingerprint
paths = {name: str(Path(module.__file__).resolve()) for name, module in sys.modules.items()
         if name.split(".")[0] in {"camera_platform", "pyisetcam"}
         and getattr(module, "__file__", None)}
if not paths or any(not Path(path).is_relative_to(root) for path in paths.values()):
    raise RuntimeError("A product module was imported outside the installed runtime")
print(json.dumps({"environment": environment_fingerprint(), "module_paths": paths,
                  "python": sys.executable, "isolated": sys.flags.isolated}))
"""


def draft(name, **kwargs):
    return {
        "request_id": uuid4().hex,
        "name": name,
        "question": "같은 시험에서 설계 차이를 확인할 수 있는가?",
        "decision": {
            "status": "investigate",
            "rationale": "계산 경향은 확보했으나 독립 실측이 필요함",
        },
        **kwargs,
    }


class Verification:
    def __init__(self, output, *, wheel, runtime_root, python):
        self.output = output.absolute()
        self.wheel, self.runtime, self.python = (
            wheel.absolute(),
            runtime_root.absolute(),
            python.absolute(),
        )
        require(
            not self.output.exists() and not self.output.is_symlink(),
            "Use a fresh output directory; existing evidence is never overwritten",
        )
        require(
            self.output.resolve() == self.output and not self.output.is_relative_to(self.runtime),
            "Output must be canonical and outside the immutable runtime",
        )
        for parent in self.output.parents:
            require(
                not (parent / "registry.sqlite").exists()
                and not (parent / "installed-runtime.json").exists(),
                "Evidence output cannot be inside an existing application state",
            )
        self.output.mkdir(parents=True)
        self.owner, self.client, self.log = OwnedAPI(), None, None
        self.report = {
            "schema": "camera_case_installed_verification_v1",
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "primary_activated": False,
            "primary_accessed": False,
            "original_proof_reused_as_execution_evidence": False,
            "execution_mode": "fresh_installed_http_worker",
            "checks": [],
            "post_count": 0,
            "scope": "Exact installed wheel/runtime, same Mac and explicit selected dependencies. "
            "Real Worker calculations before restart; saved records reopen after restart. "
            "No clean-OS, browser or physical validation claim. One own QA blob is intentionally "
            "removed and restored from preserved bytes to test unavailable evidence handling.",
            "source_files_before": source_files(),
            "wheel": str(self.wheel),
            "runtime_root": str(self.runtime),
            "python": str(self.python),
        }
        self.save()

    def save(self):
        path = self.output / "verification.json"
        temporary = self.output / ".verification.json.tmp"
        temporary.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)

    def probe(self, label):
        command = [str(self.python), "-I", "-B", "-c", PROBE, str(self.runtime)]
        result = subprocess.run(
            command, cwd=self.workspace, capture_output=True, text=True, timeout=30, check=False
        )
        (self.output / (label + ".stdout.log")).write_text(result.stdout)
        (self.output / (label + ".stderr.log")).write_text(result.stderr)
        require(result.returncode == 0, "Installed isolated import probe failed")
        data = json.loads(result.stdout)
        require(
            data["environment"] == self.manifest["environment"]
            and data["isolated"] == 1
            and Path(data["python"]).absolute() == self.python,
            "Installed probe environment or interpreter differs from the selected runtime",
        )
        require(
            data["module_paths"]
            and all(
                Path(path).is_relative_to(self.runtime) for path in data["module_paths"].values()
            ),
            "Product module imported outside installed runtime",
        )
        self.report[label] = data
        self.save()
        return data

    def static_http(self):
        result = {}
        for path, expected in self.manifest["files"].items():
            if path.startswith("camera_platform/static/"):
                name = path.removeprefix("camera_platform/static/")
                response = self.client.get(self.url + "/" + quote(name, safe="/"))
                response.raise_for_status()
                actual = hashlib.sha256(response.content).hexdigest()
                require(actual == expected, "Served static bytes differ from the installed wheel")
                result[name] = actual
        require("cases.js" in result, "Installed case UI was not served")
        return result

    def check(self, name, value):
        require(value, name)
        self.report["checks"].append(name)
        self.save()

    def get(self, path):
        response = self.client.get(path)
        response.raise_for_status()
        return response.json()

    def post(self, path, payload, expected):
        intent = {
            "path": path,
            "payload": copy.deepcopy(payload),
            "expected_status": expected,
            "state": "pending",
            "at": datetime.now(UTC).isoformat(),
        }
        self.report.setdefault("post_intents", []).append(intent)
        self.save()
        response = self.client.post(path, json=payload, headers={"x-platform-token": self.token})
        intent.update(state="responded", actual_status=response.status_code)
        self.report["post_count"] += 1
        self.save()
        require(response.status_code == expected, response.text)
        result = response.json()
        intent["response_id"] = result.get("id")
        self.save()
        return result

    def start(self):
        require(
            source_files() == self.report["source_files_before"],
            "Executable source changed before API start",
        )
        require(
            verify_inputs(self.wheel, self.runtime) == (self.manifest, self.package_binding),
            "Wheel or runtime changed before launch",
        )
        process = self.owner.spawn(self.command, cwd=self.workspace, env=self.env, log=self.log)
        event = {
            "event": "launch",
            "api_pid": process.pid,
            "command": self.command,
            "started_at": datetime.now(UTC).isoformat(),
        }
        self.report.setdefault("process_lifecycle", []).append(event)
        self.save()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            require(process.poll() is None, "QA API exited")
            try:
                health = self.get("/health")
                if health.get("worker") == "running":
                    require(health["state_dir"] == str(self.state), "Wrong QA state")
                    require(health["workspace"] == str(self.workspace), "Wrong QA workspace")
                    inventory = processes()
                    group = [p for p in inventory if p["pgid"] == process.pid]
                    api = next((p for p in group if p["pid"] == process.pid), None)
                    expected_worker = " ".join(
                        [
                            str(self.python),
                            "-m",
                            "camera_platform.worker",
                            "--state-dir",
                            str(self.state),
                            "--workspace",
                            str(self.workspace),
                        ]
                    )
                    workers = [
                        p
                        for p in group
                        if p["ppid"] == process.pid and p["command"] == expected_worker
                    ]
                    if api and len(workers) == 1:
                        require(
                            api["command"].startswith(str(self.python) + " -I -B -c"),
                            "Wrong API interpreter or isolated bootstrap command",
                        )
                        require(
                            os.getpgid(process.pid) == process.pid != os.getpgrp(),
                            "API did not retain its private process group",
                        )
                        require(
                            {p["pid"] for p in group} == {process.pid, workers[0]["pid"]},
                            "Unexpected process shares the owned API/Worker group",
                        )
                        version = self.get("/workbench")["version"]
                        require(version == "0.15.0", "Expected installed version 0.15.0")
                        (self.state / "server.pid").write_text(str(process.pid) + "\n")
                        launcher.verify_running_package(
                            self.url + "/", self.state, launcher.load_installed_runtime(self.state)
                        )
                        event["runtime_witness"] = json.loads(
                            (self.state / "runtime-process.json").read_text()
                        )
                        event["static_sha256"] = self.static_http()
                        event.update(api=api, worker=workers[0], health=health, version=version)
                        self.report.update(version=version, worker=workers[0], api_pid=process.pid)
                        self.token = self.get("/session")["token"]
                        self.save()
                        return
            except httpx.RequestError:
                pass
            time.sleep(0.1)
        raise RuntimeError("QA startup or exact Worker identity timed out")

    def stop_for_restart(self):
        previous_pid = self.owner.process.pid
        cleanup = self.owner.cleanup()
        self.report["restart_cleanup"] = cleanup
        self.save()
        require(
            cleanup is not None and cleanup["process_group_stopped"],
            "Previous API/Worker group is not confirmed stopped",
        )
        remaining = [p for p in processes() if p["pgid"] == previous_pid]
        self.report["restart_cleanup"]["remaining_processes"] = remaining
        require(not remaining, "Previous API/Worker still appears in the process inventory")
        with socket.socket() as connection:
            connection.settimeout(2)
            closed = connection.connect_ex(("127.0.0.1", self.port)) != 0
        self.report["restart_cleanup"]["port_closed"] = closed
        require(closed, "Previous QA listener is still reachable")
        self.save()

    def run(self, assembly, test):
        job = self.post(
            "/runs",
            {
                "assembly_id": assembly,
                "test_id": test,
                "plugin_id": "response_db",
                "parameters": {"frames": 2, "output_size": [8, 8]},
            },
            202,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            result = self.get("/runs/" + job["id"])
            if result["status"] in {"succeeded", "failed", "cancelled"}:
                require(result["status"] == "succeeded", str(result))
                require(
                    result["environment"] == self.manifest["environment"],
                    "Actual Worker calculation used a different installed environment",
                )
                for artifact in [*result.get("input_artifacts", []), *result["artifacts"]]:
                    response = self.client.get("/artifacts/" + artifact["sha256"])
                    response.raise_for_status()
                    require(
                        hashlib.sha256(response.content).hexdigest() == artifact["sha256"],
                        "HTTP artifact bytes differ from their declared hash",
                    )
                    self.report.setdefault("artifact_http_sha256", {})[artifact["sha256"]] = {
                        "sha256": hashlib.sha256(response.content).hexdigest(),
                        "bytes": len(response.content),
                        "run_id": result["id"],
                    }
                return result
            time.sleep(0.1)
        raise RuntimeError("Worker did not finish")

    def record_failure(self, exc):
        self.report.update(status="failed", error=repr(exc), qa_left_live_for_browser=False)
        try:
            self.report["cleanup"] = self.owner.cleanup()
        except BaseException as cleanup_error:
            self.report["cleanup"] = {
                "error": repr(cleanup_error),
                "process_group_stopped": False,
                "process_group": self.owner.process.pid if self.owner.process else None,
            }
        finally:
            self.report["finished_at"] = datetime.now(UTC).isoformat()
            self.save()

    def missing_blob_case(self, original, first, other, test):
        case = self.post(
            "/case_reviews",
            draft(
                "HTTP QA · 근거 복구 검토",
                requirements=[
                    {"key": "R1", "description": "별도 설계의 고정 시험", "test_id": test}
                ],
                assembly_ids=[other["assembly_id"]],
                evidence=[{"kind": "runs", "id": other["id"], "requirement_keys": ["R1"]}],
            ),
            201,
        )
        shared = {item["sha256"] for item in first["artifacts"]}
        unique = [item for item in other["artifacts"] if item["sha256"] not in shared]
        require(unique, "Negative evidence test requires an artifact unshared with normal case")
        artifact = unique[0]
        path = self.state / "blobs" / artifact["sha256"]
        require(
            path.is_file() and not path.is_symlink() and sha(path) == artifact["sha256"],
            "Only an owned, verified QA artifact may be temporarily removed",
        )
        content = path.read_bytes()
        preserved = self.output / ("preserved-negative-test-" + artifact["sha256"])
        with preserved.open("xb") as stream:
            stream.write(content)
        before = rows(self.state)
        negative = {
            "case_id": case["id"],
            "removed_path": str(path),
            "sha256": artifact["sha256"],
            "preserved_path": str(preserved),
            "restored": False,
            "rows_before": before,
        }
        self.report["missing_blob_regression"] = negative
        self.save()
        path.unlink()
        try:
            bootstrap = self.get("/bootstrap")
            workbench = self.get("/workbench")
            unavailable = next(
                item for item in bootstrap["case_reviews"] if item["id"] == case["id"]
            )
            response = self.client.get("/case_reviews/" + case["id"])
            negative.update(unavailable=unavailable, strict_detail_status=response.status_code)
            self.check(
                "missing evidence is isolated from bootstrap and workbench",
                unavailable["schema"] == "camera_case_review_unavailable_v1"
                and unavailable["availability"]["status"] == "unavailable"
                and bool(unavailable["availability"]["reason"])
                and "decision" not in unavailable
                and "snapshots" not in unavailable
                and unavailable["authority"]["physical_validation"] is False
                and unavailable["authority"]["release_approval"] is False
                and workbench["version"] == "0.15.0"
                and response.status_code == 404,
            )
            self.check(
                "normal case remains exact while unrelated case evidence is missing",
                self.get("/case_reviews/" + original["id"]) == original,
            )
        finally:
            if path.exists():
                require(sha(path) == artifact["sha256"], "QA blob changed during recovery")
            else:
                with path.open("xb") as stream:
                    stream.write(content)
            negative["restored"] = sha(path) == artifact["sha256"]
            self.save()
        self.check(
            "restored original artifact reopens the exact immutable case without writes",
            self.get("/case_reviews/" + case["id"]) == case and rows(self.state) == before,
        )
        negative["rows_after"] = rows(self.state)
        return case

    def execute(self):
        try:
            require(
                self.python.is_file() and os.access(self.python, os.X_OK),
                "Explicit Python must be an executable file",
            )
            self.manifest, self.package_binding = verify_inputs(self.wheel, self.runtime)
            self.report.update(
                package_binding=self.package_binding, environment=self.manifest["environment"]
            )
            inputs = {
                "wheel": {"path": str(self.wheel), "sha256": sha(self.wheel)},
                "manifest": {
                    "path": str(self.runtime / "runtime-manifest.json"),
                    "sha256": sha(self.runtime / "runtime-manifest.json"),
                },
            }
            inputs.update(
                {
                    name: {"path": str(ROOT / name), "sha256": value}
                    for name, value in self.report["source_files_before"].items()
                }
            )
            self.report.update(
                inputs=inputs,
                wheel_sha256=sha(self.wheel),
                manifest_sha256=inputs["manifest"]["sha256"],
            )
            external = Path(tempfile.mkdtemp(prefix="camera-case-installed-qa-")).resolve()
            require(not external.is_relative_to(ROOT), "QA workspace must be outside checkout")
            self.workspace, self.state = external / "workspace", external / "camera-case-qa-state"
            self.workspace.mkdir()
            for base in [self.workspace, self.workspace / "src"]:
                for name in ["camera_platform", "pyisetcam"]:
                    poison = base / name
                    poison.mkdir(parents=True)
                    (poison / "__init__.py").write_text(
                        "raise RuntimeError('checkout import forbidden')\n"
                    )
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                self.port = sock.getsockname()[1]
            require(self.port != 8770, "Never use the primary port")
            self.url = f"http://127.0.0.1:{self.port}"
            self.env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
            self.env.pop(launcher.MEEP_ENV, None)
            self.env.pop(launcher.MITSUBA_ENV, None)
            self.env.update(
                PYTHONPATH=str(self.runtime), PYTHONDONTWRITEBYTECODE="1", PYTHONSAFEPATH="1"
            )
            self.probe("probe_before")
            self.pin = launcher.save_runtime_pin(self.state, self.runtime)
            self.report["runtime_pin"] = self.pin
            self.command = [
                str(self.python),
                "-I",
                "-B",
                "-c",
                launcher.INSTALLED_BOOTSTRAP,
                str(self.runtime),
                str(self.state),
                self.pin["manifest_sha256"],
                "serve",
                "--workspace",
                str(self.workspace),
                "--state-dir",
                str(self.state),
                "--port",
                str(self.port),
            ]
            self.log = (self.output / "server.log").open("xb")
            self.client = httpx.Client(base_url=self.url + "/api/v1", trust_env=False, timeout=20)
            self.report.update(
                url=self.url,
                base_url=self.url,
                state_dir=str(self.state),
                workspace=str(self.workspace),
                command=self.command,
                python=str(self.python),
            )
            self.start()
            bootstrap = self.get("/bootstrap")
            self.report["qa_initial_rows"] = rows(self.state)
            self.check("fresh case registry is empty", bootstrap["case_reviews"] == [])
            test = bootstrap["tests"][0]["id"]
            first = self.run(bootstrap["assemblies"][0]["id"], test)
            self.report["run_a"] = first
            payload = draft(
                "HTTP QA · 주변부 응답 검토",
                requirements=[
                    {"key": "R1", "description": "고정 시험에서 색 응답 확인", "test_id": test}
                ],
                assembly_ids=[first["assembly_id"]],
                evidence=[{"kind": "runs", "id": first["id"], "requirement_keys": ["R1"]}],
                unresolved=["고온 조건의 독립 RAW 미확보"],
                next_experiments=[
                    {
                        "question": "고온에서 경향이 유지되는가?",
                        "reason": "온도 적용 범위 확인",
                        "mode": "measurement",
                        "conditions": {"temperature_c": {"value": 60, "unit": "degC"}},
                    }
                ],
            )
            original = self.post("/case_reviews", payload, 201)
            frozen = original["snapshots"]["evidence"][0]
            self.check("actual Worker result frozen in case", frozen["snapshot"] == first)
            self.check(
                "full result digest is exact and distinct from request digest",
                frozen["record_sha256"] == digest(first) != first["content_hash"],
            )
            before_retry = rows(self.state)
            self.check(
                "identical retry returns exact original",
                self.post("/case_reviews", payload, 201) == original,
            )
            self.check("identical retry writes no rows", rows(self.state) == before_retry)
            self.post(
                "/case_reviews", {**payload, "question": "changed under same request id"}, 409
            )
            other = self.run(bootstrap["assemblies"][1]["id"], test)
            self.report["run_b"] = other
            measurement_payload = self.get("/templates/measurement")
            prediction = other["result"]["prediction"]
            measurement_payload.update(
                name="Unrelated simulated comparison, not a physical measurement",
                source_kind="simulated",
                source_uri="run://" + other["id"],
                method_and_version="retained-simulation-comparison/1",
                physical_instance=None,
                dataset_ids=["simulated-case-qa-" + uuid4().hex],
                instrument={},
                assembly_id=other["assembly_id"],
                test_id=other["test_id"],
            )
            for key in (
                "values",
                "axes",
                "conditions",
                "included_effects",
                "metric_id",
                "unit",
                "reference_plane",
            ):
                measurement_payload[key] = copy.deepcopy(prediction[key])
            measurement = self.post("/measurements", measurement_payload, 201)
            validation = self.post(
                "/validations",
                {
                    "run_id": other["id"],
                    "measurement_id": measurement["id"],
                    "test_id": test,
                },
                201,
            )
            self.report.update(unrelated_measurement=measurement, unrelated_validation=validation)
            self.check(
                "unrelated simulated comparison preserves observed status and physical unknown",
                measurement["source_kind"] == "simulated"
                and validation["status"] == "INCONCLUSIVE"
                and validation["physical_validation_pass"] is False,
            )
            second = self.post("/case_reviews", draft("HTTP QA · 다른 설계 사례"), 201)
            self.check(
                "unrelated run measurement validation and case cannot change saved evidence",
                self.get("/case_reviews/" + original["id"]) == original,
            )
            bad = copy.deepcopy(payload)
            bad.update(request_id=uuid4().hex)
            bad["evidence"][0]["id"] = other["id"]
            self.post("/case_reviews", bad, 422)
            revision_payload = {
                **payload,
                "request_id": uuid4().hex,
                "previous_revision_id": original["id"],
                "decision": {"status": "deferred", "rationale": "독립 실측을 기다리며 판단 보류"},
            }
            revised = self.post("/case_reviews", revision_payload, 201)
            self.check(
                "revision preserves original and rationale",
                revised["revision"] == 2
                and self.get("/case_reviews/" + original["id"]) == original,
            )
            self.check(
                "history includes full immutable revisions",
                self.get("/case_reviews/" + revised["id"] + "/history") == [original, revised],
            )
            self.check(
                "notes grant no validation or release authority",
                revised["authority"]
                == {
                    "engineering_note": True,
                    "physical_validation": False,
                    "release_approval": False,
                },
            )
            self.post("/case_reviews", {**revision_payload, "request_id": uuid4().hex}, 409)
            third = self.missing_blob_case(original, first, other, test)
            case_records_before = {
                record["id"]: record for record in [original, revised, second, third]
            }
            before_restart = rows(self.state)
            posts = self.report["post_count"]
            self.stop_for_restart()
            self.start()
            self.check(
                "all rows preserved across confirmed API and Worker restart",
                rows(self.state) == before_restart,
            )
            self.check(
                "all three cases reopen after restart",
                sorted(x["id"] for x in self.get("/case_reviews"))
                == sorted([revised["id"], second["id"], third["id"]]),
            )
            self.check(
                "original and revised evidence reopen after restart",
                self.get("/case_reviews/" + revised["id"] + "/history") == [original, revised],
            )
            self.check("restart performs zero POST requests", posts == self.report["post_count"])
            case_records_after = {
                identity: self.get("/case_reviews/" + identity) for identity in case_records_before
            }
            self.check(
                "every saved case full record is exact after restart",
                case_records_after == case_records_before,
            )
            self.report.update(
                case_a=original,
                case_a_revision=revised,
                case_b=second,
                case_c=third,
                rows_after=rows(self.state),
            )
            launches = self.report["process_lifecycle"]
            self.report["restart"] = {
                "old_api_pid": launches[0]["api_pid"],
                "new_api_pid": launches[-1]["api_pid"],
                "old_worker_pid": launches[0]["worker"]["pid"],
                "new_worker_pid": launches[-1]["worker"]["pid"],
                "old_process_group_stopped": self.report["restart_cleanup"][
                    "process_group_stopped"
                ],
                "post_count": self.report["post_count"] - posts,
                "rows_before": before_restart,
                "rows_after": rows(self.state),
                "case_records_before": case_records_before,
                "case_records_after": case_records_after,
            }
            self.report["runtime_binding"] = launches[-1]["runtime_witness"]
            self.report["static_sha256"] = launches[-1]["static_sha256"]
            self.report["records"] = {
                key: self.report[key]
                for key in ("case_a", "case_a_revision", "case_b", "case_c", "run_a", "run_b")
            }
            self.report["record_sha256"] = {
                value["id"]: digest(value) for value in self.report["records"].values()
            }
            self.report["artifact_sha256"] = {
                item["name"]: item["sha256"] for item in first["artifacts"]
            }
            self.check(
                "same installed environment and imports after restart",
                self.probe("probe_after") == self.report["probe_before"],
            )
            self.check(
                "all wheel runtime payloads and metadata unchanged after execution",
                verify_inputs(self.wheel, self.runtime) == (self.manifest, self.package_binding),
            )
            self.report["source_files_after"] = source_files()
            self.check(
                "verifier and orchestration helpers unchanged across execution and restart",
                self.report["source_files_after"] == self.report["source_files_before"],
            )
            self.report.update(
                status="passed_for_declared_scope",
                qa_left_live_for_browser=True,
                cleanup_owner="root after browser verification",
                finished_at=datetime.now(UTC).isoformat(),
            )
            self.save()
            print(
                json.dumps(
                    {
                        "status": self.report["status"],
                        "url": self.url,
                        "pid": self.owner.process.pid,
                        "worker": self.report["worker"],
                        "checks": len(self.report["checks"]),
                        "output": str(self.output),
                    }
                )
            )
        except BaseException as exc:
            self.record_failure(exc)
            raise
        finally:
            if self.client is not None:
                self.client.close()
            if self.log is not None:
                self.log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    args = parser.parse_args()
    Verification(
        args.output_directory, wheel=args.wheel, runtime_root=args.runtime_root, python=args.python
    ).execute()


if __name__ == "__main__":
    main()
