"""Local design studies layered over the read-only CameraE2E catalog.

Study records deliberately live in a separate database. A component observation,
a historical report, and a newly executed camera experiment retain their scopes.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .component_catalog import component_catalog_get, component_catalog_search


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def text_value(value: Any, name: str, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name}: 1–{maximum}자의 텍스트가 필요합니다.")
    return value.strip()


class ConflictError(ValueError):
    """The user is editing an older snapshot, or an experiment is already active."""


TEMPLATES = [
    {
        "id": "concept",
        "title": "렌즈·센서 컨셉 검토",
        "question": "사용 조건에 맞는 카메라 조합은 무엇인가?",
        "hypothesis": "렌즈와 센서 조합의 차이가 수광량과 출력 영상에 영향을 준다.",
        "reports": ["lens", "physical"],
        "icon": "◉",
    },
    {
        "id": "edge",
        "title": "주변부 화질 원인 조사",
        "question": "주변부 열화는 어느 단계에서 나타나는가?",
        "hypothesis": "광학 조건과 센서 응답의 영향을 분리해 확인한다.",
        "reports": ["lens", "fdtd", "physical"],
        "icon": "⌗",
    },
    {
        "id": "lowlight",
        "title": "저조도 설계 비교",
        "question": "노출을 바꾸면 신호·잡음·포화가 어떻게 달라지는가?",
        "hypothesis": "노출 증가의 수광량 이득과 포화 위험을 같은 조건에서 비교한다.",
        "reports": ["physical", "temporal"],
        "icon": "◐",
    },
]

REPORTS = {
    "physical": {
        "title": "센서 물리 추정 비교",
        "stage": "sensor",
        "scope": "센서 추정 모델 · 실측 검증 아님",
        "directory": "reports/physical_sensor_e2e/comparison",
        "document": "index.html",
        "data": "comparison.json",
    },
    "lens": {
        "title": "Lens DB / CameraE2E 광학 검토",
        "stage": "lens",
        "scope": "기하 PSF·연결성 검토 · 회절/제품 검증 별도",
        "directory": "reports/lens_improvements_20260906/camera_preview",
        "document": "lens_db_camerae2e_report.html",
        "data": "lens_db_camerae2e_summary.json",
    },
    "fdtd": {
        "title": "FDTD 센서 응답 근거",
        "stage": "sensor",
        "scope": "광학 응답 추정 · 수렴/실측 범위 확인 필요",
        "directory": "reports/fdtd_sensor",
        "document": "fdtd_sensor_report.html",
        "data": "fdtd_sensor_summary.json",
    },
    "temporal": {
        "title": "노출·움직임·행 읽기 실험",
        "stage": "scene",
        "scope": "시간 적분 예제 · 잡음을 끈 결과는 SNR 비교 제외",
        "directory": "reports/temporal_capture",
        "document": "index.html",
        "data": "temporal_capture_metrics.json",
    },
    "perception": {
        "title": "영상과 인식 성능",
        "stage": "evaluation",
        "scope": "저장된 평가 모델·데이터 조건에 한정",
        "directory": "reports/perception/task_perception",
        "document": "task_perception_report.html",
        "data": "task_perception_summary.json",
    },
    "isp": {
        "title": "ISP 지연·제어 타이밍",
        "stage": "isp",
        "scope": "타이밍 모델 · 실제 HW 측정과 구분",
        "directory": "reports/hwisp",
        "document": "hwisp_technical_report.html",
        "data": "timeline_summary.json",
    },
}


class DesignWorkbench:
    def __init__(
        self,
        workspace: Path | str,
        state_dir: Path | str | None = None,
        catalog_root: Path | str | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.catalog_root = Path(catalog_root or self.workspace / "camerae2e_db").resolve()
        self.state_dir = Path(state_dir or self.workspace / "outputs/design-workbench").resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.database = self.state_dir / "studies.sqlite"
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="design-experiment")
        self.busy = False
        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS studies "
                "(id TEXT PRIMARY KEY, version INTEGER NOT NULL, document TEXT NOT NULL)"
            )
            # A process restart never leaves a run pretending to be active.
            for row in conn.execute("SELECT id,document FROM studies").fetchall():
                study = json.loads(row["document"])
                changed = False
                for run in study.get("experiments", []):
                    if run["status"] in {"queued", "running"}:
                        run.update(
                            status="interrupted", error="서버가 종료되어 실행이 중단됐습니다."
                        )
                        changed = True
                if changed:
                    study["version"] += 1
                    study["updated_at"] = utcnow()
                    conn.execute(
                        "UPDATE studies SET version=?,document=? WHERE id=?",
                        (study["version"], encode(study), study["id"]),
                    )

    def connect(self):
        conn = sqlite3.connect(self.database, timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def catalog(self):
        path = self.catalog_root / "catalog.sqlite"
        if not path.is_file():
            raise FileNotFoundError("통합 catalog.sqlite가 없습니다. DB 경로를 확인하세요.")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def bootstrap(self) -> dict:
        counts, warnings = {}, []
        try:
            with closing(self.catalog()) as conn:
                counts = dict(
                    conn.execute("SELECT family,count(*) FROM components GROUP BY family")
                )
                counts["runs"] = conn.execute("SELECT count(*) FROM simulation_runs").fetchone()[0]
                counts["revisions"] = conn.execute(
                    "SELECT count(*) FROM model_revisions"
                ).fetchone()[0]
        except (FileNotFoundError, sqlite3.Error) as exc:
            warnings.append(str(exc))
        return {
            "schema": "camerae2e_design_workbench_v1",
            "queried_at": utcnow(),
            "counts": counts,
            "warnings": warnings,
            "templates": TEMPLATES,
            "studies": self.list_studies(),
            "reports": self.reports(),
        }

    def list_studies(self):
        with closing(self.connect()) as conn:
            docs = [json.loads(r[0]) for r in conn.execute("SELECT document FROM studies")]
        return sorted(
            [
                {
                    **{k: s[k] for k in ("id", "title", "kind", "updated_at", "version")},
                    "variant_count": len(s["variants"]),
                    "experiment_count": len(s["experiments"]),
                    "decision_count": len(s["decisions"]),
                }
                for s in docs
            ],
            key=lambda s: s["updated_at"],
            reverse=True,
        )

    def get_study(self, identity: str):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT document FROM studies WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError("설계 케이스를 찾을 수 없습니다.")
        return json.loads(row[0])

    def create_study(self, payload: dict):
        template = next((t for t in TEMPLATES if t["id"] == payload.get("kind")), None)
        if template is None:
            raise ValueError("지원하지 않는 케이스 유형입니다.")
        study = {
            "id": str(uuid.uuid4()),
            "version": 1,
            "kind": template["id"],
            "title": text_value(payload.get("title", template["title"]), "제목", 160),
            "question": template["question"],
            "hypothesis": template["hypothesis"],
            "conditions": {
                "scene": "macbeth",
                "luminance": 10.0,
                "exposure_ms": 4.0,
                "frames": 4,
                "seed": 42,
                "output_size": 32,
            },
            "requirements": [],
            "variants": [],
            "evidence": [],
            "experiments": [],
            "decisions": [],
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        with self.lock, self.connect() as conn:
            conn.execute("INSERT INTO studies VALUES (?,?,?)", (study["id"], 1, encode(study)))
        return study

    def mutate(self, identity: str, payload: dict, action):
        with self.lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT document FROM studies WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise KeyError("설계 케이스를 찾을 수 없습니다.")
            study = json.loads(row[0])
            if payload.get("version") != study["version"]:
                raise ConflictError(
                    "다른 저장 또는 실험으로 내용이 갱신됐습니다. 새로고침 후 다시 저장하세요."
                )
            action(study)
            study["version"] += 1
            study["updated_at"] = utcnow()
            conn.execute(
                "UPDATE studies SET version=?,document=? WHERE id=?",
                (study["version"], encode(study), identity),
            )
        return study

    def update_study(self, identity: str, payload: dict):
        def apply(study):
            for key in ("title", "question", "hypothesis"):
                if key in payload:
                    study[key] = text_value(payload[key], key, 160 if key == "title" else 2000)
            if "conditions" in payload:
                study["conditions"] = validate_conditions(payload["conditions"])
            if "requirements" in payload:
                reqs = payload["requirements"]
                if not isinstance(reqs, list) or len(reqs) > 12:
                    raise ValueError("요구사항은 최대 12개입니다.")
                study["requirements"] = [validate_requirement(r) for r in reqs]

        return self.mutate(identity, payload, apply)

    def components(self, query="", family=None):
        if family not in (None, "lens", "sensor"):
            raise ValueError("부품 유형을 확인하세요.")
        return component_catalog_search(query[:200], family=family, root=self.catalog_root)

    def component(self, identity):
        result = component_catalog_get(identity, root=self.catalog_root)
        for revision in result["revisions"]:
            revision.pop("snapshot", None)
        for artifact in result["artifacts"]:
            artifact["url"] = "/api/artifacts/" + artifact["artifact_id"]
        return result

    def freeze_revision(self, identity, family, point_id=None):
        if not identity:
            return None
        with closing(self.catalog()) as conn:
            row = conn.execute(
                "SELECT r.*,c.model,c.manufacturer,c.family FROM model_revisions r "
                "JOIN components c USING(component_id) WHERE revision_id=?",
                (identity,),
            ).fetchone()
            if row is None or row["family"] != family:
                raise ValueError(f"{family} 모델 버전을 확인하세요.")
            result = dict(row)
            result["snapshot"] = json.loads(result.pop("snapshot_json"))
            points = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM operating_points WHERE revision_id=?", (identity,)
                )
            ]
            if point_id:
                point = next((p for p in points if p["operating_point_id"] == point_id), None)
                if point is None:
                    raise ValueError("선택한 동작 조건이 모델 버전에 속하지 않습니다.")
            else:
                point = points[0] if len(points) == 1 else None
            if point:
                point["conditions"] = json.loads(point.pop("conditions_json"))
            result["operating_point"] = point
            if family == "sensor":
                geometry = conn.execute(
                    "SELECT * FROM sensor_geometry WHERE revision_id=?", (identity,)
                ).fetchone()
                result["geometry"] = dict(geometry) if geometry else {}
                runs = conn.execute(
                    "SELECT * FROM simulation_runs WHERE revision_id=? AND status='simulated' "
                    "ORDER BY created_at DESC,run_id DESC",
                    (identity,),
                ).fetchall()
                result["runtime_binding"] = None
                for run in runs:
                    if point and run["operating_point_id"] != point["operating_point_id"]:
                        continue
                    metadata = json.loads(run["metadata_json"])
                    if metadata.get("legacy_run_id"):
                        result["runtime_binding"] = {
                            "run_id": run["run_id"],
                            "legacy_run_id": metadata["legacy_run_id"],
                        }
                        break
        return result

    def add_variant(self, identity, payload):
        lens = self.freeze_revision(
            payload.get("lens_revision_id"), "lens", payload.get("lens_point_id")
        )
        sensor = self.freeze_revision(
            payload.get("sensor_revision_id"), "sensor", payload.get("sensor_point_id")
        )
        if not lens and not sensor:
            raise ValueError("렌즈 또는 센서 모델 버전을 선택하세요.")
        optics_mode = payload.get("optics_mode", "review_only")
        if optics_mode not in ("review_only", "diffraction_proxy", "raytrace"):
            raise ValueError("광학 모델 경로를 확인하세요.")
        exposure = payload.get("exposure_ms")
        if exposure is not None:
            exposure = finite_number(exposure, "노출", 0.1, 64)
        variant = {
            "id": str(uuid.uuid4()),
            "label": text_value(payload.get("label"), "설계안 이름", 100),
            "lens": lens,
            "sensor": sensor,
            "optics_mode": optics_mode,
            "exposure_ms": exposure,
            "created_at": utcnow(),
            "compatibility": {
                "status": "unknown",
                "reason": "이미지 서클·유효 면적·CRA·기구 적합성을 별도 확인해야 합니다.",
            },
        }

        def apply(study):
            if len(study["variants"]) >= 24:
                raise ValueError("한 케이스의 설계안은 최대 24개입니다.")
            study["variants"].append(variant)

        return self.mutate(identity, payload, apply)

    def reports(self):
        result = []
        for identity, spec in REPORTS.items():
            directory = self.workspace / spec["directory"]
            data = directory / spec["data"]
            doc = directory / spec["document"]
            images = sorted(p for p in directory.glob("*.png") if p.is_file())[:8]
            if not directory.exists():
                continue
            result.append(
                {
                    **spec,
                    "id": identity,
                    "available": doc.is_file() or data.is_file(),
                    "document_url": f"/evidence/{identity}/{spec['document']}"
                    if doc.is_file()
                    else None,
                    "data_url": f"/evidence/{identity}/{spec['data']}" if data.is_file() else None,
                    "images": [
                        {"name": p.name, "url": f"/evidence/{identity}/{p.name}"} for p in images
                    ],
                    "source_sha256": hashlib.sha256(data.read_bytes()).hexdigest()
                    if data.is_file()
                    else None,
                }
            )
        return result

    def report(self, identity):
        spec = next((r for r in self.reports() if r["id"] == identity), None)
        if spec is None:
            raise KeyError("근거 자료를 찾을 수 없습니다.")
        data = self.workspace / spec["directory"] / spec["data"]
        if data.is_file() and data.stat().st_size <= 30_000_000:
            content = data.read_bytes()
            raw = json.loads(content)
            spec["source_sha256"] = hashlib.sha256(content).hexdigest()
            if identity == "physical":
                spec["cases"] = [
                    {
                        k: case.get(k)
                        for k in (
                            "case_id",
                            "sensor_id",
                            "mode",
                            "scene",
                            "illuminant",
                            "fnumber",
                            "cra_deg",
                            "azimuth_deg",
                            "common_output",
                            "mean_photoelectrons",
                            "saturation",
                            "snr_eligible",
                            "snr_interpretation",
                            "center_comparison",
                        )
                    }
                    for case in raw.get("cases", [])
                ]
                spec["method"] = {
                    k: raw.get(k)
                    for k in (
                        "schema",
                        "controls",
                        "comparison_contract",
                        "limitations",
                        "estimate_only",
                        "measured_accuracy_verified",
                        "implementation_sha256",
                    )
                }
            else:
                spec["summary"] = raw
        return spec

    def attach_evidence(self, identity, payload):
        report = self.report(payload.get("report_id"))
        selected = payload.get("case_ids", [])
        if (
            not isinstance(selected, list)
            or len(selected) > 50
            or any(not isinstance(x, str) for x in selected)
        ):
            raise ValueError("선택한 과거 실험 ID를 확인하세요.")
        cases = {c["case_id"]: c for c in report.get("cases", [])}
        if not set(selected) <= cases.keys():
            raise ValueError("원본 보고서에 없는 실험 ID입니다.")
        item = {
            "id": str(uuid.uuid4()),
            "report_id": report["id"],
            "title": report["title"],
            "scope": report["scope"],
            "source_sha256": report["source_sha256"],
            "case_ids": selected,
            "case_snapshots": [cases[key] for key in selected],
            "method": report.get("method"),
            "note": text_value(payload.get("note", "검토 근거로 연결"), "근거 메모"),
            "created_at": utcnow(),
        }
        return self.mutate(identity, payload, lambda s: s["evidence"].append(item))

    def add_decision(self, identity, payload):
        def apply(study):
            variant = payload.get("variant_id")
            if variant and not any(v["id"] == variant for v in study["variants"]):
                raise ValueError("채택 설계안을 확인하세요.")
            refs = payload.get("evidence_ids", [])
            valid = {e["id"] for e in study["evidence"]} | {
                e["id"] for e in study["experiments"] if e["status"] == "complete"
            }
            if not isinstance(refs, list) or not refs or not set(refs) <= valid:
                raise ValueError("연결된 근거 또는 완료된 실험을 하나 이상 선택하세요.")
            study["decisions"].append(
                {
                    "id": str(uuid.uuid4()),
                    "variant_id": variant,
                    "evidence_ids": refs,
                    "conclusion": text_value(payload.get("conclusion"), "검토 결론"),
                    "remaining": text_value(
                        payload.get("remaining", "추가 확인 필요"), "남은 확인 사항"
                    ),
                    "created_at": utcnow(),
                    "author": "user",
                    "study_version": study["version"],
                    "requirements": study["requirements"],
                    "conditions": study["conditions"],
                }
            )

        return self.mutate(identity, payload, apply)

    def start_experiment(self, identity, payload):
        with self.lock:
            if self.busy:
                raise ConflictError("현재 비교 실험이 실행 중입니다. 완료 후 시작하세요.")
            study = self.get_study(identity)
            ids = payload.get("variant_ids", [])
            if not isinstance(ids, list) or len(ids) != 2 or len(set(ids)) != 2:
                raise ValueError("서로 다른 설계안 두 개를 선택하세요.")
            variants = [next((v for v in study["variants"] if v["id"] == key), None) for key in ids]
            if any(v is None for v in variants):
                raise ValueError("설계안을 찾을 수 없습니다.")
            from .design_experiments import validate_variants

            validate_variants(variants)
            run = {
                "id": str(uuid.uuid4()),
                "status": "queued",
                "variant_ids": ids,
                "conditions": validate_conditions(study["conditions"]),
                "requirements": study["requirements"],
                "hypothesis": study["hypothesis"],
                "created_at": utcnow(),
                "result": None,
            }
            result = self.mutate(identity, payload, lambda s: s["experiments"].append(run))
            self.busy = True
            self.executor.submit(self._execute, identity, run, variants)
        return result

    def _execute(self, identity, run, variants):
        def update(values):
            with self.lock:
                current = self.get_study(identity)

                def apply(study):
                    item = next(e for e in study["experiments"] if e["id"] == run["id"])
                    item.update(values)

                self.mutate(identity, {"version": current["version"]}, apply)

        try:
            update({"status": "running"})
            from .design_experiments import run_comparison

            result = run_comparison(
                variants,
                run["conditions"],
                self.state_dir / "runs" / run["id"],
                self.catalog_root,
                run["requirements"],
            )
            update({"status": "complete", "finished_at": utcnow(), "result": result})
        except Exception as exc:
            update({"status": "failed", "finished_at": utcnow(), "error": str(exc)[:2000]})
        finally:
            with self.lock:
                self.busy = False

    def resolve_artifact(self, identity):
        with closing(self.catalog()) as conn:
            row = conn.execute(
                "SELECT path,availability,integrity_status FROM artifacts WHERE artifact_id=?",
                (identity,),
            ).fetchone()
        if not row or row["availability"] != "available" or row["integrity_status"] == "failed":
            raise FileNotFoundError("사용할 수 없는 근거 파일입니다.")
        return contained_file(self.catalog_root, row["path"])


def contained_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("허용되지 않은 파일 경로입니다.")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise FileNotFoundError("허용된 자료 경로에서 파일을 찾을 수 없습니다.")
    return path


def finite_number(value, name, low, high):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f"{name}: {low}–{high} 범위의 수치가 필요합니다.")
    return float(value)


def validate_conditions(value):
    if not isinstance(value, dict):
        raise ValueError("실험 조건을 확인하세요.")
    result = {}
    for key, low, high in (
        ("luminance", 0.1, 1000),
        ("exposure_ms", 0.1, 64),
        ("frames", 2, 8),
        ("seed", 0, 2147483647),
        ("output_size", 32, 64),
    ):
        result[key] = finite_number(value.get(key), key, low, high)
    for key in ("frames", "seed", "output_size"):
        if result[key] != int(result[key]):
            raise ValueError(f"{key}: 정수가 필요합니다.")
        result[key] = int(result[key])
    if result["output_size"] not in (32, 64):
        raise ValueError("출력 크기는 32 또는 64입니다.")
    if value.get("scene") not in ("macbeth", "uniform", "slanted_edge"):
        raise ValueError("지원하는 정적 장면을 선택하세요.")
    result["scene"] = value["scene"]
    return result


METRICS = {
    "mean_photoelectrons": {"title": "평균 광전자", "unit": "e−"},
    "raw_temporal_noise_dn": {"title": "RAW 시간 잡음", "unit": "DN RMS"},
    "raw_snr_db": {"title": "RAW 시간 SNR", "unit": "dB"},
    "saturation_fraction": {"title": "포화 비율", "unit": "fraction"},
}


def validate_requirement(value):
    if (
        not isinstance(value, dict)
        or value.get("metric") not in METRICS
        or value.get("operator") not in (">=", "<=")
    ):
        raise ValueError("지원하는 지표와 판정 방향을 선택하세요.")
    return {
        "id": str(value.get("id") or uuid.uuid4()),
        "metric": value["metric"],
        "operator": value["operator"],
        "threshold": finite_number(value.get("threshold"), "판정 기준", -1000000, 1000000),
    }


def evaluate_requirements(requirements, variants):
    result = []
    for variant in variants:
        for req in requirements:
            actual = variant["metrics"].get(req["metric"])
            status = "unknown"
            if actual is not None:
                passed = (
                    actual >= req["threshold"]
                    if req["operator"] == ">="
                    else actual <= req["threshold"]
                )
                status = "passed" if passed else "failed"
            result.append(
                {**req, "variant_id": variant["variant_id"], "actual": actual, "status": status}
            )
    return result
