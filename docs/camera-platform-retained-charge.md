# Retained photodiode charge and incomplete transfer

Development milestone: **0.16.0.dev15, bounded numerical and workflow verification passed**.
The primary installed application remains v0.15. This page defines an authored
behavioral model and its verified software scope. Physical accuracy and a
customer-installable release remain unestablished.

## Purpose and boundaries

This extension makes frame history affect the actual saved RAW. An incomplete
transfer leaves photodiode charge available for the next exposure. The new
`retained_charge_v1` runtime uses a separate immutable capture program; existing
independently reset HDR programs retain their original meaning.

The reservoir is a full native sensor grid. Its four native RGGB sites have
authored, constant transfer efficiencies. Capacity is an explicit integer number
of electrons, equal to the chosen readout model's well capacity. The initial
reservoir is empty at each row's first exposure start. The first version refuses
cropped state and arbitrary preloaded occupancy.

Photocharge enters this reservoir during the declared exposure window. Outside
that window an ideal upstream gate excludes new photocharge without clearing
the reservoir. Dark generation continues during both exposure and intervening
gaps. This gated collection assumption is an authored abstraction, not a model
of a particular transistor circuit. There is no trap capture/release, decay,
barrier solution, transfer-duration response, or temperature dependence of the
transfer efficiency. Only the existing declared dark-current law depends on
temperature.

## Events and conservation

For every row, the first empty state begins at its first exposure start. Each
subsequent frame follows these operations:

1. Accumulate dark charge since the preceding transfer; clip to reservoir
   capacity and record overflow.
2. If this frame requests **PD clear**, remove the reservoir charge and record
   the removed population. A read-node reset does not perform PD clear.
3. Integrate the scene and optional periodic illumination over the row's
   half-open exposure window. Apply the persistent native PRNU once, add dark
   generated during that window, and sample this incoming population once.
4. Add the incoming population to the retained charge, clip to capacity, and
   record exposure overflow **before** transfer.
5. At exposure end, split that same population into delivered and retained
   electrons. The delivered population is passed to a fresh read node and its
   voltage/gain/noise/ADC stages. HCG and LCG read the same delivered population.

At equal timestamps the order is previous transfer → optional PD clear → next
exposure open. Transfer is an explicit model event at row exposure end; it is
not inferred from transport or sensor-readout-complete timestamps. Existing
recorded control settings remain distinct from this authored event contract.
For rolling rows, a frame's retained-state array represents each row immediately
after that row's transfer. Rows have different event times: this is not a sensor
snapshot at one common wall-clock instant. Reservoirs have no inter-site dynamic
coupling in this model.

For stochastic mode, incoming charge is Poisson and transfer is binomial:
`delivered ~ Binomial(occupied, efficiency)`, `retained = occupied - delivered`.
Independent Poisson draws for those two outputs would violate conservation.
Noise-disabled mode uses continuous deterministic flow. In particular, clipping
a mean is not the exact expected result of clipping a random population.

The ledger preserves, per pixel and frame:

```
previous retained + gap generated + exposure generated
  = gap overflow + PD clear loss + exposure overflow + delivered + next retained
```

For capacity 100, prior charge 80, new charge 80 and efficiency 0.5, the correct
deterministic result is overflow 60, delivered 50, retained 50. Capacity applied
after transfer would produce a different and invalid result for this contract.

No dark and no release imply gap invariance. With nonzero dark, a longer gap can
change the next RAW and overflow. A claimed release-time dependence would need
a separately specified release model.

## RAW, evidence and replay

The encode-only electrical boundary receives an already sampled, capacity-bound
delivered population. It must not add PRNU, dark or shot noise again, or clip a
second collection well. Per-path DCG input ceilings and ADC/digital clipping
are readout effects; they do not change the retained photodiode state.

RAW represents **delivered total charge with frame history**. Dividing it by
the current exposure or subtracting only the current exposure's dark is not an
estimate of the current scene's photocharge. Legacy HDR fusion, calibrated ISP
photocharge replay and static response fitting must not silently admit this
acquisition. The preview is a declared RAW code display, not a correction of
lag. Simulation state arrays are truth diagnostics and cannot be supplied to
a measured-RAW decoder as an unobservable correction.

The entire model, ordered controls, initial-state rule, clear policy, scene
timeline, native geometry and seed are immutable inputs to one job. Charge and
readout randomness use separate streams so readout gain/noise changes do not
advance a different physical reservoir. There is no external mutable sensor
state. A retry starts from the same initial state and cannot advance it twice.
Arrays, per-event times, state hashes and source/program identities are retained
with the run and its export package.

## Workbench and API

The Model Factory adds a retained-charge program draft after selecting an
assembly and its recorded control sequence. Native R/Gr/Gb/B efficiencies are
editable individually. The full JSON exposes each ordered frame's
`pd_clear_before_exposure` policy, readout model, event contract and provenance.
An edited program becomes a new revision. A Test fixes that program and the
selected scene/control records before a run is submitted.

The corresponding API flow is:

1. `GET /api/v1/templates/capture-program` with `mode=retained_charge`,
   `assembly_id` and `control_sequence_id`.
2. Register the reviewed draft using `POST /api/v1/capture_programs`.
3. Request and register the temporal Test template with the program ID and
   both sequence IDs.
4. Submit `retained_charge_v1` with those fixed IDs and parameters `seed` and
   the optional boolean `noise_enabled`. The SDK exposes `submit_retained_charge`.

The execution form offers stochastic sampling or deterministic flow. Results
show the actual saved frame preview and a separate simulation-truth ledger.
`raw_groups.npy` has axes `[frame, readout, row, column]`, including a size-one
readout axis for a single reader. Paired paths preserve HCG/LCG identity.
The primary preview shows the first readout; all groups remain in the artifacts.

The original full-native development result below predates the opt-in fixed
receiver ROI. Current development source can bind a smaller native receiver ROI
for retained v1/v2/v3/v4. Its row event schedule, native CFA phase, seeded
full-sensor fixed-pattern maps and state hash retain the full sensor origin.
An explicit `capture_crop` requires the source to cover its PSF and
charge-collection halo. A partial native source used without `capture_crop`
instead assumes zero light outside its declared boundary, so it cannot establish
full-frame equivalence near that boundary. With noise enabled, equal seeds do not
make overlapping pixels' sampled shot/readout noise identical between full and
cropped runs; deterministic means and fixed patterns are comparable. Omitted
sensor regions are not modeled as cleared, and no physical
cross-pixel reservoir coupling is claimed. The ROI cannot change within one
retained sequence; v5 photo-HDR inverse still requires a full-native sensor.
Native dimension, frame-pixel and evidence-memory bounds remain in force.

## Required qualification

Qualification must show actual bright→dark RAW lag, its cancellation by PD
clear, the complete-transfer limit, site-specific efficiencies, overflow before
transfer, stochastic conservation and binomial moments, continuous dark across
gaps, rolling-row/PWM integration, and common delivered charge for paired reads.
It must also cover rejection of incompatible consumers, same-seed reruns,
immutable queued-input checks, artifact/export readback and bounded resources.
Tests and software evidence do not establish physical-camera accuracy or a
customer-installable release.


## Verified development evidence

The frozen Python environment is
`878c2d75dcfc6cc3b1bf2fb44505efeb74a1d5907873327f21d7d8c5921abe8a`.
All **3,996 platform tests** passed, with 14 existing warnings and no skipped
or failing tests. The final static UI passed **176 Node module tests**. These
module tests are separate from the actual browser interaction below.

The isolated HTTP/Worker proof passed 1,933 checks: one optical Factory and nine
retained-charge runs, with eight deterministic runs including one forced repeat
and one stochastic nonlinear DCG run. All nine full-evidence bundles were read
back. A separate offline review passed 5,644 checks. The HTTP fixture stays below
full-well and has equal efficiencies at all sites; saturation, stochastic moments,
rolling/PWM cases and site differentiation are covered by the focused tests in
the full suite, not asserted from those nine HTTP jobs.

For the authored **8×8 full native sensor**, a bright frame followed by two dark
frames with 50% transfer gives mean RAW **156.5781 → 110.2656 → 87.1875 DN**.
PD clear before the second frame changes the two later means to **64 DN** (black).
The retained run's mean delivered population is **229.6753 → 114.8377 → 57.4188
 electrons**, recorded as simulation truth. Complete transfer, longer dark gaps,
nonlinear encoding and paired reads have separately saved cases.

Actual CUA browser verification authored another program with native efficiencies
`[0.4, 0.5, 0.6, 0.7]`, registered a matching Test and ran it with seed 731 and
noise disabled. Three saved frame previews, their timing, the charge ledger and
the explicit legacy replay refusal were observed. Independent GET-only readback
passed 1,161 checks. A duplicate generic preview initially used a wrong
representative-grid caption; that preview was removed, and the corrected UI was
reloaded and checked. Python simulation sources did not change. The original
candidate evidence and a separate 450-check correction review are retained.

After API/Worker restart, the original HTTP evidence passed 2,301 readback checks
and the browser-created run passed 1,170; actual browser reload showed the same
saved run and ledger. Backup/restore reproduced **38 records, 11 jobs, 71 audit
rows and 236 reachable blobs** exactly in a new directory. Owned QA processes and
the QA browser tab were closed; state and evidence remain available. The primary
v0.15 application retained its existing records and stayed healthy.

See [evidence summary](../reports/camera_platform/retained-charge-v016/evidence-summary.json),
[actual saved comparison](../reports/camera_platform/retained-charge-v016/numerical-comparison/retained-charge-comparison.png)
and [full regression](../reports/camera_platform/retained-charge-v016/regression-summary.json).
These checks establish implementation and workflow behavior, not hardware
accuracy, scalable full-resolution performance or commercial readiness.

## Next bounded ISP connection

A separate retained-RAW replay contract can expose code-domain ISP experiments
while preserving source bytes, frame/readout identities and history semantics.
Such a path must not use the internal reservoir truth to claim observable
current-exposure photocharge recovery. Existing photocharge calibration and HDR
fusion remain refused until their assumptions are explicitly extended. This is
planned work, not part of dev15. Electrical closed-loop HDR/LFM, temporal/HW ISP,
and adaptive multiobjective optimization remain priority extensions alongside
general optics, pixel and circuit-model work.
