"""Bounded sequence viewer and event decision report; no eager native image load."""

# HTML/JavaScript template lines intentionally preserve browser source.
# ruff: noqa: E501
import html
import json
from pathlib import Path


def sequence_report(output, spec, result):
    output = Path(output)
    # The browser loads only the selected arm/frame, not thousands of PNGs.
    (
        output / "report.html"
    ).write_text("""<!doctype html><meta charset="utf-8"><title>도로 HDR 시퀀스</title>
<style>body{background:#101827;color:#e8eef7;font:16px system-ui;margin:24px}select,input{font:inherit;margin:8px}main{display:flex;flex-wrap:wrap;gap:16px}figure{margin:0;width:640px}canvas{width:100%}a{color:#8bc8ff}pre{white-space:pre-wrap}</style>
<h1>도로 HDR 시퀀스 — 원본 timestamp와 결과 사용 시각</h1><p>RGB spectral proxy. 무효 지원 영역은 검게 표시됩니다. HDR과 단일 노출은 서로 다른 취득·출력 주기를 유지합니다.</p>
<label>사건<select id="event"></select></label><label>광량<select id="condition"></select></label><label>Seed<select id="seed"></select></label><input type="range" id="frame" min="0" value="0"><main id="views"></main><pre id="time"></pre><script src="sequence-viewer.js"></script>""")
    (output / "sequence-viewer.js").write_text(""""use strict";
(async()=>{const r=await fetch('result.json').then(x=>x.json()),views=document.querySelector('#views'),slider=document.querySelector('#frame');
const options=(id,values)=>{const s=document.querySelector('#'+id);for(const value of [...new Set(values)]){const o=document.createElement('option');o.value=value;o.textContent=value;s.append(o);}return s;};
const event=options('event',r.outputs.map(o=>o.event_id)),condition=options('condition',r.outputs.map(o=>o.condition)),seed=options('seed',r.outputs.map(o=>o.seed));
let revision=0;
function draw(){const epoch=++revision;const subset=r.outputs.filter(o=>o.event_id===event.value&&o.condition===condition.value&&String(o.seed)===seed.value),times=[...new Set(subset.map(o=>o.timing.result_available_ns/1e9))].sort((a,b)=>a-b);slider.max=Math.max(0,times.length-1);slider.value=Math.min(Number(slider.value),Number(slider.max));const t=times[Number(slider.value)];views.replaceChildren();
for(const arm of [...new Set(subset.map(o=>o.arm))]){const previous=subset.filter(o=>o.arm===arm&&o.timing.result_available_ns/1e9<=t).sort((a,b)=>b.timing.result_available_ns-a.timing.result_available_ns)[0];if(!previous)continue;const f=document.createElement('figure'),caption=document.createElement('figcaption'),image=document.createElement('img');image.width=640;image.height=360;image.src=previous.path+'/rgb.png';image.loading='lazy';caption.textContent=arm+' capture '+previous.timestamp_s+' · available '+previous.timing.result_available_ns+' ns';f.append(image,caption);views.append(f);}document.querySelector('#time').textContent=JSON.stringify({result_available_s:t,display:'latest available output at or before cursor; acquisition cadence and delay are preserved'},null,2);}
for(const s of [event,condition,seed,slider])s.oninput=draw;draw();})();""")


def decision_report(package, decision):
    package = Path(package)
    rows = []
    sequences = []
    for record in decision["records"]:
        margin = record["braking_margin_interval_m"]
        rows.append(
            "<tr>"
            + "".join(
                "<td>" + html.escape(str(x)) + "</td>"
                for x in (
                    record["split"],
                    record["event_id"],
                    record["arm"],
                    record["condition"],
                    record["seed"],
                    record["status"],
                    record["recognition_time_interval_s"],
                    margin,
                    len(record["confirmed_false_track_ids"]),
                    len(record["uncertain_track_ids"]),
                )
            )
            + "</tr>"
        )
        relative = (
            "evaluation/"
            + record["split"]
            + "/"
            + record["event_id"]
            + "/"
            + record["arm"]
            + "-"
            + record["condition"]
            + "-"
            + str(record["seed"])
            + ".json"
        )
        sequences.append(
            {
                "label": " / ".join(
                    str(record[k]) for k in ("split", "event_id", "arm", "condition", "seed")
                ),
                "url": relative,
            }
        )
    (package / "decision-sequences.json").write_text(json.dumps(sequences, ensure_ascii=False))
    (package / "decision-report.html").write_text(
        """<!doctype html><meta charset="utf-8"><title>HDR 사건별 설계 판단</title>
<style>body{font:15px system-ui;background:#101827;color:white;margin:24px}td,th{padding:7px;border:1px solid #789}canvas{width:640px;height:360px}a{color:#8bc8ff}</style>
<h1>"""
        + html.escape(decision["title"])
        + """</h1><p>결론: """
        + html.escape(decision["status"])
        + """ · 기준: """
        + html.escape(decision["baseline"])
        + """ · 선택: """
        + html.escape(decision["selected"])
        + """</p>
<p>선정 lock 이후 별도 평가. 놓친 사건은 분모에 남고 seed는 독립 사건이 아닙니다. 실제 충돌 회피·안전 인증을 뜻하지 않습니다.</p><a href="decision.json">결정·실패·지연 민감도 원본</a>
<p><select id="sequence"></select><input type="range" id="frame" min="0" value="0"></p><canvas id="canvas" width="640" height="360"></canvas><pre id="time"></pre>
<table><tr><th>분할</th><th>사건</th><th>방식</th><th>광량</th><th>Seed</th><th>판정</th><th>인지 시각 구간</th><th>제동 여유 구간</th><th>오검출</th><th>미결정 track</th></tr>"""
        + "".join(rows)
        + """</table><script src="decision-viewer.js"></script>"""
    )
    (package / "decision-viewer.js").write_text(""""use strict";
(async()=>{const list=await fetch('decision-sequences.json').then(r=>r.json()),select=document.querySelector('#sequence'),slider=document.querySelector('#frame'),canvas=document.querySelector('#canvas'),ctx=canvas.getContext('2d');let frames=[],epoch=0;
for(const s of list){const o=document.createElement('option');o.textContent=s.label;o.value=s.url;select.append(o);}
async function load(){const current=++epoch;const r=await fetch(select.value).then(x=>x.json());if(current!==epoch)return;frames=r.frames;slider.max=Math.max(0,frames.length-1);slider.value=0;draw();}
function draw(){const current=++epoch,f=frames[Number(slider.value)];if(!f)return;const image=new Image();image.onload=()=>{if(current!==epoch)return;ctx.drawImage(image,0,0);for(const t of f.tracks){ctx.strokeStyle=t.stable?'#5cff95':'#ffd46b';ctx.lineWidth=2;ctx.strokeRect(t.bbox[0],t.bbox[1],t.bbox[2]-t.bbox[0],t.bbox[3]-t.bbox[1]);ctx.fillStyle=ctx.strokeStyle;ctx.fillText('track '+t.track_id+' observed '+t.streak,t.bbox[0],t.bbox[1]);}};image.src=f.image_path;document.querySelector('#time').textContent=JSON.stringify({capture_s:f.timestamp_s,result_available_ns:f.result_available_ns,observed_tracks:f.tracks},null,2);}
select.onchange=load;slider.oninput=draw;await load();})();""")
