# Calibrated nonlinear RAW → advanced ISP → candidate Study

Source milestone 0.16.0.dev11 extends the retained nonlinear RAW from dev10 into advanced ISP and candidate Study. This is a bounded software calculation workflow; installed primary v0.15 and physical-camera validation remain separate. Verification evidence is in `reports/camera_platform/nonlinear-isp-v016/`.

## Scope and physical boundary

Retained nonlinear sequential HDR, reference HDR, and explicitly selected DCG HCG/LCG RAW feed a new ISP v3 graph. Acquisition is never rerun by replay or candidate evaluation. The new graph reconstructs calibrated signed photocharge, applies spatial LSC, same-site denoise, demosaic, fixed WB/CCM, bounded sharpening, and display tone. The original linear ISP v1/v2 and Study v1 contracts retain their units and saved request identities.

DN is not proportional to charge under the nonlinear response. The new decoder inverts the bound charge–voltage curve and subtracts expected dark charge once. It carries stochastic variance in electron² and deterministic quantization intervals separately. Invalid physical outputs retain NaN plus masks; finite work buffers prevent invalid neighbors from contaminating filters. Display-only invalid pixels are black.

Normalization is a fixed electron scale: sequential full well or the DCG model's shared charge capacity. A DCG path's effective input ceiling controls its decoder validity, not its brightness normalization. The selected path uses its diagonal read-noise variance; complete paired acquisition/calibration/covariance identity remains evidence. HCG and LCG from the same acquisition cannot become independent validation datasets.

## Versioned interfaces

- `camera_nonlinear_raw_selection_v1`: `sequential_frames`, or `shared_charge_path` with explicit `readout_id: hcg|lcg`. All original acquisitions are retained.
- `camera_isp_profile_v3` and `camera_isp_replay_v3`: explicit decoder, electron² filter floor, same-source baseline, physical stage masks and arrays.
- `camera_charge_isp_calibration_v1`: separately bound spatial LSC in calibrated photocharge coordinates; no reinterpretation of the legacy DN noise model.
- `camera_isp_study_v2` and `camera_isp_metric_spec_v2`: identical selected RAW and decoder across candidates, fixed ROIs, explicit charge normalization and metric units.
- Independent charge-domain references declare their normalization and common demosaic/WB/CCM plane; legacy DN references are not silently substituted.

Source selection preserves the original complete RAW artifact SHA, selected-view identity, original indices and acquisition groups. Source/program/calibration/model/settings/dark evidence is verified before preparation and execution, and rechecked for cached readback/export. Legacy acquisition environment need not equal the replay environment. A new job separately freezes its execution environment.

## Acceptance conditions

1. Actual numerical effects of LSC, denoise and sharpening; signed negative residuals; nonlinear knots; invalid neighbors; all-invalid inputs; shared DCG normalization.
2. Same original RAW and calibration across all candidates, including a same-decoder baseline. No latent oracle arrays are consumed.
3. Tampered source, calibration, dark map, path identity or applied conditions refuse execution/readback. Original acquisition files remain unchanged.
4. Fixed-condition stationary repeats only for temporal residual noise metrics. HDR brackets and LFM changes do not become noise repetitions merely by declaration. Invalid fixed-ROI samples give unknown metrics rather than silently shrinking support.
5. Meaningful four-candidate Worker execution, explicit selection and independent validation, including refusal of HCG/LCG holdout leakage.
6. Bounded array/memory/artifact forecasts and prepublication resource refusal; old Study/replay/selection evidence remains readable.
7. API/SDK and actual browser source/path selection, authored profiles, replay and Study evidence.

## Limits retained

These are software calculation and evidence workflows. They do not establish measured camera accuracy, full output covariance, real ISP equivalence, optimal settings beyond evaluated candidates, or commercial release readiness. General optics, pixel/electrical model expansion, HDR/LFM behavior, measured calibration, scalable workflows and installable delivery remain part of the full platform goal.

The current bridge accepts registered simulation runs, not uploaded nonlinear physical captures. LSC is explicitly authored; no response/noise fitting is claimed. Charge metrics after fixed WB/CCM use `electron_equivalent` (the arithmetic RGB guide times the fixed charge normalization), not literal output electron counts or a calibrated luminance measurement. Complete paired DCG identity is retained, but the selected path uses its diagonal read variance rather than a propagated full output covariance.

The Study forecast exactly counts arrays and bound copied metadata, checks stage-summary/metric/completion graph limits and enforces the bundle cap. The additional 64 KiB per-cell ownership/analysis reserve remains a layout allowance without a dedicated runtime guard; four tested input modes fit the forecast, which is not a universal mathematical admission proof.

Actual HTTP verification exercised sequential HDR, reference HDR, HCG and LCG replays, all 49 output arrays per replay, four fixed-RAW candidates and two fixed candidates on a separately authored optical input. The held-out workflow proves lineage separation and execution, not generalization to a real camera. Missing physical references stay unknown. The explicit QA selection is an arbitrary non-baseline choice to exercise that workflow, not a claim of best image quality.
