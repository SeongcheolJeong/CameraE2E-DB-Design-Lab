# Actual electrical exposure control → code-domain ISP

Development source `0.16.1.dev5` connects completed `electrical_control_v1` single-readout acquisitions to the existing retained-code ISP replay and bounded candidate comparison. The installed primary remains `0.15.0`; this source work is not an installed release qualification.

## Acquisition contract

The initial ControlSequence fixes frame starts, the initial exposure and controls held constant by the AE policy. It is not the realized exposure sequence. The adapter checks the original registered Assembly, Test, SceneSequence, ControlSequence, CaptureProgram, models, factory links and input blobs, then reads the original integer `raw_groups.npy` and exact summary bytes.

The adapter replays the pure controller against those original RAW samples, without rerunning camera physics. This reconstructs applied exposure, command readiness/latch/application, RAW statistics and the final command queue. The reconstructed trace must equal the saved control trace, realized sample geometry, frame records, readout descriptors, row events and observed timing arrays. A self-consistent rewritten summary alone cannot replace that correspondence.

Historical captures remain readable across processing-environment changes. Execution and cache checks re-resolve source evidence. Integral JSON float seeds previously accepted by capture, such as `591.0`, retain their original request representation; only the RNG receipt uses the normalized integer seed. Booleans, fractional/non-finite and out-of-range seeds are refused.

The original retained-charge resolver and selection contract remain supported. Electrical control requires the original `single` path; closed-loop HCG/LCG feedback is still a separate future extension.

## Processing boundary

The processor receives selected integer RAW, its exact RAW format, one fixed ISP profile, native geometry, actual frame records and actual row events. The separate planning contract, realized acquisition, control trace and source snapshots are retained as evidence. Photodiode occupancy, reservoir ledgers, optical truth and noise-free arrays are not passed into processing.

The available stages remain black-relative code conversion, same-Bayer-site spatial filtering, causal temporal filtering, demosaic, fixed color, bounded detail and display transfer. Exposure changes reset temporal filter state before blending. Later frames with unchanged settings may blend past algorithm output according to the declared code-difference gate. This can add persistence; it is not residual-charge correction, current-exposure charge inversion or exposure normalization.

Signed relative timestamps are preserved. Original row endpoints and sequence span must fit the shared safe-integer time contract; exposure durations remain positive and bounded. The adapter does not shift a negative clock into a new time origin.

## Workflow

From a completed electrical-control result, open **실제 AE RAW로 ISP 비교**. The source list shows saved actual exposure metadata and verifies original evidence when preparing a model/replay. Register a new immutable ISP profile, process the entire source sequence, or compare bounded filter candidates on the same RAW.

Saved replay/comparison details show the selected frame's applied exposure, gain, latch and applied command separately from the exposure proposed after observing that frame. Policy enabled/disabled is explicit. Original frame IDs, RAW hashes, plans, actual acquisition and trace survive detail reads and ZIP export.

Comparison axes and ROI/limits still use the existing bounded JSON editor. The maximum is two axes and twelve candidates. Metrics describe code-space change relative to a bypass baseline; neither the baseline nor Pareto membership establishes physical truth, denoising quality or an automatic design winner.

## Verification and limits

Executable evidence is kept in `reports/camera_platform/electrical-isp-v0161/`: source/legacy regression, independent time/causality tests, replay and comparison integration, UI state tests, real API/Worker runs, browser-authored settings and restart byte checks. Initial failures and their corrections are preserved separately from final results.

The real QA fixtures are authored 16×16 full-native sensors with four frames: linear deterministic AE and nonlinear PWM AE with declared noise. This verifies the implementation path, not throughput, physical-camera accuracy or customer readiness. Independent physical measurements remain zero and total predictive uncertainty remains unknown.

Next scientific extensions remain fixed HCG/LCG observation feedback, an explicit state-aware HDR contract, finite-distance/general optics and richer pixel/electrical identification. Existing independent-reset HDR assumptions cannot be transferred to retained-charge AE without defining what signal is actually reconstructed.
