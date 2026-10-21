'use strict';
// Exercise actual field rendering; this is not browser-observation evidence.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('src/camera_platform/static/scenes.js','utf8');
const scope={esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
  num:v=>Number(v).toPrecision(5),details:title=>`<details>${title}</details>`};
vm.createContext(scope);
vm.runInContext(source.slice(source.indexOf('  const absoluteProjection='),source.indexOf('  function projectionView(source){')),scope);
const fields=[];
scope.field=(...args)=>{fields.push(args);return `<input id="${args[1]}">`;};
scope.projection={projection_model:'paraxial_on_axis_projection_v1',
  on_axis_transmittance:{wavelength_nm:[500,600],values:[null,.8]},source_uri:'fixture://T',method_and_version:'declared:1'};
const legacy=vm.runInContext("projectionFields(projection,field,'scene-projection-throughput')",scope);
assert.match(legacy,/렌즈 분광 투과율/);
assert.equal(fields.length,4);
assert.equal(fields[0][3],null);
assert.equal(fields[2][1],'scene-projection-source');
assert.equal(fields[3][1],'scene-projection-method');
fields.length=0;
scope.projection={projection_model:'prescription_radiometry_projection_v1',
  on_axis_irradiance_per_radiance_sr:{wavelength_nm:[500,600],values:[.03,.0075]},
  lens_model_id:'lens-<unsafe>',factory_job_id:'factory-original',lens_model_sha256:'1'.repeat(64),
  factory_job_sha256:'2'.repeat(64),psf_factory_job_id:'psf-original',psf_factory_job_sha256:'3'.repeat(64),
  stop_transmission_sha256:'4'.repeat(64)};
const absolute=vm.runInContext("projectionFields(projection,field,'scene-throughput')",scope);
assert.match(absolute,/data-scene-absolute-radiometry/);
assert.match(absolute,/E\/L · sr/);
assert.match(absolute,/0\.030000 sr/);
assert.match(absolute,/0\.0075000 sr/);
assert.match(absolute,/lens-&lt;unsafe&gt;/);
assert.equal(fields.length,0,'Bound E/L and provenance must not become independent editable throughput');
assert.doesNotMatch(absolute,/<input|투과율 · 0–1/);
assert.equal(vm.runInContext('projectionSpectrum(undefined)',scope),undefined);
console.log('PASS: legacy unknown transmittance remains editable; verified E/L has readonly sr values and escaped frozen lineage');
