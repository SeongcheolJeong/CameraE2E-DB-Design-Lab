"use strict";
(async()=>{
  const data=await (await fetch("viewer.json")).json(), $=id=>document.getElementById(id);
  $("title").textContent=data.title; $("decision").textContent=JSON.stringify(data.decision);
  $("resources").textContent=JSON.stringify(data.resources,null,2);
  if(data.decision.status==="connection_only_not_selection"){
    $("decision-link").href="first-sequence.json";$("decision-link").textContent="연결 진단 원본";$("lock-link").hidden=true;
  }
  const ids=[...new Set(data.sequences.map(s=>s.event_id))], cameras=[...new Set(data.sequences.map(s=>s.candidate_id))];
  for(const id of ids)$("event").add(new Option(id,id));
  for(const id of cameras)$("candidate").add(new Option(id,id));
  $("candidate").value=data.decision.selected_id;
  for(const r of data.records.filter(r=>r.processing_latency_s===0.1)){
    const row=document.createElement("tr");
    for(const val of [r.event_id+" / "+r.seed,r.candidate_id,r.status,JSON.stringify(r.braking_margin_interval_m),r.confirmed_false_track_ids.length+" / "+r.uncertain_track_ids.length]){const td=document.createElement("td");td.textContent=val;row.append(td);}$("records").append(row);
  }
  let generation=0, timer=null;
  function sequences(){const event=$("event").value;return [data.sequences.find(s=>s.event_id===event&&s.candidate_id==="L1-S1"),data.sequences.find(s=>s.event_id===event&&s.candidate_id===$("candidate").value)];}
  function timeline(seq,n){
    const s=seq.find(Boolean);if(!s)return;
    const ctx=$("timeline").getContext("2d"), start=s.frames[0].timestamp_s, end=s.frames.at(-1).timestamp_s;
    const x=t=>80+1120*(t-start)/Math.max(1e-9,end-start), b=data.braking;
    const required=v=>v*b.remaining_delay_s+v*v/(2*b.deceleration_m_s2)+b.buffer_m;
    const points=s.motion.map(m=>({...m,margin:m.remaining_distance_m-required(m.speed_m_s)}));
    const lo=Math.min(0,...points.map(m=>m.margin)),hi=Math.max(1,...points.map(m=>m.margin));
    const y=value=>210-100*(value-lo)/Math.max(1e-9,hi-lo);
    ctx.clearRect(0,0,1280,300);ctx.font="17px system-ui";ctx.lineWidth=2;
    for(let side=0;side<2;side++){
      const v=seq[side];if(!v)continue;const color=side?"#7badff":"#6df0a8",row=35+side*35;
      ctx.fillStyle=color;ctx.fillText(v.candidate_id+" 안정 track",80,row-8);
      for(const f of v.frames){ctx.fillStyle=f.tracks.some(t=>t.stable)?color:"#425064";ctx.fillRect(x(f.timestamp_s),row,Math.max(3,1120/v.frames.length),8);}
      const record=v.records.find(r=>r.seed===v.records[0].seed&&r.processing_latency_s===0.1);
      const interval=record?.recognition_time_interval_s;
      if(interval){ctx.globalAlpha=.3;ctx.fillStyle=color;ctx.fillRect(x(interval[0]),100,Math.max(2,x(interval[1])-x(interval[0])),115);ctx.globalAlpha=1;}
    }
    if(s.risk){
      ctx.strokeStyle="#e8c49c";ctx.beginPath();points.forEach((m,i)=>i?ctx.lineTo(x(m.timestamp_s),y(m.margin)):ctx.moveTo(x(m.timestamp_s),y(m.margin)));ctx.stroke();
      ctx.strokeStyle="#f27979";ctx.beginPath();ctx.moveTo(80,y(0));ctx.lineTo(1200,y(0));ctx.stroke();ctx.fillStyle="#e8c49c";ctx.fillText("제동 여유 m  "+lo.toFixed(1)+" … "+hi.toFixed(1),80,245);
    }else{ctx.fillStyle="#e8c49c";ctx.fillText("비위험 구간: 대상까지의 제동 여유는 정의하지 않습니다.",80,180);}
    const frame=s.frames[Math.min(n,s.frames.length-1)],marker=x(frame.timestamp_s);ctx.strokeStyle="#ffffff";ctx.beginPath();ctx.moveTo(marker,15);ctx.lineTo(marker,220);ctx.stroke();ctx.fillStyle="#ffffff";ctx.fillText("0 s",80,280);ctx.fillText((end-start).toFixed(2)+" s",1130,280);
    const now=points.reduce((a,m)=>Math.abs(m.timestamp_s-frame.timestamp_s)<Math.abs(a.timestamp_s-frame.timestamp_s)?m:a,points[0]);
    $("motion").textContent=s.risk?`관측 시점 속도 ${now.speed_m_s.toFixed(2)} m/s · 충돌 경계까지 ${now.remaining_distance_m.toFixed(2)} m · 필요 거리 ${required(now.speed_m_s).toFixed(2)} m · 결과 지연은 별도 인지 구간에 포함`:`비위험 구간 · 관측 시점 속도 ${now.speed_m_s.toFixed(2)} m/s`;
  }
  async function render(){
    const g=++generation, seq=sequences(), n=Number($("frame").value);
    timeline(seq,n);
    for(let side=0;side<2;side++){
      const name=side?"right":"left", s=seq[side]; if(!s){$(name+"-metrics").textContent="이 사건에서는 계산하지 않은 후보입니다.";$(name).getContext("2d").clearRect(0,0,1280,720);continue;}
      const f=s.frames[Math.min(n,s.frames.length-1)], image=new Image();image.src=f.image;await image.decode();if(g!==generation)return;
      const ctx=$(name).getContext("2d");ctx.drawImage(image,0,0);ctx.font="20px system-ui";ctx.lineWidth=3;
      for(const t of f.tracks){ctx.strokeStyle=t.stable?"#6df0a8":"#ffd076";ctx.fillStyle=ctx.strokeStyle;ctx.strokeRect(t.bbox[0],t.bbox[1],t.bbox[2]-t.bbox[0],t.bbox[3]-t.bbox[1]);ctx.fillText("track "+t.track_id+" "+t.streak+" frames",t.bbox[0],Math.max(20,t.bbox[1]-5));}
      $(name+"-title").textContent=s.candidate_id;
      $(name+"-metrics").textContent=JSON.stringify(s.records.find(r=>r.seed===s.records[0].seed&&r.processing_latency_s===0.1),null,2);
      $("time").textContent=(f.timestamp_s-s.frames[0].timestamp_s).toFixed(3)+"s / 결과 사용 "+(f.timestamp_s-s.frames[0].timestamp_s+0.1).toFixed(3)+"s";
    }
  }
  function reset(){ $("frame").max=Math.max(...sequences().filter(Boolean).map(s=>s.frames.length-1),0);$("frame").value=0;render().catch(showError);}
  function showError(e){$("decision").textContent="결과 표시 오류: "+e.message;}
  $("event").onchange=reset;$("candidate").onchange=reset;$("frame").oninput=()=>render().catch(showError);
  $("play").onclick=()=>{if(timer){clearInterval(timer);timer=null;return;}timer=setInterval(()=>{if(Number($("frame").value)>=Number($("frame").max)){clearInterval(timer);timer=null;return;}$("frame").value=Number($("frame").value)+1;render().catch(showError);},1000/12);};
  reset();
})().catch(e=>{document.getElementById("decision").textContent=e.message;});
