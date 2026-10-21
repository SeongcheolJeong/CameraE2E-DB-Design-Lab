# Electrical readout, periodic light and RAW HDR

This source milestone is `0.16.0.dev1`. It adds a composed `hdr_bracket_v1`
runtime to the existing image-plane optics and timing model. The installed
primary remains v0.15 until a separately qualified transition. Passing the
software checks below does not establish physical-camera validation.

Historical-version note: the scope table below describes the dev1 independent-exposure
milestone. Later source includes [shared-charge DCG](camera-platform-dcg.md); its absence
from this table is not a current platform limitation. The proposed next electrical/HDR
extension is [nonlinear readout with RAW inverse calibration](camera-platform-nonlinear-readout-next-slice.md).

## Current scope

| Module | Implemented contract | Boundary |
|---|---|---|
| `electrical.py` | Independent full well, conversion gain, analog gain, digital gain, ADC scale/pedestal, read/post-gain noise, temperature dark law, persistent native PRNU/DSNU | Independent reset per exposure; no shared-charge DCG or transistor/charge-transport model |
| `periodic_light.py` | Constant or PWM waveform, exact integer ON durations and analytic linear/ZOH irradiance integration | A uniform multiplier on the complete declared image; no separately inferred ambient component |
| `hdr.py` | Censored inverse-variance fusion from actual integer RAW and explicit calibration; signed electron-rate and floating RGB; separate Reinhard preview | Static aligned target; variance is an estimate; no deghosting or calibrated total uncertainty |
| `hdr_runtime.py` | Full-sequence timing validation, existing PSF/RI/effective-QE response, per-frame electrical capture/ISP, optional HDR | General lens/pixel transport and arbitrary ISP graphs are still open |
| `capture_programs.py` | Immutable assembly-bound electrical/light/HDR program with source and normalized artifacts | Explicit model assumptions, draft authority; saving a model does not validate it |

An applied frame's `gain` is the **analog gain** in this runtime. Digital gain
belongs to the fixed electrical program. The legacy sensor readout supplies the
initial draft and RAW format; the explicit program electrical model determines
the new capture. Optical effective QE already includes CFA, microlens and
collection effects. Those factors must not be multiplied again.

## Readout and calibration

The declared chain is photocharge × persistent PRNU plus temperature-adjusted
dark charge → Poisson draw → full-well clipping → input read noise → conversion
gain and analog gain → post-gain voltage noise → ADC rounding/clipping → digital
gain about the black pedestal → final rounding/clipping. Disabling temporal noise
preserves fixed patterns. Each frame has an independent temporal seed; the sensor
pattern seed and full native geometry preserve patterns across exposures/crops.

HDR consumes RAW and nine calibration fields: `dn_per_e`, `black_level_dn`,
`white_level_dn`, `full_well_e`, `dark_e`, `read_noise_variance_e2`,
`quantization_variance_e2`, `linear_floor_dn`, and `linear_ceiling_dn`.
The two linear bounds account for hidden full-well/ADC clipping after gain.
For example, digital attenuation can move ADC-zero clipping to a positive final
RAW code. Rejecting only RAW zero would incorrectly admit that clipped sample.
The calibration bounds are model constants, not a sampled saturation mask or
the noiseless simulated scene.

The signed rate is `((RAW-black)/dn_per_e-dark_e)/exposure_s`. Shot-noise
variance is estimated from RAW, with supplied read and quantization terms.
The inverse-variance result includes a validity mask and separate temporal
consistency/unknown masks. One usable exposure cannot establish consistency.
All-invalid rates/variances remain NaN with false masks; only the display preview
uses black. Exported `.npy` files retain these values. JSON summaries are finite.
`linear_rgb` stays in electron/pixel/s, before display scaling and clipping.

## LED/PWM and HDR are different operations

Waveforms have an explicit `clock_id`, integer `period_ns`, `on_time_ns` and
`phase_origin_ns`, and nonnegative `low`/`high` levels. The runtime verifies the
waveform clock against both scene and applied-control clocks. Integration uses
half-open native row windows and integer cycle/moment arithmetic, including at
large timestamps. It does not approximate PWM edges with sampled sine knots.

With `hdr_merge: null`, arbitrary supported scene changes, rolling rows and PWM
phases produce per-frame RAW, effective exposure and clipping diagnostics.
This supports LFM investigations; it is not a vendor LFM implementation.

With HDR enabled, there must be at least two independent exposures of one exact
static scene. A PWM exposure must span an integer number of full periods, so
all exposures/rows estimate the same cycle-average rate. Partial-period HDR is
refused. In particular, dividing mixed ambient-plus-LED RAW by LED ON time does
not recover a common radiance. There is no such correction in this runtime.

## API and Python SDK

1. Select an assembly with an explicit pixel-integrated PSF, register a timed
   spectral scene, and register its applied control sequence. Existing Factory
   controls provide these inputs. Choose a static scene for HDR.
2. `GET /api/v1/templates/capture-program?assembly_id=...` returns the authored
   electrical/HDR draft. Review parameters/provenance; set `hdr_merge` to null
   for phase-dependent LFM diagnostics. Register via `POST /api/v1/capture_programs`.
3. Request `/api/v1/templates/temporal-test` with `assembly_id`,
   `scene_sequence_id`, `control_sequence_id`, and `capture_program_id`; register
   the returned test. It pins exact record hashes and the sampled RAW metric.
4. Submit `/api/v1/runs` with `plugin_id: hdr_bracket_v1`, those IDs and `test_id`.
   The seed and optional `field_radiometry_mode=source_position_ri_v1` select
   the calculation; an optional separate unmodulated scene and receiver crop
   must use their pinned Test contract. A changed program needs a new test and
   yields a different cache identity.

```python
from camera_platform.sdk import PlatformClient

# Use an explicitly chosen development/qualified server and registered IDs.
with PlatformClient(base_url) as client:
    program = client.create_capture_program(program_draft)
    test = client.post("tests", client.template(
        "temporal-test", assembly_id=assembly_id,
        scene_sequence_id=scene_id, control_sequence_id=control_id,
        capture_program_id=program["id"],
    ))
    queued = client.submit_hdr(
        assembly_id, test["id"], scene_id, control_id, program["id"], seed=42,
    )
    run = client.wait(queued["id"])
```

Results retain original RAW/ADC codes, electrical clipping diagnostics, timing,
waveform row integrals, fusion weights/rates/masks, floating RGB and preview.
Noiseless simulation arrays are separately labeled as simulation oracles and are
not supplied to HDR fusion. The validation metric is the sampled dark-corrected
RAW channel mean, not a continuous expected DN or a fused-HDR accuracy claim.
The evidence bundle includes the fixed program and source artifacts. Existing
RAW ISP replay reprocesses each original bracket frame and retains its timing;
it does not re-run or claim to reproduce HDR fusion.

The optional source-position RI path weights each preserved native source
cell/subcell before PSF and then integrates each temporal optical rate through
the declared row/PWM windows. If two illumination components are supplied,
both must share one exact source grid and RI map. With HDR fusion enabled,
subcell-only changes also violate the static-scene gate. This is discrete
calculation evidence, not a measured LFM/HDR accuracy claim; see the
[source-position temporal contract](camera-platform-temporal-source-position-ri.md).

The default preview uses an authored extended Reinhard white point of 4 and
display scale of `0.00001 / electron` at a 0.01 s reference exposure. These are
display choices, not sensor calibration. The same retained evidence can be
inspected offline with `tools/plot_camera_hdr_investigation.py BUNDLE.zip OUTPUT.png`.

## Verification and next model priorities

Run the new numerical and integration tests with:

```sh
.venv-platform/bin/python -m pytest tests/platform/test_periodic_light.py tests/platform/test_electrical.py tests/platform/test_hdr.py tests/platform/test_hdr_runtime.py tests/platform/test_hdr_workflow.py -q
```

Root evidence is recorded under `reports/camera_platform/hdr-electrical-v016`.
An older v0.15 package proof does not cover this source change.

The user's priority is model capability. Continue with general optical/pixel
transport and charge collection, correlated/shared-charge gain paths and motion
aware HDR/LFM, configurable advanced ISP, and sensitivity/tolerance plus
multi-objective optimization. Independent environment adoption remains open but
is deferred behind this domain work. Preserve the original seven modules and
four execution modes; this bounded milestone does not close their full scope.

Method references: the [EMVA 1288 linear model](https://www.emva.org/wp-content/uploads/EMVA1288Linear_4.0Release.pdf)
informs separation of gain, saturation and noise terms; [HDR+](https://www.hdrplusdata.org/)
illustrates processing RAW fusion separately from final rendering. Neither
EMVA certification nor the full HDR+ pipeline is implemented here.
