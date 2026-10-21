/* Independent headless browser; uses only the explicit test server. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require('playwright');
const base=process.argv[2] || 'http://127.0.0.1:8771';
const input=JSON.parse(fs.readFileSync(process.argv[3] || 'reports/camera_platform/uncertainty-http-verification.json','utf8'));
const out=path.resolve(process.argv[4] || 'reports/camera_platform/uncertainty-ui');
fs.mkdirSync(out,{recursive:true});
const report={schema:'camera_uncertainty_ui_verification_v1',base_url:base,checks:[],errors:[],posts:[],screenshots:[],user_browser_profile_accessed:false};
const check=(name,value)=>{assert(value,name);report.checks.push({name,status:'passed'});};
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.CAMERA_UI_BROWSER || chromium.executablePath()});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror',error=>report.errors.push(String(error)));
    page.on('console',item=>{if(item.type()==='error')report.errors.push(item.text());});
    page.on('request',req=>{if(req.method()==='POST')report.posts.push(req.url());});
    await page.goto(base+'/#experiments');
    for(const kind of ['run','observed'].filter(kind=>input.records[kind])) {
      const id=input.records[kind];
      await page.locator(`[data-action="select-run"][data-id="${id}"]`).click();
      const panel=page.locator(`[data-uncertainty-run="${id}"]`);
      await panel.waitFor();
      const response=await page.request.get(base+'/api/v1/runs/'+id);assert(response.ok());
      const run=await response.json(),budget=run.result.prediction.uncertainty_budget;
      const ranges=budget.conditional_intervals.channel_ratios;
      check(kind+' all declared field or ROI rows shown',await panel.locator('tbody tr').count()===ranges.lower.length);
      for(let i=0;i<ranges.lower.length;i++)for(let c=0;c<2;c++) {
        const text=await panel.locator('tbody tr').nth(i).locator('td').nth(c+1).innerText();
        const expected=[ranges.lower[i][c],ranges.upper[i][c]].map(x=>x.toLocaleString('en-US',{maximumFractionDigits:6})).join(' – ');
        assert.equal(text,expected);
      }
      check(kind+' visible bounds match exact returned prediction',true);
      check(kind+' partial scope is explicit',(await panel.innerText()).includes('전체 불확실도 미확인'));
      await panel.locator('summary').filter({hasText:'미확인 항목'}).click();
      check(kind+' model discrepancy remains visible',(await panel.innerText()).includes('model_discrepancy'));
      await panel.locator('summary').filter({hasText:'반영한 입력 경계'}).click();
      check(kind+' original model ID and hash accessible',(await panel.innerText()).includes(budget.sources.qe.source_content_hash));
      await panel.locator('summary').filter({hasText:'반영한 입력 경계'}).click();
      await panel.locator('summary').filter({hasText:'미확인 항목'}).click();
      for(const [label,width,height] of [['desktop',1440,1000],['mobile',390,844]]) {
        await page.setViewportSize({width,height});await panel.scrollIntoViewIfNeeded();
        check(kind+' '+label+' no page overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
        const file=path.join(out,kind+'-'+label+'.png');await page.screenshot({path:file,fullPage:true});report.screenshots.push(file);
      }
      await page.setViewportSize({width:1440,height:1000});
    }
    check('no browser errors',report.errors.length===0);
    check('inspection creates no records',report.posts.length===0);
    report.status='passed';report.browser_version=browser.version();report.verified_at=new Date().toISOString();
  } finally {
    await browser.close();fs.writeFileSync(path.join(out,'headless-qa.json'),JSON.stringify(report,null,2)+'\n');
  }
  process.stdout.write(JSON.stringify({status:report.status,checks:report.checks.length,report:path.join(out,'headless-qa.json')})+'\n');
})().catch(error=>{process.stderr.write(String(error.stack)+'\n');process.exitCode=1;});
