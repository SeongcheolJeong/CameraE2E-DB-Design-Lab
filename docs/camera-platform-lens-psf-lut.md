# Patent Lens optical PSF Library

`/lens-psf-lut.html` generates and reopens source-bound optical response LUTs.
Virtual Lab links to it. A LUT is a durable `lens_optical_psf_lut` Registry job;
its completed manifest and intensity archive are content-addressed artifacts.
The original patent/source DB revisions are retained without modification.

## Contract and optical meaning

`camera_lens_optical_psf_lut_v1` selects an execution-ready patent prescription,
applied physical iris, fixed authored image plane, exact wavelengths, object
gnomonic field bounds, optical grid, and adaptive interpolation policy. Initial
sources are Canon p0014 and p0248, using the existing reviewed prescription
adapters; this does not make every patent DB entry executable. p0248 retains
the patent F/4.12 at587.6nm derived physical iris; source-reported, derived,
and applied pupil values and material assumptions are recorded separately.

RayOptics traces admitted pupil rays and OPD. The existing reference-sphere
scalar angular spectrum is propagated with Prysm. Stored intensity samples
have unit1/µm² and contain **no physical receiving-pixel area integral**. The
finite optical grid is a numerical quadrature, not an assumed sensor pixel.
An exact550nm chief reference preserves chromatic displacement on the same
sensor plane at other wavelengths. Physical +Y points upward; array rows down.
Centered rotational symmetry permits reuse of physical ray bundles at equal
radial field angles; the complex pupil is rotated before propagation.

Each node compares physical pupil density, direction-grid density and optical
sample spacing independently, with the existing3% normalized L1,95% retained
energy,0.02-wave OPD and0.25-wave adjacent phase gates. Exhausting the physical
ray ladder refuses generation; no geometric/analytic PSF replaces it. Absolute
E/L throughput/RI is **not** encoded in unit-normalized PSF intensity and must
come from the matching reciprocal lens-radiometry response. This first library
stores optical shape and chief geometry, not a complete radiometry or 3D scene
renderer. Polarization, coating-amplitude and coherent sensor-angle response
are outside this scalar model.

## Field grid and adaptive refinement

`initial_grid_xy=[3,3]` means nine object-direction nodes, not nine pupil rays.
The coordinates are `u=tan(horizontal direction)`, `v=tan(vertical direction)`;
the actual radial angle is`atan(hypot(u,v))`. Sensor-plane chief positions are
stored separately. An even initial grid additionally gets the optical axis.
`half_field_deg_xy` expresses horizontal/vertical half extents. Source image
height is checked before PSF work.120° UI capability does not expand a patent's
supported field; no source extrapolation is performed.

For every cell, four corner PSFs predict the PSF at its center and four edge
midpoints by bilinear interpolation. Those five independent positions are
actually solved and compared at **every** stored wavelength. The error is
`sum(abs(predicted-actual))*optical_sample_um²`. Cells beyond the declared
normalized outer radius use a stricter tolerance (`outer_tolerance_scale`).
Failed cells split into four until the configured level/node budget. This is
an observed five-probe interpolation check, not a global error bound between
probes. A budget/depth-limited cell remains unresolved, and runtime refuses
interpolation there. An independently qualified exact node remains reusable.
No spectral interpolation is claimed: the runtime requires a stored wavelength.

## Sensor adapter and reuse

`integrate_pixel()` applies rectangle-overlap integration of the optical
quadrature, with explicit receiving pitch, kernel size and source subpixel
offset. It applies the aperture once, retains the mass/finite-support loss and
does not renormalize away a cropped tail. A kernel retaining less than95% is
rejected. At least two optical quadrature samples per pixel are required; this
is a bounded adapter policy, not a universal accuracy proof. A new sensor with
insufficient optical sampling requires a finer optical LUT. CFA/QE/readout are
downstream of this operation. The same optical density hash is used at every
supported pitch; no ray tracer is instantiated by sampling.

`apply_local_radiance()` convolves a monochromatic radiance plane on the optical
grid before receiving area integration. Global scene geometry/field variation
must be handled by a caller using the field cells and halo. This helper does
not automatically install the new LUT into every legacy CameraE2E branch or
silently replace an existing sensor-integrated PSF.

## API and SDK

- `GET /api/v1/lens-psf-luts/template`
- `POST /api/v1/lens-psf-luts` (common Registry/Worker submission)
- `GET /api/v1/lens-psf-luts` and`/{id}`
- `POST /{id}/sample` or`/{id}/sample.npz` (field/wave/pitch integration)
- `POST /{id}/cancel`, `GET /{id}/bundle`
- Common`GET /api/v1/jobs/{id}` and SDK`wait()` retain typed result dispatch.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient('http://127.0.0.1:8780', timeout=600) as c:
    spec = c.lens_psf_lut_template()['specification']
    job = c.submit_lens_psf_lut(spec)
    result = c.wait(job['id'], timeout=21600, poll_interval=10,
                    expected_kind='lens_optical_psf_lut')
    assert result['status'] == 'succeeded', result.get('error')
    kernel = c.sample_lens_psf_lut(result['id'], angle_deg_xy=[0,0],
                    wavelength_nm=550, pixel_pitch_um=2.9, kernel_size=129)
    assert kernel['ray_tracing_calls'] == 0
```

`tools/run_lens_psf_lut.py` supplies the same SDK entry point. Cached node files
are keyed by lens revision/source/applied prescription, exact optical request,
backend and solver hashes. A second named submission at the same specification
reassembles the DB artifact from verified node hashes without tracing. Noisy RAW
is not involved. Cancelling retains completed node caches for a subsequent job.
The existing T7 project500GB policy/free-space checks are retained. The current
16GB decoded disk-LUT policy and node/level limits are explicit software
resource policies, replacing the earlier2GB in-memory library policy.

## Verification

Tests cover analytic Airy point intensity, sensor rectangle integration and
energy, fractional position, independent sensor pitches, adaptive nonlinear
fields, unresolved-cell refusal, convolution, actual Registry persistence,
reopened/cache-only sampling, and same-size artifact/cache corruption. Numerical
qualification is separate from measured lens or product validation.

## Refused fields and the local camera adapter

A physically refused node is retained with its reason and an explicit refused
status. Its zero-filled archive slot is storage only and can never be sampled
as a PSF. Adjacent cells refine around the hole or remain unresolved. The initial
12°×8° preparation exposed the existing reference-sphere backend's star-convex
pupil limitation at one650nm condition; that failed job remains preserved.
The prepared smaller field is a new explicit configuration, not a silent drop
in spatial accuracy. Existing source valid-field checks still apply.

Completed optical nodes are additionally cached under optical parameters and
source/backend hashes independent of field-refinement policy. Changing a field
grid/tolerance can therefore reuse matching physical nodes. A refused node also
retains its exact failure evidence. A changed optical solver or physical iris
gets a different cache identity.

`lens_psf_lut_sensor.evaluate_local_sensor_rate()` supplies the camera input
adapter: high-density spectral radiance → stored optical convolution → receiving
area integration → absolute QE → four-site and RGGB mean electron rates. It
requires absolute radiometry bound to the same source revision and applied
prescription hash, an explicit wavelength integration width and enough optical
halo. No noisy/clipped RAW is scaled. Its output feeds the existing electrical
capture and fixed ISP functions. Sensor angle response remains the caller's
effective QE approximation, not a new joint coherent pixel-structure solver.

## Prepared actual result · 2026-10-17

[Open the retained Lens PSF Library demo](http://127.0.0.1:8780/lens-psf-lut.html?run=run-70eb335446fb4a88b1253c7f378dff07).

- Lens: Canon US20200174235A1 p0248, patent-derived F/4.12 physical iris;
  revision`4daeca8f-5d81-57b0-8ae3-928f23de2b36`.
- Explicit horizontal6° × vertical4° field;450/550/650nm;769² optical samples
  at0.5µm spacing. This is a prepared subfield, not a full patent lens field
  or a120° prescribed lens qualification.
- Initial3×3 grid, adaptive level1,75 attempted field positions.71 positions
  have qualified three-band optical PSFs (213 usable PSFs);4 positions are
  refused by the pupil backend and retained as coverage holes.
- Four of16 final cells meet the configured interpolation checks. Eight
  need more refinement under the outer4% L1 rule; four include refused
  physical nodes. These12 cells refuse arbitrary field interpolation. Exact
  qualified stored nodes remain usable across the prepared domain.
- First generation`run-d0cbf86392754f869bb5bb5d8c61b16c` traced132 physical
  pupil bundles and took916.01s including storage checks. Reassembly
  `run-70eb335446fb4a88b1253c7f378dff07` reused all75 node records, generated
  zero new fields and traced zero bundles (116.69s including storage scans,
  archive reconstruction and publication). Camera latency is not inferred.
- Both runs retain identical optical archive SHA-256:
  `68638127887c62bca135b6940a13b512fed122a5b87da0b212499d19f39229fc`.
- The same550nm optical response was integrated at1.45/2.9/5.8µm pitches,
  retaining99.9928/99.9937/99.9955% conditional energy with matched support.
  The new2.9µm receiving kernel's shape L1 difference from the retained
  independently integrated historical diffraction kernel is0.0260644.

The local camera adapter was also executed using retained matching absolute
radiometry, IMX462 structural QE/readout and a declared three-band synthetic
edge. Same185.6µm physical ROI and4ms exposure yield native128²/64²/32² RAW
and fixed-ISP outputs at the three virtual pitches. These are virtual geometry
studies using the same readout/QE, not three sensor products or a color-accuracy
evaluation. RAW originals and reproduction scripts are in
`outputs/lens-psf-lut-20261017/` alongside`verification.json`,
`http-verification.json`, the actual calculation figure and browser proof.

51 related API/optical/Registry/sensor tests pass. HTTP checks verify the common
lifecycle, actual three-pitch reuse, NPZ and manifest download, unchanged optical
hash and422 refusal for unsupported wavelength, insufficient optical sampling
and unresolved field interpolation. The original failed broad-field attempt
`run-ec455f9ff94d4ccfa9b5fc8937a180b5` and its source evidence are retained.

## IMX462 full-field extension · 2026-10-17

The first prepared6°×4° partial library remains immutable. The new generation
uses horizontal13.02° × vertical7.40°, adaptive depth4 and1536 field-node budget.
The central8% and outer4% interpolation L1 limits,3% optical-refinement limit,
95% retained energy,0.02-wave OPD and0.25-wave phase gates are unchanged.

The old `Non-star-convex pupil` refusal at450nm was traced to an unconverged
chief aim. At a1.6774236° field, the backend returned an8.2039mm aim whose
actual stop intercept missed the center by18.8098mm. Increasing pupil sampling
could not repair the incorrect ray launch. `aim_at_stop()` now verifies the
actual stop intercept and uses a bracketed meridional solve when necessary.
A trace failure beyond the stop retains the already-computed stop coordinate;
it is never treated as a zero residual. Corrected chief rays must reach the
stop center within1e-6mm and pass all physical apertures. The original boundary
and non-star-convex-pupil checks remain in place.

Large libraries now use a disk-backed NPY archive, memory-mapped runtime reads
and streamed ZIP64 downloads. The explicit library budget is16GB decoded on
storage, within the existing500GB project policy; it is not a hardware limit.
Small libraries retain their compressed NPZ format. Source-bound canonical
quadrant nodes are reused by exact reflection of centered rotational optics.
The pupil and optical spatial sampling are unchanged. Optional process-local
solvers (`CAMERA_LUT_PROCESSES=1..4`) parallelize independent physical nodes.
Each process retires after four nodes to bound long-lived native solver heap
growth. Verified node files survive interruption and process replacement.
No sensor receiving aperture is included in either archive format.

`lens_psf_lut_camera` is a separate durable acquisition job. It verifies the
retained optical source and inversely traces550nm chief geometry to the actual
1937×1097 active-area coordinates at2.9µm pitch. The active perimeter is checked
at68 points. This identifies approximately13.0128° ×7.3912° as the required
field, including the modeled geometric distortion. Sensor-family active-array
information is retained separately from the TechInsights structural model;
exact report-subtype and product-mode correspondence remains unverified.

Nine native128×128 probes are calculated at center, cardinal and corner
positions. Each uses fine-grid three-band radiance, the stored optical PSF,
receiving-area integration once, actual reciprocal radiometry at the selected
image height and pupil-averaged planar sensor QE. Independent4ms RAW captures
and the same fixed sensor-RGB ISP are retained with seeds, native CFA origins,
source hashes and assumptions. The response is locally stationary within each
probe: this demo is not a full-frame rendering. The chart uses three10nm bands
at450/550/650nm and is not a D65/Macbeth color-accuracy measurement. Readout and
material proxies are visible alongside acquired structural facts.

API/SDK additions:

- `POST /api/v1/lens-psf-cameras` with `lut_run_id`, optional exposure/gain/seed.
- `GET /api/v1/lens-psf-cameras`, `/{id}`, `POST /{id}/cancel`.
- `PlatformClient.submit_lens_psf_camera(lut_run_id, exposure_ms=4)` and common
  `wait(id, expected_kind="lens_psf_lut_camera")` return typed evidence.
- `tools/run_lens_psf_camera.py --lut-run <id>` repeats the acquisition without
  regenerating optical PSFs. Reciprocal radiometry uses hash-checked caches.
- The Library page shows native RAW/ISP, temporal repeat noise, optical MTF at
  sensor Nyquist, source classification and downloadable original arrays.

Full-field camera submission refuses a partial interpolation library. Completed
camera packages retain the full original LUT hash and a separate camera job ID.

The even-sized central probe on the odd-sized active array is slightly off
axis (about2.05µm radial image height). The old uniform rear-disk quadrature
failed the existing3% absolute irradiance convergence gate there. The camera
adapter now supports a complete-disk nonuniform radial quadrature with edges
`(i/N)^2` and exact annular-area weights. It retains the original enclosing
disk, ray/intersection operator, physical apertures and transmission; it does
not trim support to obtain convergence. Each result retains its coarse/fine
grid,3% irradiance/RI and0.05 angular-CDF agreement checks. This is a numerical
sampling repair, not a new lens or a relaxed physical acceptance criterion.
The bounded camera quadrature adapter has a600s execution budget per wavelength
pair; increasing this resource budget does not change the convergence gates.

### Completed full-field library

`run-8cec0e30b1f34d8ea40cb9afc53362f7` is the completed13.02° ×7.40° library:

- 689 qualified field responses ×3 wavelengths =2067 optical PSFs; zero refused
  nodes and zero unresolved cells.
- 148/148 leaf cells qualify:4 at level1,16 at level2 and128 at level3.
- Maximum observed inner-cell L1:0.070601 under0.08; outer-cell L1:0.027161
  under0.04. These are field-shape interpolation checks, not MTF error bounds.
- Maximum optical-grid/pupil refinement L1:0.005492 under0.03. Minimum retained
  conditional energy:0.997488. Maximum OPD interpolation error:0.005430 waves;
  maximum neighboring phase increment:0.036264 waves.
- 181 unique canonical quadrant nodes supply the689 response positions by
  verified centered-system reflections. The resumed job reused668 node calls
  and generated21 additional canonical nodes. The earlier160 canonical nodes
  remain in the source-bound cache.
- The optical NPY archive is4,889,372,876 bytes. The resumed calculation and
  storage preflight took457.74s before final artifact publication, after the
  earlier interrupted preparation. This is not a from-scratch runtime claim.

The interrupted memory-growth run`run-0159b844011f42aba22225ad72fe3f8e` is
retained as cancelled. No tolerance, optical grid, prescription or source
binding changed when resuming it. The earlier partial6°×4° artifacts remain
available separately.

### Completed native RAW / ISP demonstration

[Open the full-field library and native camera result](http://127.0.0.1:8780/lens-psf-lut.html?run=run-8cec0e30b1f34d8ea40cb9afc53362f7&camera=run-66b2bbbe82e7436ba7ef7a4145f9c7c9).

Camera job`run-66b2bbbe82e7436ba7ef7a4145f9c7c9` contains nine native128²
probes, four independently noisy4ms/gain1 captures per probe (36 integer RAW
arrays), four-site mean electron rates, fixed-ISP arrays and PNG previews.
The chart photon radiance scale is explicitly1e17 photons/s/m²/sr/nm over
three10nm bands. No frame-wise brightness normalization is applied.

| Probe | Mean photoelectrons | Temporal noise DN | Dark flat-patch SNR | Optical550nm MTF at native Nyquist X/Y |
|---|---:|---:|---:|---:|
| Center |287.82|4.98|4.26|0.395/0.395|
| Outer NE |283.52|4.88|4.30|0.375/0.392|

These numbers describe the declared synthetic input and assumed readout, not
a measured Sony product ranking. All nine probes have zero ADC clipping in
this exposure. The near-axis reciprocal integral now meets the unchanged3%
gate (maximum observed relative change2.234%); the complete rear disk is kept.

The camera run took91.09s through capture and report creation, including the
storage preflight and prepared reciprocal-response reuse, before the final
storage check/publication. The downloaded camera ZIP is6,530,087 bytes. The
689-field optical archive is shared without generating new optical PSFs.

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_lens_psf_camera.py \
  --lut-run run-8cec0e30b1f34d8ea40cb9afc53362f7 --exposure-ms 4
```

Evidence is retained in`outputs/lens-psf-lut-imx462-20261017/`:
`delivery.json`, `camera-delivery.json`, `verification.json`, the downloaded
`camera-bundle.zip`, regression logs and browser captures. HTTP readback checks
all36 RAW arrays, every field's mean response/ISP/preview hashes, three-pitch
reuse at an interpolated outer field, and the unchanged optical density hash.
Sixty related optical/lifecycle tests and the added full-disk quadrature test
pass; the final eight connection/storage tests were also rerun after fixes.
