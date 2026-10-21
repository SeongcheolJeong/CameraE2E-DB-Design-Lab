#!/usr/bin/env python3
"""Compare a frozen DB calibration to new, independent synthetic Macbeth acquisitions.

This report is an analysis artifact. It never edits the active DB model or serves
its synthetic reference as physical measurement evidence.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import secrets
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from camera_platform import calibration_db_color as bridge
from camera_platform import calibration_db_color_engine as engine
from camera_platform import reference_isp
from camera_platform.registry import SQLiteRegistry, canonical, digest, now
from camera_platform.service import environment_fingerprint

LABELS = {
    "observation": "가상 실측 · 새 취득 A",
    "before": "보정 전 · DB QE + 기준 CCM",
    "qe_only": "QE만 보정 · 기준 CCM",
    "after": "보정 후 · 추정 QE + CCM",
    "replicate": "가상 실측 · 독립 반복 B",
}
FIELD_LABELS = {"center": "중심", "outer-ne": "주변 우상"}
ROLES = ("observation", "replicate", "before", "after")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical(data), encoding="utf-8")


def authored_reference(wave):
    """Authored v1 camera definition, independently evaluated, never a fitted profile."""
    wave = np.asarray(wave, dtype=float)
    qe = np.column_stack(
        [
            0.65 * np.exp(-0.5 * ((wave - 615) / 42) ** 2),
            0.70 * np.exp(-0.5 * ((wave - 540) / 35) ** 2),
            0.58 * np.exp(-0.5 * ((wave - 450) / 30) ** 2),
        ]
    )
    return {
        "schema": "authored_spectral_camera_reference_v1",
        "generator": "original calibration_color_demo.template authored camera definition",
        "source_kind": "synthetic",
        "wavelength_nm": wave.tolist(),
        "qe_rgb": np.maximum(qe, 0.0001).tolist(),
        "matrix": [[1.30, -0.12, -0.08], [-0.10, 1.24, -0.08], [0.03, -0.10, 1.20]],
        "white_balance": [1.1, 1.0, 1.2],
        "reference_white_e": 2000.0,
        "exposure_scale": 1.0,
        "target_space": "camera_linear_rgb_pre_clipping",
        "matrix_convention": "row_vector",
    }


def reference_raw(rate, readout, exposure_s, seed):
    """Independent NumPy Poisson + read-noise + ADC implementation for this linear DB mode."""
    rng = np.random.default_rng(seed)
    charge = rng.poisson((np.asarray(rate) + readout["dark_current_e_per_s"]) * exposure_s)
    charge = np.clip(charge, 0, readout["full_well_e"]).astype(float)
    charge += rng.normal(0, readout["read_noise_e"], charge.shape)
    dn = readout["black_level_dn"] + readout["dn_per_e"] * charge
    return np.clip(np.rint(dn), 0, 2 ** readout["bit_depth"] - 1).astype(np.int32)


def errors(prediction, observation):
    p, o = np.asarray(prediction, dtype=float), np.asarray(observation, dtype=float)
    if p.shape != o.shape or p.ndim != 2 or p.shape[1] != 3:
        raise ValueError("Metrics require matching patches x RGB arrays")
    if not np.isfinite(p).all() or not np.isfinite(o).all():
        raise ValueError("Metrics reject nonfinite inputs")
    delta = p - o
    return {
        "rmse": float(np.sqrt(np.mean(delta**2))),
        "channel_rmse": np.sqrt(np.mean(delta**2, axis=0)).tolist(),
        "mae": float(np.mean(np.abs(delta))),
        "max_abs": float(np.max(np.abs(delta))),
        "patch_rmse": np.sqrt(np.mean(delta**2, axis=1)).tolist(),
        "residual_rgb": delta.tolist(),
    }


def reduction(before, after):
    return None if before <= 0 else float(100 * (1 - after / before))


def _linear_mode(prepared, readout):
    model = prepared["electrical_model"]
    if (
        model["digital_gain"] != 1
        or model["post_gain_noise_v"] != 0
        or model["dark_current_doubling_c"] is not None
        or any(model["fixed_pattern"][k] != 0 for k in ("prnu_sigma", "dsnu_e_per_s"))
    ):
        raise ValueError("Independent reference sampler only covers the frozen linear no-FPN mode")
    physical_k = (
        model["conversion_gain_v_per_e"]
        * prepared["spec"]["gain"]
        * (2 ** model["adc"]["bit_depth"] - 1)
        / model["adc"]["full_scale_v"]
    )
    if not np.isclose(physical_k, readout["dn_per_e"], rtol=1e-12):
        raise ValueError("Independent ADC transfer differs from frozen DB readout")


def _load_source(workspace, registry, replay_id, destination):
    run, bound = registry.get_run_with_result_binding(replay_id)
    if (
        run["status"] != "succeeded"
        or run.get("job_kind") != bridge.JOB_KIND
        or bound is None
        or json.loads(bound["path"].read_bytes()) != run["result"]["summary"]
    ):
        raise ValueError("A verified completed DB QE/CCM replay is required")
    service = SimpleNamespace(registry=registry)
    model = bridge.get_model(service, run["specification"]["model_id"])
    if model != run["model_snapshot"]:
        raise ValueError("Saved replay/model identity differs")
    color = model["calibration_record_snapshot"]
    bridge._verify_color(service, color)
    task = json.loads(
        bridge.calibration_color.file(service, color["id"], "inputs.json").read_bytes()
    )
    if digest(task) != color["input_sha256"]:
        raise ValueError("Original calibration input changed")
    for name, entry in model["source_files"].items():
        relative = bridge.calibration_db._relative(name)
        binding = registry.artifact_binding(
            entry["sha256"], owner_kind=bridge.MODELS, owner_id=model["id"]
        )
        data = binding["path"].read_bytes()
        if len(data) != entry["bytes"] or sha(data) != entry["sha256"]:
            raise ValueError("Frozen source bytes changed: " + name)
        p = destination / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    ctx = model["source_context"]
    prepared = engine.prepare(
        workspace,
        destination,
        None,
        ctx["candidate_id"],
        ctx["condition_id"],
        ctx["manifest_sha256"],
    )
    application = engine.binding(prepared, color)
    bridge._same_application(model["resolved"], application)
    return run, model, prepared, application, task


def _plot(path, before, after, repeat, unit):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 3.2), constrained_layout=True)
    x = np.arange(1, 25)
    ax.bar(x - 0.20, before, width=0.38, label="Before calibration", color="#b77740")
    ax.bar(x + 0.20, after, width=0.38, label="After calibration", color="#087f82")
    ax.plot(x, repeat, color="#527288", marker=".", label="Independent repeat A vs B")
    ax.set(xlabel="Macbeth patch", ylabel="RGB patch RMSE (" + unit + ")", xticks=x)
    ax.grid(axis="y", alpha=0.2)
    ax.legend(ncol=3, fontsize=8)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def _image(root, relative, files):
    data = (root / relative).read_bytes()
    if files.get(relative) != sha(data):
        raise ValueError("Report image hash mismatch")
    return "data:image/png;base64," + base64.b64encode(data).decode()


def render_report(root, result):
    def esc(value):
        return html.escape(str(value), quote=True)

    figures = []
    for f in result["fields"]:
        name = FIELD_LABELS[f["field_id"]]
        rows = []
        for domain, label, unit in (
            ("raw", "RAW · DN", "DN"),
            ("isp", "ISP · 선형 camera RGB", "linear RGB"),
        ):
            panels = []
            for role in ("before", "observation", "after"):
                relative = f["series"][role]["images"][domain]
                src = _image(root, relative, result["files"])
                panels.append(
                    f"<figure><figcaption>{esc(LABELS[role])}</figcaption>"
                    f'<img src="{src}" alt="{esc(name + label + LABELS[role])}"></figure>'
                )
            metric = f["metrics"][domain]
            before, after, repeat = (metric[k]["rmse"] for k in ("before", "after", "repeat"))
            gain = reduction(before, after)
            plot = _image(root, f["plots"][domain], result["files"])
            channel_rows = "".join(
                f"<tr><td>{c}</td><td>{metric['before']['channel_rmse'][i]:.6g}</td>"
                f"<td>{metric['after']['channel_rmse'][i]:.6g}</td>"
                f"<td>{metric['repeat']['channel_rmse'][i]:.6g}</td></tr>"
                for i, c in enumerate(("R", "G", "B"))
            )
            rows.append(
                f'<h3>{esc(label)}</h3><div class="three">{"".join(panels)}</div>'
                f'<p class="metric">24패치 RMSE: <b>{before:.6g} → {after:.6g} {unit}</b>'
                f" · 감소 {gain:.2f}% · 독립 반복 A↔B {repeat:.6g} {unit}</p>"
                f"<table><tr><th>채널</th><th>보정 전 오차</th><th>보정 후 오차</th>"
                f"<th>독립 반복 차이</th></tr>{channel_rows}</table>"
                f'<img class="plot" src="{plot}" alt="{esc(name + label)} 패치별 오차">'
            )
        mid = f["metrics"]["isp"]["qe_only"]["rmse"]
        figures.append(
            f'<section data-field="{f["field_id"]}"><h2>{name}</h2>'
            + "".join(rows)
            + f"<p>QE만 적용한 ISP RMSE: {mid:.6g}. "
            "보정 전 → QE만 적용 → QE·CCM 적용의 영향을 같은 출력 공간에서 비교합니다. "
            "QE만 적용과 최종 CCM 결과는 동일 RAW를 사용합니다.</p></section>"
        )
    agg = result["aggregate"]
    cards = "".join(
        f"<div><span>{label}</span><b>{agg[k]['before']['rmse']:.5g} → "
        f"{agg[k]['after']['rmse']:.5g}</b><small>RMSE · "
        f"{agg[k]['reduction_percent']:.2f}% 감소</small></div>"
        for k, label in (("raw", "전체 RAW · DN"), ("isp", "전체 ISP · 선형 RGB"))
    )
    details = esc(
        json.dumps(
            {k: result[k] for k in ("protocol", "source", "aggregate")},
            ensure_ascii=False,
            indent=2,
        )
    )
    return (
        """<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DB 동일 조건 · 독립 합성 맥베스 보정 검증</title><style>
*{box-sizing:border-box}body{margin:0;background:#f1f4ef;color:#183c3c;
font:15px/1.65 system-ui,sans-serif}
main{max-width:1280px;margin:auto;padding:32px}
header,section{background:white;border:1px solid #d5dfd7;
border-radius:15px;padding:28px;margin:0 0 24px}h1{font-size:32px;line-height:1.3}h2{font-size:26px}
h3{margin-top:32px}.eyebrow{color:#087f82;letter-spacing:.1em;font-weight:700}.note{background:#faf4e9;
padding:12px 16px;border-left:3px solid #a7793c}.cards,.three{display:grid;gap:16px}
.cards{grid-template-columns:1fr 1fr}
.cards>div{background:#edf5f2;padding:18px;border-radius:10px}.cards b{display:block;font-size:25px}
.cards small{display:block}.three{grid-template-columns:repeat(3,1fr)}
figure{margin:0;border:1px solid #dae3df;
padding:10px;border-radius:8px}figcaption{font-size:13px;font-weight:650;margin-bottom:10px}img{width:100%;display:block}
.plot{margin-top:18px}.metric{background:#edf6f3;padding:12px}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{padding:9px;border-bottom:1px solid #dce5e0;text-align:left}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
button,a{color:#087f82}button{padding:8px 15px;border:1px solid #8db8ae;
border-radius:6px;background:white;cursor:pointer}
.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}select{padding:8px}code{overflow-wrap:anywhere;font-size:12px}
@media(max-width:800px){main{padding:12px}header,section{padding:16px}.three{grid-template-columns:1fr}.cards{grid-template-columns:1fr}}
@media print{body{background:white}.toolbar{display:none}
header,section{border:0;padding:0;break-before:page}header{break-before:auto}}
</style><main><header><div class="eyebrow">CAMERAE2E · SYNTHETIC HOLDOUT VALIDATION</div>
<h1>같은 DB 조건에서,<br>새 가상 실측값에 얼마나 가까워졌는가</h1>
<p class="note">가상 실측은 별도 seed로 새로 생성한 합성 데이터입니다.
추정 프로파일로 정답을 만들지 않았습니다.
보정 프로파일은 기존 파장 스캔·학습 RAW에서 이미 얻은 값을 고정 적용했고,
이번 RAW로 재피팅하지 않았습니다. 독립 취득·오차 비교는 확인했으며,
공통 광학·전기 모델을 사용하는 합성 검증입니다. 실물 카메라 정합은 미확립입니다.</p>
"""
        + f'<div class="cards">{cards}</div>'
        + "<p>조건: Canon p0248 × IMX462 구조 모델 · D65 · 1000 cd/m² · 1 ms · gain 1 · "
        "25°C · RGGB RAW12 · 256×256 · 취득당 8프레임 · 중심/주변 우상 · 450–650 nm.</p>"
        + "<p>RAW 미리보기는 모두 동일한 64–576 DN 표시 구간을 사용합니다. ISP 미리보기는 모두 "
        "동일한 WB·정규화·gamma 2.2를 사용합니다. "
        "수치 오차는 clipping/gamma 이전의 선형 배열에서 계산합니다. "
        "camera RGB는 표준 sRGB가 아니므로 ΔE 색 정확도로 해석하지 않습니다.</p>"
        + f"<p><code>{esc(result['id'])}</code> · {esc(result['created_at'])}</p>"
        + '<div class="toolbar"><label>시야 <select id="field"><option value="all">모두</option>'
        '<option value="center">중심</option>'
        '<option value="outer-ne">주변 우상</option></select></label>'
        '<button onclick="print()">인쇄 / PDF 저장</button><a href="report.json">수치 JSON</a> · '
        '<a href="evidence.zip">RAW·ISP·근거 ZIP</a></div></header>'
        + "".join(figures)
        + "<section><h2>검증 방법과 해석</h2><ol>"
        "<li>이전 보정 모델과 원본 DB의 처방·시야·스펙트럼·전기적 응답을 hash로 고정했습니다.</li>"
        "<li>원래 합성 카메라의 Gaussian QE와 authored CCM에서 새 기준 RAW A/B를 생성했습니다. "
        "포아송·읽기 잡음·ADC는 별도 NumPy 구현을 사용하고, 모든 취득의 seed를 분리했습니다.</li>"
        "<li>기준 RAW, 보정 전/후 예측 RAW를 각각 8장 취득했습니다. 보정 전후에는 같은 "
        "광량·노출·black·DN/e·read noise·dark current를 유지했습니다.</li>"
        "<li>ISP 기준은 가상 카메라의 고정된 원래 CCM 출력입니다. RAW만으로 ISP 정답을 "
        "추정한 것이 아닙니다. 보정 전 ISP도 같은 camera RGB 출력 공간의 "
        "기준 CCM으로 처리했습니다.</li>"
        "<li>24패치 내부 50% 영역의 R·G·B 평균으로 오차를 계산했습니다. A/B 반복 차이는 "
        "잡음에 의한 비교 기준이며, 통계적 신뢰구간이나 검증 합격 임계값은 아닙니다.</li>"
        "<li>QE는 파장별 광원 스캔에서 얻은 프로파일을 사용합니다. 새 Macbeth holdout을 "
        "이용한 QE·CCM 피팅, 노출/WB 조정, 패치별 정규화나 "
        "후보 선택은 수행하지 않았습니다.</li></ol>"
        f"<details><summary>입력·조건·수치·출처 전체 근거</summary><pre>{details}</pre></details>"
        '</section></main><script>document.querySelector("#field").addEventListener("change",e=>'
        'document.querySelectorAll("[data-field]").forEach(s=>s.hidden=e.target.value!=="all"&&'
        "s.dataset.field!==e.target.value));</script></html>"
    ).encode()


def run(workspace, state_dir, replay_id, output, seed):
    workspace, root = Path(workspace).resolve(), Path(output).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("Preserve the existing experiment; choose a new empty output directory")
    root.mkdir(parents=True, exist_ok=True)
    if type(seed) is not int or not 0 <= seed <= 2**31 - 40000:
        raise ValueError("Seed must leave room for disjoint acquisition streams")
    registry = SQLiteRegistry(state_dir)
    with tempfile.TemporaryDirectory(prefix="db-holdout-source-", dir=root) as tmp:
        parent, model, prepared, applied, task = _load_source(
            workspace, registry, replay_id, Path(tmp)
        )
        reference = authored_reference(engine.WAVE)
        color = model["calibration_record_snapshot"]
        if (
            color["source_kind"] != "synthetic"
            or task["source_notes"]["target"]
            != "Authored synthetic camera linear ISP output; not measured colour accuracy"
        ):
            raise ValueError("This reference generator is scoped to the original authored demo")
        readout = applied["db_readout"]
        _linear_mode(prepared, readout)
        protocol = {
            "schema": "camera_db_macbeth_holdout_protocol_v1",
            "analysis_code_sha256": sha(Path(__file__).read_bytes()),
            "source_kind": "synthetic",
            "seed": seed,
            "frames_per_acquisition": 8,
            "acquisitions": {
                fid: {
                    role: [seed + ri * 10000 + fi * 100 + n for n in range(8)]
                    for ri, role in enumerate(ROLES)
                }
                for fi, fid in enumerate(engine.FIELDS)
            },
            "calibration_model_id": model["id"],
            "profile_sha256": model["profile_sha256"],
            "reference_generator_sha256": digest(reference),
            "reference": reference,
            "configuration": prepared["spec"],
            "readout": readout,
            "fitting_on_this_dataset": False,
            "candidate_selection_on_this_dataset": False,
            "prior_calibration_acquisitions": {
                role: task["ccm_" + role]["acquisition_id"] for role in ("training", "validation")
            },
            "reference_isp_target": "authored original CCM, paired with independent reference RAW",
            "common_output_space": "camera_linear_rgb_pre_clipping",
            "before_isp_policy": "DB baseline QE + imported baseline CCM in common output space",
            "conditions_scope": "Same DB optics, radiance, exposure, local geometry and readout",
            "physical_validation": "not_established",
            "independence_scope": (
                "Disjoint acquisitions and independent readout sampler; shared optical/ISP model"
            ),
        }
        seeds = [n for f in protocol["acquisitions"].values() for role in f.values() for n in role]
        if len(set(seeds)) != len(seeds):
            raise ValueError("Holdout acquisition seeds overlap")
        training_seed = task["source_notes"]["seed"]
        if set(seeds) & {training_seed + 100, training_seed + 101}:
            raise ValueError("Holdout and original calibration acquisition seeds overlap")
        save_json(root / "protocol.json", protocol)  # Freeze BEFORE any new RAW acquisition.
        save_json(root / "frozen-model.json", model)
        save_json(root / "reference-generator.json", reference)
        solution = {"result": prepared["optical"], "arrays": prepared["arrays"]}
        truth_qe = np.asarray(reference["qe_rgb"])[:, [0, 1, 1, 2]]
        after_qe = np.asarray(applied["applied_response"]["qe_rggb"])
        truth_matrix = np.diag(
            np.asarray(reference["white_balance"]) / reference["reference_white_e"]
        ) @ np.asarray(reference["matrix"])
        matrices = {
            "observation": truth_matrix,
            "replicate": truth_matrix,
            "before": np.asarray(applied["isp_adapter"]["baseline_electron_to_output_matrix"]),
            "qe_only": np.asarray(applied["isp_adapter"]["baseline_electron_to_output_matrix"]),
            "after": np.asarray(applied["isp_adapter"]["electron_to_output_matrix"]),
        }
        tuning = deepcopy(prepared["spec"]["isp"])
        tuning["wb"], tuning["ccm"], tuning["color_calibration_id"] = (
            [1.0] * 3,
            np.eye(3).tolist(),
            None,
        )
        field_map = {f["id"]: f for f in prepared["optical"]["fields"]}
        fields, arrays = [], {k: {} for k in ("raw", "isp")}
        for fid in engine.FIELDS:
            print("Fresh matched DB transport and holdout: " + fid, flush=True)
            photons, _ = engine.charts._scene(
                "color", field_map[fid]["geometry"], prepared["spectral"]
            )
            rates = {
                "observation": engine.optics.apply_field(solution, fid, photons, pixel_qe=truth_qe)[
                    "mean_electron_rate"
                ],
                "before": engine.optics.apply_field(
                    solution,
                    fid,
                    photons,
                    angular_response=lambda theta, phi, wave: (
                        engine.sensor.absolute_angular_response(prepared["pixel"], theta, phi, wave)
                    ),
                )["mean_electron_rate"],
                "after": engine.optics.apply_field(solution, fid, photons, pixel_qe=after_qe)[
                    "mean_electron_rate"
                ],
            }
            rates["replicate"] = rates["observation"]
            series = {}
            for role in ROLES:
                folder = root / fid / role
                folder.mkdir(parents=True)
                np.save(folder / "mean-electron-rate.npy", rates[role], allow_pickle=False)
                frames, records, linears = [], [], []
                for i, acquisition_seed in enumerate(protocol["acquisitions"][fid][role]):
                    if role in ("observation", "replicate"):
                        raw = reference_raw(
                            rates[role],
                            readout,
                            prepared["spec"]["exposure_ms"] / 1000,
                            acquisition_seed,
                        )
                    else:
                        raw = engine.charts._capture(
                            rates[role],
                            prepared["electrical_model"],
                            prepared["spec"],
                            acquisition_seed,
                        )["arrays"]["raw_dn"]
                    path = folder / f"raw-{i:02d}.npy"
                    np.save(path, raw, allow_pickle=False)
                    records.append(
                        {
                            "path": path.relative_to(root).as_posix(),
                            "seed": acquisition_seed,
                            "sha256": sha(path.read_bytes()),
                        }
                    )
                    frames.append(raw)
                    linear, _, _ = reference_isp.process(raw, readout, matrices[role], tuning)
                    linears.append(linear)
                stack, linear = np.stack(frames), np.mean(linears, axis=0)
                raw_mean = stack.astype(float).mean(axis=0)
                np.save(folder / "mean-raw.npy", raw_mean, allow_pickle=False)
                np.save(folder / "mean-linear.npy", linear, allow_pickle=False)
                engine._save_image(folder / "raw.png", (raw_mean - readout["black_level_dn"]) / 512)
                engine._save_image(folder / "isp.png", np.clip(linear, 0, 1) ** (1 / 2.2))
                series[role] = {
                    "raw_frames": records,
                    "raw_patch_rgb_dn": engine._raw_metrics(stack, 4095)["patch_rgb_dn"],
                    "isp_patch_rgb": engine._patches(linear),
                    "isp_matrix": matrices[role].tolist(),
                    "images": {
                        k: (folder / (k + ".png")).relative_to(root).as_posix()
                        for k in ("raw", "isp")
                    },
                    "saturated_fraction": float(np.mean((stack == 0) | (stack == 4095))),
                }
                if role == "after":
                    qe_linears = [
                        reference_isp.process(raw, readout, matrices["qe_only"], tuning)[0]
                        for raw in frames
                    ]
                    series["qe_only"] = {
                        "raw_frames": records,
                        "raw_patch_rgb_dn": series[role]["raw_patch_rgb_dn"],
                        "isp_patch_rgb": engine._patches(np.mean(qe_linears, axis=0)),
                        "isp_matrix": matrices["qe_only"].tolist(),
                    }
            metrics, plots = {}, {}
            for domain, key, unit in (
                ("raw", "raw_patch_rgb_dn", "DN"),
                ("isp", "isp_patch_rgb", "linear RGB"),
            ):
                metrics[domain] = {
                    r: errors(series[r][key], series["observation"][key])
                    for r in ("before", "after", "qe_only", "replicate")
                }
                metrics[domain]["repeat"] = metrics[domain].pop("replicate")
                path = root / fid / (domain + "-patch-errors.png")
                _plot(
                    path,
                    metrics[domain]["before"]["patch_rmse"],
                    metrics[domain]["after"]["patch_rmse"],
                    metrics[domain]["repeat"]["patch_rmse"],
                    unit,
                )
                plots[domain] = path.relative_to(root).as_posix()
                for role in ("observation", "before", "after", "qe_only", "replicate"):
                    arrays[domain].setdefault(role, []).extend(series[role][key])
            fields.append(
                {
                    "field_id": fid,
                    "geometry": field_map[fid]["geometry"],
                    "series": series,
                    "metrics": metrics,
                    "plots": plots,
                    "ccm_raw_identical": series["qe_only"]["raw_frames"]
                    == series["after"]["raw_frames"],
                }
            )
        aggregate = {}
        for domain in ("raw", "isp"):
            aggregate[domain] = {
                role: errors(arrays[domain][role], arrays[domain]["observation"])
                for role in ("before", "after", "qe_only", "replicate")
            }
            aggregate[domain]["reduction_percent"] = reduction(
                aggregate[domain]["before"]["rmse"], aggregate[domain]["after"]["rmse"]
            )
        result = {
            "schema": "camera_db_macbeth_synthetic_holdout_v1",
            "id": "db-holdout-" + digest(protocol)[:32],
            "created_at": now(),
            "source_kind": "synthetic",
            "physical_validation": "not_established",
            "protocol": protocol,
            "source": {
                "replay_id": parent["id"],
                "model_id": model["id"],
                "profile_sha256": model["profile_sha256"],
                "source_identity": prepared["source_identity"],
                "spectral_sha256": prepared["spectral_sha256"],
                "source_files": model["source_files"],
                "calibration_created_at": color["created_at"],
                "code": environment_fingerprint(),
                "analysis_code_sha256": sha(Path(__file__).read_bytes()),
            },
            "fields": fields,
            "aggregate": aggregate,
            "new_raw_frames": 64,
            "refit_performed": False,
            "status": "synthetic_holdout_completed",
        }
    result["files"] = {
        p.relative_to(root).as_posix(): sha(p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
    (root / "report.html").write_bytes(render_report(root, result))
    result["files"]["report.html"] = sha((root / "report.html").read_bytes())
    save_json(root / "report.json", result)
    manifest = {
        p.relative_to(root).as_posix(): sha(p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
    save_json(
        root / "manifest.json", {"schema": "camera_holdout_artifact_manifest_v1", "files": manifest}
    )
    with zipfile.ZipFile(root / "evidence.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in [*manifest, "manifest.json"]:
            z.write(root / name, name)
    with zipfile.ZipFile(root / "evidence.zip") as z:
        assert z.testzip() is None
        assert all(sha(z.read(name)) == value for name, value in manifest.items())
    verification = {
        "status": "passed",
        "files_verified": len(manifest),
        "raw_frames": 64,
        "no_refit": True,
        "disjoint_acquisition_seeds": True,
        "ccm_raw_identical": all(f["ccm_raw_identical"] for f in fields),
        "zip_sha256": sha((root / "evidence.zip").read_bytes()),
    }
    save_json(root / "verification.json", verification)
    print(
        json.dumps(
            {
                "id": result["id"],
                "status": result["status"],
                "report": str(root / "report.html"),
                "metrics": {
                    domain: {
                        "before_rmse": aggregate[domain]["before"]["rmse"],
                        "after_rmse": aggregate[domain]["after"]["rmse"],
                        "reduction_percent": aggregate[domain]["reduction_percent"],
                    }
                    for domain in ("raw", "isp")
                },
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--state-dir", default="outputs/camera-platform-virtual-lab-cra-20261011")
    parser.add_argument("--replay", default="run-ea76c588911140edabbe3a2528d7fbdf")
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()
    run(
        args.workspace,
        args.state_dir,
        args.replay,
        args.output,
        args.seed if args.seed is not None else secrets.randbelow(2**31 - 40000),
    )
