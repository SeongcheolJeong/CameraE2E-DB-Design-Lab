# Temporal image-plane capture runtime

`temporal_scene_v1` integrates a declared spectral-image timeline into native
sensor row exposure windows, then performs one pyisetcam digital readout and one
configured software ISP execution for each frame. It produces changing RAW and
RGB arrays. It does not run a 3D renderer or an automatic exposure controller.

This is implementation evidence for an explicitly bounded image-plane model.
The supplied fixtures are authored synthetic inputs. No measured accuracy or
quantified physical model uncertainty is established.

## Service, SDK and evidence

The local service stores immutable `scene_sequences` and `control_sequences`.
Scene records snapshot every referenced spectral image; control records bind
the assembly and exact scene sequence. Each has original UTF-8 JSON and a
normalized contract artifact. Authored fixtures also preserve their full recipe.

The UI registers these records in Model Factory and selects both in Experiments.
It then creates a matching Test Specification and queues a `temporal_scene_v1`
CameraRun. Source artifacts, model versions and per-frame validity domains are
frozen in the job. Source or environment drift requires an API restart.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient() as platform:
    # A registered camera containing an explicit PSF Lens is required.
    scene = platform.create_temporal_fixture(platform.template(
        "temporal-fixture", assembly_id=assembly_id))
    control = platform.create_control_sequence(platform.template(
        "control-sequence", assembly_id=assembly_id, scene_sequence_id=scene["id"]))
    test = platform.post("tests", platform.template(
        "temporal-test", assembly_id=assembly_id, scene_sequence_id=scene["id"],
        control_sequence_id=control["id"]))
    run = platform.wait(platform.submit_temporal(
        assembly_id, test["id"], scene["id"], control["id"], seed=42)["id"])
```

The default PSF publication retains every original application condition.
Fresh publication may explicitly opt into `condition_scope: optical_response`:
exposure/gain no longer belong to the linear optical response's fixed conditions,
but parent Lens, Sensor and each actually applied ISP validity domains still gate
every frame. This never widens an already fixed parent. The declared PSF pupil
F-number is stored separately and conflicting explicit aperture requests fail.

Temporal comparisons require exactly matching clocks, ordered frame IDs, starts,
applied timestamps, exposure, gain and acquisition ISP IDs. Static optical
conditions remain separate from per-frame conditions. Unknown uncertainty remains
`INCONCLUSIVE`; incomplete frame-domain declarations cannot fall back to an
apparently broader global domain. A replay keeps original acquisition records
and a separate `replay_isp_model_id`.

Service summaries expose `frames` as an ordered list with `frame_index`, settings,
timing, `preview_artifact_id` and `preview_url`, and a separate `frame_count`.
The pure runtime below uses numeric `summary.frames` and `summary.frame_ids`;
the service enriches it with persistent artifact identities.

## Pure runtime interface

`camera_platform.temporal.TemporalSceneRuntime` implements
`describe_capabilities`, `validate_request`, `estimate_resources`, and `execute`.
`request_template()` returns a runnable authored moving-step example.

```python
from camera_platform.temporal import TemporalSceneRuntime, request_template

request = request_template()
plugin = TemporalSceneRuntime()
assert plugin.validate_request(request) == []
result = plugin.execute(request)
raw = result["arrays"]["raw_frames"]       # integer [frame,row,column], DN
rgb = result["arrays"]["rgb_frames"]       # float [frame,row,column,RGB]
frame_ids = result["summary"]["frame_ids"] # same order as both arrays
```

The top-level optical/readout fields are the existing spatial contracts:
`wavelength_nm`, `qe`, `pixel_pitch_um`, `readout`, `lens`,
`sensor_angular_response`, and `psf`; `temperature_c`, `seed`, and
`noise_enabled` are optional. Temperature and the optical models remain fixed.
There is no top-level exposure, frame count, image resizing, or ISP override.
The additional required fields are:

| Field | Contract |
| --- | --- |
| `scene_sequence` | Expanded `timed_spectral_scene_v1`, with explicit clock, interpolation, and `{time_ns,scene}` knots |
| `control_sequence` | `recorded_control_sequence_v1`, with fixed shutter mode, unique frame IDs, starts, applied settings and optional commanded settings |
| `isp_profiles` | Mapping from every referenced applied **or commanded** ISP model ID to its validated `camera_isp_profile_v1` payload; extra unresolved IDs are refused |

Registry-facing timelines use immutable `spectral_image_id` references. The
service resolves each reference to the existing `spectral_ideal_image_v1` scene
contract before calling the pure runtime. Every expanded scene must have the
same exact wavelength axis, pitch, image shape, and native full-sensor/crop
geometry. The irradiance unit is `W/m^2/nm`, at
`sensor_ideal_image_before_psf_ri`; on-axis lens throughput is already included.
RGB samples are never treated as a spectrum.

See [timing contracts](camera-platform-timing.md) and
[spatial input and PSF contracts](camera-platform-spatial.md) for the complete
field requirements. A compact timing fragment is:

```json
{
  "schema": "recorded_control_sequence_v1",
  "clock": {
    "clock_id": "capture-clock-1",
    "unit": "ns",
    "reference": "relative",
    "origin": "Explicitly declared acquisition reference event"
  },
  "mode": {
    "shutter": "rolling",
    "line_time_ns": 10000,
    "hidden_top_rows": 2,
    "readout_basis": "full_sensor_rows"
  },
  "frames": [{
    "frame_id": "frame-0042",
    "frame_start_ns": 10000000,
    "applied": {
      "applied_at_ns": 9900000,
      "exposure_ns": 4000000,
      "gain": 1,
      "isp_model_id": "immutable-isp-model-id",
      "command_id": null
    }
  }]
}
```

The clock and origin must match the scene timeline exactly. Exposure windows
must lie wholly inside its declared support. No extrapolation occurs. Missing
command records and optional completion/transport/application times remain
unknown. Recorded completion times refer to the full frame; image availability
and exposure integration are checked for the simulated crop. Runtime wall time
does not fill missing hardware timestamps.

## Physical integration and settings

For each distinct scene, `spatial.photoelectron_rate(request)` validates the
input and computes the PSF convolution, radial relative illumination, angular
sensor response, QE and wavelength integral. It returns collected photoelectrons
per pixel per second without exposure, gain, dark charge, noise, clipping or
readout. This helper accepts the spatial optical/scene/readout fields while
refusing `exposure_s`, `frames`, and `isp_profile`. It imposes no artificial
one-second charge limit; the temporal runtime checks actual integrated charge
before engine sampling.

The shared spatial helper is the only optical path. The runtime does not call
`camera_compute_temporal`, which would repeat optics and could resize the image.

For an interval from integer nanoseconds `L` to `R`, let `a=max(row_start,L)`
and `b=min(row_end,R)`. An empty overlap contributes zero. A linear irradiance
interval contributes:

```text
w_right_seconds = (b-a) * ((b-L)+(a-L)) / (2*(R-L)) * 1e-9
w_left_seconds  = (b-a) * 1e-9 - w_right_seconds
charge += rate_left * w_left_seconds + rate_right * w_right_seconds
```

`zero_order_hold` instead uses the left rate throughout `[L,R)`. All declared
knots are included, including one-nanosecond pulses; there is no uniform sampling
parameter. Integer subtraction precedes floating-point conversion, preserving
short intervals even at large supported relative timestamps.

For rolling shutter, local crop row `r` starts at:

```text
frame_start_ns + (hidden_top_rows + crop_origin[0] + r) * line_time_ns
```

For global shutter, all rows start at `frame_start_ns`. The only supported
readout basis is the declared full-sensor row numbering. A native crop is not
silently interpreted as an accelerated region-of-interest sensor readout mode.

Only each frame's **applied** exposure, gain and ISP ID affect pixels:

1. Applied exposure sets the integrated photocharge and mean dark charge.
2. The readout adds dark charge and optional Poisson/read noise once, clips at
   full well, applies recorded gain and black level, and quantizes/clips the ADC.
3. The applied ISP profile processes that frame's RAW. WB and CCM never change
   the physical RAW capture. Requested settings are retained separately and can
   differ from the recorded applied values.

The base `readout.gain` is a validated model field; each frame's applied gain
replaces it for execution. The service additionally checks all applied values
against the selected immutable models' validity domains. This pure numerical
adapter cannot establish applicability of a vendor model by itself.

Fixed patterns are explicitly zero: PRNU, DSNU and column FPN are not modeled.
They therefore remain unchanged across frames. Dynamic noise uses the recorded
run seed plus frame index, independently of knot count. Adding exact knots to a
static timeline does not add read noise or add readouts. Same-pixel overlapping
exposures and mid-frame setting changes are refused by the timing contract.

## Output and evidence

`execute` separates a JSON-compatible `summary` from numeric NumPy `arrays`.
Every array has a unit in `summary.array_units`.

| Artifact | Shape / meaning |
| --- | --- |
| `channel_photoelectron_rate_scenes` | `[unique_scene,H,W,RGB]`, post-optics rate in electron/pixel/s |
| `row_scene_integration_weights_s` | `[frame,unique_scene,row]`, exact declared time-basis integrals |
| `channel_electrons`, `mean_electrons` | Integrated pre-readout channel charge and native CFA charge |
| `channel_reference_signal_dn` | Continuous expected dark/black-corrected channel DN after clipping |
| `reference_dn`, `raw_frames` | Continuous expected raw DN and actual sampled integer ADC frames |
| `row_start_ns`, `row_end_ns` | `[frame,row]`, half-open exposure windows |
| `frame_start_ns` | `[frame]`, recorded frame references |
| `rgb_frames` and ISP intermediates | All applied ISP stages in frame order |
| `rgb` | First-frame preview only |

Radius/CFA maps, spectral RI and the PSF kernel are retained. Scene knot indices,
canonical numeric scene hashes, original source descriptions, per-scene flux
diagnostics, complete control snapshots, per-frame readout metadata and ISP
summaries identify the calculation. Registry-managed source image bytes remain
the original-input evidence; numeric content hashes do not claim identical file
encodings.

The prediction is `raw.channel_mean_dn_sequence`, unit `DN`, with frame-major
`frame × channel(R,G,B)` axes. It averages continuous clipped dark/black-corrected
expectations at native CFA samples. Actual sampled RAW channel means are reported
separately. Per-frame exposure/gain/temperature conditions are explicit. Model
uncertainty remains `unknown`. Changing-frame variance contains scene changes,
so this runtime does not publish it as temporal noise or SNR.

## Limits and unresolved physics

The local adapter accepts up to 16 frames, 128 knots, 1,048,576 total frame
pixels and 64,000,000 total spectral samples across distinct input scenes. Each
image and kernel must satisfy the spatial runtime's own dimensions, wavelength
and decoded-byte limits. Resource estimates report array sizes, not a measured
process memory peak; large legal inputs can exceed one GiB once intermediate
arrays are included. There is no automatic coarsening or dropped frame policy.

The temporal integral is exact for the declared linear/ZOH **irradiance** model,
within floating-point arithmetic. Linear interpolation of two moving-object
images is a crossfade, not a reconstructed continuous object trajectory. Dense
authored image knots can describe an approximation to motion, but depth,
occlusion and disocclusion, geometric reprojection, varying aberrations and
aliased source content are not recovered. The PSF is fixed, locally shift
invariant and pixel integrated; outside the supplied crop is declared dark.

There is no hardware register simulator, closed-loop AE/AWB, multi-exposure HDR,
temperature dynamics, or complete LED flicker mitigation model. Supplied temporal
irradiance can include a declared flicker waveform, but results do not establish
a hardware LFM specification or physical validation. A production extension
needs measured timing/control logs, calibrated optical/sensor models, independent
holdout captures and declared uncertainty in addition to implementation tests.

## Development closed-loop extension

The separate [`closed_loop_scene_v1` development module](camera-platform-closed-loop.md)
now drives this physical capture kernel from RAW AE/AWB statistics, with declared
latencies, independent command queues and per-frame exposure/gain/WB. It is
available through the development Python runtime only. Registry/model-domain
preflight, API/SDK/worker, replay/validation conditions, UI and installed-package
integration was pending at the v0.8.1 checkpoint; that installed workbench had the recorded
control scope described above.

## v0.9.0 integration update

[Closed-loop AE/AWB](camera-platform-closed-loop.md) now has separate immutable policy/program
records, whole-domain model admission, API/SDK/Worker execution, preserved RAW replay and a
`#controls` workbench view. Existing recorded-setting runs retain their original contracts; an
old temporal run recomputed with v0.9.0 retained identical RAW and RGB artifact hashes.
Closed-loop physical comparison still needs an explicitly matched observation operator.
