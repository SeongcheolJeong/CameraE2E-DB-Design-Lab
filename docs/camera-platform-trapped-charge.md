# Authored photodiode trap capture and release

Status: `0.16.1.dev8` source implementation, bounded independent numerical
checks and the separate HTTP/Worker/browser/restart qualification are complete
within the authored fixtures described below. This source is not installed or promoted.
This source extension adds a finite trap reservoir to the retained-photodiode
capture path. It does not establish measured silicon behavior, an installed
release, physical LFM performance, or completion of the commercial platform.
The existing seven modules and four execution modes remain the product scope.

## Model and physical boundary

Each native pixel has free photodiode charge `Q`, trapped charge `T`, free-charge
capacity `W`, and trap capacity `C`. The authored continuous reference below
saturation is:

```
capture = k * Q * (1 - T/C)
release = T/tau
dQ/dt = photo_generation(t) + dark_generation - capture + release
dT/dt = capture - release
```

`k` has units s⁻¹, `tau` has units s, and charge/capacity have units electrons.
Native R/Gr/Gb/B sites can have different authored parameters. Trap capacity is
separate from the readout full well. Only the free-charge capacity must equal
the selected electrical reader's charge capacity.

The model is `camera_trapped_photodiode_v1`. Exact fields are `schema`,
`capacity_e`, `transfer_efficiency`, `trap_capacity_e`, `capture_rate_s`,
`release_time_s` and `integration`. Each site-dependent quantity is a four-item
R/Gr/Gb/B list. Free and trap capacities are positive integers; capture rates
may be zero, and release times must be positive. An authored example is:

```json
{
  "schema": "camera_trapped_photodiode_v1",
  "capacity_e": 30000,
  "transfer_efficiency": [1, 1, 1, 1],
  "trap_capacity_e": [3000, 3000, 3000, 3000],
  "capture_rate_s": [10, 10, 10, 10],
  "release_time_s": [0.01, 0.01, 0.01, 0.01],
  "integration": {
    "method": "bounded_split_step_v1",
    "max_step_ns": 100000,
    "max_transition_probability": 0.02
  }
}
```

The example requires a readout with the same 30000-electron free capacity. These
values are numerical fixture parameters, not fitted device properties.

This is an authored model of traps exchanging charge with the accessible PD
reservoir. It is not a transfer-gate interface solution, a semiconductor
Shockley–Read–Hall device model, or a fitted physical trap population. Existing
response/noise fits do not independently identify these parameters. Temperature
dependence, spatial coupling, multiple trap energies, transfer barriers,
CDS/kTC/reset circuit dynamics and measured parameter identification are outside
this extension.

## Time, state and conservation

Both reservoirs start empty at each native row's first exposure opening. There
is no implied prehistory, arbitrary supplied occupancy, or mutable state shared
between jobs. Rolling-row state arrays represent each row at its own event
time; they are not a common-wall-clock sensor snapshot.

Photocharge is admitted only during exposure. Dark generation and trap exchange
continue during gaps and exposures. The upstream photo gate does not drain
internal trap release. PD clear removes free charge and records its loss;
occupied traps survive that clear. A fresh read node remains a separate reader
operation and does not clear either physical reservoir implicitly.

Actual scene-knot and PWM-edge chronology must reach the trap integrator. A
single integrated photon count for an entire exposure is insufficient: equal
total photocharge arriving early and late can produce different final occupancy
and delivered RAW. Splitting must preserve exact authored row boundaries and
half-open illumination intervals, including zero gaps and signed relative clocks.

At row exposure end, transfer partitions one free-charge population into
delivered and residual charge. Both HCG and LCG encode the same delivered
population. Trap occupancy is not duplicated or separately sampled for each
readout path.

The complete per-pixel ledger obeys:

```
previous free + previous trapped + generated photo/dark
  = delivered + next free + next trapped + overflow + explicit clear loss
```

Capture and release are internal exchanges. They must not appear as additional
generation. Overflow from release into a full PD is counted as loss; full trap
occupancy does not permit negative vacancies or invented electrons. State
identity binds both reservoir arrays, model, mode, step and predecessor.

A longer gap need not increase the next observed RAW: release before a later
PD clear can drain charge that would otherwise remain trapped. The direction of
that change depends on the declared event schedule.

Saved `retained_initial_trapped_e`, `retained_trapped_e`,
`retained_captured_e`, `retained_released_e` and gap/exposure exchange ledgers
complement the existing generated/delivered/residual/overflow/clear arrays.
`trap_segment_start_ns`, `trap_segment_end_ns`, `trap_segment_duration_ns`,
`trap_segment_phase` and `trap_segment_count` preserve the ordered integration
intervals. Padding advances no time and generates no charge for that row.

## Numerical approximation

The approved first implementation uses bounded first-order Lie steps, rather
than an exact continuous reaction solver. Within each step it performs generation
and PD overflow, trap release and resulting overflow, then capture. Capture
holds the vacancy fraction fixed for that substep; stochastic capture is a
binomial proposal capped by the available vacancies. This capacity-capped
distribution is not the exact continuous-time Markov chain (CTMC).

The declared time-step bound limits per-step reaction probabilities, but a small
probability alone is not a universal error bound. Independent tests must compare
deterministic refinement with an independently integrated mass-action ODE and,
for a small finite population, compare stochastic distributions with the exact
finite-state CTMC matrix exponential. The runtime does not automatically certify
a caller's accuracy tolerance by halving the step; the reported refinement
checks have their own specific fixtures and thresholds. A bounded work budget
is distinct from charge conservation or numerical accuracy. Parameter combinations
requiring excessive work must be refused; silently coarsening the time step is
not an acceptable recovery.

Noise-off mode uses continuous deterministic populations. It is not the exact
mean of a clipped stochastic system, especially near either capacity. Numerical
evidence in an unsaturated domain must not be presented as validation of all
saturation regimes. Analytic release checks isolate the release operation;
coupled recapture generally changes the full observed decay curve.

## Runtime and downstream consumers

The entry is `camera_retained_charge_capture_program_v2` under the existing
`retained_charge_v1` plugin, with `camera_retained_row_events_v2` and
`camera_retained_charge_result_v2`. Existing v1 programs retain their complete
array and summary behavior, including the original RNG stream progression.
The trap extension adds a new explicit model/state/event contract rather than
silently changing the meaning of old saved records.

The entire model, ordered scene/control inputs, native geometry, initial-state
rule, clear policy, integration settings and seed belong to the immutable job.
Charge kinetics and electrical readout randomness remain separate. Retry starts
from the same zero state and cannot advance a hidden sensor twice. The ledger
and time segmentation must fit preflight output and working-memory bounds.

Trap-bearing RAW is delivered total charge with history. Dividing it by the
current exposure, subtracting only current-exposure dark, or using occupancy
truth as a correction does not recover current scene photocharge. Code-domain
ISP may process original RAW under its explicit history-preserving contract;
truth arrays remain diagnostic evidence. Generic HDR inversion, physical lag
correction and electrical AE support are not implied. Until a separate causal
integration is implemented and qualified, the electrical-control runtime must
refuse trap programs.

The API starts with `GET /api/v1/templates/capture-program` using
`mode=trapped_charge`, the assembly ID and control-sequence ID. The existing
capture-program registration, temporal Test and `retained_charge_v1` submission
flow then binds the complete program. The editor exposes four-site trap
capacity, capture rate, release time and maximum integration step; the result
view separates trap/free charge and exchange from the original RAW preview.
Code-domain replay accepts the exact v2 row-event contract while keeping the
processor inputs limited to original RAW, format, profile, geometry, frame
records and events. Trap truth is not passed to the processor.

Execution remains full-native only, at most 16 frames, with separate array,
working-memory and pixel-substep bounds. The core admits at most 512 segments,
100000 substeps and 20000000 pixel-substeps per frame; native dimensions and
512 MiB core/helper memory constraints can impose tighter limits. Runtime
segmentation also preserves exact scene/PWM edges, so requested sequences can
reach the 512-segment bound before the core substep limit. Planning checks work
and cumulative interval-storage bounds before constructing all row/segment
arrays. Output evidence is separately bounded to 192 MiB and the combined
runtime working-array estimate to 1 GiB. These are admission estimates, not
measured peak-memory or full-resolution throughput qualifications.

## Required independent evidence

- Complete pre-trap v1 array/summary fingerprints for all four electrical
  families, with noise disabled and enabled.
- Independent ODE time-step refinement, release-only analytic agreement and
  small-population CTMC distribution comparison, with bounded numerical scope.
- Continuous and integer conservation through generation, saturation, capture,
  release, PD clear and transfer; full transfer efficiency still permits trap
  history to produce a later dark-frame signal.
- Equal-photon early/late illumination with different trap state and actual RAW;
  rolling-row/PWM boundaries and gap/clear ordering.
- Shared delivered HCG/LCG population, deterministic seeded replay, immutable
  original artifacts, resource refusal and unsupported-consumer refusal.

The independent test implementation is
[test_trapped_charge_independent.py](../tests/platform/test_trapped_charge_independent.py).
The pre-trap golden receipt is
[independent-v1-before.json](../reports/camera_platform/trapped-charge-v0161/independent-v1-before.json).
Final independent test counts and source pins are recorded in
[independent-review.json](../reports/camera_platform/trapped-charge-v0161/independent-review.json),
with [JUnit results](../reports/camera_platform/trapped-charge-v0161/independent-final.xml).
The reference ODE and CTMC comparisons, plus actual PWM/RAW differences, are
saved in [independent-reference-results.json](../reports/camera_platform/trapped-charge-v0161/independent-reference-results.json).
The final independent suite passes **25 cases**: eight pre-trap v1 whole-output
checks, eight core/reference cases, and nine actual-runtime/boundary cases.
This is a focused count, not a full-platform regression total.

For the unsaturated ODE fixture, maximum free/trapped charge error decreases
from 0.0227125 to 0.0113538, 0.00567629 and 0.00283799 electrons as the step is
halved from 1 ms to 0.125 ms. For the fixed three-electron/two-trap CTMC fixture,
the exact split-distribution maximum probability error decreases from 0.00217605
to 0.00108914 and 0.000544849 at 50, 100 and 200 steps. Separate 30000-site
integer ensembles check the production reaction distribution with explicit
sampling allowances. Neither result qualifies arbitrary rates or saturation.

The noise-disabled runtime fixture has identical integrated photocharge for
early and late PWM pulses, but first-frame RAW means of 1321.5 and 1358.5 DN.
Its next frame generates zero photocharge while trap release reaches the actual
RAW; transfer efficiency is one. This is an authored 8×10, two-frame numerical
example, not a measured lag specification or a physical LFM improvement.

## Operational qualification

The final selected-source archive contains 146 platform files: ten changed,
including two new files, 136 unchanged and none removed relative to dev7. It is
a source snapshot, not an installer or a complete dependency/asset closure.
The [test inventory](../reports/camera_platform/trapped-charge-v0161/test-inventory.json)
records **237 unique focused Python tests and seven Node UI tests**. One test
appears in two suites and is counted once. The 52-case workflow suite predates
only the final AE capability-label correction; the 32-case AE/capability suite
checks that correction. These totals are not full-platform regression coverage.

On the final source, a real HTTP API and Worker completed **six jobs**: one
PSF Factory, four trap captures and one code-domain ISP replay. The four-frame,
8×8 rolling-row fixture uses equal integrated photocharge with early versus late
PWM pulses. Whole-run RAW means are **519.890625 and 517.578125 DN**, with final
trapped totals **1932.803363 and 3433.022846 electrons**. The no-capture control
has zero trapped charge; the sampled case has integer charge and exact
conservation. These are different fixtures from the independent 8×10 example
above. They establish implemented time-order sensitivity, not physical LFM
performance. The ISP replay uses original RAW and declared metadata, without
access to the trap truth ledger.

The [HTTP verification](../reports/camera_platform/trapped-charge-v0161/http/verification.json)
passes **348 checks**. The
[restart verification](../reports/camera_platform/trapped-charge-v0161/http-restart/verification.json)
passes **350 checks**, including exact equality of nine selected records and
all **206 members in two result ZIPs**. This is bounded selected-record evidence,
not a new whole-database backup/restore or installer qualification.

Actual browser interaction created a new saved capture program after changing
the R-site release time from 0.01 to 0.02 seconds. The trap editor, event/charge
ledger, original 8×8 PNG, corrected AE capability list and post-restart image
were observed. Screenshots were inspected inline and were not saved as files.
See the [browser receipt](../reports/camera_platform/trapped-charge-v0161/ui-verification.json)
and [restart receipt](../reports/camera_platform/trapped-charge-v0161/ui-restart.json).

Initial browser QA exposed inherited trap capability labels in the AE adapter,
even though that adapter correctly refused trap programs. The labels were
corrected and the actual Worker/browser campaign rerun on the final source.
The initial campaign remains separate evidence under `initial-qa/`; its six
jobs are not added to the six final-source jobs above. Earlier failed attempts
and fixes are retained in the report directory.

All three owned API/Worker phases and the owned browser tab were closed.
Each phase preserved the primary v15 logical DB, rows and PIDs. The v16
candidate wheel and preceding dev7 evidence/archive hashes remain unchanged.
The current document adds this operational section after the independent
numerical review; its reviewed historical version is preserved as
[independent-reviewed-contract.md](../reports/camera_platform/trapped-charge-v0161/independent-reviewed-contract.md).
See [evidence-summary.json](../reports/camera_platform/trapped-charge-v0161/evidence-summary.json)
for pins, cleanup and the explicit limits.

Physical measurement cases: **0**; physical accuracy: **not established**;
predictive uncertainty: **unknown**. Causal AE for trap-bearing programs,
stateful HDR reconstruction, CDS/reset circuits and motion-aware ISP remain open.

