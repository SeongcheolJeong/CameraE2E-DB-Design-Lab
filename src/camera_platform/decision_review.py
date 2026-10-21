"""Read-only presentation of two saved design decisions; never run or reselect.

JSON documents are pinned to their recorded bytes. Lists read metadata only;
only a selected case opens its original integer RAW member. The displayed rail
mask is derived from final codes, not mislabeled as a full-well truth mask.
"""

from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import zipfile

import numpy as np

STUDIES = {
    "signal": {
        "directory": "reports/camera_platform/constrained-selection-20260926",
        "title": "신호 우선 · 포화와 노출 제약",
        "documents": {
            "results": ("results.json", "3b457c5c0fc7263e216729d5971a5e84471f27b2e9f26d932a0dca25a0ae2989"),
            "plan": ("plan.json", "8db4f0f37e4eec9a493104819d71a3f15fac9726f29b72442805d8b698b6cb12"),
            "choice": ("selection.json", "513bc11167f2238efb18bc1baecd6ecf4fafbc256c0528a49e8ab92cc26cc48b"),
        },
    },
    "motion": {
        "directory": "reports/camera_platform/motion-color-tradeoff-20260926",
        "title": "움직임 · RAW 채널비 유지",
        "documents": {
            "results": ("results.json", "430b9bae8bef5b39c8165c7e465418d5890020d5784c83d289f4ecc59a86fa1d"),
            "plan": ("plan.json", "c85bc595b4eaaa29a5c81a0b6ab390bd3da94305abdf000dbb848a9e3d8cdcb1"),
            "choice": ("conditional-choice.json", "7ec692a77bddff1e9c29fb9c28c7e0a2dadfcb8b0241ba4b6f1f65de060b310d"),
            "calibration": ("frozen-neutral-gains.json", "4d81e82b628ce03ba091993b4beff8eef340d518ef4abed36ef0cbc5b3c8617f"),
        },
    },
}
LIMIT_LABELS = {
    "minimum_green_dn": "Green 평균 신호 하한 · DN",
    "minimum_green_snr_p10": "예측 Green SNR p10 하한 · 선형비",
    "maximum_well_fraction": "전하 저장소 포화율 상한 · 비율",
    "maximum_white_fraction": "최종 RAW 상단 도달률 상한 · 비율",
    "maximum_exposure_ms": "노출 상한 · ms",
    "minimum_signed_motion_transfer": "부호를 보존한 광전하 대비 유지율 하한 · 비율",
    "maximum_post_calibration_channel_ratio_error": "고정 보정 후 RAW 채널비 차이 상한 · 비율",
}


class EvidenceError(ValueError):
    """Saved bytes or their cross-links no longer match the review binding."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_PACKAGE_DIRECTORY = Path(__file__).resolve().parent / "data" / "r1" / "decisions"
_PACKAGE_MANIFEST_SHA256 = "dc4453513722a659b535ffb37bf53c956003a58903bb4f1ba2f7477d38ea3993"


class DecisionReview:
    def __init__(self, workspace: Path, studies: dict | None = None, *, source: str = "package",
                 package_root: Path | None = None):
        if source not in {"package", "workspace"}:
            raise ValueError("Unknown saved decision source")
        self.source = source
        self.workspace = Path(workspace).resolve()
        self.root = (Path(package_root).resolve() if package_root is not None else _PACKAGE_DIRECTORY
                     ) if source == "package" else self.workspace
        self.studies = deepcopy(STUDIES if studies is None else studies)
        self.manifest_files = self._package_manifest() if source == "package" else None

    def _package_manifest(self) -> dict:
        """Bind packaged, read-only evidence to the copied source bytes."""
        path = self.root / "manifest.json"
        if path.is_symlink() or not path.is_file():
            raise EvidenceError("Packaged saved decision manifest is missing")
        content = path.read_bytes()
        if _sha(content) != _PACKAGE_MANIFEST_SHA256:
            raise EvidenceError("Packaged saved decision manifest changed")
        manifest = json.loads(content)
        if (manifest.get("schema") != "camera_r1_readonly_decisions_bundle_v1"
                or manifest.get("license_notice") != "LICENSE"
                or manifest.get("physical_validation") != "not_established"
                or manifest.get("read_only") is not True
                or not isinstance(manifest.get("files"), dict)):
            raise EvidenceError("Packaged saved decision manifest is invalid")
        files = manifest["files"]
        expected_documents = {f"{identity}/{name}" for identity, study in self.studies.items()
                              for name, _ in study["documents"].values()}
        if not expected_documents.issubset(files) or "LICENSE" not in files:
            raise EvidenceError("Packaged saved decision documents are incomplete")
        for relative, entry in files.items():
            parts = Path(relative).parts
            if (parts not in [("LICENSE",)] and
                    (len(parts) != 2 or parts[0] not in self.studies or Path(parts[1]).name != parts[1])):
                raise EvidenceError("Packaged saved decision path is invalid")
            if (not isinstance(entry, dict) or not isinstance(entry.get("bytes"), int)
                    or entry["bytes"] < 0 or not isinstance(entry.get("sha256"), str)
                    or len(entry["sha256"]) != 64):
                raise EvidenceError("Packaged saved decision inventory is invalid")
            asset = self.root / relative
            if asset.is_symlink() or not asset.is_file() or asset.stat().st_size != entry["bytes"]:
                raise EvidenceError("Packaged saved decision asset missing or changed: " + relative)
        self._verified_package_bytes("LICENSE", files=files)
        return files

    def _verified_package_bytes(self, relative: str, *, files: dict | None = None) -> bytes:
        inventory = self.manifest_files if files is None else files
        entry = inventory.get(relative) if inventory is not None else None
        if entry is None:
            raise EvidenceError("Unregistered packaged saved decision asset")
        path = self.root / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise EvidenceError("Packaged saved decision asset missing or changed: " + relative)
        content = path.read_bytes()
        if _sha(content) != entry["sha256"]:
            raise EvidenceError("Packaged saved decision asset changed: " + relative)
        return content

    def _study(self, identity):
        if identity not in self.studies:
            raise KeyError("Unknown saved decision")
        return self.studies[identity]

    def _path(self, identity: str, name: str) -> Path:
        if not isinstance(name, str) or Path(name).name != name or "\\" in name:
            raise EvidenceError("Only a recorded basename can be opened")
        directory = (self.root / identity if self.source == "package"
                     else self.root / self._study(identity)["directory"])
        path = directory / name
        if (path.is_symlink() or not path.resolve().is_relative_to(self.root)
                or path.resolve().parent != directory.resolve()):
            raise EvidenceError("Saved evidence escapes the selected workspace")
        if self.source == "package" and f"{identity}/{name}" not in self.manifest_files:
            raise EvidenceError("Unregistered packaged saved decision asset")
        return path

    def document(self, identity: str, key: str) -> bytes:
        documents = self._study(identity)["documents"]
        if key not in documents:
            raise KeyError("Unknown saved document")
        name, expected = documents[key]
        path = self._path(identity, name)
        if self.source == "package":
            content = self._verified_package_bytes(f"{identity}/{name}")
        else:
            with path.open("rb") as stream:
                content = stream.read(2 * 1024**2 + 1)
        if len(content) > 2 * 1024**2 or _sha(content) != expected:
            raise EvidenceError("Saved decision document changed: " + key)
        return content

    def _load(self, identity):
        result = json.loads(self.document(identity, "results"))
        plan = json.loads(self.document(identity, "plan"))
        choice = json.loads(self.document(identity, "choice"))
        if result.get("status") != "completed" or result.get("plan") != plan:
            raise EvidenceError("A completed result and its original plan are required")
        if identity == "signal":
            decision = choice["decision"]
            if (result["selection"] != decision or not result["selection_frozen_before_holdout"]
                    or result["selection_sha256"] != self._study(identity)["documents"]["choice"][1]
                    or choice["plan_sha256"] != self._study(identity)["documents"]["plan"][1]):
                raise EvidenceError("Signal selection lost its frozen plan/result binding")
        else:
            decision = choice
            calibration = self.document(identity, "calibration")
            if (result["conditional_choice"] != choice or not result["no_reselection_after_holdout"]
                    or choice["holdout_seen"] is not False
                    or result["conditional_choice_sha256"] != self._study(identity)["documents"]["choice"][1]
                    or _sha(calibration) != choice["calibration_sha256"]
                    or plan["prior_selection_sha256"] != self._study("signal")["documents"]["choice"][1]):
                raise EvidenceError("Motion choice/calibration lost its saved decision binding")
        return result, plan, choice, decision

    def decision(self, identity: str) -> dict:
        result, plan, choice, decision = self._load(identity)
        selected = decision["selected_candidate_id"]
        rows = (result["training"] + result["holdout"] if identity == "signal"
                else result["development"] + result["holdout"])
        cases = []
        for row in rows:
            split = row["split"]
            if row.get("domain") == "outside_training_per_band_peak_envelope":
                split = "stress"
            motion = row.get("motion")
            case_id = _sha(row["arrays_file"].encode())[:24]
            cases.append({
                "case_id": case_id, "candidate_id": row["candidate_id"],
                "scenario_id": row["scenario_id"], "split": split,
                "domain": row.get("domain"), "noise_enabled": row.get("noise_enabled", False),
                "seed": row.get("seed"), "selected": row["candidate_id"] == selected,
                "exposure_ms": row["exposure_ms"], "gain": row["gain"],
                "green_dn": row["green_dn"], "green_snr_p10": row["green_snr_p10"],
                "well_fraction": row["well_fraction"], "white_fraction": row["white_fraction"],
                "signed_charge_transfer": motion["charge"]["signed_transfer"] if motion else None,
                "signed_raw_transfer": motion["raw"]["signed_transfer"] if motion else None,
                "raw_transfer_clipped": motion.get("raw_contrast_unscorable_due_clipping", False) if motion else False,
                "channel_ratio_error": row["color_after"]["maximum_absolute_relative_error"] if "color_after" in row else None,
                "failures": row.get("failures", row.get("violations", [])),
                "raw_sha256": row["raw_sha256"],
                "raw_url": f"/api/v1/decision-review/{identity}/cases/{case_id}/raw",
                "detail_url": f"/api/v1/decision-review/{identity}/cases/{case_id}",
            })
        if len({c["case_id"] for c in cases}) != len(cases):
            raise EvidenceError("Duplicate saved case identities")
        limits = dict(plan["limits"] if identity == "signal" else plan["signal_limits"])
        if identity == "motion":
            limits.update({key: plan[key] for key in (
                "minimum_signed_motion_transfer", "maximum_post_calibration_channel_ratio_error")})
        counts = []
        for split in ("train", "development", "holdout", "stress"):
            for noise in (False, True):
                group = [r for r in cases if r["selected"] and r["split"] == split and r["noise_enabled"] == noise]
                if group:
                    counts.append({"split": split, "noise_enabled": noise, "count": len(group),
                                   "passed": sum(not r["failures"] for r in group)})
        return {
            "id": identity, "title": self._study(identity)["title"], "selected_candidate_id": selected,
            "frozen_at_utc": choice["frozen_at_utc"], "automatic_approval": False,
            "physical_validation": "not_established", "read_only": True,
            "limits": [{"key": k, "label": LIMIT_LABELS[k], "value": v} for k, v in limits.items()],
            "candidate_count": decision.get("candidate_count", len(plan["candidates"])),
            "feasible_count": decision.get("feasible_count", len(decision.get("feasible_candidates", []))),
            "candidates": decision["candidates"] if identity == "signal" else [
                {"candidate_id": c["candidate_id"], "stop": c["stop"],
                 "exposure_ms": c["exposure_ms"], "gain": c["gain"],
                 "prior_signal_feasible": c["feasible"],
                 "motion_development_feasible": c["candidate_id"] in decision["feasible_candidates"],
                 "prior_worst_green_snr_p10": c["worst_green_snr_p10"]}
                for c in plan["candidates"]],
            "cases": cases, "evaluation_groups": counts,
            "source_identity": plan["source_identity"],
            "color_reference": plan.get("color_reference"),
            "calibration": json.loads(self.document(identity, "calibration")) if identity == "motion" else None,
            "documents": [{"key": key, "name": name, "sha256": checksum,
                           "url": f"/api/v1/decision-review/{identity}/documents/{key}"}
                          for key, (name, checksum) in self._study(identity)["documents"].items()],
            "scope_notes": [
                "저장된 결과 조회이며 계산·재선택·모델 승인·RAW 재처리를 수행하지 않습니다.",
                "후보 선택용 입력, 미사용 평가, 범위 밖 스트레스는 별도로 표시합니다. 합산한 합격률은 보증이 아닙니다.",
                "SNR은 모델 예측의 Green 하위 10% 분위·선형비이며 실측 잡음이나 dB가 아닙니다.",
                "합성 구면 처방·16×16 native crop·500/600 nm 두 파장 조건입니다. 실물 검증이 아닙니다.",
            ] + ([
                "공통 중심시각의 global box shutter입니다. rolling shutter·트랩·일반 3D 운동은 포함하지 않습니다.",
                "광전하 대비와 정수 RAW 대비를 구분하고 부호를 보존합니다. 음수는 대비 반전입니다.",
                "clear·2 ms가 RAW 채널비 기준이므로 그 오차는 정의상 0입니다. 절대 색 정확도가 아닙니다.",
                "모든 움직임·채널비 계산은 잡음 OFF입니다. 보정은 한 neutral 입력에서 고정했습니다.",
            ] if identity == "motion" else [
                "노출 상한은 움직임 흐림 평가를 대신하지 않습니다. 이 결정은 색·움직임 비용을 목적에 포함하지 않았습니다.",
            ]),
        }

    def catalog(self) -> dict:
        decisions, unavailable = [], []
        for identity in self.studies:
            try:
                decisions.append(self.decision(identity))
            except FileNotFoundError:
                unavailable.append({"id": identity, "reason": "저장된 연구 파일이 이 작업공간에 없습니다."})
        return {"read_only": True, "decisions": decisions, "unavailable": unavailable,
                "comparison_basis": "Different frozen engineering objectives, not replacement of an earlier result"}

    def _raw(self, identity: str, case_id: str):
        result, _, _, _ = self._load(identity)
        rows = result["training"] + result["holdout"] if identity == "signal" else result["development"] + result["holdout"]
        row = next((r for r in rows if _sha(r["arrays_file"].encode())[:24] == case_id), None)
        if row is None:
            raise KeyError("Unknown saved case")
        name = row["arrays_file"]
        path = self._path(identity, name)
        if path.stat().st_size > 64 * 1024**2:
            raise EvidenceError("Selected archive exceeds the preview budget")
        archive_source = (io.BytesIO(self._verified_package_bytes(f"{identity}/{name}"))
                          if self.source == "package" else path)
        with zipfile.ZipFile(archive_source) as archive:
            if archive.namelist().count("raw_dn.npy") != 1:
                raise EvidenceError("Require exactly one original RAW member")
            member = archive.getinfo("raw_dn.npy")
            if member.file_size > 4 * 1024**2 + 65536:
                raise EvidenceError("Original RAW exceeds the preview budget")
            content = archive.read(member)
        raw = np.load(io.BytesIO(content), allow_pickle=False)
        if (not isinstance(raw, np.ndarray) or raw.ndim != 2 or raw.dtype.kind not in "iu"
                or raw.dtype.itemsize > 4 or not raw.size or raw.size > 1_048_576
                or np.any(raw < 0) or np.any(raw > 4095)
                or _sha(raw.tobytes(order="C")) != row["raw_sha256"]):
            raise EvidenceError("Original RAW differs from the saved acquisition fingerprint")
        return row, raw, content

    def raw_bytes(self, identity: str, case_id: str) -> bytes:
        return self._raw(identity, case_id)[2]

    def case(self, identity: str, case_id: str) -> dict:
        from PIL import Image

        row, raw, _ = self._raw(identity, case_id)
        def png(image):
            stream = io.BytesIO()
            Image.fromarray(image).save(stream, format="PNG")
            return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
        return {"candidate_id": row["candidate_id"], "case_id": case_id,
                "shape": list(raw.shape), "dtype": str(raw.dtype), "raw_sha256": row["raw_sha256"],
                "raw_verified": True, "display_range_dn": [64, 4095],
                "raw_png": png(np.rint(np.clip((raw.astype(float)-64)/(4095-64), 0, 1)*255).astype(np.uint8)),
                "final_rail_png": png((raw == 4095).astype(np.uint8)*255),
                "rail_meaning": "Final integer RAW equals 4095; not a full-well mask",
                "raw_url": f"/api/v1/decision-review/{identity}/cases/{case_id}/raw"}


def install_routes(app, workspace: Path, *, workbench_url="/#experiments", source="package"):
    """Install concrete GET routes BEFORE the platform's generic registry routes."""
    from fastapi import HTTPException
    from fastapi.responses import Response

    review = DecisionReview(workspace, source=source)
    def invoke(function, *args):
        try:
            return function(*args)
        except (FileNotFoundError, KeyError) as exc:
            raise HTTPException(404, "Saved decision or selected evidence is unavailable") from exc
        except (EvidenceError, zipfile.BadZipFile, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/decision-review", tags=["saved_decision_review"])
    def catalog():
        return {**invoke(review.catalog), "workbench_url": workbench_url}

    @app.get("/api/v1/decision-review/{identity}/cases/{case_id}", tags=["saved_decision_review"])
    def case(identity: str, case_id: str):
        return invoke(review.case, identity, case_id)

    @app.get("/api/v1/decision-review/{identity}/cases/{case_id}/raw", tags=["saved_decision_review"])
    def raw(identity: str, case_id: str):
        content = invoke(review.raw_bytes, identity, case_id)
        return Response(content, media_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="{identity}-{case_id}-original-raw.npy"'})

    @app.get("/api/v1/decision-review/{identity}/documents/{key}", tags=["saved_decision_review"])
    def document(identity: str, key: str):
        content = invoke(review.document, identity, key)
        return Response(content, media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="{identity}-{key}.json"'})
