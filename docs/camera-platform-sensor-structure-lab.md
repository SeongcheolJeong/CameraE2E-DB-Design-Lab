# TechInsights DTI / OCL Structure Lab

## Purpose and source revision

The first adapter uses IMX662 `DEF-2206-805` with reviewed DEP evidence. It compares the reported structure, a hypothetical no-DTI structure and a +0.1 pitch OCL offset. This is structural sensitivity analysis, not measured product performance.

`DEP-2206-801` contains **6.6 µm DTI depth and 0.23 µm average width**. The legacy catalog classified the width as depth. The extraction rule is corrected; existing catalog bytes remain unchanged. Each study carries a new structure revision, the original catalog hash, reviewed evidence hash, page references, correction ledger and assumptions. Report fields not used in this solver remain visible as `retained_unmodeled` rather than being discarded.

## Running

Open `/sensor-structure.html` on a current Camera Platform server. The Reference Bench navigation links to it. The default job runs **48 real Meep samples**: three structures × two angles × two polarizations × (two meshes + time control + PML control). This is not an instant slider calculation. Intermediate numerical results are visible during execution, explicitly unqualified until all checks finish.

```python
from camera_platform.sdk import PlatformClient
with PlatformClient('http://127.0.0.1:8783') as client:
    spec = client.sensor_structure_template()['specification']
    job = client.submit_sensor_structure_study(spec)
    result = client.wait(job['id'], timeout=86400,
                         expected_kind='sensor_structure_study')
    print(result['status'], result['result']['summary']['status'])
```

API: `POST /api/v1/sensor-structure-studies`; common `/api/v1/jobs/{id}`, SDK `wait`, typed detail/cancel and bound artifact download. Execution success is separate from numerical qualification. Cancellation preserves completed samples; resubmitting identical inputs reuses checksum-verified samples. Changed structure, settings or worker source creates a different cache identity.

The existing T7 500 GB project budget is enforced. Each solver subprocess has an explicit RSS and elapsed-time limit. No automatic mesh or resolution reduction occurs. Other project writers can hold the storage lock; a failed storage acquisition is reported rather than bypassed.

## Physical scope

- 2D: green cross-section, finite Gaussian incident beam, s/p polarization. The input angle is the central phase-gradient direction of a finite beam; it is not a single-angle infinite plane wave. Power is normalized to a separate incident reference run.
- DTI oxide liner: 65 nm per side extended over the depth is an assumption, not a measured full trench cross-section. Poly-Si uses a bulk-Si optical constant proxy.
- OCL: inferred elliptical cap constrained by reported height and vertex radius. Measured full sag is unavailable.
- Absorption is split between active Si, trench, CFA, lens and other layers. Trench absorption is never counted as active carrier collection. Si continues into the lower PML.
- Material dispersion is fitted at the task's single wavelength. It must not be reused as a broadband material fit.
- Energy, mesh, time, PML, passivity and support-tail checks are separate. Thresholds: energy 1%, mesh/control 5% for 2D or 3% for 3D, guard tail 1.5%. Negative local dissipation is recorded and can prevent qualification; it is not silently converted into validation success.
- The original signed absorption map is retained alongside a nonnegative visualization map. Visual clipping is not evidence of passivity.

## External 3D extension

Set `dimension=3`. The local worker exports a frozen package and returns `awaiting_external_3d`; it does not allocate the large 3D grid locally. Run `python run.py` in a Meep + NumPy + SciPy environment on a suitable host. Keep manifest, task and worker files unchanged. Copy the whole directory back under `/Volumes/T7/CameraE2E` and use the UI import or:

```python
job = client.import_sensor_structure_results(export_job_id, external_directory)
result = client.wait(job['id'], timeout=86400,
                     expected_kind='sensor_structure_study')
```

Import checks source job, structure revision, worker, every task, array hashes, coordinate axes and power accounting. 3D is a periodic 2×2 Bayer plane-wave response at phi=0, not a spatial crosstalk kernel. It does not characterize arbitrary spatial illumination or a full pupil angular distribution.

`camera_platform.sensor_structure_response.photoelectron_rate` accepts qualified 3D samples, optical-image photon irradiance (photons/m²/s/nm), explicit spectral widths, exact wavelength/angle nodes and declared collection efficiency. It replaces QE, multiplies pixel area once and preserves native R/Gr/Gb/B origin. `capture` feeds this rate to the existing electrical RAW and fixed ISP functions. Unsupported interpolation and 2D promotion are rejected. No actual qualified external 3D result has yet been demonstrated by this feature; RAW/ISP end-to-end qualification remains pending that result. Existing `fdtd_estimate_v1` interpolation gates are not bypassed by this exact-node adapter.

## Evidence and delivery status

Unit tests exercise source correction, immutable catalog identity, invalid settings, cache identities, missing samples, failed controls, negative-power preservation, checksum failure and exact-node conversion. These are contract tests, not measured sensor validation.

Actual solver pilot outputs live under `/Volumes/T7/CameraE2E/sensor-structure-development`. Early pilots are retained, including failed conservation/passivity results. Only complete source-frozen study jobs should be used as comparison packages. The UI labels incomplete or numerically unqualified results explicitly; no simulated result is marked physically validated.

Absorbed power follows the E/D dissipation formulation in the [official Meep example](https://github.com/NanoComp/meep/blob/master/python/examples/absorbed_power_density.py). Component-specific Yee coordinates are retained; display interpolation is separate from the power integral.

## Trend screening v1 (2026-10-18)

`trend_screening_v1` evaluates matched baseline/variant differences independently
for total Si absorption, central absorption, neighbor fraction, left/right
asymmetry and centroid/pitch. U is the largest observed change in the paired
difference across numerical settings, **not** a confidence interval or error bound.
Default meaningful changes are relative 1% for absorption, 0.1 percentage point
for neighbor/asymmetry and 0.01 pitch for centroid. Below 1% incident-power
baseline absorption, signal differences use absolute units instead.

Magnitude-stable requires U <= 0.2 abs(D); direction-stable requires abs(D) > 2U.
Missing controls, sign reversal or invalid numerical data remain unresolved.
Energy residual <= 1%, passivity and guard absorption <= 1.5% are retained.
The original strict assessment remains separately visible. No trend result
qualifies a 2D model for 3D QE, spatial kernels or camera RAW prediction.

POST `/api/v1/sensor-structure-studies/{id}/trend-assessments` freezes hashes of
complete samples and reevaluates them without Meep. POST the same prefix with
`/trend-studies` to resume missing samples and paired adaptive controls. Body
`{"policy": {}}` selects defaults; saved summaries include the full policy and
its hash. SDK: `reassess_sensor_structure(id, policy)` and
`submit_sensor_structure_trend(id, policy)`. Standard wait/cancel/download apply.

60/80 grids and high-grid time/PML/half-cell controls are required. Selective
100-grid and neighborhood-7 controls are requested by the evaluator. The separate
phase worker translates geometry, source and monitors together by half an x cell;
this is not a new microlens offset. Fixed PML boundaries also make this a small
boundary-clearance perturbation. Original worker bytes and cached results stay
unchanged. Numerical sample identity excludes assessment thresholds.

A run reserves the existing external storage budget and executes one solver at a
time. Its eight-hour allowance includes elapsed solver time represented by reused
samples plus the new execution wall time. Budget exhaustion publishes partial
results as unresolved without decreasing resolution. Historical failed attempts
that have no saved timing cannot be retrospectively reconstructed.

Completed JSON/NPZ pairs are validated and atomically published, NPZ first and
JSON last. Cancellation polling uses a short read-only status query, tolerates
transient DB contention, and fails after a bounded 60-second outage while
preserving completed samples. It does not decode the entire frozen job graph.

UI: absolute response / paired difference / observed sensitivity, separate s/p
and unpolarized values, required checks and three independent qualification
boundaries. The downloadable sample manifest points to hash-bound SSD originals.

Interrupted adaptive attempts consume a persistent discarded-time ledger. A worker
terminated before closing its ledger conservatively exhausts the allowance on
resume. Changing policy thresholds cannot reset that ledger.

## Separate OCL camera sensitivity demonstration

`/sensor-structure-camera.html` connects the stable OCL central-response ratios
to an explicitly assumed local gain on an existing IMX662 camera mean response,
then performs fresh integer RAW acquisition and fixed ISP processing. This does
not change the 2D qualification boundary above: it is a hypothesis-driven
sensitivity experiment, not a derived camera QE or spatial kernel. Green-only
and common-channel extensions, virtual field angle mapping, readout proxies and
fixed color calibration are displayed separately. See
[OCL camera sensitivity](camera-platform-ocl-camera-sensitivity.md) for contracts,
source hashes, SDK entry and limitations.
