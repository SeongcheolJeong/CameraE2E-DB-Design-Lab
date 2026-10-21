/* Exercises the real planning, atomic submission, worker and comparison UI. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require('playwright');
const base=process.argv[2]||'http://127.0.0.1:8771';
const out=path.resolve(process.argv[3]||'reports/camera_platform/studies-ui');
fs.mkdirSync(out,{recursive:true});
const input=process.argv[4]?JSON.parse(fs.readFileSync(process.argv[4],'utf8')):null;
const report={read_only:!!input,schema:'camera_study_ui_verification_v1',base_url:base,checks:[],errors:[],posts:[],screenshots:[],physical_validation:'not_established',user_browser_profile_accessed:false};
const check=(name,test)=>{assert(test,name);report.checks.push({name,status:'passed'});};
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER||chromium.executablePath()});
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror',e=>report.errors.push(String(e)));
    page.on('console',e=>{if(e.type()==='error')report.errors.push(e.text());});
    page.on('request',r=>{if(r.method()==='POST')report.posts.push(new URL(r.url()).pathname);});
    const get=async route=>{const r=await page.request.get(base+'/api/v1/'+route);assert(r.ok(),await r.text());return r.json();};
    const initial=await get('bootstrap');report.server_version=initial.version;
    const expected=process.env.CAMERA_EXPECTED_STATE_DIR;report.expected_state_dir=expected||null;
    check('Expected workspace identity',expected?initial.state_dir===path.resolve(expected):initial.state_dir.endsWith(input?'/camera-platform':'/camera-platform-studies-qa'));
    check('Managed worker running',(await get('health')).worker==='running');
    await page.goto(base+'/#experiments');
    await page.locator('[data-action="experiment-mode"][data-mode="study"]').click();
    let study;
    if(input){
      study=await get('studies/'+input.study_id);report.study_id=study.id;
      await page.locator(`[data-study-action="select"][data-id="${study.id}"]`).click();
    }else{
    await page.locator('#study-name').fill('UI exposure and read-noise · '+Date.now());
    await page.locator('#study-question').fill('노출·Read noise를 바꾸면 어떤 후보가 SNR과 포화 조건을 만족하는가?');
    await page.locator('#study-snr').fill('25');
    const before=await get('bootstrap');
    const previewResponse=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/studies/plan'&&r.request().method()==='POST');
    await page.locator('[data-study-action="preview"]').click();
    const pr=await previewResponse;assert(pr.ok(),await pr.text());const plan=await pr.json();
    await page.locator('[data-study-plan]').waitFor();
    check('UI plans all five baseline and OAT candidates',plan.run_count===5);
    assert.deepEqual((await get('bootstrap')).counts,before.counts);
    check('Planning writes no revisions or runs',true);
    check('Plan links actual immutable source hashes',Object.keys(plan.source_hashes).length===5);
    await page.locator('#study-values-0').fill('0.002, 0.004, 0.006');
    check('Changing a coordinate removes stale submission controls',await page.locator('[data-study-action="submit"]').count()===0);
    await page.locator('#study-values-0').fill('0.002, 0.004, 0.008');
    await page.locator('[data-study-action="preview"]').click();
    await page.locator('[data-study-action="submit"]').waitFor();
    const submitted=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/studies'&&r.request().method()==='POST');
    await page.locator('[data-study-action="submit"]').click();
    const sr=await submitted;assert(sr.ok(),await sr.text());study=await sr.json();report.study_id=study.id;
    check('Submitted revisions and full plan are persisted',study.run_ids.length===5);
    }
    let analysis;
    const deadline=Date.now()+60000;
    do{analysis=await get('studies/'+study.id+'/analysis');if(analysis.status==='complete')break;await new Promise(r=>setTimeout(r,700));}while(Date.now()<deadline);
    check('All five real worker runs complete',analysis.coverage.succeeded===5);
    await page.waitForFunction(id=>document.querySelector(`[data-study-analysis="${id}"]`)?.textContent.includes('5개 완료'),study.id,{timeout:10000});
    const panel=page.locator(`[data-study-analysis="${study.id}"]`);
    const baseRow=analysis.rows.find(r=>r.is_baseline),lowExposure=analysis.rows.find(r=>r.changed.exposure_s===.002);
    check('Half exposure produces half expected photoelectrons',Math.abs(lowExposure.metrics.mean_photoelectrons_e/baseRow.metrics.mean_photoelectrons_e-.5)<1e-12);
    const lowNoise=analysis.rows.find(r=>r.changed.read_noise_e===1),highNoise=analysis.rows.find(r=>r.changed.read_noise_e===5);
    check('Noise change leaves charge unchanged and changes SNR',lowNoise.metrics.mean_photoelectrons_e===highNoise.metrics.mean_photoelectrons_e&&lowNoise.metrics.min_theoretical_snr_db>highNoise.metrics.min_theoretical_snr_db);
    check('Low SNR candidate excluded by explicit constraint',lowExposure.feasibility==='infeasible'&&lowExposure.pareto_status==='excluded');
    check('Sample Pareto front retains higher SNR and short-exposure tradeoffs',lowNoise.pareto_status==='non_dominated'&&analysis.rows.find(r=>r.changed.exposure_s===.008).pareto_status==='non_dominated');
    check('Four secants preserve unresolved derivative accuracy',analysis.sensitivities.length===4&&analysis.sensitivities.every(s=>s.derivative_error==='unknown'));
    for(const row of analysis.rows){
      const tr=panel.locator(`[data-study-row="${row.index}"]`);
      for(const [metric,digits] of [['min_theoretical_snr_db',5],['min_saturation_margin_e',3],['max_relative_color_shading',5]]){
        assert.equal(await tr.locator(`[data-study-metric="${metric}"]`).innerText(),row.metrics[metric].toLocaleString('en-US',{maximumFractionDigits:digits}));
      }
    }
    check('Visible metric table matches all returned model results',true);
    check('Saved result constraints and new plan are clearly separate',(await panel.locator('.study-applied-constraints').innerText()).includes('25 dB')&&await page.getByRole('heading',{name:'새 실험 계획',exact:true}).count()===1);
    check('Plot and physical limitations shown',await panel.locator('svg').count()===1&&(await panel.innerText()).includes('실물 정확도 미확립'));
    const downloadEvent=page.waitForEvent('download');await panel.locator('[data-study-action="export"]').click();
    const download=await downloadEvent;const file=path.join(out,'analysis.json');await download.saveAs(file);
    const exported=JSON.parse(fs.readFileSync(file,'utf8'));
    assert.deepEqual(exported.rows,analysis.rows);check('Export preserves exact results and provenance',exported.study_hash===study.content_hash&&exported.total_uncertainty.kind==='unknown');
    for(const [label,width,height] of [['desktop',1440,1000],['mobile',390,844]]){
      await page.setViewportSize({width,height});await page.evaluate(()=>window.scrollTo(0,0));
      check(label+' has no page overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      const file=path.join(out,'study-'+label+'.png');await page.screenshot({path:file,fullPage:true});report.screenshots.push(file);
    }
    await page.setViewportSize({width:1440,height:1000});
    await panel.locator(`[data-study-action="run"][data-id="${lowNoise.run_id}"]`).click();
    await page.getByRole('heading',{name:'이 결과가 설명하는 범위',exact:true}).waitFor({timeout:10000});
    check('Candidate opens actual frozen camera result',(await page.locator('#main').innerText()).includes('이 결과가 설명하는 범위'));
    const final=await get('bootstrap');
    check('Study creates no capture or measurement evidence',final.counts.captures===initial.counts.captures&&final.counts.measurements===initial.counts.measurements);
    if(input){assert.deepEqual(final.counts,initial.counts);check('Read-only verification creates no records',report.posts.length===0);}
    check('No browser errors',report.errors.length===0);
    report.status='passed';report.browser_version=browser.version();report.verified_at=new Date().toISOString();report.analysis=analysis;
  } finally {await browser.close();fs.writeFileSync(path.join(out,'headless-qa.json'),JSON.stringify(report,null,2)+'\n');}
  process.stdout.write(JSON.stringify({status:report.status,checks:report.checks.length,report:path.join(out,'headless-qa.json')})+'\n');
})().catch(e=>{process.stderr.write(String(e.stack)+'\n');process.exitCode=1;});
