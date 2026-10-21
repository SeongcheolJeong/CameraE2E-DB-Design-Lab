# Reference exposure constrained RAW fusion

Source scope: `0.16.0.dev7`. The new `reference_hdr_v1` runtime captures a
changing declared image timeline under constant light, then combines agreeing
RAW samples while preserving one explicit reference exposure. It extends the
model investigation workflow; it does not establish physical-camera validation.
Current qualification and installation status belong to the
[development handoff](../reports/camera_platform/development-handoff.json).
The later opt-in RAW-only global integer translation profile is documented
separately in [RAW global translation HDR](camera-platform-reference-hdr-translation.md);
the v1 behavior and evidence below remain unchanged.

## Supported contract

The capture program uses `camera_reference_hdr_capture_program_v1` with exactly
`schema`, `electrical_model`, `waveform`, and `hdr_merge`. Its mandatory merge
profile uses `camera_reference_hdr_merge_v1` with `reference_frame_index` and a
complete existing `camera_hdr_merge_v1` `base_profile`. The reference is zero
based, in `[0, 15]`, and must exist in the actual sequence of 2–16 exposures.

The existing electrical acquisition kernel provides independent reset/readout,
rolling or global exposure, declared scene interpolation, and per-frame ISP.
All frames retain the same native sensor geometry and crop/CFA phase. Full
sequence clock, coverage, overlap, ADC-format, and resource checks still apply.
Fusion consumes actual integer native RAW plus exposure and electrical
calibration; it does not receive scene truth, noiseless photocharge, motion
vectors, or an alignment oracle. The electrical chain and calibration fields
are described in [Electrical readout and RAW HDR](camera-platform-electrical-hdr.md).

Only `waveform: null` or a valid `kind: constant` waveform is accepted. A supplied
constant waveform must share the scene/control clock. Even whole-period PWM is
rejected by this runtime: moving scene content and identical LED ON durations
do not establish a common rate. Use `hdr_bracket_v1` with `hdr_merge: null` for
the existing frame-by-frame PWM/LFM investigation path.

## How reference preservation works

Each RAW sample is converted to a signed electron rate using its black level,
conversion scale, expected dark charge, and exposure. The existing linear
floor/full-well/ADC censoring and read-noise guard determine usable samples.
All usable exposure pairs are compared at the same native RAW site using the
declared noise-scaled `base_profile.consistency_sigma` threshold. Fewer than
two usable exposures leaves consistency unknown.

| Native RAW condition | Output policy |
|---|---|
| Reference sample is invalid | Rate and estimated variance are NaN, validity is false, and every weight is zero; another exposure cannot fill the pixel |
| A disagreement occurs in the reflected 3×3 neighborhood | Keep the usable reference alone, with weight one |
| Reference is usable, all usable pairs agree, and no neighboring disagreement is detected | Combine usable rates with normalized inverse-variance weights |
| Only the reference is usable and no disagreement is detected nearby | Keep it alone and retain the unknown-consistency status |

The guard is a fixed OR over the reflected 3×3 native RAW neighborhood. It is
not motion estimation. Agreement does not prove scene correspondence; undetected
motion can remain. Disagreement can also result from illumination, noise, or
calibration error. There is no registration, intra-exposure blur correction,
global deghosting, or recovery of a clipped reference. Consequently this method
does not claim valid bright-region dynamic-range expansion beyond the reference.
Linear scene interpolation is an image crossfade, not geometric motion rendering.

Retained artifacts include `raw_frames.npy`, `hdr_exposure_rate.npy`,
`hdr_weights.npy`, `hdr_merged_rate.npy`, `hdr_estimated_variance.npy`, and the
reference-valid, disagreement, expanded-disagreement, fused, fallback, and
unknown masks. `hdr_linear_rgb.npy` is signed linear RGB in electron/pixel/s;
its validity conservatively requires the reflected 3×3 RAW neighborhood.
Invalid linear values remain NaN. `hdr_rgb.npy` and the main PNG are tone-mapped
display previews and show invalid pixels as black. Per-frame ISP previews and
the merged preview are separate processing paths.

Estimated variance is a plug-in independent shot/read/quantization-noise result
conditional on the selected weights. It excludes gate selection bias and motion
error; it is not calibrated total uncertainty. Physical validation remains
`not_established`, and total uncertainty remains unknown. No vendor LFM,
hardware equivalence, or shared-charge DCG claim follows from these outputs.

## API, SDK, and Workbench

1. Register an assembly, native spectral-image timeline, and its applied control
   sequence. The controls fix each exposure, analog gain, timing, and ISP.
2. Request `GET /api/v1/templates/capture-program?assembly_id=...&mode=reference_guarded`.
   Review the reference index, electrical model, base merge profile, and source
   fields, then register a new version with `POST /api/v1/capture_programs`.
3. Request `/api/v1/templates/temporal-test` with `assembly_id`,
   `scene_sequence_id`, `control_sequence_id`, and `capture_program_id`; register
   the result with `POST /api/v1/tests`. It uses `reference_hdr_test_v1` and pins
   the exact sources. A changed program needs a corresponding new test.
4. Submit `POST /api/v1/runs` with those IDs, `test_id`, and
   `plugin_id: reference_hdr_v1`. Only the temporal-noise seed is a run parameter.

```python
from camera_platform.sdk import PlatformClient

# base_url and the three input IDs identify a chosen server and registered data.
with PlatformClient(base_url) as client:
    draft = client.template(
        "capture-program", assembly_id=assembly_id, mode="reference_guarded",
    )
    draft["payload"]["hdr_merge"]["reference_frame_index"] = 0
    program = client.create_capture_program(draft)
    test = client.post("tests", client.template(
        "temporal-test", assembly_id=assembly_id,
        scene_sequence_id=scene_id, control_sequence_id=control_id,
        capture_program_id=program["id"],
    ))
    queued = client.submit_reference_hdr(
        assembly_id, test["id"], scene_id, control_id, program["id"], seed=42,
    )
    run = client.wait(queued["id"])
```

In **모델 팩토리**, use **새 기준 노출 HDR 프로그램** to choose the reference
index and review the full JSON before registering a version. In **실험 · 비교**,
choose **기준 노출 보존 · 움직임 HDR**, the matching program, timeline, controls,
and fixed test. The result panel shows reference identity, fused/fallback/unknown
pixel counts, the merged preview, and original frames. Inspect saved arrays and
their masks before interpreting the display.

Submission and Worker execution bind the exact program, assembly, sensor,
sequences, request, and source artifacts. The test observation remains sampled
dark-corrected RAW channel means; passing it is not a fused-image accuracy test.
Existing RAW replay reprocesses original frames and preserves their acquisition
conditions; it does not reproduce this fusion. The old `hdr_bracket_v1`,
`camera_capture_program_v1`, and `camera_hdr_merge_v1` keep their separate static
HDR behavior and contract. Reference fusion does not broaden those contracts.

## Evidence read performance

`isp_studies._EvidenceReader` reuses already verified Study, cell, analysis,
selection, and export snapshots within one public read operation. Case evidence
resolution shares one such reader across its linked ISP evidence, avoiding
repeated reads of the same dependency graph. It is never retained on the shared
service or used across requests. Callers must not mutate its records or perform
writes within its lifetime. A new public operation verifies source records,
completed jobs, and blob bytes again.

On the same saved local ISP Study/case fixture, three reads per source version
had medians **6.918 s (dev6) → 1.897 s (final dev7)**, about 73% less time.
The full case hash was unchanged. These are local in-process Workbench bootstrap
observations, not a throughput or scaling guarantee. An earlier intermediate
measurement was 1.853 s; the final source was measured again after the version
change. See the [paired comparison](../reports/camera_platform/evidence-performance-v016/comparison.json).
The change preserves existing evidence contents and does not remove the large
embedded case payload or establish a normalized evidence-storage design.

## Verification and remaining scope

The numerical, runtime, and workflow regression suites are
`tests/platform/test_reference_hdr.py`, `test_reference_hdr_runtime.py`, and
`test_reference_hdr_workflow.py`. They cover reference-invalid behavior,
pairwise/neighbor disagreement, native CFA phase, RAW-only fusion, changing
captured pixels, constant-light restrictions, fixed evidence, and legacy static
behavior. The final scoped run passed 376 Python tests and 80 Node tests. A
separate real API/Worker executed one PSF Factory job, five scripted acquisitions,
and a sixth acquisition submitted through the browser. The browser created a
reference-index-3 program and fixed test, verified immediate rejection of index
16, and displayed the completed saved result. Exact full rows, runs and bundle
manifest were preserved across an API/Worker restart.

The reference-index-0 authored 32×48 example had 606 fused, 626 fallback,
42 reference-only/unknown, and 262 invalid sites. This is an implementation
example, not an accuracy or noise benchmark. Its original retained arrays are
shown in [this comparison](../reports/camera_platform/reference-hdr-v016/reference-comparison.png).
The independent noiseless moving/PWM counterexample changes 896 of 8192 RAW
samples and 2880 RGB samples despite equal effective exposure; its original
requests and arrays are retained in
[counterexample evidence](../reports/camera_platform/reference-hdr-v016/pure-motion-pwm-counterexample/verification.json).
The [milestone report](../reports/camera_platform/reference-hdr-v016/evidence-summary.json)
defines the verification scope. Primary installed v0.15 is unchanged; this is
a source milestone, not an installed commercial release.

General optical/pixel/charge-transport and circuit models, moving PWM fusion,
registration and deghosting, calibrated physical validation, and broader ISP and
optimization capabilities remain open. This bounded source extension preserves
the platform's original seven-module and four-execution-mode scope.
