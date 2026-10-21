# Retained RAW code-domain ISP and bounded candidate comparison

Development milestone: **0.16.0.dev16, bounded numerical/API/browser scope
qualified**. Regression and targeted correction evidence are distinguished below;
the full suite was not rerun after the final narrow API correction. Operational
finalization, including restart, backup and cleanup, follows the
[evidence summary](../reports/camera_platform/retained-isp-v016/evidence-summary.json).
The primary installed application remains **v0.15**. Physical-camera accuracy,
an installable v0.16 release and the complete commercial goal remain unestablished.

This extension connects the history-bearing RAW from
[retained photodiode charge](camera-platform-retained-charge.md) to authored
spatial and temporal ISP experiments. It extends the existing ISP/experiment
workflow; the platform retains its seven modules and four principal modes.
The experiment page adds **시간 이력 ISP 비교** as a workbench within that structure.

## Source and processing boundary

Only a completed original `retained_charge_v1` run is accepted. The source
resolver verifies its registered assembly, Test, scene, controls, capture
program, immutable snapshots, RAW/summary bytes, electrical receipts and row
events. A replay or candidate result cannot substitute for the original run.

Selection uses exactly:

```json
{"schema":"camera_retained_raw_selection_v1","readout_id":"single"}
```

The other readout IDs are case-sensitive `HCG` and `LCG`, when present in the
original paired acquisition. All original frames are selected, in original
order, from that one readout. There is no frame subset, path fusion, arbitrary
readout index or implicit first-path fallback. The saved RAW container remains
`[frame, readout, row, column]`; the processing input is an independent integer
`[frame, row, column]` copy. Its selected-array hash and original artifact hash
are distinct identities.

Single and paired linear and nonlinear charge-to-voltage acquisitions are
supported as code sources. `response_type=nonlinear_charge_voltage` is preserved;
the code processor does not relabel nonlinear codes as a linear electrical
response. Selected format fields are `cfa`, `bit_depth`, `black_level_dn`,
`response_type`, `packing`, `layout` and `hdr_streams=1`. The ISP model must match
that format. The sole comparison normalization is finite numeric equivalence
of `black_level_dn`: JSON `64` and `64.0` represent the same declared black
level. Booleans, strings and nonfinite black levels are refused; every other
format field retains strict canonical type/value comparison. Original model
and source documents remain unchanged in evidence. The model's applicability
domain must cover every original frame's exposure and gain.

The processor receives only selected integer RAW, that format, the explicit ISP
profile, native receiver geometry, recorded controls and the original row event
schedule. Reservoir, generated/delivered electrons, ideal scene, optical truth
and read-noise draws remain evidence; they are not correction inputs. No
photocharge inversion, current-exposure dark subtraction, exposure division,
lag correction, HDR fusion or adaptive exposure/white balance is performed.

## Explicit profile and numerical stages

An immutable ISP model has payload schema
`camera_retained_code_isp_profile_v1`. The graph and float64 precision are fixed:

1. Preserve signed `RAW − declared_black` in DN, including below-black codes.
2. Optionally apply `same_site_bilateral_code_v1` independently on the four
   native RGGB subgrids. Its reflected 3×3 site-neighborhood uses fixed spatial
   weights and a code-difference Gaussian; it does not mix R/Gr/Gb/B samples.
   `strength` is in `[0,1]`, and `range_sigma_dn` is in DN.
3. Optionally apply `causal_difference_gate_v1` to the spatial-stage output,
   using the previous filtered output at the same native site.
4. Normalize by `declared_white − declared_black`, then bilinearly demosaic with
   reflected boundaries, apply fixed RGB white-balance gains and the fixed
   output-row/input-column color matrix.
5. Optionally apply `bounded_code_detail_v1`: the mean RGB guide's difference
   from a fixed spatial blur produces a thresholded, capped signed adjustment
   shared by the three components. `amount` is `[0,1]`; `threshold` and
   `max_delta` are normalized code coordinates, not electrons or scene radiance.
6. Clip to `[0,1]` and use the profile's fixed linear or sRGB display transfer.

Each optional stage defaults to `mode=bypass`. A zero strength, previous weight
or detail amount is also an exact numerical bypass. Fixed color and display
parameters remain authored settings; they do not establish physical color
accuracy. Signed pre-tone arrays are preserved separately from clipped display
RGB. Final RAW codes equal to zero or declared white are observable code rails;
they do not identify every earlier ADC, digital-gain or full-well clipping event.
The spatial filter excludes rail neighbors and preserves a rail-valued center.

## Causal state, event times and delay

For current spatial-stage code `x`, previous filtered code `z_previous`, weight
`w` and threshold `t`:

```
if no reset and abs(x - z_previous) <= t:
    z = (1 - w) * x + w * z_previous
else:
    z = x
```

`previous_weight` is `[0,0.95]`; `threshold_dn` is DN. This is causal algorithm
state, independent of photodiode charge state. It can increase delay or ghosting
after a real scene change. A reduced code difference is not evidence of reduced
sensor noise, stationary SNR or correction of physical lag.

State resets on the first frame, recorded PD clear, settings change, excessive
per-row inter-transfer interval, current code rail or previous code rail.
The three profile reset-policy switches must all be true in this version.
The settings signature includes exposure duration, gain, recorded temperature,
row-open offsets relative to frame start and row exposure durations. Absolute
frame timestamps advancing normally do not themselves constitute a settings
change. The time interval is derived from consecutive original row transfer
events, not nominal FPS or transport-complete times. A row exceeding
`max_inter_transfer_interval_ns` resets; equality is admitted. The backend accepts
an integer threshold in `[1, 2^62−1]` ns.

Replay evidence includes the previous algorithm state, blend gate, reset-reason
bits and row intervals alongside stage arrays. A retry begins with empty ISP
history and reprocesses the complete selected sequence. It never advances an
external mutable photodiode or ISP state.

## API and immutable model workflow

Use these endpoints under `/api/v1`:

| Operation | Endpoint / contract |
|---|---|
| Discover original readouts | `GET /retained-isp/sources` |
| Draft an ISP for the selected format | `GET /templates/retained-isp-model?source_id=…&readout_id=…` |
| Register the authored model revision | `POST /models`, with the retained code profile payload |
| Queue replay | `POST /replays`, schema `camera_retained_code_isp_replay_v1` |
| Read/list replay jobs | `GET /replays/{id}` and `GET /replays` |
| Cancel/export replay | `POST /replays/{id}/cancel`; `GET …/export` or `…/bundle` |

The replay request names `name`, `source_kind="run"`, `source_id`, the exact
selector, `isp_model_id` and optional boolean `force`. It produces job kind
`isp_replay`, plugin `retained_isp_replay_v1`. The SDK exposes
`retained_isp_sources`, `retained_isp_template` and `submit_retained_isp`.

The registered profile is replay-only: the composer emits an error when it is
placed into a camera acquisition assembly, and acquisition preflight refuses
that incompatible composition. Existing ordinary RAW replay, calibrated
photocharge ISP and stationary ISP studies retain their separate source rules.

## One bounded comparison job

Comparison schema is `camera_retained_code_comparison_v1`, with job kind
`retained_isp_comparison` and plugin `retained_isp_comparison_v1`. Its specification
includes the same original run, exact readout selector and registered ISP model,
plus a fixed ROI, authored axes, objectives and explicit limits.

One or two axes may vary only `spatial_filter.strength`,
`temporal_filter.previous_weight` or `detail.amount`. The Cartesian product has
at most 12 candidates. Every swept block must already have an active mode in
the registered model; a sweep over a bypass-mode block is refused. Zero axis
values remain valid bypass candidates. All unswept settings, color transforms,
tone, source frames and readout remain fixed.

The baseline disables all three optional filter modes and sets their three
numeric axes to zero on the same profile. It is a processing reference, not an
independent scene truth. The fixed half-open ROI uses `row_start`, `row_end`,
`column_start`, `column_end`, is at least 2×2, and must fit the full native raster.
Filtering always processes the complete selected raster before metrics use the
ROI; an ROI does not crop or reset algorithm state.

Per-frame metrics use the actual `pre_tone_code_rgb_frames`. RGB components enter
absolute errors individually, so opposite color errors cannot cancel. The
metrics are mean absolute, RMS and maximum absolute baseline delta; fraction of
pre-tone components outside `[0,1]`; spatial total-variation descriptor; and mean
absolute distortion of adjacent-frame code changes relative to the baseline.
Spatial variation is mean absolute horizontal/vertical neighboring RGB code
difference. It is not an estimate of scene detail or sensor noise. Error and
variation units are `normalized_code_rgb`; clipping fraction has unit `1`.

The first frame's temporal metric is `null/unknown` because no preceding frame
exists. Temporal worst-case reduction uses subsequent frames; a one-frame
sequence has no known temporal metric and cannot satisfy temporal feasibility.
Other objective and limit values use the maximum across their supported frames.
Missing values remain unknown and cannot silently become zero or pass a limit.

Objectives must explicitly minimize both `mean_abs_baseline_delta` and
`spatial_tv`; additional declared code metrics may be included. Required limits
are `max_frame_mean_abs_delta` and
`max_frame_temporal_change_distortion`. Feasibility is evaluated first, followed
by Pareto dominance among feasible candidates. Smoothing or darkening cannot
qualify merely by reducing spatial variation without meeting the authored
fidelity and time-change limits. Loose limits can still admit poor-looking
results; the system does not infer user preferences or assert better quality.
It reports no selected winner and performs no automatic model promotion.

The API sequence is `POST /retained-isp/comparisons/plan` with the specification,
then `POST /retained-isp/comparisons` with `{specification, plan_hash}`. Planning
returns `plan_hash`, `frozen_request` and `cost`. Submission revalidates the source,
registered profile, candidates, environment and cost before queueing; a changed
plan is refused. Optional `force` is execution policy and is excluded from the
immutable specification/hash. GET list/detail, POST `/{id}/cancel`, GET
`/{id}/export` and GET `/{id}/bundle` use the same comparison route prefix.

## Resource and evidence bounds

The original RAW artifact is bounded to 64 MiB. Selection permits at most 16
frames and 1,000,000 selected frame-pixels in total. The processor additionally
requires even native receiver dimensions from 2 through 4096 and an explicit
crop origin within the bounded full sensor,
RGGB unpacked integer codes with at most four bytes per sample, and bit depth
1–24. Its own frame-pixel bound is 1,048,576, but the stricter source bound still
applies. The processor's array forecasts must fit 128 MiB output and 1 GiB
working memory; these can reject a shape below the pixel-count limit.

Replay and comparison plan the complete original source/Factory artifact closure
and new evidence before enqueueing, with a 256 MiB archive budget. Comparison
also budgets the retained baseline plus one candidate's processing peak, NPY
headers, all candidate outputs, copied/generated summary, logical manifest,
per-frame PNGs and ZIP overhead. It executes candidates sequentially rather than
keeping all candidate arrays in working memory. Forecasts are safety bounds,
not measured latency, throughput or full-resolution scalability claims.

Each comparison stores actual float64 pre-tone and display RGB arrays for the
baseline and every candidate, their exact profiles/coordinates, per-frame
metrics, ranking constraints, source hashes and saved frame PNGs. PNGs have a
fixed `[0,1]` display mapping without per-image autoscaling. Preview groups bind
each image to its candidate and original frame ID.

GET/export verification checks frozen inputs and artifact byte identities.
Historical readback can retain its recorded execution environment; execution
requires the current environment. Export includes original source documents,
registered model, controls/events/program, optical Factory lineage and artifact
bytes, in addition to comparison outputs. Source diagnostics in that package
are explicitly evidence-only. Comparison ZIP members preserve semantic artifact
names and hashes, including aliases, and are checked against the planned bound.

## Verified development scope and remaining work

Focused coverage includes native-site isolation, signed/bypass behavior,
temporal gates and reset reasons, row timing, format/source refusal, code rails,
actual bright-to-dark filtering, metric sign/color cancellation, temporal
distortion limits, unknown metric handling, immutable plans, cache/force,
artifact corruption, resource forecasts and cancellation.

The [regression summary](../reports/camera_platform/retained-isp-v016/regression-summary.json)
records 4,108 collected tests in the full baseline run: 4,107 passed and one
stale OpenAPI test failed because it expected two replay request variants.
That test now checks all three explicit wire contracts while preserving the
legacy fields. Actual browser authoring then exposed the `64.0`/`64` JSON
black-level spelling issue. Its one-file API correction passed 53 targeted
replay/comparison/legacy API tests. The final combined coverage is **4,118 unique
Python tests, zero unresolved failures**, and **193 Node tests passed**. These
are combined full-baseline and targeted-correction results; **the full suite was
not rerun after that narrow product correction**. Source selection and DSP
algorithms did not change in that correction.

The [isolated HTTP/Worker proof](../reports/camera_platform/retained-isp-v016/http-acceptance-summary.json)
saved five replays and one four-candidate comparison over authored 8×8,
three-frame retained acquisitions, including linear, nonlinear, HCG/LCG and a
stochastic source. Its 7,428 assertions include repeated artifact/closure checks,
237 GETs, 10 POSTs, 19 saved JSON records, 180 unique artifact blobs and six
bundles. A separate numerical reference checks the saved code stages. These
counts describe software evidence, not independent physical measurements.

Actual CUA browser operation registered an authored ISP model, completed a
replay and completed a **six-candidate comparison**, with saved frames and
metrics displayed. The separate
[GET-only browser-result proof](../reports/camera_platform/retained-isp-v016/browser-verification.json)
passed **2,544 checks, 106 GETs and zero POSTs**, preserving six readback records,
93 artifacts and two bundles. Those GET counts belong to the independent
readback, not to the browser authoring operations. The selected source retains
its actual linear/nonlinear format; comparison creates no promoted model and
claims no physical validation. Final restart, preservation and cleanup outcomes
are recorded in the linked evidence summary rather than inferred here.

The next priority is to consolidate the verified optical, pixel, electrical and
ISP work through dev16 into an **installable v0.16 package**, with installed
execution, restart and existing-record preservation evidence, before the next
electrical closed-loop HDR/LFM proposal. Independent physical validation,
general optical/pixel/circuit extensions, motion-aware temporal ISP,
fixed-point/hardware correspondence and broader adaptive multiobjective
optimization remain within the seven-module/four-mode goal. Code-domain
candidate metrics alone do not establish those capabilities or complete
commercial readiness.

Implementation: `retained_isp_source.py`, `retained_isp.py`,
`retained_isp_replay.py`, `retained_isp_comparison.py` and
`static/retained-isp.js` under `src/camera_platform/`.

## dev23: authored time reference and v2 comparison

The development-source v2 comparison binds a separately authored RGB time
reference to the original RAW path, source Test, row-transfer clock, immutable
fixed color stage and ROI. It evaluates the actual candidate output after
detail, before tone, and keeps truth arrays outside ISP processing. Absolute
code bias, signal-change error and amplitude error are mandatory constraints;
unknown support is excluded from Pareto. The result remains a sampled software
comparison of an authored synthetic truth, not a physical accuracy claim or
automatic winner. See the [time-reference workflow](camera-platform-temporal-reference.md)
for import/Test/API steps and scope. The historical v1 comparison and its
same-RAW baseline remain available.
