#!/usr/bin/env python3
"""Build a self-contained Korean delivery report from verified ISP Lab evidence."""

# Keep the standalone HTML template's CSS rules and prose paragraphs together.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode


def load(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def esc(value):
    return html.escape(str(value), quote=True)


def number(value, digits=4):
    return "—" if value is None else f"{value:,.{digits}f}"


def ratio(value):
    return "—" if value is None else f"{100 * value:,.1f}%"


def table(headers, rows):
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + "".join(f"<th>{esc(h)}</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows)
        + "</tbody></table></div>"
    )


def demo_url(base, run, candidate, *, baseline=None, baseline_candidate="candidate-00"):
    return (
        base.rstrip("/")
        + "/reference-workshop.html?"
        + urlencode(
            {
                "run": run,
                "candidate": candidate,
                "baseline": baseline or run,
                "baseline_candidate": baseline_candidate,
                "field": "outer-ne",
            }
        )
    )


def render(delivery):
    groups = {group["label"]: group for group in delivery["groups"]}
    noise = groups["noise-detail"]
    selection = noise["selection"]
    status = {
        "baseline_retained": "기준 ISP 유지",
        "recommended_within_demo": "선언한 제약 안에서 후보 선택",
        "inconclusive": "선택 미결정",
    }.get(selection["status"], selection["status"])
    reasons = {
        "texture_below_limit": "질감 유지 부족",
        "additional_halo_above_limit": "halo 증가",
        "color_regression_above_limit": "색차 퇴행",
        "missing_or_invalid_field_metrics": "지표 또는 지지 영역 불충분",
    }
    noise_profiles = {c["id"]: c["profile"] for c in noise["candidates"]}
    noise_rows = []
    for row in selection["rows"]:
        profile = noise_profiles[row["candidate_id"]]
        selected = " · 선택" if row["candidate_id"] == selection["selected_candidate_id"] else ""
        noise_rows.append(
            [
                row["candidate_id"] + selected,
                f"{profile['raw_denoise']['strength']:g} / {profile['linear_sharpen']['amount']:g}",
                number(row["noise"], 6),
                ratio(row["texture_retention"]),
                number(row["halo_increase"]),
                number(row["delta_e_increase"]),
                "조건 충족"
                if row["eligible"]
                else ", ".join(reasons.get(r, r) for r in row["reasons"]),
            ]
        )
    color_rows, fit_rows = [], []
    for scale in ("0.9", "1.0", "1.1"):
        group = groups["color-cfa-" + scale]
        baseline, changed = group["candidates"]
        for field_id, label in (("center", "중심"), ("outer-ne", "주변 NE")):
            a, b = baseline["fields"][field_id], changed["fields"][field_id]
            support = (
                a["metric_support"]["color"]["eligible"]
                and b["metric_support"]["color"]["eligible"]
            )
            color_rows.append(
                [
                    "×" + scale,
                    label,
                    number(a["delta_e00_mean"]) + " → " + number(b["delta_e00_mean"]),
                    number(a["color_noise_luma"], 6) + " → " + number(b["color_noise_luma"], 6),
                    "평가 가능" if support else "색 패치 지지 부족",
                ]
            )
        fitted = changed["color_calibration"]
        fit_rows.append(
            [
                "×" + scale,
                number(fitted["holdout_delta_e00_mean"]),
                number(fitted["holdout_delta_e00_max"]),
                " / ".join(number(v, 6) for v in fitted["matrix_column_l2_norm"]),
                f"{fitted['white_anchor_max_abs_error']:.2e}",
            ]
        )
    demosaic_rows = []
    lens_groups = [("demosaic-baseline", "기준 렌즈")]
    if "demosaic-curvature" in groups:
        lens_groups.append(("demosaic-curvature", "곡률 ×1.005"))
    lens_note = (
        "기준 렌즈와 최초 유한 곡률면의 곡률 ×1.005를 각각 계산한 뒤"
        if len(lens_groups) == 2
        else "고정된 기준 특허 렌즈로 계산한 뒤"
    )
    for label, lens in lens_groups:
        for candidate in groups[label]["candidates"]:
            center = candidate["fields"]["center"]["demosaic"]
            outer = candidate["fields"]["outer-ne"]["demosaic"]
            demosaic_rows.append(
                [
                    lens,
                    candidate["profile"]["demosaic_method"],
                    number(center["chroma_residual_rms"], 6),
                    number(outer["chroma_residual_rms"], 6),
                    number(center["luma_rmse"], 6),
                    number(outer["luma_rmse"], 6),
                ]
            )
    performance_rows = []
    for group in delivery["groups"]:
        performance_rows.append(
            [
                group["label"],
                len(group["candidates"]),
                sum(len(c["fields"]) for c in group["candidates"]),
                number(group["timing"]["execution_wall_seconds"], 1),
                number(group["timing"]["queue_seconds"], 2),
                f"{group['logical_bytes'] / 1e9:.3f} / {group['allocated_bytes'] / 1e9:.3f}",
            ]
        )
    cards = "".join(
        f'<a class="demo" href="{esc(item["url"])}"><span>0{index}</span>'
        f"<h2>{esc(item['title'])}</h2><p>{esc(item['question'])}</p><b>비교 화면 열기 ↗</b></a>"
        for index, item in enumerate(delivery["demos"], 1)
    )
    counts, storage = delivery["counts"], delivery["storage"]
    numbers = [
        (counts["jobs"], "완료 작업"),
        (counts["candidates"], "ISP·취득 케이스"),
        (counts["fields"], "native 시야 결과"),
        (f"{counts['raw_arrays']:,}", "보존 RAW 사본"),
    ]
    stats = "".join(
        f"<div><strong>{esc(v)}</strong><span>{esc(k)}</span></div>" for v, k in numbers
    )
    source_rows = [
        [
            g["label"],
            g["id"],
            (g.get("source") or {}).get("run_id", "새 chirp 취득"),
            (g.get("source") or {}).get("candidate_id") or "—",
        ]
        for g in delivery["groups"]
    ]
    return f'''<!doctype html>
<html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CameraE2E · Reference ISP Lab</title>
<style>
:root{{font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;color:#18332d;background:#f2f5f1;font-size:15px;line-height:1.65}}
*{{box-sizing:border-box}}body{{margin:0}}main{{max-width:1180px;margin:auto;padding:52px 32px 70px}}
.eyebrow{{font-size:12px;letter-spacing:2px;font-weight:700;color:#3c8071}}h1{{font-size:44px;line-height:1.2;letter-spacing:-1.7px;margin:16px 0}}h2{{font-size:24px;letter-spacing:-.6px;margin:0 0 12px}}h3{{font-size:18px;margin:24px 0 10px}}
p{{margin:8px 0 16px}}.lead{{max-width:890px;font-size:18px;color:#4c6258}}.tag{{font-size:12px;display:inline-block;padding:5px 12px;background:#e0ece3;border-radius:20px;margin:0 6px 8px 0}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);background:#183f36;color:#fff;border-radius:14px;margin:28px 0;overflow:hidden}}.stats div{{padding:23px 25px;border-right:1px solid #ffffff20}}.stats strong{{font-size:33px;display:block;font-variant-numeric:tabular-nums}}.stats span{{font-size:12px;color:#b7d3c7}}
.demos{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:24px 0 32px}}a{{color:#08665d}}.demo{{text-decoration:none;background:#fff;border:1px solid #d7e2d9;border-radius:14px;padding:24px;display:flex;flex-direction:column}}.demo>span{{font-size:12px;color:#779d8d}}.demo h2{{font-size:21px;margin-top:12px}}.demo p{{color:#5b7064;font-size:14px;flex:1}}.demo b{{font-size:13px}}
section{{background:#fff;border:1px solid #dce4db;border-radius:14px;margin:18px 0;padding:28px}}.caption{{color:#6b7e72;font-size:13px}}.result{{padding:15px 18px;background:#edf4ee;border-left:3px solid #428776;margin:18px 0}}.result b{{color:#195e4a}}
.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px;font-variant-numeric:tabular-nums}}th{{text-align:left;font-weight:600;color:#678073;background:#f4f7f2}}td,th{{padding:12px 10px;border-bottom:1px solid #e7ece5;vertical-align:top}}td{{white-space:nowrap}}tr:last-child td{{border-bottom:0}}details table{{font-size:11px}}summary{{cursor:pointer;font-weight:600}}code{{font-size:12px;overflow-wrap:anywhere}}footer{{font-size:12px;color:#78897d;margin-top:28px}}.footlinks{{display:flex;gap:20px;flex-wrap:wrap}}
@media(max-width:760px){{main{{padding:25px 16px}}h1{{font-size:32px}}.demos{{grid-template-columns:1fr}}.stats{{grid-template-columns:repeat(2,1fr)}}section{{padding:20px}}}}
@media print{{:root{{background:white}}main{{padding:0}}section,.demo{{break-inside:avoid}}.stats{{color:#18332d;background:#edf4ee}}a{{text-decoration:none}}}}
</style>
<main><header><div class="eyebrow">CAMERAE2E · REFERENCE ISP LAB</div>
<h1>렌즈·센서의 설계 차이를<br>ISP의 선택과 손실까지 연결합니다.</h1>
<p class="lead">특허 처방과 검토된 센서 구조에서 생성한 원본 RAW를 보존하고, 잡음·질감, 색 보정, demosaic를 같은 입력과 표시 조건에서 비교한 실행 패키지입니다.</p>
<span class="tag">US10310255B2 · p0014</span><span class="tag">IMX462 구조 기반 · 2.9 μm</span><span class="tag">9개 시야 · 256×256 native ROI</span><span class="tag">동일 RAW 재처리</span>
<div class="stats">{stats}</div></header><div class="demos">{cards}</div>
<section><div class="eyebrow">01 · NOISE / DETAIL</div><h2>잡음만 줄이는 것이 좋은 ISP 선택은 아닙니다.</h2>
<p>RAW NR와 제한된 sharpen을 6개 조합으로 비교했습니다. 모든 시야의 질감·색·halo 조건을 먼저 확인하고, 통과한 후보 중 가장 나쁜 시야의 시간 잡음을 줄이는 후보를 선택합니다.</p>
<div class="result"><b>{esc(status)}</b> · 선택 ID: {esc(selection.get("selected_candidate_id") or "없음")}<br>선택안의 최악 시야 잡음 개선: {ratio(selection.get("noise_improvement_fraction"))}</div>
{table(["후보", "NR / sharpen", "최악 시간 잡음", "최소 질감 유지", "최대 추가 halo", "최대 ΔE 증가", "판정"], noise_rows)}
<p class="caption">질감 ≥95%, 같은 edge 사이트의 추가 overshoot/undershoot ≤0.05, ΔE00 증가 ≤1.0. 잡음 이득이 1% 미만이면 기준안을 유지합니다. 시간 잡음은 반복 RAW의 픽셀별 시간 분산이며 선형 RGB 단위입니다.</p>
<p><a href="{esc(delivery["legacy_gaussian"]["url"])}">동일 원본의 기존 Gaussian과 RAW NR 비교 열기 ↗</a></p><p class="caption">같은 source RAW와 선형 표시 단위를 사용하지만 기존 Gaussian의 지표 측정 영역은 새 공통 유효 영역과 다릅니다. 이 표의 숫자와 직접 순위를 매기지 않고 별도 비교 화면으로 제공합니다.</p></section>
<section><div class="eyebrow">02 · SENSOR / COLOR</div><h2>CFA가 달라지면 색 행렬과 색 잡음도 다시 봅니다.</h2>
<p>CFA 두께 ×0.9/1.0/1.1 각각에서 고정 spectral CCM과 재피팅한 CCM을 동일 RAW로 비교했습니다. 광학·픽셀 모델에 결합된 행렬을 모든 시야에 그대로 적용합니다.</p>
{table(["CFA", "위치", "실제 차트 ΔE00 · 고정 → 재피팅", "색 차트 시간 잡음 Y · 고정 → 재피팅", "측정 지지"], color_rows)}
<h3>피팅에 사용하지 않은 Macbeth24 · 모델 진단</h3>
{table(["CFA", "보류 ΔE00 평균", "보류 ΔE00 최대", "RGB 행렬 column L2 norm", "white 최대 오차"], fit_rows)}
<p class="caption">Munsell64로 피팅하고 Macbeth24를 보류했습니다. 보류 색차는 잡음 없는 모델 진단이며 위의 실제 취득 차트 평가와 구분됩니다. 행렬 계수 크기는 잡음 증폭을 설명하는 단서이고, 잡음 결과는 반복 취득에서 따로 측정합니다.</p></section>
<section><div class="eyebrow">03 · OPTICS / DEMOSAIC</div><h2>렌즈를 거친 같은 입력에서 demosaic의 잔차를 비교합니다.</h2>
<p>{esc(lens_note)}, 같은 chirp RAW에서 세 가지 demosaic를 비교했습니다. 고주파 입력은 광학 halo까지 이어지며 native 계산 후 확대하지 않습니다.</p>
{table(["광학", "Demosaic", "중심 chroma RMS", "주변 NE chroma RMS", "중심 luma RMSE", "주변 NE luma RMSE"], demosaic_rows)}
<p class="caption">45° chirp, 0.02–0.45 cycles/native pixel. 위 잔차는 co-located R/Gr/Gb/B 모델 응답을 기준으로 잡음·ADC를 우회한 재구성 진단입니다. 측정 정답이나 광학 단독 MTF가 아닙니다. 일반 RAW 처리 영상도 비교 화면에서 확인할 수 있습니다.</p></section>
<section><div class="eyebrow">DELIVERY · REPRODUCIBLE EVIDENCE</div><h2>원본·설정·수치를 하나의 패키지로 보존했습니다.</h2>
<p>Manifest 파일 <b>{counts["files"]:,}개</b>와 RAW 사본 <b>{counts["raw_arrays"]:,}개</b>를 확인했습니다. 논리 용량은 <b>{storage["logical_bytes"] / 1e9:.3f} GB</b>, SSD 할당 용량은 <b>{storage["allocated_bytes"] / 1e9:.3f} GB</b>입니다.</p>
{table(["작업", "케이스", "시야", "실제 실행(s)", "실행 전 대기(s)", "논리 / 할당 GB"], performance_rows)}
<p class="caption">해시 체인을 확인한 로컬 Registry의 running→succeeded 시각 차이로 실제 작업 벽시계 시간을 계산했습니다. 취득·ISP·파일 저장·완료 처리를 포함하며 created→running 대기는 별도입니다. 후보별 시간이 저장되지 않은 작업을 후보 수로 나누어 추정하지 않습니다. ISP 하드웨어 지연이나 실시간 처리율이 아닙니다. 용량은 이번 {counts["jobs"]}개 작업의 manifest 범위이며 기존 source와 광학 캐시는 제외합니다. 재처리 RAW 사본 수를 독립 촬영이나 장면 수로 세지 않습니다.</p>
<details><summary>고정된 source와 실행 ID</summary>{table(["작업", "결과 run", "원본 run", "원본 candidate"], source_rows)}</details>
<p class="caption">같은 source의 hash, 9개 시야, 지표 지지 영역, 색 calibration의 불변 ID와 학습/보류 분리, chirp basis, 고정 white·gamma, 현재 구현 fingerprint를 검사했습니다.</p>
<div class="footlinks"><a href="delivery.json">전달 명세 JSON</a><a href="verification.json">전체 검증·측정 JSON</a><a href="runs.json">작업 영수증</a></div></section>
<footer><b>적용 범위</b> · 기하 ray PSF는 회절을 포함하지 않습니다. 9개 로컬 탐침은 전체 제품 센서 프레임이 아니며 모델 수렴·실측 보정·제품 품질 인증을 뜻하지 않습니다. 원자료와 파생자료의 이용 조건은 기존 Reference 출처 계약을 따릅니다.<br>생성: {esc(delivery["generated_at"])} · 검증 SHA-256: <code>{esc(delivery["verification_sha256"])}</code></footer></main></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8780")
    parser.add_argument(
        "--verification", default="outputs/reference-isp-lab-delivery-20261011/verification.json"
    )
    parser.add_argument("--runs", help="Default: runs.json next to verification")
    parser.add_argument("--output-dir", help="Default: directory containing verification")
    args = parser.parse_args()
    verification_path = Path(args.verification)
    runs_path = Path(args.runs) if args.runs else verification_path.with_name("runs.json")
    verification = load(verification_path)
    if verification.get("status") != "verified" or verification.get("failures"):
        raise ValueError("Final verification must be verified with no failures before delivery")
    receipts = {r["label"]: r for r in load(runs_path)}
    groups = {g["label"]: g for g in verification["groups"]}
    required = {
        "noise-detail",
        "color-cfa-0.9",
        "color-cfa-1.0",
        "color-cfa-1.1",
        "chirp-baseline",
        "demosaic-baseline",
    }
    optional = {"chirp-curvature", "demosaic-curvature"}
    if set(groups) not in (required, required | optional) or not all(
        c["passed"] for c in verification["checks"]
    ):
        raise ValueError("All six core demo jobs and any included optional pair must be verified")
    for label, group in groups.items():
        receipt = receipts[label]
        if (
            receipt["status"] != "succeeded"
            or receipt["id"] != group["id"]
            or receipt["manifest_sha256"] != group["manifest_sha256"]
        ):
            raise ValueError("Verification and latest receipts differ: " + label)
        if group.get("timing", {}).get("source") != "hash_verified_local_registry_audit":
            raise ValueError("Job has no hash-verified Registry timing")
    noise, color, demosaic = (
        groups[k] for k in ("noise-detail", "color-cfa-0.9", "demosaic-baseline")
    )
    delivery = {
        "schema": "camera_reference_isp_lab_delivery_v1",
        "status": "verified",
        "title": "Reference ISP Lab · 렌즈·센서 설계와 ISP의 상충관계",
        "generated_at": datetime.now(UTC).isoformat(),
        "physical_validation": "not_established",
        "verification_sha256": sha(verification_path),
        "runs_sha256": sha(runs_path),
        "source_code_sha256": verification["verified_source_code_sha256"],
        "optional_lens_variant_included": verification["optional_lens_variant_included"],
        "counts": {
            "jobs": len(groups),
            "candidates": verification["candidates_verified"],
            "fields": verification["fields_verified"],
            "files": verification["files_verified"],
            "raw_arrays": verification["raw_arrays_verified"],
        },
        "storage": verification["manifest_artifact_storage"],
        "timing_audit": verification["timing_audit"],
        "timing_totals": {
            "execution_wall_seconds": sum(
                g["timing"]["execution_wall_seconds"] for g in groups.values()
            ),
            "queue_seconds": sum(g["timing"]["queue_seconds"] for g in groups.values()),
            "meaning": "sum of individual job intervals; not total development elapsed time or hardware latency",
        },
        "groups": verification["groups"],
        "demos": [
            {
                "title": "잡음과 질감의 선택",
                "question": "더 깨끗한 결과가 정말 더 좋은 ISP인가?",
                "url": demo_url(args.base_url, noise["id"], "candidate-04"),
            },
            {
                "title": "CFA 변경과 색 재보정",
                "question": "센서가 달라지면 색과 색 잡음은 어떻게 바뀌는가?",
                "url": demo_url(args.base_url, color["id"], "candidate-01"),
            },
            {
                "title": "렌즈와 demosaic의 결합",
                "question": "같은 광학 입력에서 demosaic 선택이 고주파 재구성에 무엇을 바꾸는가?",
                "url": demo_url(args.base_url, demosaic["id"], "candidate-02"),
            },
        ],
        "legacy_gaussian": {
            "url": demo_url(
                args.base_url,
                noise["id"],
                "candidate-02",
                baseline="run-cbdb6ba5d2804c58a97c37ea23c0bb08",
            ),
            "source_run_id": "run-34aadb199e4d416bb0ba390ccf79636c",
            "comparability": "same preserved RAW and linear display units; old whole-ROI metrics are not ranked against new supported-mask metrics",
        },
        "limits": verification["limits"],
    }
    output = Path(args.output_dir) if args.output_dir else verification_path.parent
    output.mkdir(parents=True, exist_ok=True)
    (output / "delivery.json").write_text(
        json.dumps(delivery, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    (output / "report.html").write_text(render(delivery))
    print(output / "report.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
