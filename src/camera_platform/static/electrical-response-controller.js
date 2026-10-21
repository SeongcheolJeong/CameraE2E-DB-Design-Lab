'use strict';

function CameraElectricalResponseController(ctx) {
  const {api,records,render,isActive}=ctx, ui=CameraElectricalResponseUI(ctx);
  const state={text:'',bindings:{},options:{},epoch:0,history:[],record:null,selectedId:'',cell:'',pair:'',frame:'',plan:null,error:''};
  let timer=null, historyEpoch=0, recordEpoch=0, templateEpoch=0, refreshing=false;
  const ids=['assembly_id','scene_sequence_id','control_sequence_id','capture_program_id'];
  const terminal=record=>['succeeded','failed','cancelled'].includes(record?.status);
  const collection='/electrical-response/studies';
  function options() {
    const assembly=state.bindings.assembly_id, scene=state.bindings.scene_sequence_id;
    state.options={assemblies:records('assemblies'),
      scene_sequences:records('scene_sequences').filter(x=>!x.assembly_id||x.assembly_id===assembly),
      control_sequences:records('control_sequences').filter(x=>x.assembly_id===assembly&&(!scene||x.scene_sequence_id===scene)),
      capture_programs:records('capture_programs').filter(x=>x.assembly_id===assembly&&x.payload?.schema==='camera_electrical_control_capture_program_v1')};
  }
  function defaults() {
    if(!state.bindings.assembly_id){
      const program=records('capture_programs').find(x=>x.payload?.schema==='camera_electrical_control_capture_program_v1');
      ui.setBindings(state,{assembly_id:program?.assembly_id||records('assemblies')[0]?.id||''});
    }
    options();
    for(const [field,kind] of [['scene_sequence_id','scene_sequences'],['control_sequence_id','control_sequences'],['capture_program_id','capture_programs']]) {
      if(!state.bindings[field])ui.setBindings(state,{[field]:state.options[kind]?.[0]?.id||''});
      options();
    }
  }
  function schedule() {
    if(timer!==null||!isActive()||!state.selectedId||terminal(state.record))return;
    timer=setTimeout(async()=>{timer=null;try{await refreshRecord(false);}catch(error){state.error=error.message;render();}},1500);
  }
  function saveRecord(record) {
    historyEpoch++;
    state.record=record;
    state.history=[record,...state.history.filter(x=>x.id!==record.id)];
  }
  async function refreshRecord(visible=true) {
    if(!state.selectedId)return;
    if(refreshing){schedule();return;}
    const id=state.selectedId,ticket=++recordEpoch;
    refreshing=true;if(visible)state.loading=true;
    try {
      const result=await api(collection+'/'+encodeURIComponent(id));
      if(ticket!==recordEpoch||state.selectedId!==id)return;
      if(result?.id!==id)throw Error('응답의 비교 작업 ID가 선택한 기록과 다릅니다.');
      saveRecord(result);state.error='';
    } catch(error) {if(ticket===recordEpoch&&state.selectedId===id){state.record=null;throw error;}}
    finally {refreshing=false;state.loading=false;render();schedule();}
  }
  async function activate() {
    defaults();const ticket=++historyEpoch;
    const history=await api(collection);
    if(ticket!==historyEpoch)return;
    if(!Array.isArray(history))throw Error('비교 작업 목록 형식이 올바르지 않습니다.');
    state.history=history;
    if(!state.selectedId&&history.length){state.selectedId=history[0].id;state.record=null;}
    render();await refreshRecord(false);
  }
  async function click(button) {
    const action=button.dataset.electricalresponseAction;
    if(state.submitting)return;
    try {
      if(action==='refresh'){await activate();return;}
      if(action==='record-refresh'){await refreshRecord();return;}
      if(action==='template') {
        if(ids.some(key=>!state.bindings[key]))throw Error('카메라·장면·초기 제어·전기 AE 프로그램을 선택하세요.');
        const ticket=++templateEpoch, epoch=state.epoch, text=state.text, bindings=JSON.stringify(state.bindings);
        state.loading=true;render();
        try {
          const result=await api('/electrical-response/template?'+new URLSearchParams(state.bindings));
          if(ticket===templateEpoch&&epoch===state.epoch&&text===state.text&&bindings===JSON.stringify(state.bindings))ui.setText(state,JSON.stringify(result,null,2));
        } catch(error) {
          if(ticket===templateEpoch&&epoch===state.epoch&&text===state.text&&bindings===JSON.stringify(state.bindings))throw error;
        } finally {if(ticket===templateEpoch)state.loading=false;render();}
        return;
      }
      if(action==='plan') {
        const ticket=ui.beginPlan(state);render();
        try {ui.acceptPlan(state,ticket,await api(collection+'/plan',ticket.specification));}
        catch(error){ui.rejectPlan(state,ticket,error);}
        render();return;
      }
      if(action==='submit') {
        const payload=ui.submitPayload(state);state.submitting=true;render();
        try {
          const result=await api(collection,payload);
          if(!result?.id)throw Error('제출 응답의 작업 ID가 없습니다. 재제출 전에 기록을 새로고침하세요.');
          recordEpoch++;state.selectedId=result.id;state.cell='';state.pair='';state.frame='';saveRecord(result);
        } finally {state.submitting=false;render();schedule();}
        return;
      }
      if(action==='cancel') {
        const id=state.selectedId;if(!id||terminal(state.record))return;
        state.submitting=true;render();
        try {const result=await api(collection+'/'+encodeURIComponent(id)+'/cancel',{});if(id===state.selectedId)saveRecord(result);}
        finally {state.submitting=false;render();schedule();}
      }
    } catch(error) {state.error=error.message;render();}
  }
  async function change(target) {
    const field=target.dataset.electricalresponseField;if(!field)return false;
    if(ids.includes(field)) {
      const next={[field]:target.value};
      if(field==='assembly_id')Object.assign(next,{scene_sequence_id:'',control_sequence_id:'',capture_program_id:''});
      if(field==='scene_sequence_id')next.control_sequence_id='';
      ui.setBindings(state,next);defaults();render();
    } else if(field==='history') {
      recordEpoch++;state.selectedId=target.value;state.record=null;state.cell='';state.pair='';state.frame='';render();
      // A prior selection read may still be pending. It cannot overwrite this
      // selection; schedule the current read after its completion if necessary.
      if(refreshing)schedule();else await refreshRecord();
    } else if(['cell','pair','frame'].includes(field)){ui.select(state,{[field]:target.value});render();}
    return true;
  }
  function input(target) {
    if(target.dataset.electricalresponseField!=='json')return false;
    ui.setText(state,target.value);
    // Preserve cursor and partially invalid JSON while disabling stale submission.
    document.querySelectorAll('[data-electricalresponse-action="submit"]').forEach(x=>{x.disabled=true;});
    return true;
  }
  return {view(){options();return ui.render(state);},activate,click,change,input};
}
