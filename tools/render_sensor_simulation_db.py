"""Render a local, filterable overview of the current stored sensor simulations.

This reads the DB through public search/get APIs and never loads NPZ arrays or
changes source records. Run only after a build completes for a stable snapshot.
"""

# ruff: noqa: E501
# Long lines belong to the self-contained HTML/CSS/JS template below.

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyisetcam.sensor_simulation_db import (  # noqa: E402
    sensor_simulation_db_get,
    sensor_simulation_db_search,
    sensor_simulation_db_summary,
)


def _source_links(value):
    """Use only local source paths actually recorded in the saved metadata."""
    result = {}

    def visit(item):
        if isinstance(item, dict):
            page = item.get("pdf_page_1_based", item.get("pdf_page_1based"))
            for key, val in item.items():
                if isinstance(val, str) and key in {
                    "path",
                    "source_path",
                    "source_file",
                    "pdf_path",
                    "optical_model_path",
                    "catalog_path",
                }:
                    path = Path(val)
                    if (
                        path.is_absolute()
                        and path.is_file()
                        and path.suffix.lower()
                        in {
                            ".pdf",
                            ".html",
                            ".json",
                        }
                    ):
                        result.setdefault(str(path), {"path": str(path), "page": page})
                visit(val)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return sorted(
        result.values(), key=lambda item: (not item["path"].endswith(".pdf"), item["path"])
    )


def _inventory_row(entry, document):
    m = document["metadata"]
    model = m.get("model_snapshot", {})
    settings = m.get("simulation_settings", {})
    profile = m.get("readout_profile", {})
    readout = profile.get("readout", {})
    classification = profile.get("provenance", {}).get("classification")
    case = next(
        (
            c
            for c in m.get("cases", [])
            if c.get("illuminant") == "D65" and abs(c.get("exposure_s", 0) - 0.004) < 1e-12
        ),
        None,
    )
    peaks = []
    if model.get("eqe"):
        qe = np.asarray(model["eqe"], float)
        wave = np.asarray(model["wavelength_nm"], float)
        for index, channel in enumerate(model["filter_names"]):
            peak_index = int(np.argmax(qe[:, index]))
            peaks.append(
                {
                    "channel": channel,
                    "eqe": float(qe[peak_index, index]),
                    "wavelength_nm": float(wave[peak_index]),
                }
            )
    path = Path(entry["result_path"])
    sources = _source_links({"model": model, "policy": m.get("source_scope_policy", {})})
    return {
        "report_code": entry["report_code"],
        "sensor_id": entry["sensor_id"],
        "device_name": entry["device_name"],
        "manufacturer": entry["manufacturer"],
        "model": entry["model"],
        "status": entry["status"],
        "reason": entry.get("reason"),
        "run_id": entry["run_id"],
        "result_sha256": entry["result_sha256"],
        "input_digest": document.get("input_digest"),
        "measured_accuracy_verified": False,
        "sample_count": m.get("sample_count", 0),
        "normal_eqe_peaks": peaks,
        "pixel_pitch_um": model.get("pixel_pitch_um"),
        "native_cfa": m.get("native_cfa", {}),
        "d65_f4_4ms": None
        if case is None
        else {
            "mean_photoelectrons": case["mean_photoelectrons"],
            "temporal_std_dn": case["temporal_std_dn"],
            "temporal_snr_db": case["temporal_snr_db"],
            "snr_eligible": case["snr_eligible"],
            "saturation": case["saturation"],
            "sites": case["sites"],
            "nframes": case["nframes"],
            "fnumber": settings.get("fnumber", 4.0),
            "luminance_cd_m2": settings.get("luminance_cd_m2"),
            "size": settings.get("size"),
        },
        "readout_classification": classification,
        "readout_profile": profile.get("profile"),
        "operating_point": profile.get("operating_point"),
        "readout": readout,
        "readout_fixed_pattern": profile.get("fixed_pattern", {}),
        "readout_conditions": profile.get("provenance", {}).get("conditions", {}),
        "readout_fields": profile.get("provenance", {}).get("fields", {}),
        "capabilities": entry.get("capabilities", {}),
        "source_scope_policy": m.get("source_scope_policy", {}),
        "source_links": sources,
        "assumptions": model.get("optical_assumptions", model.get("assumptions", {})),
        "collection_efficiency": model.get("collection_efficiency"),
        "caveats": m.get("caveats", []),
        "checks": m.get("checks", {}),
        "result_path": str(path),
        "arrays_path": str(path.with_name("arrays.npz")) if document.get("arrays_sha256") else None,
    }


def _csv_rows(rows):
    for row in rows:
        baseline = row["d65_f4_4ms"] or {}
        yield {
            **{
                k: row[k]
                for k in (
                    "report_code",
                    "sensor_id",
                    "device_name",
                    "manufacturer",
                    "status",
                    "model",
                    "pixel_pitch_um",
                    "sample_count",
                    "readout_classification",
                    "readout_profile",
                    "operating_point",
                    "measured_accuracy_verified",
                    "reason",
                    "run_id",
                    "result_sha256",
                    "input_digest",
                    "result_path",
                    "arrays_path",
                )
            },
            "normal_eqe_peaks": json.dumps(row["normal_eqe_peaks"], ensure_ascii=False),
            "D65_F4_4ms_mean_photoelectrons": baseline.get("mean_photoelectrons"),
            "D65_F4_4ms_raw_snr_db": baseline.get("temporal_snr_db"),
            "D65_F4_4ms_temporal_std_dn": baseline.get("temporal_std_dn"),
            "D65_F4_4ms_snr_eligible": baseline.get("snr_eligible"),
            "full_well_e": row["readout"].get("full_well_e"),
            "pre_adc_read_noise_e": row["readout"].get("read_noise_e"),
            "source_links": json.dumps(row["source_links"], ensure_ascii=False),
        }


def _page(rows, summary):
    encoded = json.dumps(rows, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    counts = summary["counts"]
    cards = [
        ("보고서 기록", counts["total"]),
        ("시뮬레이션 완료", counts["simulated"]),
        ("물리 추정", counts["by_model"].get("physical_estimate_v1", 0)),
        ("DB 추정", counts["by_model"].get("db_estimate_v1", 0)),
        ("RAW 프레임", summary["sample_count"]),
    ]
    tiles = "".join(
        f'<div class="card"><span>{html.escape(label)}</span><strong>{n:,}</strong></div>'
        for label, n in cards
    )
    substitutions = {
        "__ROWS__": encoded,
        "__TILES__": tiles,
        "__EXCLUDED__": str(counts["excluded"]),
        "__FAILED__": str(counts["failed"] + counts["quarantined"]),
        "__DATE__": html.escape(summary["created_at_utc"][:19].replace("T", " ") + " UTC"),
        "__READOUT__": html.escape(
            " · ".join(
                f"{key}: {value}건"
                for key, value in summary["readout_classification_counts"].items()
            )
        ),
    }
    page = _HTML
    for key, value in substitutions.items():
        page = page.replace(key, value)
    return page


def render_sensor_simulation_db(*, root=None, output_dir=None):
    """Write only overview artifacts; retain each report and pinned run separately."""
    output = Path(
        output_dir or Path(__file__).resolve().parents[1] / "reports/sensor_simulation_db"
    )
    source = sensor_simulation_db_summary(root=root)
    if source["status"] != "available":
        raise FileNotFoundError(source["db_path"])
    entries = sensor_simulation_db_search(root=root)
    rows = [
        _inventory_row(
            entry,
            sensor_simulation_db_get(
                entry["report_code"],
                root=root,
                run_id=entry["run_id"],
                load_arrays=False,
            ),
        )
        for entry in entries
    ]
    for row in rows:
        for name in ("result", "arrays"):
            path = row[f"{name}_path"]
            row[f"{name}_href"] = (
                quote(os.path.relpath(path, output.resolve()), safe="/") if path else None
            )
    counts = Counter(row["status"] for row in rows)
    summary = {
        "schema": "camerae2e_sensor_simulation_overview_v1",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "source_db": source["db_path"],
        "source_db_sha256": source["db_sha256"],
        "counts": {
            "total": len(rows),
            **{s: counts[s] for s in ("simulated", "excluded", "failed", "quarantined")},
            "by_model": dict(Counter(row["model"] for row in rows if row["status"] == "simulated")),
        },
        "sample_count": sum(row["sample_count"] for row in rows),
        "readout_classification_counts": dict(
            Counter(row["readout_classification"] or "not_simulated" for row in rows)
        ),
        "measured_accuracy_verified": False,
        "scope": "Current run per TechInsights report; repeated product reports are not merged.",
        "data_loading": "Public search/get APIs; pinned run IDs; no NPZ array loading.",
        "readout_warning": "Generic assumed electronics and named camera proxies are not bare-sensor measurements.",
        "comparison_warning": "Physical and DB optical-loss semantics differ; numbers do not establish a product ranking.",
        "current_run_ids": {row["report_code"]: row["run_id"] for row in rows},
        "input_digest_counts": dict(Counter(row["input_digest"] for row in rows)),
    }
    if summary["counts"] != source["counts"]:
        raise RuntimeError("DB coverage changed during rendering; retry after the build completes.")
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    with (output / "inventory.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        records = list(_csv_rows(rows))
        if records:
            writer = csv.DictWriter(stream, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
    (output / "index.html").write_text(_page(rows, summary), encoding="utf-8")
    return {
        "output_dir": str(output.resolve()),
        "counts": summary["counts"],
        "sample_count": summary["sample_count"],
    }


_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CameraE2E · Image sensor simulations</title>
<style>
:root{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#202c3b;background:#f3f5f8}*{box-sizing:border-box}body{margin:0}main{max-width:1680px;margin:auto;padding:42px 34px}h1{font-size:34px;letter-spacing:-1px;margin:8px 0}h2{font-size:18px;margin:0 0 12px}.eyebrow{font-size:12px;letter-spacing:2px;font-weight:700;color:#596f89}.intro{color:#53647b;margin:4px 0 24px;max-width:1100px}.notice{background:#fff4db;border:1px solid #efd7a3;border-radius:10px;padding:14px 18px;font-size:14px;color:#674f26}.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin:23px 0}.card{background:white;border:1px solid #e0e5ed;border-radius:12px;padding:17px 21px}.card span{color:#64768b;font-size:13px}.card strong{display:block;font-size:30px;font-variant-numeric:tabular-nums;margin-top:3px}.meta{display:flex;flex-wrap:wrap;gap:18px;font-size:13px;color:#5a6e85;margin:15px 0}.filters{display:flex;flex-wrap:wrap;gap:10px;background:white;padding:17px;border:1px solid #e0e5ed;border-radius:12px 12px 0 0;margin-top:24px}input,select,button{font:inherit;border:1px solid #cdd6e2;background:white;border-radius:7px;padding:9px 12px;color:inherit}input{flex:1;min-width:230px}button{cursor:pointer}button:hover{background:#edf2f8}.counter{margin-left:auto;align-self:center;color:#596f89;font-size:13px}.tablewrap{overflow:auto;max-height:73vh;background:white;border:1px solid #e0e5ed;border-top:0;border-radius:0 0 12px 12px}table{border-collapse:collapse;width:100%;min-width:1320px;font-size:13px}th{position:sticky;top:0;z-index:1;background:#eaf0f6;color:#4b6078;text-align:left;padding:13px 11px;font-size:12px;white-space:nowrap}td{padding:13px 11px;border-bottom:1px solid #edf0f5;vertical-align:top}tr:hover>td{background:#f8faff}td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.product{font-weight:650;font-size:14px;max-width:200px;overflow-wrap:anywhere}.muted{color:#74849a;font-size:12px}.badge{display:inline-block;border-radius:5px;padding:3px 7px;font-size:11px;white-space:nowrap;background:#eef1f5;color:#596b80}.simulated{background:#e4f3eb;color:#256444}.excluded{background:#f5eee4;color:#846034}.physical{background:#e9edfa;color:#485d9b}.assumed{background:#fff2d7;color:#886827}.proxy{background:#e8eff8;color:#435e89}a{color:#315c95;text-decoration:none}a:hover{text-decoration:underline}.peak{white-space:nowrap;font-size:12px}.detail td{background:#f8fafc!important;padding:20px 24px}.detailgrid{display:grid;grid-template-columns:1.2fr 1fr 1.2fr;gap:25px}.detailgrid p{margin:6px 0}.detailgrid pre{white-space:pre-wrap;word-break:break-word;font-size:11px;max-height:240px;overflow:auto;background:#edf1f6;padding:12px;border-radius:6px}.sources a{display:block;overflow-wrap:anywhere;font-size:12px;margin:5px 0}.reason{color:#855c2f;max-width:1100px}.notes{margin:22px 0;font-size:13px;color:#5a6e85;max-width:1300px}.notes p{margin:7px 0}footer{font-size:12px;color:#8090a3;margin-top:24px} .empty{padding:45px;text-align:center;color:#73849a}
@media(max-width:900px){main{padding:25px 16px}.cards{grid-template-columns:repeat(2,1fr)}.detailgrid{grid-template-columns:1fr}.counter{width:100%;margin-left:0}h1{font-size:28px}}
</style></head><body><main>
<div class="eyebrow">CAMERAE2E / STORED SIMULATIONS</div>
<h1>이미지센서 시뮬레이션 DB</h1>
<p class="intro">실제 실행한 센서 응답과 RAW 결과를 보고서 단위로 조회합니다. 같은 제품의 DEF·DEP 보고서는 각각 보존하며, 이 기록 수는 서로 다른 제품 수와 같지 않습니다.</p>
<div class="notice"><strong>모든 결과는 추정치입니다. 측정 정확도는 검증되지 않았습니다.</strong> 공통 CFA 광학 물성과 수집 효율 가정을 사용합니다. 대부분의 읽기 계통은 가정값이며 일부만 공개 카메라 자료 기반 proxy입니다.</div>
<section class="cards">__TILES__</section>
<div class="meta"><span>제외 __EXCLUDED__건 · 실패/격리 __FAILED__건</span><span>__READOUT__</span><span><a href="inventory.csv">CSV 다운로드</a> · <a href="summary.json">요약 JSON</a></span></div>
<div class="filters"><input id="query" type="search" aria-label="센서 또는 보고서 검색" placeholder="센서명, 제조사, 보고서 코드 검색">
<select id="manufacturer" aria-label="제조사"><option value="">전체 제조사</option></select>
<select id="status" aria-label="상태"><option value="">전체 상태</option><option value="simulated">완료</option><option value="excluded">제외</option><option value="failed">실패</option><option value="quarantined">격리</option></select>
<select id="model" aria-label="모델"><option value="">전체 모델</option><option value="physical_estimate_v1">물리 추정</option><option value="db_estimate_v1">DB 추정</option></select>
<button id="reset">초기화</button><span class="counter" id="counter" aria-live="polite"></span></div>
<div class="tablewrap"><table><thead><tr><th>센서 / 제조사</th><th>보고서</th><th>상태 / 모델</th><th>픽셀 / CFA</th><th>정상입사 EQE 최대 · 채널별</th><th>광전자 e⁻/px</th><th>RAW SNR dB</th><th>읽기 계통</th><th>근거 / 결과</th></tr></thead><tbody id="body"></tbody></table></div>
<div class="notes"><p><strong>표의 RAW 조건:</strong> D65 균일장 100 cd/m² · F/4 · 4 ms · 32×32 native pixels · 8 frames. 전체 실행은 D65/A/F11 × 1/4/16 ms와 dark/bright 스트레스를 포함합니다. EQE 최대는 400–700 nm, 10 nm 간격의 정상입사 계산값입니다.</p><p>물리 추정은 정상입사·각도·pupil 응답을, DB 추정은 native 채널 스펙트럼을 제공합니다. 현재 direction-cosine pupil 정의에서 F/2·CRA 40°는 전체 pupil이 60° 범위를 넘어 미지원으로 남깁니다. 정상입사 최대값과 F/4 pupil RAW 값은 서로 다른 조명 조건입니다.</p><p>광전자 수는 픽셀 면적에도 영향을 받습니다. FWC·읽기잡음 가정, 모델별 광학 손실 정의, 표본 오차가 달라 이 표로 제품 순위를 확정할 수 없습니다. 포화되거나 신호가 없는 경우 SNR을 표시하지 않습니다. 원문 링크는 이 컴퓨터의 저장 파일을 가리킵니다.</p></div>
<footer>생성 __DATE__ · 읽기 전용 DB 조회 · NPZ 배열 재계산 없음</footer>
</main><script>
const rows=__ROWS__;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(v,n=2)=>v===null||v===undefined?'—':Number(v).toLocaleString('en-US',{minimumFractionDigits:n,maximumFractionDigits:n});
const pathurl=p=>p.startsWith('/')?'file://'+p.split('/').map(encodeURIComponent).join('/'):p;
const link=(p,label,page)=>p?`<a href="${esc(pathurl(p)+(page?'#page='+page:''))}" target="_blank" rel="noopener">${esc(label)}</a>`:'';
const state={simulated:'완료',excluded:'제외',failed:'실패',quarantined:'격리'};
const modelname={physical_estimate_v1:'물리 추정',db_estimate_v1:'DB 추정'};
const manufacturer=document.getElementById('manufacturer');
[...new Set(rows.map(r=>r.manufacturer))].sort().forEach(name=>{let o=document.createElement('option');o.value=name;o.textContent=name;manufacturer.appendChild(o)});
function details(r){
 const c=r.d65_f4_4ms,p=r.source_scope_policy,ro=r.readout;
 const raw=c?`<p>전자 ${fmt(c.mean_photoelectrons)} e⁻/px · 시간잡음 ${fmt(c.temporal_std_dn)} DN</p><p>RAW SNR ${fmt(c.temporal_snr_db)} dB · ${c.snr_eligible?'비포화 조건':'SNR 비교 제외'}</p><p>실행 ${r.sample_count} frames · 각도 ${r.capabilities.angular_response?'지원':'미지원'} · Bayer RGB ${r.capabilities.isp?'지원':'미지원'}</p>`:'<p class="reason">'+esc(r.reason||'실행 결과 없음')+'</p>';
 const sources=r.source_links.map(s=>link(s.path,s.path.split('/').pop()+(s.page?' · p'+s.page:''),s.page)).join('')||'<span class="muted">직접 원문 경로 미기록. 저장 JSON의 출처 정보를 확인하세요.</span>';
 return `<tr class="detail"><td colspan="9"><div class="detailgrid"><section><h2>조건과 결과</h2>${raw}<p>모델: ${esc(r.model||'—')}</p><p>출처 정책: ${esc(p.action||'—')} · ${p.source_reviewed?'검토한 예외/범위':'전수 원문 검증 아님'}</p><p class="muted">${esc(p.reason||'')}</p><p class="reason">${esc(r.reason||'')}</p><p class="muted">Run ${esc(r.run_id)}</p></section><section><h2>읽기 계통과 가정</h2><p>${esc(r.readout_profile||'—')} / ${esc(r.operating_point||'—')}</p><p>FWC ${fmt(ro.full_well_e,0)} e⁻ · pre-ADC RN ${fmt(ro.read_noise_e,3)} e⁻</p><p>출처 분류: ${esc(r.readout_classification||'미실행')}</p><p class="muted">조건과 각 항목의 측정/가정 구분은 저장 JSON readout_profile에 보존됩니다.</p><pre>${esc(JSON.stringify({optical:r.assumptions,collection_efficiency:r.collection_efficiency,fixed_pattern:r.readout_fixed_pattern},null,2))}</pre></section><section><h2>근거와 저장 아티팩트</h2><div class="sources">${link(r.result_href,'결과 · 모델 snapshot · 가정 JSON')}${link(r.arrays_href,'계산 배열 NPZ')}${sources}</div><p class="muted">모든 출처는 로컬 파일입니다. 파일 이동 시 저장 경로를 다시 확인하세요.</p></section></div></td></tr>`;
}
function render(){const q=document.getElementById('query').value.toLowerCase();const st=document.getElementById('status').value;const mo=document.getElementById('model').value;const ma=manufacturer.value;
 const selected=rows.filter(r=>(!q||[r.device_name,r.manufacturer,r.report_code,r.sensor_id].join(' ').toLowerCase().includes(q))&&(!st||r.status===st)&&(!mo||r.model===mo)&&(!ma||r.manufacturer===ma));
 document.getElementById('counter').textContent=`${selected.length} / ${rows.length} 기록`;
 document.getElementById('body').innerHTML=selected.map(r=>{const c=r.d65_f4_4ms;const assumed=r.readout_classification==='unmeasured_assumption';const pat=r.native_cfa.pattern;const cfa=pat?`${pat.length}×${pat[0].length} cell`:'';
 const peaks=r.normal_eqe_peaks.map(p=>`<div class="peak">${esc(p.channel)} ${fmt(p.eqe*100,1)}% <span class="muted">${fmt(p.wavelength_nm,0)} nm</span></div>`).join('');
 return `<tr><td><div class="product">${esc(r.device_name)}</div><span class="muted">${esc(r.manufacturer)}</span></td><td>${esc(r.report_code)}</td><td><span class="badge ${esc(r.status)}">${esc(state[r.status])}</span><div class="muted">${esc(modelname[r.model]||'—')}</div></td><td>${fmt(r.pixel_pitch_um,2)} μm<div class="muted">${esc(cfa)}</div></td><td>${peaks||'—'}</td><td class="num">${fmt(c?.mean_photoelectrons)}</td><td class="num">${fmt(c?.snr_eligible?c.temporal_snr_db:null)}</td><td>${r.readout_classification?`<span class="badge ${assumed?'assumed':'proxy'}">${assumed?'가정값':'카메라 proxy'}</span>`:'—'}</td><td><button class="expand" data-code="${esc(r.report_code)}" aria-expanded="false">상세</button></td></tr>`}).join('')||'<tr><td colspan="9" class="empty">일치하는 기록이 없습니다.</td></tr>';
 document.querySelectorAll('.expand').forEach(button=>button.addEventListener('click',()=>{const row=button.closest('tr');const open=button.getAttribute('aria-expanded')==='true';if(open){row.nextElementSibling.remove();button.textContent='상세'}else{row.insertAdjacentHTML('afterend',details(rows.find(r=>r.report_code===button.dataset.code)));button.textContent='접기'}button.setAttribute('aria-expanded',String(!open))}));}
['query','manufacturer','status','model'].forEach(id=>document.getElementById(id).addEventListener('input',render));document.getElementById('reset').addEventListener('click',()=>{['query','manufacturer','status','model'].forEach(id=>document.getElementById(id).value='');render()});render();
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="CameraE2E DB root (not the SQLite file)")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "reports/sensor_simulation_db",
    )
    args = parser.parse_args()
    print(json.dumps(render_sensor_simulation_db(root=args.root, output_dir=args.output), indent=2))


if __name__ == "__main__":
    main()
