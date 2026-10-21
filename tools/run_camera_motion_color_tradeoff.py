"""Compare two frozen prescription candidates with motion and spectral costs.

Reuse archived Factory assets, not new ray tracing. New moving-grating inputs
are integrated over a common-midpoint global box shutter. No rolling shutter,
trap history, automatic camera controls or registered model changes are implied.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import html
import io
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from camera_platform.motion_color_tradeoff import (
    box_shutter_transfer, exposure_averaged_grating, projected_transfer,
    fit_neutral_channel_gains, channel_ratio_error,
)
from camera_platform.constrained_selection import SelectionLimits, summarize_output, violations
from camera_platform.prescription_response import require_same_design_scene
from camera_platform.response_chain import evaluate_response
from run_camera_prescription_comparison import load_cases, reference_photo, reference_raw

PRIOR = ROOT / "reports/camera_platform/constrained-selection-20260926/selection.json"
IDS = ("radial-e4-g1", "clear-e2-g1")


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scenario(identity, split, f, v, phase, bands):
    return dict(scenario_id=identity, split=split, frequency_cpp=f, velocity_px_s=v,
                phase_at_midpoint_cycles=phase, band_factors=bands,
                modulation=.5 if f else 0.)


def scenarios():
    development = [
        scenario("dev-motion-500", "development", .125, 500., .17, [1.,1.]),
        scenario("dev-motion-1000", "development", .125, 1000., .29, [1.,1.]),
        scenario("dev-spectrum-blue", "development", 0., 0., 0., [1.6,.4]),
        scenario("dev-spectrum-red", "development", 0., 0., 0., [.35,1.65]),
    ]
    held = [
        scenario("holdout-f01-v750", "holdout", .1, 750., .37, [.8,1.2]),
        scenario("holdout-f01-vminus1125", "holdout", .1, -1125., .61, [1.3,.7]),
        scenario("holdout-f01-v1250", "holdout", .1, 1250., .43, [.55,1.45]),
        scenario("stress-temporal-zero", "stress", .125, 2000., .11, [1.,1.]),
        scenario("stress-contrast-inversion", "stress", .125, 3000., .19, [1.,1.]),
    ]
    return development, held


def independent_time_average(shape, spec, exposure):
    # Direct Gauss-Legendre time quadrature. Does not call the sinc helper.
    nodes, weights = np.polynomial.legendre.leggauss(64)
    t = nodes * exposure / 2
    x = np.arange(shape[1]) - (shape[1]-1)/2
    phases = (spec["frequency_cpp"] * (x[None,:]-spec["velocity_px_s"]*t[:,None])
              + spec["phase_at_midpoint_cycles"])
    value = (1+spec["modulation"]*np.cos(2*np.pi*phases))
    return np.broadcast_to(np.sum(value * weights[:,None]/2, axis=0), shape)


def rgb_signal(output):
    a, s = output["arrays"], output["summary"]
    black = s["electrical"]["model"]["adc"]["black_level_dn"]
    return [float(np.mean(a["raw_dn"][a["cfa_channel_index"]==c]-black)) for c in range(3)]


def study(directory):
    directory = Path(directory).resolve()
    if ROOT not in directory.parents or directory.exists():
        raise ValueError("Use a NEW output directory inside this checkout")
    prior_hash = sha(PRIOR)
    prior = json.loads(PRIOR.read_text())["decision"]
    candidates = [deepcopy(next(c for c in prior["candidates"] if c["candidate_id"]==i)) for i in IDS]
    if any(not c["feasible"] for c in candidates):
        raise ValueError("The two inputs must be previously feasible fixed candidates")
    cases = load_cases()
    require_same_design_scene([cases["clear"],cases["radial"]])
    development, held = scenarios()
    limits = SelectionLimits(**prior["limits"])
    directory.mkdir(parents=True, exist_ok=False)
    plan = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "prior_selection_sha256": prior_hash, "candidates": candidates,
        "source_identity": {n:cases[n].identity() for n in ("clear","radial")},
        "development": development, "holdout_recipes": held,
        "signal_limits": prior["limits"],
        "minimum_signed_motion_transfer": .8,
        "maximum_post_calibration_channel_ratio_error": .03,
        "calibration": "One new uniform neutral spectrum [1,1]; fixed gains before development and holdout",
        "color_reference": "clear-e2-g1, relative RAW ratios; not absolute color accuracy",
        "input": "Uniform shared radiance derived from archived per-band spatial means, multiplied by a moving sinusoid in the fixed original native window",
        "timing": "Global box shutter centered at common relative time zero; not two different poses or rolling shutter",
        "development_temporal_frequency_envelope_hz": [0,125],
        "prior_holdouts": "Not reused; earlier results are development context, not fresh confirmation",
        "implementation_sha256": {str(p.relative_to(ROOT)):sha(p) for p in [Path(__file__),
            ROOT/"src/camera_platform/motion_color_tradeoff.py",ROOT/"src/camera_platform/response_chain.py",
            ROOT/"tools/run_camera_prescription_comparison.py"]},
        "limits_note": "Authored research brief; not customer specifications or identified hardware support",
    }
    write_json(directory/"plan.json",plan)
    result = dict(status="in_progress", plan=plan, acquisitions=[], development=[], holdout=[],
                  new_captures=0, new_factory_jobs=0, reused_factory_jobs=4,
                  physical_validation="not_established")
    cache = {}
    try:
        def acquire(candidate, spec):
            # Cache only identical actual physics inputs, never independent reference answers.
            cache_key = (candidate["candidate_id"],tuple(spec["band_factors"]),spec["frequency_cpp"],
                         spec["velocity_px_s"],spec["phase_at_midpoint_cycles"],spec["modulation"])
            if cache_key in cache:
                return cache[cache_key]
            case = cases[candidate["stop"]]
            req = case.rate_request()
            base = np.asarray(req["scene"]["spectral_irradiance"],dtype=float)
            exposure = candidate["exposure_ms"] / 1000
            mean_field = np.broadcast_to(base.mean(axis=(0,1)), base.shape).copy()
            mean_field *= np.asarray(spec["band_factors"])
            grating = exposure_averaged_grating(base.shape[:2],frequency_cpp=spec["frequency_cpp"],
                velocity_px_s=spec["velocity_px_s"],exposure_s=exposure,
                modulation=spec["modulation"],phase_at_midpoint_cycles=spec["phase_at_midpoint_cycles"])
            quad = independent_time_average(base.shape[:2],spec,exposure)
            time_error = float(np.max(np.abs(grating-quad)))
            if time_error > 1e-10:
                raise AssertionError("Independent shutter integral disagrees")
            req["scene"]["spectral_irradiance"] = mean_field*grating[...,None]
            req["scene"]["source"] = {"kind":"estimated","description":
                "New global-shutter, common-midpoint moving-grating research input: "+spec["scenario_id"]+
                "; original per-band E/L exactly once; fixed dark exterior; not 3D rendering"}
            output = evaluate_response(req,case.electrical_model,exposure_s=exposure,
                                       analog_gain=candidate["gain"],noise_enabled=False,seed=2901)
            ref,_ = reference_photo(req,exposure)
            expected = reference_raw(ref,case.electrical_model,exposure,candidate["gain"])
            a = output["arrays"]
            photo_error = float(np.max(np.abs(a["mean_photocharge_e"]-ref))/max(float(ref.max()),1e-20))
            raw_error = int(np.max(np.abs(a["raw_dn"]-expected)))
            if photo_error > 2e-10 or raw_error != 0:
                raise AssertionError("Independent photocharge/ADC mismatch")
            label = f"acquisition-{result['new_captures']:03d}-{candidate['candidate_id']}"
            selected = {k:a[k] for k in ["raw_dn","mean_photocharge_e","well_saturated_mask",
                "adc_high_mask","cfa_channel_index","incident_spectral_irradiance"]}
            np.savez_compressed(directory/(label+".npz"),**selected)
            write_json(directory/(label+"-summary.json"),output["summary"])
            row = {"candidate_id":candidate["candidate_id"],"scenario":deepcopy(spec),
                   "exposure_ms":candidate["exposure_ms"],"gain":candidate["gain"],
                   "arrays_file":label+".npz", "raw_sha256":hashlib.sha256(a["raw_dn"].tobytes()).hexdigest(),
                   "photo_relative_error":photo_error,"independent_raw_difference_dn":raw_error,
                   "time_integral_maximum_error":time_error,**summarize_output(output)}
            result["new_captures"] += 1
            result["acquisitions"].append(row)
            cache[cache_key] = output,row
            return output,row

        # Calibration is constructed and frozen before any evaluation spectrum.
        neutral = scenario("neutral-calibration","calibration",0.,0.,0.,[1.,1.])
        calibration = {c["candidate_id"]:acquire(c,neutral) for c in candidates}
        reference_rgb = rgb_signal(calibration["clear-e2-g1"][0])
        wb = {i:fit_neutral_channel_gains(rgb_signal(o),reference_rgb).tolist()
              for i,(o,r) in calibration.items()}
        write_json(directory/"frozen-neutral-gains.json",{
            "gains_rgb":wb,"calibration_acquisitions":{i:r["arrays_file"] for i,(o,r) in calibration.items()},
            "reference_candidate_id":"clear-e2-g1","retuned_on_evaluation":False})
        wb_hash = sha(directory/"frozen-neutral-gains.json")

        def evaluate_scenario(spec):
            current = {c["candidate_id"]:acquire(c,spec) for c in candidates}
            reference = rgb_signal(current["clear-e2-g1"][0])
            rows = []
            for c in candidates:
                output, base_row = current[c["candidate_id"]]
                row = {**base_row,"scenario_id":spec["scenario_id"],"split":spec["split"]}
                row["color_before"] = channel_ratio_error(rgb_signal(output),reference)
                row["color_after"] = channel_ratio_error(rgb_signal(output),reference,wb[c["candidate_id"]])
                failed = violations(row,limits)
                if row["color_after"]["maximum_absolute_relative_error"] > .03:
                    failed.append("RAW_channel_ratio_consistency")
                if spec["modulation"]:
                    flat_spec = {**spec,"scenario_id":spec["scenario_id"]+"-flat", "modulation":0.,
                                 "frequency_cpp":0.,"velocity_px_s":0.,"phase_at_midpoint_cycles":0.}
                    static_spec = {**spec,"scenario_id":spec["scenario_id"]+"-static","velocity_px_s":0.}
                    flat,_ = acquire(c,flat_spec)
                    static,_ = acquire(c,static_spec)
                    a,b,d = output["arrays"],flat["arrays"],static["arrays"]
                    mask = a["cfa_channel_index"]==1
                    charge = projected_transfer(a["mean_photocharge_e"],b["mean_photocharge_e"],d["mean_photocharge_e"],mask)
                    ideal = box_shutter_transfer(spec["frequency_cpp"],spec["velocity_px_s"],c["exposure_ms"]/1000)
                    if abs(charge["signed_transfer"]-ideal) > 1e-8 or charge["template_relative_residual"] > 1e-8:
                        raise AssertionError("Motion transfer differs after field-PSF/charge transport")
                    white = 2**output["summary"]["electrical"]["model"]["adc"]["bit_depth"]-1
                    clipped = any(np.any(x["well_saturated_mask"][mask]) or
                                  np.any(x["raw_dn"][mask]==white) or np.any(x["raw_dn"][mask]==0)
                                  for x in (a,b,d))
                    row["motion"] = {"expected_signed_transfer":ideal,"charge":charge,
                        "raw":None if clipped else projected_transfer(a["raw_dn"],b["raw_dn"],d["raw_dn"],mask),
                        "motion_path_px":abs(spec["velocity_px_s"])*c["exposure_ms"]/1000,
                        "raw_contrast_unscorable_due_clipping":bool(clipped)}
                    if clipped or charge["signed_transfer"] < .8:
                        failed.append("dynamic_contrast_retention")
                else:
                    row["motion"] = None
                row["failures"] = failed
                rows.append(row)
                print(spec["scenario_id"],c["candidate_id"],
                      "motion",None if row["motion"] is None else round(row["motion"]["charge"]["signed_transfer"],6),
                      "color",round(100*row["color_after"]["maximum_absolute_relative_error"],3),
                      "failures",failed,flush=True)
            return rows

        for spec in development:
            result["development"].extend(evaluate_scenario(spec))
        feasible = [c for c in candidates if all(not r["failures"] for r in result["development"]
                                               if r["candidate_id"]==c["candidate_id"])]
        feasible.sort(key=lambda c:(-c["worst_green_snr_p10"],c["exposure_ms"],c["candidate_id"]))
        choice = feasible[0]["candidate_id"] if feasible else None
        frozen = {"selected_candidate_id":choice,"feasible_candidates":[c["candidate_id"] for c in feasible],
            "selection_basis":"additional_development_motion_color_gates_then_prior_training_SNR_rank",
            "original_selection_unchanged":True,"calibration_sha256":wb_hash,
            "frozen_at_utc":datetime.now(timezone.utc).isoformat(),"holdout_seen":False,
            "automatic_approval":False}
        write_json(directory/"conditional-choice.json",frozen)
        choice_hash = sha(directory/"conditional-choice.json")
        result["conditional_choice"] = frozen
        for spec in held:
            result["holdout"].extend(evaluate_scenario(spec))
        if sha(PRIOR)!=prior_hash or sha(directory/"conditional-choice.json")!=choice_hash or sha(directory/"frozen-neutral-gains.json")!=wb_hash:
            raise AssertionError("A frozen choice/calibration or previous evidence changed")
        for c in cases.values():
            c.assert_unchanged()
        result.update(status="completed",prior_selection_unchanged=True,
            conditional_choice_sha256=choice_hash,calibration_sha256=wb_hash,
            no_reselection_after_holdout=True)
        write_json(directory/"results.json",result)
        render(directory,result)
        print("RESULT",directory/"report.md","conditional_choice",choice,flush=True)
    except Exception as error:
        result.update(status="failed",error=type(error).__name__+": "+str(error))
        write_json(directory/"failed.json",result)
        raise


def render(directory,result):
    from PIL import Image
    lines=["# 두 처방 후보: 움직임·RAW 색비 비용과 별도 입력 평가", "",
        "기존 radial 4ms/gain1과 clear 2ms/gain1을 변경하지 않고 비교했다.",
        "공통 노출 중심시각, global box shutter, 고정 창 안에서 움직이는 합성 정현파. 3D/rolling shutter가 아니다.",
        "추가 연구 기준: signed motion transfer >=80%, 고정 neutral 보정 후 RAW R/G·B/G 상대 차이 <=3%.",
        "기존 신호·SNR·포화·노출 기준도 유지했다. 실제 고객 사양이나 색 정확도 기준은 아니다.",
        "clear 2ms가 색비 기준이므로 그 후보의 색비 잔차는 정의상 0이다. 절대 색 정확도 우수성을 뜻하지 않는다.", "",
        "## 개발 입력", "", "| 입력 | 후보 | Motion transfer | 고정 보정 후 색비 최대 차이 | 판정 |",
        "|---|---|---:|---:|---|"]
    def add(row):
        motion = f"{100*row['motion']['charge']['signed_transfer']:.3f}%" if row['motion'] else '해당 없음'
        return f"| {row['scenario_id']} | {row['candidate_id']} | {motion} | {100*row['color_after']['maximum_absolute_relative_error']:.3f}% | {', '.join(row['failures']) or '통과'} |"
    lines += [add(r) for r in result["development"]]
    lines += ["", "조건 추가 후 고정 후보: **"+str(result["conditional_choice"]["selected_candidate_id"])+"**.",
        "이 선택은 새로운 연구 조건을 추가한 별도 결정이다. 과거 signal-only 선택 기록은 변경하지 않았다.",
        "선택과 neutral 보정은 아래 평가 전 고정했으며, 실패를 보고 후보나 문턱값을 바꾸지 않았다.", "",
        "## 새로운 미사용 입력 및 속도 스트레스", "",
        "| 입력 | 후보 | Motion transfer | 고정 보정 후 색비 최대 차이 | 판정 |", "|---|---|---:|---:|---|"]
    lines += [add(r) for r in result["holdout"]]
    lines += ["", "## 범위", "",
        "Motion transfer는 같은 후보의 정적 대비에 대한 상대 값이다. 전체 렌즈 MTF나 객체 검출 성능이 아니다.",
        "상수 입력의 경계/PSF envelope를 제거한 뒤 동일 후보의 정적 template에 투영했다. 부호를 보존하여 반전도 드러낸다.",
        "균일 neutral 한 입력에서만 R/G/B multiplier를 정했다. 보정된 픽셀·RAW를 원본 대신 저장하거나 ISP에 적용하지 않았다.",
        "RAW 채널비는 500/600nm 두 노드 모델의 reference-camera 일관성이다. Delta-E·실물 색 정확도·일반 분광 성능이 아니다.",
        "광전하는 offline 진단에만 사용했다. 원본을 처리하는 알고리즘이나 측정값으로 가장하지 않았다.",
        "공간/각도 응답은 기존 분리 근사, source-cell은 중심 표본, 바깥 장면은 어둡다는 원래 경계다.",
        "공통 중심시각 비교라 추가 모션 위치 차이는 없다. 같은 시작 trigger의 다른 노출 종료 지연은 별도 문제다.",
        "현재 계산은 잡음 OFF다. 실측, 시간 연속 인지, 임의 파형/가림, rolling shutter, trap, 실시간 HW, 대형 해상도는 미평가다.",
        f"새 response-chain 취득 {result['new_captures']}건, 새 Factory 0건. 원본 등록 모델·DB·설치본·서버는 변경하지 않았다.", ""]
    (directory/"report.md").write_text("\n".join(lines),encoding="utf-8")
    cards=[]
    for row in result["development"]+result["holdout"]:
        if row["motion"] is None:
            continue
        with np.load(directory/row["arrays_file"],allow_pickle=False) as saved:
            pixels=np.uint8(np.rint(np.clip((saved["raw_dn"].astype(float)-64)/(4095-64),0,1)*255))
        stream=io.BytesIO()
        Image.fromarray(pixels).resize((240,240),Image.Resampling.NEAREST).save(stream,format="PNG")
        title=row["scenario_id"]+" / "+row["candidate_id"]
        cards.append("<figure><img src='data:image/png;base64,"+base64.b64encode(stream.getvalue()).decode()+
                     "' alt='"+html.escape(title)+"'><figcaption>"+html.escape(title)+"</figcaption></figure>")
    (directory/"comparison.html").write_text("<!doctype html><html lang='ko'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'><title>CameraE2E motion/color</title>"
        "<style>body{max-width:1200px;margin:auto;padding:24px;font:16px/1.6 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        ".grid{display:flex;flex-wrap:wrap;gap:16px}figure{margin:0}img{max-width:100%;image-rendering:pixelated}</style>"
        "<h1>노출·움직임·채널비 비용</h1><p>RAW 표시는 모든 후보에서 64–4095 DN으로 동일합니다.</p>"
        "<div class='grid'>"+"".join(cards)+"</div><pre>"+html.escape("\n".join(lines))+"</pre></html>",encoding="utf-8")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",required=True,type=Path)
    args=parser.parse_args()
    study(args.output)


if __name__=="__main__":
    main()
