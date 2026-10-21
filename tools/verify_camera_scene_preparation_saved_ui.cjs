'use strict';

// Review an existing preparation proof without submitting jobs or creating records.
// Usage: node <script> <base-url> <preparation-proof.json> <output-directory>
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {isDeepStrictEqual} = require('node:util');
const {chromium} = require('playwright');

assert(process.argv.length === 5, 'Expected base URL, preparation proof, output directory');
const base = new URL(process.argv[2]).origin;
const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = path.resolve(process.argv[4]);
assert.equal(input.schema, 'camera_scene_preparation_verification_v1');
assert.equal(input.status, 'passed');
assert(input.state_dir && input.environment?.platform_version);
const ids = input.records;
for (const name of ['source_assembly', 'scene', 'assembly', 'render', 'image', 'test', 'run']) {
  assert(typeof ids[name] === 'string' && ids[name], 'Missing proof identity: ' + name);
}
fs.mkdirSync(out, {recursive: true});
const report = {
  schema: 'camera_scene_preparation_saved_ui_v1',
  base_url: base,
  state_dir: input.state_dir,
  source_proof: path.resolve(process.argv[3]),
  checks: [], errors: [], posts: [], blocked_writes: [],
  read_only: true, user_browser_profile_accessed: false,
  physical_validation: 'not_established',
};
const check = (name, condition) => {
  assert(condition, name);
  report.checks.push({name, status: 'passed'});
};
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const same = isDeepStrictEqual;
const save = () => fs.writeFileSync(path.join(out, 'verification.json'), JSON.stringify(report, null, 2) + '\n');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.CAMERA_UI_BROWSER || chromium.executablePath(),
  });
  const context = await browser.newContext({viewport: {width: 1480, height: 1080}});
  // Abort any unexpected browser write before it can reach the application.
  await context.route('**/*', async route => {
    const request = route.request();
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
      report.blocked_writes.push({method: request.method(), url: request.url()});
      await route.abort('blockedbyclient');
    } else await route.continue();
  });
  const page = await context.newPage();
  page.on('pageerror', error => report.errors.push(String(error)));
  page.on('request', request => {
    if (request.method() === 'POST') report.posts.push(new URL(request.url()).pathname);
  });
  const get = async endpoint => {
    const response = await page.request.get(base + '/api/v1/' + endpoint);
    assert(response.ok(), endpoint + ': ' + await response.text());
    return response.json();
  };
  const readBytes = async url => {
    const target = new URL(url, base);
    assert.equal(target.origin, base, 'Artifact must stay on the selected server');
    const response = await page.request.get(target.href);
    assert(response.ok(), target.href + ': ' + response.status());
    return response.body();
  };
  const clickGet = async (selector, endpoint) => {
    const waiting = page.waitForResponse(response =>
      response.request().method() === 'GET' &&
      new URL(response.url()).pathname === '/api/v1/' + endpoint);
    await page.locator(selector).click();
    const response = await waiting;
    assert(response.ok(), await response.text());
    return response.json();
  };
  try {
    const before = await get('workbench');
    const expectedVersion = process.env.CAMERA_EXPECTED_PLATFORM_VERSION || input.environment.platform_version;
    check('Exact target state and installed platform version',
      before.state_dir === input.state_dir && before.version === expectedVersion);
    report.version = before.version;
    report.saved_run_version = input.environment.platform_version;
    report.counts_before = before.counts;
    const catalog = await get('scene_renders/catalog');
    check('Connected renderer matches the actual saved backend', catalog.available && same(catalog.backend, input.backend));
    report.static_sha256 = {};
    for (const name of ['scenes.js', 'style.css']) report.static_sha256[name] = hash(await readBytes('/' + name));

    const endpoints = [
      'assemblies/' + ids.source_assembly, 'assemblies/' + ids.assembly,
      'scene_assets/' + ids.scene, 'scene_renders/' + ids.render,
      'spectral_images/' + ids.image, 'tests/' + ids.test, 'runs/' + ids.run,
    ];
    const snapshots = await Promise.all(endpoints.map(get));
    const [source, assembly, scene, render, image, test, run] = snapshots;
    const prepared = assembly.scene_preparation;
    check('Prepared assembly and source preserve exact proof hashes',
      assembly.content_hash === input.assembly_sha256 && source.content_hash === input.source_assembly_sha256);
    check('Preparation keeps the original parent, scene and preview identities',
      prepared?.preview_sha256 === input.preview_sha256 &&
      prepared.source_assembly_sha256 === source.content_hash &&
      prepared.scene_sha256 === input.scene_sha256 && scene.content_hash === input.scene_sha256 &&
      prepared.draft.source_assembly_id === ids.source_assembly && prepared.draft.scene_asset_id === ids.scene);
    check('Saved preparation retains unknown physical accuracy and explicit throughput requirement',
      input.physical_validation === 'not_established' && prepared.physical_validation === 'not_established' &&
      prepared.required_render_input.includes('transmittance'));
    const models = await Promise.all(assembly.model_ids.map(id => get('models/' + id)));
    const modelHashes = Object.fromEntries(models.map(model => [model.id, model.content_hash]));
    check('Every selected model matches its immutable preparation snapshot',
      same(modelHashes, input.model_hashes) && same(modelHashes, prepared.model_hashes) && same(modelHashes, assembly.model_hashes));
    const lens = models.find(model => model.kind === 'lens');
    check('Render and Camera execution use the saved preparation and installed environment',
      render.status === 'succeeded' && run.status === 'succeeded' &&
      render.request.assembly_id === ids.assembly && render.request.scene_asset_id === ids.scene &&
      run.assembly_id === ids.assembly && run.test_id === ids.test && run.scene_id === ids.image &&
      same(render.environment, input.environment) && same(run.environment, input.environment));
    check('Published input remains linked to the actual render and Test',
      image.render_job_snapshot?.id === ids.render && test.id === ids.test &&
      render.result.physical_validation === 'not_established');
    report.artifact_sha256 = {render: {}, camera: {}};
    for (const [kind, job] of [['render', render], ['camera', run]]) {
      const expected = input.artifact_sha256?.[kind];
      assert(expected && Object.keys(expected).length, 'Missing artifact proof for ' + kind);
      if (kind === 'camera') for (const name of ['raw_frames.npy', 'rgb_frames.npy']) assert(expected[name]);
      if (kind === 'render') assert(expected['spectral-radiance.npy']);
      for (const [name, sha256] of Object.entries(expected)) {
        const artifact = job.artifacts.find(item => item.name === name);
        assert(artifact && artifact.sha256 === sha256, kind + ' artifact snapshot: ' + name);
        const actual = hash(await readBytes(artifact.url));
        assert.equal(actual, sha256, kind + ' downloaded bytes: ' + name);
        report.artifact_sha256[kind][name] = actual;
      }
      check('Downloaded ' + kind + ' originals retain all proof hashes', true);
    }

    await page.goto(base + '/#scenes');
    await page.locator('[data-scene-action="new"]:enabled').waitFor();
    await page.locator('#scene-selected').selectOption(ids.scene);
    await page.locator('#scene-render-assembly').selectOption(ids.assembly);
    const draft = await clickGet('[data-scene-action="camera-template"]', 'scene_authoring/camera-template');
    await page.locator('#scene-prep-name').waitFor();
    check('Saved camera loads into preparation through a GET template',
      draft.source_assembly_id === ids.assembly && draft.scene_asset_id === ids.scene &&
      same(draft.assembly.model_ids, assembly.model_ids) && same(draft.assembly.conditions, assembly.conditions));
    check('Editor shows the exact selected Lens, Sensor, ISP and conditions',
      (await Promise.all(models.map(async model =>
        await page.locator('#scene-prep-' + model.kind).inputValue() === model.id))).every(Boolean) &&
      Number(await page.locator('#scene-prep-condition-f_number').inputValue()) === lens.properties.f_number.value &&
      same(JSON.parse(await page.locator('#scene-camera-json').inputValue()), draft));
    check('Template creates no approval and cannot save before a fresh inspection',
      await page.locator('[data-scene-action="camera-save"]').isDisabled() &&
      await page.locator('[data-scene-camera-preview]').count() === 0);
    for (const [label, width, height] of [['desktop', 1480, 1080], ['mobile', 390, 844]]) {
      await page.setViewportSize({width, height});
      await page.locator('.scene-camera-preparation').scrollIntoViewIfNeeded();
      check(label + ' preparation has no horizontal document overflow',
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.locator('.scene-camera-preparation').screenshot({path: path.join(out, 'preparation-' + label + '.png')});
      await page.screenshot({path: path.join(out, label + '.png'), fullPage: true});
    }
    await page.setViewportSize({width: 1480, height: 1080});
    const optical = await clickGet('[data-scene-action="render-template"]', 'scene_authoring/render-template');
    await page.locator('#scene-throughput-0').waitFor();
    const projection = optical.request.projection;
    check('Saved optics load while every throughput sample stays explicitly unknown',
      projection.focal_length_mm === lens.properties.focal_length.value &&
      projection.f_number === lens.properties.f_number.value &&
      projection.on_axis_transmittance.values.every(value => value === null) &&
      (await Promise.all(projection.on_axis_transmittance.values.map(async (_, index) =>
        await page.locator('#scene-throughput-' + index).inputValue() === ''))).every(Boolean) &&
      same(JSON.parse(await page.locator('#scene-render-json').inputValue()), optical.request));

    await page.locator('#scene-job').selectOption(ids.render);
    await page.locator('[data-scene-action="camera"]:enabled').waitFor();
    for (const band of [...new Set([0, projection.on_axis_transmittance.values.length - 1])]) {
      await page.locator('#scene-band').selectOption(String(band));
      const images = page.locator('.scene-preview');
      assert.equal(await images.count(), 2);
      for (const element of await images.all()) {
        await element.scrollIntoViewIfNeeded();
        await element.evaluate(image => image.decode());
      }
      check('Saved spectral and depth previews load at original resolution, band ' + band,
        await images.evaluateAll((elements, shape) => elements.every(image =>
          image.naturalWidth === shape[1] && image.naturalHeight === shape[0]), render.request.shape));
    }
    check('Rendered result retains the visible physical-validation limitation',
      (await page.locator('.scene-trust').innerText()).includes('미확립'));
    await page.locator('[data-scene-result]').screenshot({path: path.join(out, 'saved-render.png')});
    check('Read-only inspection preserves every original record and selected model',
      same(await Promise.all(endpoints.map(get)), snapshots) &&
      same(await Promise.all(assembly.model_ids.map(id => get('models/' + id))), models));
    const after = await get('workbench');
    report.counts_after = after.counts;
    check('Read-only review preserves every registry count', same(before.counts, after.counts));
    check('No POST or other mutating request was attempted', report.posts.length === 0 && report.blocked_writes.length === 0);
    check('No uncaught browser exception', report.errors.length === 0);
    report.status = 'passed';
    report.records = ids;
    report.environment = input.environment;
    report.backend = input.backend;
    report.verified_at = new Date().toISOString();
  } catch (error) {
    report.status = 'failed';
    report.failure = String(error.stack || error);
    await page.screenshot({path: path.join(out, 'failure.png'), fullPage: true}).catch(() => {});
    throw error;
  } finally {
    save();
    await browser.close();
  }
  console.log(JSON.stringify({status: report.status, checks: report.checks.length, posts: report.posts.length}));
})().catch(error => {console.error(error); process.exitCode = 1;});
